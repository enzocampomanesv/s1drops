"""Page layout: fixed-width sidebar, resizable panels, and drag handles.

All adjustable sizes are CSS variables on <html>, so the Python side never has to
know them: CSS reads the variables, and a small browser-side script (`Resizer`)
updates them while a handle is dragged. Sizes are remembered per browser in
localStorage; double-clicking a handle resets that size.

  --s1-sidebar  sidebar width (the main area is offset by exactly this)
  --s1-mapcol   share of the content width taken by the map column
  --s1-map-h    main map height
  --s1-s2-h     height of the Sentinel-2 before/after maps
"""
from __future__ import annotations

import ipyvuetify as vy
import solara
import traitlets

CSS = """
:root {
    --s1-sidebar: 380px;
    --s1-mapcol: 60%;
    --s1-map-h: 70vh;
    --s1-s2-h: 420px;
}
/* Solara's drawer is `width: min-content` (min 400px), so it grows with its widest
   control while the main area is only offset by the minimum. Pin one width and
   offset the main area by exactly that, only while the drawer is open and docked
   (on narrow screens it overlays instead). */
.v-navigation-drawer {
    width: var(--s1-sidebar) !important;
    min-width: var(--s1-sidebar) !important;
    max-width: var(--s1-sidebar) !important;
}
/* Vuetify 2 calls the main area .v-content (.v-main in Vuetify 3). It sets this
   padding inline from the drawer width it measured once at load, so it would not
   follow a resized sidebar without these rules. */
.v-application:has(.v-navigation-drawer--open:not(.v-navigation-drawer--is-mobile)) :is(.v-content, .v-main) {
    padding-left: var(--s1-sidebar) !important;
}
.v-application:not(:has(.v-navigation-drawer--open:not(.v-navigation-drawer--is-mobile))) :is(.v-content, .v-main) {
    padding-left: 0 !important;
}
/* Tabs row under the title bar: full width, white, separated by a rule. */
.v-app-bar .v-toolbar__extension {
    background: white;
    border-bottom: 1px solid rgba(0, 0, 0, 0.12);
    padding: 0 16px;
}
/* Tabs inherit the bar's white text; inactive ones would vanish on white. */
.v-app-bar .v-toolbar__extension .v-tab:not(.v-tab--active) {
    color: rgba(0, 0, 0, 0.6) !important;
}
.theme--dark .v-app-bar .v-toolbar__extension {
    background: #1e1e1e;
    border-bottom-color: rgba(255, 255, 255, 0.12);
}
.theme--dark .v-app-bar .v-toolbar__extension .v-tab:not(.v-tab--active) {
    color: rgba(255, 255, 255, 0.7) !important;
}
/* Overlays are per-cell rasters: show cells as crisp squares when zoomed in,
   not browser-smoothed blobs that blur which cells were flagged. */
.leaflet-image-layer {
    image-rendering: pixelated;
}

/* Map column | handle | side panel. */
.s1-split { display: flex; flex-wrap: nowrap; width: 100%; align-items: stretch; }
.s1-left { flex: 0 0 var(--s1-mapcol); min-width: 0; }
.s1-right { flex: 1 1 0; min-width: 0; }
.s1-map { height: var(--s1-map-h) !important; }
/* Solara wraps the map widget in a div with height:100%, i.e. the whole left
   column, which stretches to the side panel's height. Size it to the map instead,
   or the photos below can't move up when the map is dragged smaller. */
.s1-left > div:has(> .s1-map) { height: auto !important; }
.s1-s2-map { height: var(--s1-s2-h) !important; }

/* "Start here": a slow glow around the list of changes until one is picked. */
@keyframes s1-glow {
    0%, 100% { box-shadow: 0 0 0 1px rgba(33, 150, 243, 0.35); }
    50% { box-shadow: 0 0 0 3px rgba(33, 150, 243, 0.75); }
}
.s1-nudge { border-radius: 6px; animation: s1-glow 2.4s ease-in-out infinite; }

/* Drag handles: invisible until hovered, with a short grip mark in the middle. */
.s1-vsplit, .s1-hsplit, .s1-sidebar-handle { transition: background 0.15s; }
.s1-vsplit {
    flex: 0 0 10px; cursor: col-resize; align-self: stretch;
    background: linear-gradient(rgba(128,128,128,.5), rgba(128,128,128,.5)) center / 2px 40px no-repeat;
}
.s1-hsplit {
    height: 10px; cursor: row-resize; margin: 2px 0;
    background: linear-gradient(rgba(128,128,128,.5), rgba(128,128,128,.5)) center / 40px 2px no-repeat;
}
.s1-sidebar-handle {
    position: fixed; top: 112px; bottom: 0; width: 8px; z-index: 7;
    left: calc(var(--s1-sidebar) - 4px); cursor: col-resize;
}
.s1-vsplit:hover, .s1-hsplit:hover, .s1-sidebar-handle:hover {
    background-color: rgba(25, 118, 210, 0.3);
}
body:not(:has(.v-navigation-drawer--open:not(.v-navigation-drawer--is-mobile))) .s1-sidebar-handle {
    display: none;
}
/* While dragging, maps must not swallow the pointer or the drag stalls over them. */
body.s1-dragging { user-select: none; }
body.s1-dragging .leaflet-container { pointer-events: none; }

/* Phones and narrow tablets: one column in the order people work in: the map,
   then the area, list and details of the picked change, then its photos and
   verdict. The map column's own box is dissolved (display: contents) so its map
   and its photo panel can sit on either side of the list. Solara sets the row
   direction inline, hence !important. */
@media (max-width: 960px) {
    :root { --s1-map-h: 50vh; }
    .s1-split { flex-direction: column !important; }
    .s1-split > .s1-left.d-flex { display: contents !important; }  /* beats Vuetify's d-flex */
    .s1-left > div:has(> .s1-map) { order: 0; }
    .s1-right { order: 1; flex: 1 1 auto !important; width: 100%; }
    .s1-left > *:not(:has(.s1-map)) { order: 2; width: 100%; }
    .s1-vsplit, .s1-hsplit { display: none; }
}
/* Phones: icon-only buttons in the title bar, a shorter title, no name line. */
@media (max-width: 600px) {
    .v-app-bar .v-toolbar__title { font-size: 16px !important; }
    .s1-compact .v-btn__content { font-size: 0 !important; }
    .s1-compact .v-btn__content .v-icon { font-size: 22px !important; margin: 0 !important; }
    .s1-compact.v-btn { min-width: 36px !important; padding: 0 6px !important; }
    .s1-hide-phone { display: none !important; }
    /* Strongest changes: drop the Area column and tighten the rest so Confidence fits. */
    .s1-tour-cand .s1-col-area { display: none; }
    .s1-tour-cand td, .s1-tour-cand th { padding: 0 6px !important; font-size: 13px !important; }
}
"""

