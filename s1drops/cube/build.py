"""Build the long-form 100 m cube.

Design note — the grouping pitfall: ascending and descending passes can share a
solar day, so grouping the whole search by solar day would mosaic two different
look geometries into one time slice and silently destroy the orbit separation
the detection layer depends on. Instead we load each *relative orbit* separately
(a relative orbit revisits only every ~12 days, so solar-day grouping is safe
within it and correctly mosaics multi-frame passes), tag each slice with its
orbit metadata, then concatenate along time. The result is one Dataset with
dims (time, y, x) for `vv`/`vh` in linear power, plus `orbit_state` and
`relative_orbit` as 1-D coordinates along time, and a 2-D `aoi_mask`.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional, Sequence, Tuple

import numpy as np
import odc.stac
import planetary_computer as pc
import xarray as xr

from .geobox import DEFAULT_RES, aoi_to_geobox, rasterize_mask, refine_geobox
from .lcz import lcz_for_geobox, resolve_lcz_path
from .stac import DEFAULT_BANDS, ItemMeta, open_catalog, search_items
from ..config import max_cube_bytes

Bbox = Tuple[float, float, float, float]

NATIVE_RES = 10.0  # Sentinel-1 RTC native pixel spacing (m)
REDUCTIONS = ("native_median", "overview_med")


def coarsen_median(ds: xr.Dataset, factor: int) -> xr.Dataset:
    """Median-coarsen a fine dataset by `factor` in x and y (pure, dask-friendly)."""
    return ds.coarsen(x=factor, y=factor, boundary="trim").median()


def _grid_coords(gbox) -> Tuple[np.ndarray, np.ndarray]:
    """Cell-centre x/y coordinate arrays for a GeoBox."""
    t = gbox.transform
    xs = t.c + abs(t.a) * (np.arange(gbox.shape.x) + 0.5)
    ys = t.f - abs(t.e) * (np.arange(gbox.shape.y) + 0.5)
    return xs, ys


def _load_track(items, gbox, bands: Sequence[str], reduction: str, chunks: Optional[dict]) -> xr.Dataset:
    """Load one relative orbit onto the shared 100 m grid using `reduction`.

    native_median (default): read native ~10 m pixels (nearest) onto an exact
        10x refinement of the target grid, then take the spatial median of each
        100 m cell. This is the true median-of-native-pixels; robust to transient
        bright scatterers and reproducible (overviews are not involved).
    overview_med: fast path — GDAL `med` resampling straight to 100 m, which may
        read from COG overviews. ~1 dB different from native_median; exploration only.
    """
    if reduction == "overview_med":
        ch = chunks or {"time": 1, "x": 1024, "y": 1024}
        return odc.stac.load(
            items, bands=list(bands), geobox=gbox,
            resampling="med", groupby="solar_day", chunks=ch,
            patch_url=pc.sign,
        )
    if reduction == "native_median":
        factor = int(round(abs(gbox.transform.a) / NATIVE_RES))
        fine = refine_geobox(gbox, factor)
        ch = chunks or {"time": 1, "x": 100 * factor, "y": 100 * factor}
        ds = odc.stac.load(
            items, bands=list(bands), geobox=fine,
            resampling="nearest", groupby="solar_day", chunks=ch,
            patch_url=pc.sign,
        )
        out = coarsen_median(ds, factor)
        xs, ys = _grid_coords(gbox)  # pin grid bit-identically to the target
        return out.assign_coords(x=("x", xs), y=("y", ys))
    raise ValueError(f"reduction must be one of {REDUCTIONS}, got {reduction!r}")


def tag_track(ds: xr.Dataset, relative_orbit: int, orbit_state: str) -> xr.Dataset:
    """Attach per-time orbit coordinates to a loaded track (pure)."""
    n = ds.sizes["time"]
    return ds.assign_coords(
        relative_orbit=("time", np.full(n, relative_orbit, dtype="int32")),
        orbit_state=("time", np.array([orbit_state] * n, dtype="<U10")),
    )


def assemble_tracks(parts: Sequence[xr.Dataset]) -> xr.Dataset:
    """Concatenate tagged tracks along time and sort chronologically (pure)."""
    cube = xr.concat(parts, dim="time")
    return cube.sortby("time")


def usable_metas(metas: Sequence[ItemMeta], bands: Sequence[str]) -> list:
    """Scenes that carry ALL requested bands.

    Sentinel-1 images some regions in HH/HV or VV-only rather than VV/VH, and
    those scenes lack the requested asset; loading them would fail. Filtering
    here keeps the cube to scenes that actually support the requested bands.
    """
    return [m for m in metas if all(b in m.pols for b in bands)]


def estimate_timesteps(usable: Sequence[ItemMeta]) -> int:
    """Number of time slices the cube will have: one per (relative_orbit, solar_day).

    Matches the per-orbit `groupby="solar_day"` load, so it's the true count, not
    a heuristic. Used to size the memory guard before any pixels are loaded.
    """
    seen = set()
    for m in usable:
        dt = getattr(m.item, "datetime", None)
        day = dt.date().isoformat() if dt is not None else m.item.id
        seen.add((m.relative_orbit, day))
    return len(seen)


def estimate_cube_bytes(n_cells: int, n_timesteps: int, n_bands: int) -> float:
    """Estimated in-memory cube size (float32 + 30% headroom for coords/masks)."""
    return n_cells * n_timesteps * n_bands * 4 * 1.3


def build_cube(
    bbox_ll: Bbox,
    start: str,
    end: str,
    *,
    bands: Sequence[str] = DEFAULT_BANDS,
    resolution: float = DEFAULT_RES,
    aoi_geom=None,
    reduction: str = "native_median",
    name: Optional[str] = None,
    catalog=None,
    chunks: Optional[dict] = None,
    lcz_path: Optional[str] = None,
    max_bytes: Optional[float] = None,
) -> Tuple[xr.Dataset, str]:
    """Search, load per track, and assemble the long-form cube. Returns (cube, utm)."""
    if reduction not in REDUCTIONS:
        raise ValueError(f"reduction must be one of {REDUCTIONS}, got {reduction!r}")
    cat = catalog or open_catalog()
    metas = search_items(bbox_ll, start, end, bands=bands, catalog=cat)
    if not metas:
        raise RuntimeError(f"No Sentinel-1 scenes for {bbox_ll} over {start}..{end}.")
    usable = usable_metas(metas, bands)
    if not usable:
        raise RuntimeError(
            f"Found {len(metas)} scene(s) but none carry all of {tuple(bands)} here. "
            "Sentinel-1 images some regions (often maritime/coastal) in HH/HV or "
            "VV-only, so VV/VH timeseries are not available for this AOI."
        )
    skipped = len(metas) - len(usable)

    gbox, utm = aoi_to_geobox(bbox_ll, resolution)

    # Memory guard: refuse before loading anything if the cube would be too big.
    # write_cube() materialises the whole cube into RAM, so this bounds peak use.
    budget = max_cube_bytes() if max_bytes is None else max_bytes
    n_cells = int(gbox.shape.y) * int(gbox.shape.x)
    n_ts = estimate_timesteps(usable)
    est = estimate_cube_bytes(n_cells, n_ts, len(bands))
    if est > budget:
        raise RuntimeError(
            f"Estimated cube ~{est / 1e9:.1f} GB "
            f"({n_cells:,} cells × {n_ts} passes × {len(bands)} bands at "
            f"{int(round(resolution))} m) exceeds the {budget / 1e9:.1f} GB budget. "
            "Reduce the AOI or the date range (or raise S1DROPS_MAX_CUBE_BYTES)."
        )

    by_ro = defaultdict(list)
    state_of = {}
    for m in usable:
        by_ro[m.relative_orbit].append(m.item)
        state_of[m.relative_orbit] = m.orbit_state

    parts = []
    for ro in sorted(by_ro):
        ds = _load_track(by_ro[ro], gbox, bands, reduction, chunks)
        parts.append(tag_track(ds, ro, state_of[ro]))
    cube = assemble_tracks(parts)

    if aoi_geom is not None:
        mask = rasterize_mask(aoi_geom, gbox)
    else:
        mask = np.ones((gbox.shape.y, gbox.shape.x), dtype=bool)
    cube["aoi_mask"] = (("y", "x"), mask)

    lcz_src = resolve_lcz_path(lcz_path)
    if lcz_src:
        cube["lcz"] = (("y", "x"), lcz_for_geobox(gbox, utm, lcz_src))

    t = gbox.transform
    cube.attrs.update(
        {
            "collection": "sentinel-1-rtc",
            "name": name or "",
            "bbox_ll": [float(v) for v in bbox_ll],
            "start": start,
            "end": end,
            "resolution": float(resolution),
            "crs": str(utm),
            "grid_transform": [t.a, t.b, t.c, t.d, t.e, t.f],
            "grid_shape": [int(gbox.shape.y), int(gbox.shape.x)],
            "units": "linear_power_gamma0",
            "reduction": reduction,
            "n_passes": int(cube.sizes["time"]),
            "scenes_skipped_polarization": int(skipped),
            "has_lcz": bool("lcz" in cube),
            "lcz_source": "Demuzere2022_global_lcz_filter_v1" if "lcz" in cube else "",
            "lcz_epoch": 2018 if "lcz" in cube else 0,
        }
    )
    return cube, utm
