"""The monthly update scheduler: a background thread that, once a month (from the
1st, after 02:00 UTC), queues the update of every area with new satellite passes.

The update itself is `cube.refresh.update_all`, run through the bake queue (so
it never overlaps a bake). Admin > "Update now" queues exactly the same job.
"""
from __future__ import annotations

import logging
import threading
import time

from ..cube import refresh
from . import bakequeue

CHECK_EVERY_S = 30 * 60
HOUR_UTC = 2            # quiet time for the VM

_started = threading.Event()


def _loop(cache_dir) -> None:
    log = logging.getLogger(__name__)
    while True:
        try:
            if time.gmtime().tm_hour >= HOUR_UTC and refresh.due(cache_dir):
                bakequeue.enqueue_update(cache_dir, trigger="scheduled")
        except Exception:  # noqa: BLE001 — a bad check must not kill the scheduler
            log.exception("update scheduler check failed")
        time.sleep(CHECK_EVERY_S)


def start(cache_dir) -> None:
    """Start the scheduler once per server process. S1DROPS_UPDATER=0 keeps it off:
    only one process may bake (the Space4All API runs it; see deploy/compose.yaml)."""
    import os

    if os.environ.get("S1DROPS_UPDATER", "1") == "0":
        return
    if not _started.is_set():
        _started.set()
        threading.Thread(target=_loop, args=(cache_dir,), daemon=True,
                         name="monthly-update").start()
