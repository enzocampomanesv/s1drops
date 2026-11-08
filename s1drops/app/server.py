"""ASGI entry point: Solara's app plus the sign-in routes in front of it.

Run (instead of `solara run`, which serves Solara alone):
    uvicorn s1drops.app.server:app --host 127.0.0.1 --port 8765
Development, restarting on code changes:
    uvicorn s1drops.app.server:app --reload --port 8765
"""
from __future__ import annotations

import os

# Solara picks its app from the environment when its server module is imported.
os.environ.setdefault("SOLARA_APP", "s1drops.app.main")

import threading  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

import solara.server.starlette as solara_server  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

from . import preview, webauth  # noqa: E402


@asynccontextmanager
async def lifespan(app):
    """Solara's start-up and shutdown, plus computing each area's default view in
    the background so the first visitors don't wait for it, and the monthly
    update scheduler."""
    from . import explore, updater
    cache_dir = os.environ.get("S1DROPS_CACHE_DIR", "./cache")
    threading.Thread(target=explore.warm_up, args=(cache_dir,), daemon=True,
                     name="warm-up").start()
    updater.start(cache_dir)        # monthly update of every area with new passes
    async with solara_server.lifespan(app):
        yield


# Link previews are added to the page's HTML on its way out (see `preview`).
app = preview.PreviewTags(Starlette(
    routes=[*webauth.routes, *preview.routes, *solara_server.routes],  # Solara's catches all
    lifespan=lifespan,
    middleware=solara_server.middleware,
))
