"""Solara entrypoint — two tabs: Explore (public) and Admin (gated).

Run:
    export S1DROPS_ADMIN_PASSWORD='choose-one'      # enables the Admin tab
    export S1DROPS_CACHE_DIR='./cache'              # optional; defaults to ./cache
    solara run s1drops.app.main
"""
from __future__ import annotations

import os

import solara
import solara.server.settings

from . import admin, explore, layout

CACHE_DIR = os.environ.get("S1DROPS_CACHE_DIR", "./cache")

# Dark by default. The page template reads this on every request; a visitor's own
# choice from the toggle is kept in their browser and wins over it.
solara.server.settings.theme.variant = "dark"


@solara.component
def Page():
    solara.Title("Rapid change detection tool")
    solara.Style(layout.CSS)
    layout.Resizer.element()
    # The toggle starts from the server-side theme state, which is unset until the
    # browser reports its theme; seed it so the first click switches to light.
    dark = bool(solara.lab.use_dark_effective())
    if solara.lab.theme.dark is None:
        solara.lab.theme.dark = dark
    # Render the active tab's content conditionally (not as hidden Tabs panels):
    # the Explore map must mount into a full-size container, otherwise Leaflet
    # caches a zero/stale size while hidden and paints in the corner on return.
    # The tabs sit in the app bar's extension row (Solara puts Tabs there) so the
    # sidebar, which is clipped below the app bar, can never cover them. White
    # with a rule underneath, so they read as their own row, not part of the title.
    tab = solara.use_reactive(0)
    with solara.AppBar():
        solara.lab.ThemeToggle(enable_auto=False)
        with solara.lab.Tabs(value=tab, align="left",
                             background_color="#1e1e1e" if dark else "white",
                             color="primary", slider_color="primary"):
            solara.lab.Tab("Explore")
            solara.lab.Tab("Admin")
    if tab.value == 0:
        explore.Explore(CACHE_DIR)
    else:
        admin.Admin(CACHE_DIR)


@solara.component
def Layout(children=[]):
    return solara.AppLayout(children=children)
