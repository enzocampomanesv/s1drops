"""Monthly update: extend every baked area with the newest Sentinel-1 passes.

For each area in the registry, the passes after its current end are baked and
merged in (`manage.merge_time`, which keeps the area's outline and land-use
layer), written as a *new* store next to the old one, and checked before the
switch. Only then does the registry point at the new store; the old one moves to
`<cache>/previous/` (one per area, replacing the version before it), so an
update can always be rolled back by hand. Nothing is deleted until the next
successful update, and a failed or empty update leaves the area as it was.

Run by the app's scheduler once a month and by Admin > "Update now"; both go
through the bake queue so they never overlap a bake.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

from .build import build_cube, estimate_cube_bytes
from .cache import cache_key, cube_path, read_cube, write_cube
from .manage import merge_time
from .registry import CubeEntry, list_cubes, load_registry, register_cube, save_registry
from ..config import max_cube_bytes

# Planetary Computer publishes a pass a day or two after it's taken; asking for
# the last few days would only find partial coverage.
LAG_DAYS = 3
STATE_NAME = "updates.json"
PREVIOUS_DIR = "previous"
HISTORY_KEEP = 24

Progress = Optional[Callable[[str], None]]


def area_id(key: str) -> str:
    """An area's name without its date range ("Lagos_100m_nm" for
    "Lagos_100m_2017-01-01_2026-10-01_nm"): unchanged by updates, so links
    keep working."""
    return re.sub(r"_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}(?=_|$)", "", key)


def update_area(entry: CubeEntry, cache_dir: Path, *, today: Optional[date] = None,
                progress: Progress = None, catalog=None) -> str:
    """Extend one area to `today - LAG_DAYS`. Returns what happened, in words."""
    say = progress or (lambda m: None)
    cache_dir = Path(cache_dir)
    new_end = (today or date.today()) - timedelta(days=LAG_DAYS)
    old_end = date.fromisoformat(entry.end[:10])
    if new_end <= old_end:
        return f"{entry.name}: already up to date ({entry.end})"
    gap_start = old_end + timedelta(days=1)
    say(f"{entry.name}: looking for passes {gap_start}..{new_end}")
    try:
        part, _ = build_cube(entry.bbox_ll, gap_start.isoformat(), new_end.isoformat(),
                             reduction=entry.reduction, resolution=entry.resolution,
                             name=entry.name, catalog=catalog)
    except RuntimeError as e:
        if "No Sentinel-1 scenes" in str(e):
            return f"{entry.name}: no new passes yet"
        raise

    old = read_cube(entry.path)
    merged = merge_time([old, part])
    added = int(merged.sizes["time"]) - int(old.sizes["time"])
    if added <= 0:
        return f"{entry.name}: no new passes yet"
    merged.attrs.update({"name": entry.name, "start": entry.start,
                         "end": new_end.isoformat()})
    n_cells = int(merged.sizes["y"]) * int(merged.sizes["x"])
    n_bands = sum(b in merged for b in ("vv", "vh")) or 1
    if estimate_cube_bytes(n_cells, int(merged.sizes["time"]), n_bands) > max_cube_bytes():
        raise RuntimeError(f"{entry.name}: the updated cube would exceed the memory budget "
                           "(S1DROPS_MAX_CUBE_BYTES); not updated.")

    new_key = cache_key(entry.bbox_ll, entry.start, new_end.isoformat(),
                        resolution=entry.resolution, reduction=entry.reduction,
                        name=entry.name)
    new_path = cube_path(new_key, cache_dir)
    say(f"{entry.name}: writing {added} new passes")
    write_cube(merged, new_path)
    try:
        _check(read_cube(new_path), old, added)
    except Exception:
        shutil.rmtree(new_path, ignore_errors=True)
        raise

    # Switch: the registry points at the new store; the old one is kept aside.
    reg = load_registry(cache_dir, auto_rescan=False)
    reg.pop(entry.key, None)
    save_registry(cache_dir, reg)
    new_entry = register_cube(cache_dir, new_path)
    _set_aside(Path(entry.path), cache_dir)
    last = str(np.datetime_as_string(merged["time"].values.max(), unit="D"))
    return (f"{entry.name}: added {added} new pass{'es' if added != 1 else ''}, "
            f"now up to {last} ({new_entry.n_passes if new_entry else '?'} in all)")


def _check(new, old, added: int) -> None:
    """The new store before switching to it: same grid, the old passes plus the new
    ones, later ones only, and real values in the new passes."""
    if (new.sizes["y"], new.sizes["x"]) != (old.sizes["y"], old.sizes["x"]):
        raise RuntimeError("the updated cube's grid doesn't match the old one")
    if int(new.sizes["time"]) != int(old.sizes["time"]) + added:
        raise RuntimeError("the updated cube lost passes")
    if new["time"].values.max() <= old["time"].values.max():
        raise RuntimeError("the updated cube has no later passes")
    tail = new["vv"].isel(time=slice(-added, None)).values
    if not np.isfinite(tail).any():
        raise RuntimeError("the new passes have no data")


def _set_aside(store: Path, cache_dir: Path) -> None:
    """Move an area's superseded store to previous/, replacing its older version."""
    prev_dir = cache_dir / PREVIOUS_DIR
    prev_dir.mkdir(exist_ok=True)
    for old in prev_dir.glob("*.zarr"):
        if area_id(old.stem) == area_id(store.stem):
            shutil.rmtree(old, ignore_errors=True)
    if store.exists():
        shutil.move(str(store), str(prev_dir / store.name))


def update_all(cache_dir, *, today: Optional[date] = None, progress: Progress = None,
               trigger: str = "scheduled", catalog=None) -> List[str]:
    """Update every area, one at a time; one failing doesn't stop the others.
    Records the run in <cache>/updates.json (shown in the Admin tab)."""
    cache_dir = Path(cache_dir)
    results = []
    for entry in list_cubes(cache_dir):
        try:
            results.append(update_area(entry, cache_dir, today=today, progress=progress,
                                       catalog=catalog))
        except Exception as e:  # noqa: BLE001
            results.append(f"{entry.name}: not updated ({e})")
        if progress:
            progress(results[-1])
    state = load_state(cache_dir)
    state["last_run"] = time.time()
    state["history"] = ([{"at": time.time(), "trigger": trigger, "results": results}]
                        + state.get("history", []))[:HISTORY_KEEP]
    _save_state(cache_dir, state)
    return results


def load_state(cache_dir) -> Dict:
    try:
        return json.loads((Path(cache_dir) / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(cache_dir, state: Dict) -> None:
    path = Path(cache_dir) / STATE_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def due(cache_dir, now: Optional[float] = None) -> bool:
    """Whether this month's scheduled update hasn't run yet (it runs from the 1st).

    The first time the scheduler starts, it only notes the date: the first
    automatic update is then on the 1st of the next month, never straight away.
    """
    now = time.time() if now is None else now
    state = load_state(cache_dir)
    last = state.get("last_run") or state.get("scheduler_since")
    if last is None:
        state["scheduler_since"] = now
        _save_state(cache_dir, state)
        return False
    a, b = time.gmtime(last), time.gmtime(now)
    return (b.tm_year, b.tm_mon) > (a.tm_year, a.tm_mon)
