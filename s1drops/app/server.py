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

import solara.server.starlette as solara_server  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

from . import webauth  # noqa: E402

app = Starlette(
    routes=[*webauth.routes, *solara_server.routes],   # ours first: Solara's catch all
    lifespan=solara_server.lifespan,
    middleware=solara_server.middleware,
)
