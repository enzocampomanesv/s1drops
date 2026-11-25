"""Before/after photos for a change, fetched in the background: Sentinel-2 (on the
VM, can take a minute) or high-resolution Esri Wayback (a few seconds; the
tiles come straight from Esri). The page starts a job and asks for it until it
is done; each job belongs to the user who started it.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, Optional

from ..app import highres, render
from ..config import optical_radius_m, s2_candidates, s2_cloud_bucket, s2_cloud_thresh, s2_max_probes, s2_window_days
from .. import optical

KEEP_S = 30 * 60
_jobs: Dict[str, Dict] = {}
_lock = threading.Lock()


def _new(uid: str, kind: str) -> Dict:
    now = time.time()
    with _lock:
        for k in [k for k, j in _jobs.items() if now - j["created"] > KEEP_S]:
            del _jobs[k]
        job = {"id": uuid.uuid4().hex, "uid": uid, "kind": kind, "created": now, "status": "running",
               "messages": [], "result": None, "error": ""}
        _jobs[job["id"]] = job
    return job


def get(job_id: str, uid: str) -> Optional[Dict]:
    with _lock:
        j = _jobs.get(job_id)
        if j is None or j["uid"] != uid:
            return None
        return {k: j[k] for k in ("id", "kind", "status", "messages", "result", "error")}


def for_report(job_id: str, uid: str, cell, before_idx: int, after_idx: int) -> Optional[Dict]:
    """A finished Sentinel-2 job's photos as the report draws them, if it is the
    caller's and was fetched for this cell; else None (the report says so)."""
    with _lock:
        j = _jobs.get(job_id)
        if j is None or j["uid"] != uid or j["kind"] != "s2" or j["status"] != "done" or j.get("cell") != tuple(cell):
            return None
        return dict(j["raw"], before_idx=before_idx, after_idx=after_idx)


def _run(job: Dict, work) -> None:
    def say(m):
        with _lock:
            job["messages"] = job["messages"][-19:] + [str(m)]

    def go():
        try:
            result = work(say)
            with _lock:
                job.update(result=result, status="done")
        except Exception as e:  # noqa: BLE001 -- shown to the user as the job's error
            with _lock:
                job.update(error=str(e) or e.__class__.__name__, status="failed")

    threading.Thread(target=go, daemon=True).start()


def start_s2(uid: str, area, cell, date_before: str, date_after: str, cache_dir,
             cloud_pct: Optional[float] = None, window_days: Optional[int] = None) -> Dict:
    job = _new(uid, "s2")
    job["cell"] = tuple(int(i) for i in cell)
    cloud = (cloud_pct / 100.0) if cloud_pct is not None else s2_cloud_thresh()
    win = window_days if window_days is not None else int(s2_window_days())

    def work(say):
        r = optical.fetch_before_after(
            area.cube, cube_key=area.entry.key, cell=cell, before_anchor=date_before, after_anchor=date_after,
            radius_m=optical_radius_m(), window_days=win, cloud_thresh=cloud, cloud_bucket=s2_cloud_bucket(),
            max_probes=int(s2_max_probes()), n_candidates=int(s2_candidates()), cache_dir=cache_dir, progress=say)
        with _lock:
            job["raw"] = r          # the pictures themselves, for a report (never sent as JSON)
        out, bounds = {}, None
        for side in ("before", "after"):
            cands = []
            for c in r[side].get("candidates", []):
                url, bounds = render.overlay_rgb(c["rgb"], r["transform"], r["crs"])
                cands.append({"date": c["date"], "cloud": c["cloud"], "image": url})
            out[side] = {"candidates": cands, "probed": r[side].get("probed"), "note": r[side].get("note", "")}
        return dict(out, bounds=bounds, cloudPct=round(cloud * 100), windowDays=win)

    _run(job, work)
    return get(job["id"], uid)


def start_highres(uid: str, lat: float, lon: float, date_before: str, date_after: str, cache_dir) -> Dict:
    job = _new(uid, "highres")

    def work(say):
        pair = highres.find_pair(cache_dir, lat, lon, date_before, date_after, progress=say)
        sides = {}
        for side in ("before", "after"):
            s = pair.get(side)
            sides[side] = None if s is None else {
                "date": s["date"], "resM": s.get("res_m"), "source": s.get("source", ""), "tiles": s["tiles"],
                "release": s.get("release"), "describe": highres.describe(s),
            }
        return dict(sides, link=pair.get("link"))

    _run(job, work)
    return get(job["id"], uid)
