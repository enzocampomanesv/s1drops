"""Zarr cache for built cubes.

Keying is deterministic on (snapped bbox, dates, resolution, collection) so the
same AOI+range reuses a cube; a human-readable `name` (e.g. "lagos") overrides
the hash for pre-baked cities. Chunking is tuned for the click pattern: full
time axis, modest spatial tiles, so one cell's whole time series reads from few
chunks.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import xarray as xr

Bbox = Tuple[float, float, float, float]
DEFAULT_CACHE_DIR = Path("./cache")
WRITE_CHUNKS = {"time": -1, "y": 256, "x": 256}

# orbit_state is stored as a small integer code (portable, Zarr-v3-spec-safe)
# and reconstructed to a human-readable string on read.
ORBIT_CODES = {"ascending": 0, "descending": 1, "unknown": 2}
ORBIT_NAMES = {v: k for k, v in ORBIT_CODES.items()}


def _encode_orbit(cube: xr.Dataset) -> xr.Dataset:
    if "orbit_state" not in cube.coords:
        return cube
    codes = np.array(
        [ORBIT_CODES.get(str(s), 2) for s in cube["orbit_state"].values], dtype="int8"
    )
    out = cube.drop_vars("orbit_state").assign_coords(orbit_code=("time", codes))
    out.attrs["orbit_state_codes"] = json.dumps(ORBIT_NAMES)
    return out


def _decode_orbit(cube: xr.Dataset) -> xr.Dataset:
    if "orbit_code" not in cube.coords:
        return cube
    names = np.array(
        [ORBIT_NAMES.get(int(c), "unknown") for c in cube["orbit_code"].values], dtype="<U10"
    )
    return cube.assign_coords(orbit_state=("time", names))


REDUCTION_TAGS = {"native_median": "nm", "overview_med": "ov"}


def _reduction_tag(reduction: str, resolution: float) -> str:
    """Filename tag for (reduction, resolution).

    Backward-compatible: 100 m keeps the bare tag (`nm`, `ov`) so existing v1
    cubes are unaffected; other resolutions are suffixed (`nm10`) so e.g. a 10 m
    and a 100 m native_median cube of the same AOI never collide on one key.
    """
    base = REDUCTION_TAGS.get(reduction, reduction)
    return base if round(resolution) == 100 else f"{base}{int(round(resolution))}"


def cache_key(
    bbox_ll: Bbox,
    start: str,
    end: str,
    *,
    resolution: float = 100.0,
    collection: str = "sentinel-1-rtc",
    reduction: str = "native_median",
    name: Optional[str] = None,
) -> str:
    tag = _reduction_tag(reduction, resolution)
    if name:
        return f"{name}_{start}_{end}_{tag}"
    payload = json.dumps(
        [[round(v, 5) for v in bbox_ll], start, end, resolution, collection, reduction],
        sort_keys=True,
    )
    h = hashlib.sha1(payload.encode()).hexdigest()[:10]
    return f"aoi_{h}_{start}_{end}_{tag}"


def cube_path(key: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    return Path(cache_dir) / f"{key}.zarr"


def write_cube(cube: xr.Dataset, path: Path, chunks: Optional[dict] = None,
               retries: int = 4) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Materialise once: write retries then never recompute (no re-fetch from
    # Planetary Computer, no re-read of the old zarr), and source handles are
    # released before the new store is written.
    encoded = _encode_orbit(cube).load().chunk(chunks or WRITE_CHUNKS)
    delay, last = 1.0, None
    for _ in range(retries):
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            encoded.to_zarr(path, mode="w", consolidated=False)
            return path
        except (PermissionError, OSError) as e:
            last = e
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(
        f"Failed to write cube to {path} after {retries} attempts (last error: {last}). "
        "On Windows a [WinError 5] here is almost always a file lock on the cache "
        "folder from OneDrive sync or antivirus during zarr's atomic rename. Fix: put "
        "the cache on a local, non-synced path — set S1DROPS_CACHE_DIR to e.g. "
        "C:\\s1cache (not under Desktop/OneDrive) — and/or add an antivirus exclusion."
    )


def read_cube(path: Path) -> xr.Dataset:
    return _decode_orbit(xr.open_zarr(Path(path), consolidated=False))


def build_or_load(
    bbox_ll: Bbox,
    start: str,
    end: str,
    *,
    name: Optional[str] = None,
    aoi_geom=None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    resolution: float = 100.0,
    reduction: str = "native_median",
    **build_kw,
) -> Tuple[xr.Dataset, Path, bool]:
    """Return (cube, path, built_now). Reads cache if present, else builds+writes."""
    from .build import build_cube

    key = cache_key(
        bbox_ll, start, end, resolution=resolution, reduction=reduction, name=name
    )
    path = cube_path(key, cache_dir)
    if path.exists():
        return read_cube(path), path, False
    cube, _ = build_cube(
        bbox_ll, start, end, aoi_geom=aoi_geom, resolution=resolution,
        reduction=reduction, **build_kw,
    )
    write_cube(cube, path)
    return read_cube(path), path, True
