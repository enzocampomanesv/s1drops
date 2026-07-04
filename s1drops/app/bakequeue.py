"""Global, sequential bake queue.

A single background worker drains pending jobs one at a time, so back-to-back
bakes never fetch from Planetary Computer in parallel and two jobs touching the
same grid can't collide on the zarr write. State is module-level (shared across
sessions, survives UI reconnects) and in-memory only (lost on server restart).
Per-job failures are isolated: one failure marks that job failed and the worker
continues. Pending jobs can be removed; the in-flight job runs to completion.
"""
from __future__ import annotations

import itertools
import threading

import solara

from . import state
from ..cube import bake_or_extend

# Shared across all sessions of the server process.
queue = solara.reactive([])           # list of job dicts (pending/running/done/failed)
worker_running = solara.reactive(False)

_lock = threading.Lock()              # guards queue mutations + worker_running check
_ids = itertools.count(1)


def enqueue(*, label, bbox, geom, start, end, name, reduction, cache_dir) -> int:
    """Add a job and make sure the worker is running. Returns the job id."""
    job = {
        "id": next(_ids), "label": label, "status": "pending", "log": [], "result": "",
        "params": {"bbox": bbox, "geom": geom, "start": start, "end": end,
                   "name": name, "reduction": reduction, "cache_dir": cache_dir},
    }
    with _lock:
        queue.set(queue.value + [job])
    _ensure_worker()
    return job["id"]


def remove(job_id: int) -> None:
    """Remove a *pending* job (running/finished jobs are left untouched)."""
    with _lock:
        queue.set([j for j in queue.value
                   if not (j["id"] == job_id and j["status"] == "pending")])


def clear_finished() -> None:
    """Drop done/failed jobs, keeping pending and running ones."""
    with _lock:
        queue.set([j for j in queue.value if j["status"] in ("pending", "running")])


def _patch(job_id: int, **changes) -> None:
    with _lock:
        queue.set([{**j, **changes} if j["id"] == job_id else j for j in queue.value])


def _append_log(job_id: int, line: str) -> None:
    with _lock:
        queue.set([{**j, "log": j["log"] + [line]} if j["id"] == job_id else j
                   for j in queue.value])


def _ensure_worker() -> None:
    start = False
    with _lock:
        if not worker_running.value and any(j["status"] == "pending" for j in queue.value):
            worker_running.set(True)
            start = True
    if start:
        threading.Thread(target=_drain, daemon=True).start()


def _drain() -> None:
    while True:
        # Atomically claim the next pending job, or stop if none remain. Doing the
        # check and the worker_running flip under the same lock as enqueue avoids a
        # race where a job added just as the worker exits would be left stranded.
        with _lock:
            job = next((j for j in queue.value if j["status"] == "pending"), None)
            if job is None:
                worker_running.set(False)
                return
            jid = job["id"]
            p = dict(job["params"])
            queue.set([{**j, "status": "running"} if j["id"] == jid else j
                       for j in queue.value])
        try:
            entry, changed = bake_or_extend(
                p["bbox"], p["start"], p["end"], name=p["name"], aoi_geom=p["geom"],
                reduction=p["reduction"], cache_dir=p["cache_dir"],
                progress=lambda m, _jid=jid: _append_log(_jid, m),
            )
            msg = (f"{'Built/updated' if changed else 'Already covered'}: {entry.key} — "
                   f"{entry.n_passes} passes, {entry.start}..{entry.end}")
            _patch(jid, status="done", result=msg)
            state.registry_version.set(state.registry_version.value + 1)
        except Exception as e:  # noqa: BLE001  (isolate per-job failure, keep draining)
            _patch(jid, status="failed", result=f"Error: {e}")
