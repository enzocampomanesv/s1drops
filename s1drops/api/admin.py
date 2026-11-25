"""Admin work for the Space4All page: preparing areas (bakes), monthly updates,
deleting areas, reference data and feedback. The same code as the Solara
Admin tab (app/admin.py, app/bakequeue.py, app/updater.py), run in the API.

Only one process may bake: the bake queue and the monthly updater run here
when S1DROPS_BAKES / S1DROPS_UPDATER are on (the default); compose turns them
off in the Solara app, whose Admin tab then points here.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..app import bakequeue, feedback, validation
from ..config import est_passes_per_year, native10_max_km2
from ..cube import list_cubes
from ..cube import refresh
from ..cube.geobox import aoi_to_geobox
from ..cube.lcz import lcz_status
from ..cube.registry import delete_cube

# label -> (resolution_m, reduction), as the Admin tab's presets.
PRESETS = {
    "100-native": ("100 m · native median (recommended)", 100.0, "native_median"),
    "100-fast": ("100 m · overview (fast, exploration only)", 100.0, "overview_med"),
    "10-native": ("10 m · native (small areas only)", 10.0, "native_median"),
}


def enabled() -> bool:
    return os.environ.get("S1DROPS_BAKES", "1") != "0"


def _days(a: str, b: str) -> int:
    from datetime import date

    return max((date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days, 0)


def resolve(cache_dir: Path, body: Dict):
    """(bbox, geometry or None, name) for a bake request: an existing area (same
    footprint, a longer period), a lon/lat box, or a GeoJSON polygon (upload or
    drawn). ValueError with what's wrong."""
    mode = body.get("mode")
    if mode == "existing":
        e = next((c for c in list_cubes(cache_dir) if c.key == body.get("key")), None)
        if e is None:
            raise ValueError("Pick one of the areas.")
        return tuple(e.bbox_ll), None, e.name
    if mode == "bbox":
        try:
            minx, miny, maxx, maxy = (float(v) for v in body.get("bbox") or [])
        except (TypeError, ValueError):
            raise ValueError("Give the box as four numbers: west, south, east, north.")
        bbox, geom = (minx, miny, maxx, maxy), None
    elif mode == "geojson":
        from ..cube.geobox import geom_from_geojson

        try:
            geom = geom_from_geojson(body.get("geojson") if isinstance(body.get("geojson"), str)
                                     else json.dumps(body.get("geojson")))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"That GeoJSON could not be read ({e}).")
        bbox = tuple(geom.bounds)
    else:
        raise ValueError("Choose how to define the area.")
    minx, miny, maxx, maxy = bbox
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise ValueError("Coordinates must be longitude/latitude (WGS84), west < east and south < north.")
    return bbox, geom, None


def estimate(cache_dir: Path, body: Dict) -> Dict:
    """Area, grid and size of a bake before queueing it (the Admin tab's line)."""
    bbox, _, _ = resolve(cache_dir, body)
    _, res, _ = PRESETS.get(body.get("preset"), PRESETS["100-native"])
    g, _ = aoi_to_geobox(bbox, res)
    ny, nx = int(g.shape.y), int(g.shape.x)
    cells = ny * nx
    km2 = cells * res * res / 1e6
    years = max(_days(body.get("start", "2024-01-01"), body.get("end", "2025-01-01")), 1) / 365.0
    passes = est_passes_per_year() * years
    gb = cells * passes * 2 * 4 * 1.3 / 1e9
    too_big = res <= 50 and km2 > native10_max_km2()
    return {"areaKm2": round(km2, 2), "grid": [ny, nx], "cells": cells, "passes": round(passes), "sizeMb": round(gb * 1000, 1),
            "problem": (f"{km2:.1f} km² is over the {native10_max_km2():.0f} km² limit for 10 m areas: "
                        "make it smaller or choose 100 m.") if too_big else None}


