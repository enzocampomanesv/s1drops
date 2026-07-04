import threading
import time

from s1drops.app import bakequeue


class _Entry:
    def __init__(self, key):
        self.key = key
        self.n_passes = 3
        self.start = "2024-01-01"
        self.end = "2025-01-01"


def _enq(name, **over):
    kw = dict(label=name, bbox=(0, 0, 1, 1), geom=None, start="s", end="e",
              name=name, reduction="native_median", resolution=100.0, cache_dir="/tmp/x")
    kw.update(over)
    return bakequeue.enqueue(**kw)


def _wait_idle(timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        busy = any(j["status"] in ("pending", "running") for j in bakequeue.queue.value)
        if not bakequeue.worker_running.value and not busy:
            return True
        time.sleep(0.02)
    return False


def _reset():
    bakequeue.queue.set([])
    _wait_idle()


def test_sequential_drain(monkeypatch):
    _reset()
    calls = []

    def fake(bbox, start, end, *, name, aoi_geom, reduction, resolution, cache_dir, progress):
        progress(f"start {name}")
        calls.append(name)
        progress(f"done {name}")
        return _Entry(name), True

    monkeypatch.setattr(bakequeue, "bake_or_extend", fake)
    for nm in ["a", "b", "c"]:
        _enq(nm)
    assert _wait_idle()
    assert calls == ["a", "b", "c"]                       # ran one-by-one, in order
    assert [j["status"] for j in bakequeue.queue.value] == ["done", "done", "done"]
    assert all(j["log"] for j in bakequeue.queue.value)   # progress captured per job


def test_failure_isolated(monkeypatch):
    _reset()

    def fake(bbox, start, end, *, name, aoi_geom, reduction, resolution, cache_dir, progress):
        if name == "bad":
            raise RuntimeError("boom")
        return _Entry(name), True

    monkeypatch.setattr(bakequeue, "bake_or_extend", fake)
    for nm in ["ok1", "bad", "ok2"]:
        _enq(nm)
    assert _wait_idle()
    status = {j["label"]: j["status"] for j in bakequeue.queue.value}
    assert status == {"ok1": "done", "bad": "failed", "ok2": "done"}  # one failure, queue continues
    bad = next(j for j in bakequeue.queue.value if j["label"] == "bad")
    assert "boom" in bad["result"]


def test_remove_pending_and_clear(monkeypatch):
    _reset()
    gate = threading.Event()
    ran = []

    def fake(bbox, start, end, *, name, aoi_geom, reduction, resolution, cache_dir, progress):
        ran.append(name)
        if name == "first":
            gate.wait(3)          # hold the worker so later jobs stay pending
        return _Entry(name), True

    monkeypatch.setattr(bakequeue, "bake_or_extend", fake)
    _enq("first")
    t0 = time.time()
    while not any(j["status"] == "running" for j in bakequeue.queue.value):
        if time.time() - t0 > 3:
            break
        time.sleep(0.01)
    _enq("second")
    sid = next(j["id"] for j in bakequeue.queue.value if j["label"] == "second")
    _enq("third")
    bakequeue.remove(sid)         # remove the still-pending "second"
    gate.set()
    assert _wait_idle()
    assert ran == ["first", "third"]     # removed job never ran
    bakequeue.clear_finished()
    assert bakequeue.queue.value == []
