"""Solara entrypoint — two tabs: Explore (public) and Admin (gated).

Run:
    export S1DROPS_ADMIN_PASSWORD='choose-one'      # enables the Admin tab
    export S1DROPS_CACHE_DIR='./cache'              # optional; defaults to ./cache
    solara run s1drops.app.main
"""
from __future__ import annotations

import os

import solara

from . import admin, explore

CACHE_DIR = os.environ.get("S1DROPS_CACHE_DIR", "./cache")


@solara.component
def Page():
    solara.Title("S1 backscatter drops")
    # Render the active tab's content conditionally (not as hidden Tabs panels):
    # the Explore map must mount into a full-size container, otherwise Leaflet
    # caches a zero/stale size while hidden and paints in the corner on return.
    tab = solara.use_reactive(0)
    with solara.lab.Tabs(value=tab):
        solara.lab.Tab("Explore")
        solara.lab.Tab("Admin")
    if tab.value == 0:
        explore.Explore(CACHE_DIR)
    else:
        admin.Admin(CACHE_DIR)


@solara.component
def Layout(children=[]):
    return solara.AppLayout(children=children)