def queue(cache_dir: Path, body: Dict) -> Dict:
    if not enabled():
        raise ValueError("Areas are not prepared here.")
    name = str(body.get("name") or "").strip()
    bbox, geom, existing = resolve(cache_dir, body)
    name = name or existing or ""
    if not name:
        raise ValueError("Give the area a name (e.g. lagos), so it can be found later.")
    if body.get("preset") not in PRESETS:
        raise ValueError("Choose a detail level.")
    _, res, red = PRESETS[body["preset"]]
    for k in ("start", "end"):
        try:
            _days("2000-01-01", body.get(k))
        except Exception:  # noqa: BLE001
            raise ValueError("Dates as YYYY-MM-DD.")
    if body["start"] >= body["end"]:
        raise ValueError("The start must be before the end.")
    est = estimate(cache_dir, body)
    if est["problem"]:
        raise ValueError(est["problem"])
    label = f"{name} · {int(round(res))} m · {body['start']}..{body['end']}"
    job_id = bakequeue.enqueue(label=label, bbox=bbox, geom=geom, start=body["start"], end=body["end"],
                               name=name, reduction=red, resolution=res, cache_dir=str(cache_dir))
    return {"id": job_id, "label": label}


def status(cache_dir: Path) -> Dict:
    """The queue (with each job's last log lines), the monthly updates and the setup."""
    lcz_state, lcz_path = lcz_status()
    jobs = [{"id": j["id"], "label": j["label"], "status": j["status"], "result": j.get("result", ""),
             "log": j.get("log", [])[-12:], "kind": j["params"].get("kind", "bake")}
            for j in bakequeue.queue.value]
    runs = refresh.load_state(cache_dir).get("history", [])[:6]
    return {
        "enabled": enabled(), "jobs": jobs,
        "updates": [{"at": r["at"], "trigger": r["trigger"], "results": r["results"]} for r in runs],
        "lcz": lcz_state, "presets": [{"key": k, "label": v[0]} for k, v in PRESETS.items()],
        "tenMetreLimitKm2": native10_max_km2(),
        "areas": [{"key": e.key, "name": e.name, "start": str(e.start)[:10], "end": str(e.end)[:10],
                   "resolution": e.resolution, "passes": e.n_passes, "fast": e.reduction == "overview_med"}
                  for e in list_cubes(cache_dir)],
    }


def update_now(cache_dir: Path) -> int:
    if not enabled():
        raise ValueError("Areas are not prepared here.")
    return bakequeue.enqueue_update(str(cache_dir), trigger="manual")


def delete_area(cache_dir: Path, key: str, confirm: str) -> None:
    e = next((c for c in list_cubes(cache_dir) if c.key == key), None)
    if e is None:
        raise LookupError("No such area.")
    if confirm.strip().lower() != e.name.strip().lower():
        raise ValueError(f"Type the area's name ({e.name}) to delete it.")
    if any(j["status"] in ("pending", "running") for j in bakequeue.queue.value):
        raise ValueError("Wait until the bake queue is idle: a bake or update may be using this area.")
    delete_cube(cache_dir, key)


# ---- reference data (admins) and feedback ----

def references(user) -> List[Dict]:
    return validation.reference_batches(user)


def feedback_inbox(user, include_done: bool) -> List[Dict]:
    return [{"id": m.id, "kind": feedback.KINDS.get(m.kind, m.kind), "message": m.message, "email": m.email,
             "context": m.context, "from": m.sender or "", "at": m.created_at, "done": bool(m.done)}
            for m in feedback.inbox(user, include_done)]


def started_updater(cache_dir: Path) -> bool:
    """The monthly updater, in this process only when it is the one baking."""
    if not enabled() or os.environ.get("S1DROPS_UPDATER", "1") == "0":
        return False
    from ..app import updater

    updater.start(str(cache_dir))
    return True


def last_update_at(cache_dir: Path) -> Optional[float]:
    runs = refresh.load_state(cache_dir).get("history", [])
    return runs[0]["at"] if runs else None


_ = time  # noqa: F841 (kept for callers formatting times)
