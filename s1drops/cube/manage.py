"""Bake / time-extend orchestration for Tab 2.

bake_or_extend looks for an existing cube with the *same grid signature* and
reduction. If found, it treats the request as a time-extension: it bakes only the
date range(s) not already covered, merges along time (de-duplicating overlapping
passes), and rewrites the store under a key spanning the union range. If not
found, it bakes a fresh, independent cube. Either way the registry is updated.

In-place *spatial* growth is intentionally not supported: a different footprint
is always a new cube. Spatial mosaic-in-place is a separate, fragile problem we
defer; "add a bigger area" means "bake a bigger AOI as a new cube".
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import xarray as xr

from .build import build_cube, estimate_cube_bytes
from .cache import DEFAULT_CACHE_DIR, cache_key, cube_path, read_cube, write_cube
from .geobox import aoi_to_geobox
from ..config import max_cube_bytes
from .registry import (
    CubeEntry,
    delete_cube,
    find_by_grid,
    grid_signature,
    register_cube,
)

Bbox = Tuple[float, float, float, float]
Progress = Optional[Callable[[str], None]]


def _d(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _s(d: date) -> str:
    return d.isoformat()


def missing_ranges(old_start, old_end, req_start, req_end) -> List[Tuple[str, str]]:
    """Date sub-ranges of [req_start, req_end] not covered by [old_start, old_end].

    Boundaries are offset by a day so the existing edge pass isn't re-fetched;
    any residual overlap is de-duplicated at merge time anyway.
    """
    o0, o1 = _d(old_start), _d(old_end)
    r0, r1 = _d(req_start), _d(req_end)
    out: List[Tuple[str, str]] = []
    if r0 < o0:
        out.append((_s(r0), _s(o0 - timedelta(days=1))))
    if r1 > o1:
        out.append((_s(o1 + timedelta(days=1)), _s(r1)))
    return out


def merge_time(cubes: List[xr.Dataset]) -> xr.Dataset:
    """Concatenate same-grid cubes along time, sort, and drop duplicate passes."""
    mask = None
    lcz = None
    attrs = {}
    parts = []
    for c in cubes:
        attrs = {**attrs, **c.attrs}
        if "aoi_mask" in c and mask is None:
            mask = c["aoi_mask"]
        if "lcz" in c and lcz is None:
            lcz = c["lcz"]
        c = c.drop_vars([v for v in ("aoi_mask", "lcz", "orbit_code", "spatial_ref")
                         if v in c.variables or v in c.coords])
        parts.append(c)
    merged = xr.concat(parts, dim="time").sortby("time")
    # de-duplicate identical timestamps (keep first occurrence)
    times = merged["time"].values
    _, keep = np.unique(times, return_index=True)
    merged = merged.isel(time=np.sort(keep))
    if mask is not None:
        merged["aoi_mask"] = mask
    if lcz is not None:
        merged["lcz"] = lcz
    merged.attrs.update(attrs)
    merged.attrs["n_passes"] = int(merged.sizes["time"])
    return merged


def bake_or_extend(
    bbox_ll: Bbox,
    start: str,
    end: str,
    *,
    name: Optional[str] = None,
    aoi_geom=None,
    reduction: str = "native_median",
    resolution: float = 100.0,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    catalog=None,
    progress: Progress = None,
) -> Tuple[CubeEntry, bool]:
    """Bake a new cube or extend an existing same-grid one. Returns (entry, changed)."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    cache_dir = Path(cache_dir)
    gbox, _ = aoi_to_geobox(bbox_ll, resolution)
    t = gbox.transform
    sig = grid_signature(
        gbox.crs, [t.a, t.b, t.c, t.d, t.e, t.f],
        [gbox.shape.y, gbox.shape.x], resolution, reduction,
    )
    existing = find_by_grid(cache_dir, sig)

    if not existing:
        say(f"Baking new cube {name or ''} {start}..{end} ...")
        cube, _ = build_cube(
            bbox_ll, start, end, aoi_geom=aoi_geom, reduction=reduction,
            resolution=resolution, name=name, catalog=catalog,
        )
        key = cache_key(bbox_ll, start, end, resolution=resolution, reduction=reduction, name=name)
        path = cube_path(key, cache_dir)
        write_cube(cube, path)
        entry = register_cube(cache_dir, path)
        say("Done.")
        return entry, True

    prev = existing[0]
    gaps = missing_ranges(prev.start, prev.end, start, end)
    if not gaps:
        say("Requested range already covered; nothing to do.")
        return prev, False

    say(f"Extending {prev.name}: baking {len(gaps)} missing range(s) ...")
    new_parts = []
    for gs, ge in gaps:
        say(f"  baking {gs}..{ge}")
        part, _ = build_cube(
            bbox_ll, gs, ge, aoi_geom=aoi_geom, reduction=reduction,
            resolution=resolution, name=prev.name, catalog=catalog,
        )
        new_parts.append(part)

    old_cube = read_cube(prev.path)
    merged = merge_time([old_cube, *new_parts])
    union_start = min(_d(prev.start), _d(start))
    union_end = max(_d(prev.end), _d(end))
    merged.attrs.update({"name": prev.name, "start": _s(union_start), "end": _s(union_end)})

    # Guard the merged cube: write_cube() loads it fully into RAM. Estimate from
    # sizes (no load) and refuse before materialising if it exceeds the budget.
    n_cells = int(merged.sizes["y"]) * int(merged.sizes["x"])
    n_bands = sum(b in merged for b in ("vv", "vh")) or 1
    est = estimate_cube_bytes(n_cells, int(merged.sizes["time"]), n_bands)
    if est > max_cube_bytes():
        raise RuntimeError(
            f"Merged cube ~{est / 1e9:.1f} GB exceeds the {max_cube_bytes() / 1e9:.1f} GB "
            "budget. Extend over a smaller added range (or raise S1DROPS_MAX_CUBE_BYTES)."
        )

    new_key = cache_key(
        bbox_ll, _s(union_start), _s(union_end),
        resolution=resolution, reduction=reduction, name=prev.name,
    )
    new_path = cube_path(new_key, cache_dir)
    say(f"Writing merged cube -> {new_path}")
    write_cube(merged, new_path)

    if Path(new_path).resolve() != Path(prev.path).resolve():
        delete_cube(cache_dir, prev.key)  # remove the superseded store + entry
    entry = register_cube(cache_dir, new_path)
    say("Done.")
    return entry, True
