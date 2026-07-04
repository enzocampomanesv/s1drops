"""Cube registry — the manifest of available cubes.

`cache/registry.json` is the source of truth for the UI's cube picker, but it is
rebuildable at any time from the zarr attrs on disk (`rescan`), so it can't drift
out of sync if a cube is copied in or a write half-fails.

A cube's *footprint* is identified by its grid signature (CRS + transform +
shape) plus resolution and reduction — not the raw lon/lat bbox, since two
slightly different bboxes can snap to the same 100 m grid. Same signature with a
new date range is a time-extension (handled in manage.py); a different signature
is a new, independent cube.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import xarray as xr

REGISTRY_NAME = "registry.json"


@dataclass
class CubeEntry:
    key: str                # zarr filename stem (unique)
    name: str               # user-facing label, e.g. "lagos"
    path: str
    crs: str
    grid_transform: List[float]
    grid_shape: List[int]
    bbox_ll: List[float]
    start: str
    end: str
    resolution: float
    reduction: str
    n_passes: int
    created_at: str

    @property
    def signature(self) -> Tuple:
        return grid_signature(
            self.crs, self.grid_transform, self.grid_shape, self.resolution, self.reduction
        )


def grid_signature(crs, transform, shape, resolution, reduction) -> Tuple:
    """Hashable footprint key. Rounded so float noise can't split a match."""
    t = tuple(round(float(v), 3) for v in transform)
    return (str(crs), t, (int(shape[0]), int(shape[1])), round(float(resolution), 3), str(reduction))


def registry_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / REGISTRY_NAME


def _entry_from_zarr(path: Path) -> Optional[CubeEntry]:
    try:
        ds = xr.open_zarr(path, consolidated=False)
    except Exception:
        return None
    a = ds.attrs
    if "crs" not in a:
        return None
    # Prefer stored grid attrs; otherwise derive from x/y coords (older cubes).
    if "grid_transform" in a and "grid_shape" in a:
        grid_transform = [float(v) for v in a["grid_transform"]]
        grid_shape = [int(v) for v in a["grid_shape"]]
    elif "x" in ds.coords and "y" in ds.coords:
        xs = np.asarray(ds["x"].values, dtype="float64")
        ys = np.asarray(ds["y"].values, dtype="float64")
        res = float(a.get("resolution", abs(xs[1] - xs[0]) if xs.size > 1 else 100.0))
        grid_transform = [res, 0.0, float(xs[0] - res / 2), 0.0, -res, float(ys[0] + res / 2)]
        grid_shape = [int(ds.sizes["y"]), int(ds.sizes["x"])]
    else:
        return None
    key = path.stem
    created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return CubeEntry(
        key=key,
        name=str(a.get("name") or key),
        path=str(path),
        crs=str(a["crs"]),
        grid_transform=grid_transform,
        grid_shape=grid_shape,
        bbox_ll=[float(v) for v in a.get("bbox_ll", [])],
        start=str(a.get("start", "")),
        end=str(a.get("end", "")),
        resolution=float(a.get("resolution", 100.0)),
        reduction=str(a.get("reduction", "")),
        n_passes=int(a.get("n_passes", ds.sizes.get("time", 0))),
        created_at=created,
    )


def scan_cubes(cache_dir: Path) -> List[CubeEntry]:
    """Discover cubes by reading the attrs of every *.zarr in the cache dir."""
    cache_dir = Path(cache_dir)
    out: List[CubeEntry] = []
    if not cache_dir.exists():
        return out
    for p in sorted(cache_dir.glob("*.zarr")):
        entry = _entry_from_zarr(p)
        if entry is not None:
            out.append(entry)
    return out


def rescan(cache_dir: Path) -> Dict[str, dict]:
    """Rebuild registry.json from disk and return the manifest dict."""
    entries = {e.key: asdict(e) for e in scan_cubes(cache_dir)}
    save_registry(cache_dir, entries)
    return entries


def load_registry(cache_dir: Path, *, auto_rescan: bool = True) -> Dict[str, dict]:
    p = registry_path(cache_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return rescan(cache_dir) if auto_rescan else {}


def save_registry(cache_dir: Path, entries: Dict[str, dict]) -> None:
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    registry_path(cache_dir).write_text(json.dumps(entries, indent=2, sort_keys=True))


def list_cubes(cache_dir: Path) -> List[CubeEntry]:
    cache_dir = Path(cache_dir)
    cubes: List[CubeEntry] = []
    for v in load_registry(cache_dir).values():
        e = CubeEntry(**v)
        # Portable: a cube always lives at <cache_dir>/<key>.zarr, so resolve the
        # path against the *current* cache dir rather than the absolute path that
        # was frozen into the registry at bake time (which breaks if the cache is
        # moved). Stale stored paths are ignored.
        e.path = str(cache_dir / f"{e.key}.zarr")
        cubes.append(e)
    return cubes


def register_entry(cache_dir: Path, entry: CubeEntry) -> None:
    reg = load_registry(cache_dir, auto_rescan=False)
    reg[entry.key] = asdict(entry)
    save_registry(cache_dir, reg)


def register_cube(cache_dir: Path, cube_path: Path) -> Optional[CubeEntry]:
    entry = _entry_from_zarr(Path(cube_path))
    if entry is not None:
        register_entry(cache_dir, entry)
    return entry


def find_by_grid(cache_dir: Path, signature: Tuple) -> List[CubeEntry]:
    return [e for e in list_cubes(cache_dir) if e.signature == signature]


def delete_cube(cache_dir: Path, key: str) -> bool:
    """Remove a cube's zarr store and its registry entry. Returns True if removed."""
    reg = load_registry(cache_dir, auto_rescan=False)
    entry = reg.pop(key, None)
    store = Path(cache_dir) / f"{key}.zarr"  # always the current cache location
    removed = False
    if store.exists():
        shutil.rmtree(store, ignore_errors=True)
        removed = True
    if entry is not None:
        save_registry(cache_dir, reg)
    return removed