_RESIZER_JS = r"""
<template><span style="display: none"></span></template>
<script>
module.exports = {
  mounted() {
    // One set of document-level listeners per page, however often this remounts.
    if (window.__s1Resizer) return;
    window.__s1Resizer = true;
    const root = document.documentElement;
    const KEY = 's1drops-layout';
    const LIMITS = {
      '--s1-sidebar': [240, 640],
      '--s1-mapcol': [30, 80],
      '--s1-map-h': [250, 2000],
      '--s1-s2-h': [200, 1600],
    };
    const unit = (k) => (k === '--s1-mapcol' ? '%' : 'px');
    const set = (k, v) => {
      const [lo, hi] = LIMITS[k];
      root.style.setProperty(k, Math.round(Math.min(hi, Math.max(lo, v))) + unit(k));
    };
    const save = () => {
      try {
        const out = {};
        for (const k in LIMITS) {
          const v = parseFloat(root.style.getPropertyValue(k));
          if (!isNaN(v)) out[k] = v;
        }
        localStorage.setItem(KEY, JSON.stringify(out));
      } catch (e) { /* storage blocked: sizes just aren't remembered */ }
    };
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
      for (const k in saved) if (LIMITS[k]) set(k, saved[k]);
    } catch (e) { /* no saved layout */ }

    const sidebar = document.createElement('div');
    sidebar.className = 's1-sidebar-handle';
    sidebar.dataset.s1Drag = '--s1-sidebar';
    sidebar.title = 'Drag to resize · double-click to reset';
    document.body.appendChild(sidebar);

    // Leaflet re-measures its container on window resize; nudge it while dragging.
    let raf = 0;
    const relayout = () => {
      if (!raf) raf = requestAnimationFrame(() => {
        raf = 0;
        window.dispatchEvent(new Event('resize'));
      });
    };
    // Responsive Plotly charts get their config just after their first draw, at
    // the default 700 px; a resize as soon as one appears fits it to its panel.
    new MutationObserver((muts) => {
      for (const m of muts) for (const n of m.addedNodes) {
        if (n.nodeType === 1 && (n.matches('.js-plotly-plot') || n.querySelector('.js-plotly-plot'))) {
          setTimeout(relayout, 50);
          return;
        }
      }
    }).observe(document.body, { childList: true, subtree: true });

    const handleOf = (e) => e.target.closest && e.target.closest('[data-s1-drag]');
    let drag = null;
    document.addEventListener('pointerdown', (e) => {
      const h = handleOf(e);
      if (!h) return;
      drag = h;
      e.preventDefault();
      document.body.classList.add('s1-dragging');
    });
    document.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const k = drag.dataset.s1Drag;
      if (k === '--s1-sidebar') {
        set(k, e.clientX);
      } else if (k === '--s1-mapcol') {
        const r = drag.closest('.s1-split').getBoundingClientRect();
        set(k, ((e.clientX - r.left) / r.width) * 100);
      } else {
        const t = document.querySelector(drag.dataset.s1Target);
        if (t) set(k, e.clientY - t.getBoundingClientRect().top);
      }
      relayout();
    });
    document.addEventListener('pointerup', () => {
      if (!drag) return;
      drag = null;
      document.body.classList.remove('s1-dragging');
      relayout();
      save();
    });
    document.addEventListener('dblclick', (e) => {
      const h = handleOf(e);
      if (!h) return;
      root.style.removeProperty(h.dataset.s1Drag);
      relayout();
      save();
    });
  },
};
</script>
"""


class Resizer(vy.VuetifyTemplate):
    """Invisible widget whose browser-side script drives the drag handles."""
    template = traitlets.Unicode(_RESIZER_JS).tag(sync=True)


def drag_handle(var: str, target: str = ""):
    """A drag handle for one size variable.

    Without `target` it is a vertical bar between the split's columns; with
    `target` (a CSS selector) it is a horizontal bar that sets that element's height.
    """
    attrs = {"data-s1-drag": var, "title": "Drag to resize · double-click to reset"}
    if target:
        attrs["data-s1-target"] = target
    return solara.v.Html(tag="div", class_="s1-hsplit" if target else "s1-vsplit",
                         attributes=attrs)
