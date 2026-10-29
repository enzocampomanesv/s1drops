"""Tab 1 — Explore (public).

Pick a cube, see a basemap/hotspot image overlay on the map, click a cell to get
its per-(pol x orbit) dB series, filter the date range, and run drop detection on
the filtered+padded view with a live plot, table, and CSV export.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
import math
import re
import threading
from typing import Dict, List, Optional, Tuple

import ipyleaflet
import numpy as np
import pandas as pd
import solara
from ipywidgets import HTML, Layout, jslink
from pyproj import Transformer

from .. import optical
from ..analysis.series import cell_index
from ..config import (
    optical_radius_m,
    s2_candidates,
    s2_cloud_bucket,
    s2_cloud_thresh,
    s2_max_probes,
    s2_window_days,
)
from ..cube import list_cubes, read_cube
from ..cube.lcz import DEFAULT_CLASSES, LCZ_NAMES, LCZ_RGB
from . import (accounts, confidence, findings, layout, logic, maputil, places, plots, render,
               report, share, state, validation)

# Plain-language UI label -> internal code. The first entry of each is the default.
POL_OPTIONS = {"VV (recommended)": "vv", "VH": "vh", "VV − VH difference": "vv_vh"}
ORBIT_OPTIONS = {"Both (recommended)": "both", "Ascending only": "ascending",
                 "Descending only": "descending"}
CHANGE_OPTIONS = {"Decrease (e.g. demolition)": "drop",
                  "Increase (e.g. new construction)": "rise", "Both": "both"}
METHOD_OPTIONS = {"Changepoint (recommended)": "pelt", "Sliding window": "threshold"}
LAYER_OPTIONS = {"Detected change": "hotspot", "Radar image": "basemap", "Land use": "lcz"}

HOTSPOT_CMAP = "YlOrRd"      # sequential: the value is a magnitude starting at 0
DEFAULT_MIN_CHANGE = 2.5     # dB; shared by the map filter and the detector
ALL_DATES = "All dates"
CUSTOM_RANGE = "Custom range"

# Land-use filter options show the class name only; the number is internal.
LCZ_BY_NAME = {LCZ_NAMES[c]: c for c in sorted(LCZ_NAMES)}
LCZ_OPTIONS = list(LCZ_BY_NAME)
LCZ_DEFAULT_SEL = [LCZ_NAMES[c] for c in DEFAULT_CLASSES]


def _classes_from_labels(labels):
    return [LCZ_BY_NAME[s] for s in labels]


def _slug(s: str) -> str:
    """Filesystem-safe token for download filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s)).strip("-") or "cube"


def _cube_labels(cubes) -> dict:
    """Readable cube label -> key, e.g. "Lagos · 2015–2026 · 100 m"; keys break ties."""
    out = {}
    for c in cubes:
        label = (f"{c.name.replace('_', ' ').title()} · "
                 f"{str(c.start)[:4]}–{str(c.end)[:4]} · {c.resolution:g} m")
        if c.reduction == "overview_med":
            label += " (fast)"
        out[label if label not in out else f"{label} [{c.key}]"] = c.key
    return out


def _day(d) -> str:
    return pd.Timestamp(d).strftime("%d %b %Y") if d else "?"


def _legend_html(legend) -> str:
    """Small HTML legend card for the map: a colour bar or category swatches."""
    if not legend:
        return ""
    box = ("background:rgba(255,255,255,0.92);padding:6px 9px;border-radius:4px;"
           "font:12px/1.35 sans-serif;color:#222;box-shadow:0 1px 4px rgba(0,0,0,.3);"
           "max-width:220px")
    if legend["kind"] == "categories":
        rows = "".join(
            f"<div><span style='display:inline-block;width:11px;height:11px;"
            f"background:{c};margin-right:5px;vertical-align:-1px'></span>{t}</div>"
            for t, c in legend["items"])
        return f"<div style='{box}'><b>{legend['title']}</b>{rows}</div>"
    bar = (f"<div style='height:10px;margin:4px 0 2px;border-radius:2px;"
           f"background:linear-gradient(to right,{','.join(legend['stops'])})'></div>")
    ticks = (f"<div style='display:flex;justify-content:space-between'>"
             f"<span>{legend['lo']}</span><span>{legend['hi']}</span></div>")
    note = f"<div style='color:#555'>{legend['note']}</div>" if legend.get("note") else ""
    return f"<div style='{box}'><b>{legend['title']}</b>{bar}{ticks}{note}</div>"


def _fit_zoom(bounds, viewport_px: int = 340) -> int:
    """Approximate web-mercator zoom that frames `bounds` in a square viewport.

    Computed from the AOI's degree span (longitude compressed by cos(lat)) rather
    than fit_bounds, so framing doesn't depend on the container being sized yet.
    """
    (s, w), (n, e) = bounds
    midlat = (s + n) / 2.0
    lat_span = max(n - s, 1e-6)
    lon_span = max((e - w) * math.cos(math.radians(midlat)), 1e-6)
    span = max(lat_span, lon_span)
    z = math.log2(viewport_px * 360.0 / (256.0 * span))
    return max(1, min(19, int(math.floor(z))))


@solara.component
def CubeMap(overlay_url, bounds, on_click, highlight: Optional[Tuple[float, float]],
            height: str = "80vh", footprint=None, legend_html: str = "", focus=None,
            outline=None, on_view=None, search_box=None, validated=None):
    map_el = ipyleaflet.Map.element(
        scroll_wheel_zoom=True, layout=Layout(height=height, width="100%"),
    )

    def setup_base():
        m = solara.get_widget(map_el)
        m.add_class("s1-map")       # height set by the --s1-map-h layout variable
        sat = ipyleaflet.basemap_to_tiles(ipyleaflet.basemaps.Esri.WorldImagery)
        sat.name = "Satellite"
        m.add(sat)
        m.add(ipyleaflet.LayersControl(position="topright"))
        maputil.add_search(m)
    solara.use_effect(setup_base, [])

    def fit_to_aoi():
        # Frame the AOI only when the cube/AOI changes — not on clicks or date
        # changes — so the user's pan/zoom is preserved during interaction.
        m = solara.get_widget(map_el)
        try:
            m.fit_bounds(bounds)
        except Exception:
            pass
    solara.use_effect(fit_to_aoi, [str(bounds)])

    def update_overlay():
        m = solara.get_widget(map_el)
        for lyr in list(m.layers):
            if isinstance(lyr, ipyleaflet.ImageOverlay):
                m.remove(lyr)
        if overlay_url:
            ov = ipyleaflet.ImageOverlay(url=overlay_url, bounds=bounds, opacity=0.7)
            ov.name = "S1 layer"
            m.add(ov)
    solara.use_effect(update_overlay, [overlay_url, str(bounds)])

    def update_highlight():
        m = solara.get_widget(map_el)
        for lyr in list(m.layers):
            if isinstance(lyr, ipyleaflet.CircleMarker):
                m.remove(lyr)
        if highlight is not None:
            m.add(ipyleaflet.CircleMarker(
                location=highlight, radius=6, color="red", fill_color="red", weight=2,
            ))
    solara.use_effect(update_highlight, [str(highlight)])

    def update_footprint():
        m = solara.get_widget(map_el)
        for lyr in list(m.layers):
            if isinstance(lyr, ipyleaflet.Rectangle) and lyr.name == "Optical AOI":
                m.remove(lyr)
        if footprint:
            rect = ipyleaflet.Rectangle(
                bounds=footprint, color="#ffcc00", weight=2, fill=False,
            )
            rect.name = "Optical AOI"
            m.add(rect)
    solara.use_effect(update_footprint, [str(footprint)])

    def update_outline():
        m = solara.get_widget(map_el)
        for lyr in list(m.layers):
            if getattr(lyr, "name", "") == "Selected area":
                m.remove(lyr)
        if outline:
            m.add(ipyleaflet.GeoJSON(data=outline, name="Selected area", style={
                "color": "#00e5ff", "weight": 3, "fill": False}))
    solara.use_effect(update_outline, [str(outline)])

    def update_validated():
        # Places with a verdict, outlined in the verdict's colour (signed-in only).
        m = solara.get_widget(map_el)
        for lyr in list(m.layers):
            if getattr(lyr, "name", "") == "Verdicts":
                m.remove(lyr)
        if validated and validated["features"]:
            m.add(ipyleaflet.GeoJSON(
                data=validated, name="Verdicts",
                style={"weight": 2, "fill": False, "dashArray": "4 3", "opacity": 0.9},
                style_callback=lambda f: {"color": f["properties"]["colour"]}))
    solara.use_effect(update_validated, [str(validated)])

    def update_search_box():
        # The map area Strongest changes was computed for, when limited to a view.
        m = solara.get_widget(map_el)
        for lyr in list(m.layers):
            if getattr(lyr, "name", "") == "Searched area":
                m.remove(lyr)
        if search_box:
            box = ipyleaflet.Rectangle(bounds=search_box, color="#ffffff", weight=2,
                                       dash_array="6 6", fill=False, opacity=0.8)
            box.name = "Searched area"
            m.add(box)
    solara.use_effect(update_search_box, [str(search_box)])

    def report_view():
        # The visible area ((south, west), (north, east)), as the browser reports it
        # after each pan/zoom, for "only within the map view".
        if on_view is None:
            return
        m = solara.get_widget(map_el)

        def changed(change):
            if change["new"]:
                on_view(tuple(tuple(float(v) for v in c) for c in change["new"]))

        m.observe(changed, "bounds")
        if m.bounds:
            on_view(tuple(tuple(float(v) for v in c) for c in m.bounds))
        return lambda: m.unobserve(changed, "bounds")
    solara.use_effect(report_view, [on_view])

    def update_legend():
        # The legend is the map's only WidgetControl, so it can be found by type.
        m = solara.get_widget(map_el)
        for c in list(m.controls):
            if isinstance(c, ipyleaflet.WidgetControl):
                m.remove(c)
        if legend_html:
            m.add(ipyleaflet.WidgetControl(widget=HTML(legend_html), position="bottomleft"))
    solara.use_effect(update_legend, [legend_html])

    def fly_to():
        # Only explicit focus requests (candidate list) move the map; plain map
        # clicks never do, so the user's own pan/zoom is left alone.
        if focus:
            m = solara.get_widget(map_el)
            lat, lon, zoom = focus
            m.center = (lat, lon)
            m.zoom = zoom
    solara.use_effect(fly_to, [str(focus)])

    def setup_click():
        m = solara.get_widget(map_el)

        def handler(**kw):
            if kw.get("type") == "click" and kw.get("coordinates"):
                lat, lon = kw["coordinates"]
                on_click((lat, lon))

        m.on_interaction(handler)
        return lambda: m.on_interaction(handler, remove=True)
    solara.use_effect(setup_click, [on_click])

    return map_el


@solara.component
def SectionHeader(title, opened, info=None):
    """Clickable collapse/expand header bound to a boolean reactive, with an
    optional (i) explanation."""
    with solara.Row(style={"align-items": "center"}, gap="4px"):
        solara.Button(
            icon=True,
            icon_name="mdi-chevron-down" if opened.value else "mdi-chevron-right",
            on_click=lambda: opened.set(not opened.value),
        )
        solara.Text(title, style={"font-weight": "600"})
        if info:
            InfoTip(info)


@solara.component
def InfoTip(text: str, left: bool = False):
    """A small (i) that shows an explanation on hover, or on tap on touch screens.

    Explanations live here rather than on the page, to keep the panels uncluttered.
    left=True opens it towards the left (for the sidebar, so it doesn't spill over
    the map). The z-index lifts it above Leaflet's panes and controls (up to 1000).
    """
    btn = solara.v.Btn(icon=True, x_small=True, v_on="info.on", style_="opacity:0.7",
                       children=[solara.v.Icon(small=True,
                                               children=["mdi-information-outline"])])
    return solara.v.Menu(
        open_on_hover=True, close_on_content_click=False, offset_y=True, max_width=360,
        left=left, z_index=2000,
        v_slots=[{"name": "activator", "variable": "info", "children": [btn]}],
        children=[solara.v.Card(children=[solara.v.CardText(
            children=[solara.Markdown(text)])])],
    )


@solara.component
def Headline(areas, resolution, summaries=None, *, by_area=True, in_view=False):
    """The totals behind the list, in one line: how many places, how much land,
    how many too recent to confirm and (for approved users) confirmed demolitions."""
    if not areas:
        return
    cell_ha = resolution * resolution / 10_000.0
    ha = sum(c.get("area_ha", cell_ha) for c in areas)
    recent = sum(1 for c in areas if c.get("recent"))
    noun = "place" if by_area else "pixel"
    parts = [f"**{len(areas)} {noun}{'s' if len(areas) != 1 else ''}** with a lasting drop"
             + (" in this map area" if in_view else ""),
             f"**{_ha(ha)}** in total"]
    if recent:
        parts.append(f"{recent} too recent to confirm")
    if summaries is not None:
        confirmed = sum(1 for sm in summaries.values() if sm.verdict == "demolition")
        parts.append(f"{confirmed} confirmed demolition{'s' if confirmed != 1 else ''}")
    with solara.Row(style={"align-items": "center"}, gap="2px"):
        solara.Markdown(" · ".join(parts), style={"font-size": "14px"})
        InfoTip("Totals for everything found with the current settings, not only the "
                "rows listed. Land is the area of the changed pixels. Confirmed "
                "demolitions are places a validator marked as demolition (and no "
                "reviewer vetoed).")


@solara.component
def ConfidenceBadge(rating, prefix=""):
    """How sure we are, as a coloured label; hover or tap it for the four reasons."""
    pill = solara.v.Html(
        tag="span", v_on="why.on", children=[prefix + rating.label],
        style_=f"display:inline-block;padding:0 8px;border-radius:9px;font-size:0.8em;"
               f"line-height:1.7;white-space:nowrap;color:#fff;cursor:help;"
               f"background:{rating.colour}")
    rows = [solara.v.Html(tag="div", style_="font-weight:600;margin-bottom:4px",
                          children=[f"Confidence: {rating.label}"])]
    for f in rating.factors:
        rows.append(solara.v.Html(tag="div", style_="margin:2px 0", children=[
            solara.v.Html(tag="span", children=[confidence.MARKS[f.level]],
                          style_="display:inline-block;width:16px;font-weight:600"),
            solara.v.Html(tag="b", children=[f.name + ": "]), f.text]))
    return solara.v.Menu(
        open_on_hover=True, close_on_content_click=False, offset_y=True, max_width=360,
        z_index=2000,
        v_slots=[{"name": "activator", "variable": "why", "children": [pill]}],
        children=[solara.v.Card(children=[solara.v.CardText(children=rows)])],
    )


@solara.component
def WithInfo(info: str, children=[]):
    """A sidebar control with its (i) explanation beside it, opening leftwards."""
    with solara.Row(style={"align-items": "center", "background": "transparent"}, gap="0px"):
        solara.Column(children=children, style={"flex": "1 1 auto", "min-width": "0",
                                                "background": "transparent"})
        InfoTip(info, left=True)


@solara.component
def ResponsiveFigure(fig):
    """solara.FigurePlotly, but filling its container's width and following resizes.

    The stock component draws at Plotly's default 700 px when it can't measure the
    container, which overflows a narrow side panel; `responsive` makes Plotly size
    to the container and re-fit whenever the window (or a drag handle) resizes it.
    """
    from plotly.graph_objs._figurewidget import FigureWidget

    el = FigureWidget.element()

    def update():
        w = solara.get_widget(el)
        w._config = {"responsive": True}   # not a constructor argument
        w.layout = fig.layout
        n = len(w.data)
        w.add_traces(fig.data)
        w.data = list(w.data)[n:]
    solara.use_effect(update, [fig])
    return el


def _grey_canvas(dark: bool):
    """Esri's muted grey canvas basemap, light or dark (keyless, unlike CARTO's)."""
    if not dark:
        return ipyleaflet.basemap_to_tiles(ipyleaflet.basemaps.Esri.WorldGrayCanvas)
    return ipyleaflet.TileLayer(
        url=("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
             "World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"),
        attribution="Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ",
        name="Esri.WorldDarkGrayCanvas", max_zoom=16)


@solara.component
def SyncedOpticalMaps(before, after, bounds, before_idx, after_idx, recenter_n=0):
    """Two before/after mini-maps (drag-resizable height) with pan/zoom synced via jslink.

    Each side offers up to N candidate scenes cycled with ◀/▶; flipping swaps only the
    ImageOverlay on the same geobox, so framing (and your zoom/pan) never changes.
    Images sit over a muted gray basemap (dark in dark mode) with the fetch-footprint
    rectangle. Each change of `recenter_n` (the Recenter button, in the caller's
    header) frames the photos again.
    """
    dark = bool(solara.lab.use_dark_effective())
    if bounds:
        (s, w), (n, e) = bounds
        center = [(s + n) / 2.0, (w + e) / 2.0]
        fit_zoom = _fit_zoom(bounds)
    else:
        center, fit_zoom = [0.0, 0.0], 2

    def selected(info, idx):
        cands = info.get("candidates", [])
        return cands[min(idx, len(cands) - 1)] if cands else None

    def cap(side, info, idx):
        cands = info.get("candidates", [])
        if not cands:
            return f"_{side}: {info.get('note', 'no clear scene found')} " \
                   f"(probed {info.get('probed', 0)})_"
        i = min(idx, len(cands) - 1)
        c = cands[i]
        return (f"_{side}: S2 {c['date']} · {c['cloud'] * 100:.0f}% AOI cloud "
                f"· probed {info.get('probed', '?')} · {i + 1}/{len(cands)}_")

    def controls(side, info, idx):
        n = len(info.get("candidates", []))
        with solara.Row(style={"align-items": "center"}):
            solara.Button(icon_name="mdi-chevron-left", icon=True,
                          on_click=lambda: idx.set(max(0, idx.value - 1)),
                          disabled=(n == 0 or idx.value <= 0))
            solara.Markdown(cap(side, info, idx.value))
            solara.Button(icon_name="mdi-chevron-right", icon=True,
                          on_click=lambda: idx.set(min(n - 1, idx.value + 1)),
                          disabled=(n == 0 or idx.value >= n - 1))

    with solara.Columns([1, 1]):
        with solara.Column(gap="2px"):
            m1 = ipyleaflet.Map.element(scroll_wheel_zoom=True,
                                        layout=Layout(height="420px", width="100%"))
            controls("before", before, before_idx)
        with solara.Column(gap="2px"):
            m2 = ipyleaflet.Map.element(scroll_wheel_zoom=True,
                                        layout=Layout(height="420px", width="100%"))
            controls("after", after, after_idx)
    layout.drag_handle("--s1-s2-h", target=".s1-s2-map")

    def setup():
        w1, w2 = solara.get_widget(m1), solara.get_widget(m2)
        for w in (w1, w2):
            w.add_class("s1-s2-map")    # height set by the --s1-s2-h layout variable
            if bounds:
                w.add(ipyleaflet.Rectangle(bounds=bounds, color="#ffcc00", weight=2, fill=False))
                w.min_zoom = max(1, fit_zoom - 1)   # small zoom-out buffer around the AOI
                w.max_zoom = 19
                w.center = center                   # explicit framing (robust vs fit_bounds)
                w.zoom = fit_zoom
        # Synced pan/zoom. If this ever misbehaves in the browser, delete the two
        # jslink lines below for independent (still individually zoomable) maps.
        jslink((w1, "center"), (w2, "center"))
        jslink((w1, "zoom"), (w2, "zoom"))
    solara.use_effect(setup, [])

    def set_basemap():
        # Tiles here are only ever basemaps (photos are ImageOverlays): replace them
        # all, including Leaflet's default OSM layer, and keep the new one underneath.
        for el in (m1, m2):
            w = solara.get_widget(el)
            rest = tuple(lyr for lyr in w.layers if not isinstance(lyr, ipyleaflet.TileLayer))
            w.layers = (_grey_canvas(dark),) + rest
    solara.use_effect(set_basemap, [dark])

    def swap(el, info, idx):
        w = solara.get_widget(el)
        for lyr in list(w.layers):
            if isinstance(lyr, ipyleaflet.ImageOverlay):
                w.remove(lyr)
        c = selected(info, idx.value)
        if c and c.get("url"):
            w.add(ipyleaflet.ImageOverlay(url=c["url"], bounds=bounds, opacity=1.0))
    # In an effect, not the button's handler: widgets can only be looked up while
    # rendering ("no render context" otherwise).
    def recenter():
        if recenter_n:
            for el in (m1, m2):
                w = solara.get_widget(el)
                w.center = center
                w.zoom = fit_zoom
    solara.use_effect(recenter, [recenter_n])

    solara.use_effect(lambda: swap(m1, before, before_idx), [before_idx.value])
    solara.use_effect(lambda: swap(m2, after, after_idx), [after_idx.value])


def _when(ts: float) -> str:
    return pd.Timestamp(ts, unit="s").strftime("%d %b %Y")


@solara.component
def VerdictItem(rec, user, on_change):
    """One recorded verdict; reviewers and admins can veto it or undo a veto."""
    vetoing = solara.use_reactive(False)
    reason = solara.use_reactive("")
    error = solara.use_reactive("")
    vetoed = rec.status == "vetoed"
    head = solara.v.Html(tag="div", children=[
        solara.v.Html(tag="b", children=[validation.VERDICTS[rec.verdict]],
                      style_="text-decoration:line-through" if vetoed else ""),
        f" · {rec.user_name} · {_when(rec.created_at)}"
        + (f" · {rec.confidence} confidence" if rec.confidence else "")])
    lines = [head]
    if rec.evidence:
        lines.append(solara.v.Html(tag="div", style_="opacity:0.8", children=[
            "Evidence: " + ", ".join(validation.EVIDENCE[e] for e in rec.evidence)]))
    if rec.event_date:
        lines.append(solara.v.Html(tag="div", style_="opacity:0.8",
                                   children=[f"When: {rec.event_date}"]))
    if rec.note:     # plain text, never rendered as markup
        lines.append(solara.v.Html(tag="div", style_="white-space:pre-wrap",
                                   children=[rec.note]))
    if rec.link:
        lines.append(solara.v.Html(tag="a", children=[rec.link], attributes={
            "href": rec.link, "target": "_blank", "rel": "noopener noreferrer nofollow"},
            style_="word-break:break-all"))
    if vetoed:
        lines.append(solara.v.Html(tag="div", style_="color:#e57373", children=[
            f"Vetoed by {rec.vetoed_by_name}" + (f": {rec.veto_reason}"
                                                  if rec.veto_reason else "")]))

    def do(action):
        try:
            action()
            error.set("")
            vetoing.set(False)
            on_change()
        except (accounts.NotAllowed, ValueError) as e:
            error.set(str(e))

    with solara.Column(gap="2px", style={"padding": "8px 0",
                                         "border-bottom": "1px solid rgba(128,128,128,.25)"}):
        solara.v.Html(tag="div", children=lines, style_="font-size:14px")
        if user.can_review:
            if vetoed:
                solara.Button("Undo veto", text=True, small=True,
                              on_click=lambda: do(lambda: validation.undo_veto(user, rec.id)))
            elif not vetoing.value:
                solara.Button("Veto", text=True, small=True, color="error",
                              on_click=lambda: vetoing.set(True))
            else:
                solara.InputText("Why are you vetoing this? (shown with it)", value=reason)
                with solara.Row():
                    solara.Button("Confirm veto", color="error", small=True,
                                  on_click=lambda: do(lambda: validation.veto(
                                      user, rec.id, reason.value)))
                    solara.Button("Cancel", text=True, small=True,
                                  on_click=lambda: vetoing.set(False))
        if error.value:
            solara.Error(error.value)


def _target_key(cube_key, t) -> str:
    """Identity of the change being judged, for resetting the verdict form."""
    if t is None:
        return ""
    return f"{cube_key}|{sorted(validation.candidate_cells(t))}|{t.get('date_after')}"


VERDICT_ICONS = {"demolition": "mdi-home-remove", "other": "mdi-swap-horizontal",
                 "none": "mdi-close-circle-outline", "unsure": "mdi-help-circle-outline"}


@solara.component
def VerdictPanel(cube, cube_key, target, records, user, photos_seen):
    """The verdict on the change being looked at, under its satellite photos.

    One tap on an answer saves it (tapping another replaces it); evidence and the
    rest are optional, under "Add details". Earlier verdicts, with veto for
    reviewers, are folded away below.
    """
    verdict = solara.use_reactive("")
    evidence = solara.use_reactive([])
    link = solara.use_reactive("")
    event_date = solara.use_reactive("")
    confidence = solara.use_reactive("")
    note = solara.use_reactive("")
    message = solara.use_reactive(("", ""))      # (kind, text)
    details_open = solara.use_reactive(False)
    history_open = solara.use_reactive(False)
    key = _target_key(cube_key, target)

    approved = user is not None and user.approved
    found = validation.matching(records, target) if (approved and target) else []
    shown = [r for r in found if r.source == "user" and r.status != "superseded"]
    mine = (next((r for r in shown if r.user_id == user.id and r.status == "active"), None)
            if approved else None)

    def _reset():
        for r, v in ((evidence, []), (link, ""), (event_date, ""), (confidence, ""),
                     (note, ""), (message, ("", "")), (details_open, False)):
            r.set(v)
    solara.use_effect(_reset, [key])
    # The answer shown as chosen is the user's own standing verdict, if any.
    solara.use_effect(lambda: verdict.set(mine.verdict if mine else ""),
                      [key, mine and mine.id])

    # They just looked at the photos: that's their evidence unless they say otherwise.
    def _prefill():
        if photos_seen and not evidence.value:
            evidence.set(["s2"])
    solara.use_effect(_prefill, [key, photos_seen])

    line = {"font-size": "13px", "opacity": "0.8"}
    if user is None:
        solara.v.Html(tag="div", style_="font-size:13px;opacity:0.8;margin-top:12px",
                      class_="s1-tour-validate",
                      children=["Know what happened here? ",
                                solara.v.Html(tag="a", attributes={"href": "/auth/login"},
                                              children=["Sign in"]),
                                " to help confirm changes."])
        return
    if not user.approved:
        solara.v.Html(tag="div", class_="s1-tour-validate",
                      style_="font-size:13px;opacity:0.8;margin-top:12px",
                      children=["Your account is waiting for approval; then you can "
                                "record what happened here."])
        return
    if target is None:
        return

    bump = lambda: state.validation_version.set(state.validation_version.value + 1)  # noqa: E731

    def save(v):
        try:
            validation.record(user, cube, target, cube_key=cube_key, verdict=v,
                              evidence=evidence.value, link=link.value,
                              event_date=event_date.value, confidence=confidence.value,
                              note=note.value)
        except (accounts.NotAllowed, ValueError) as e:
            message.set(("error", str(e)))
            return
        verdict.set(v)
        message.set(("ok", f"Saved: {validation.VERDICTS[v]}. Tap another answer to "
                           "change it."))
        bump()

    refs = [r for r in found if r.source == "reference"]
    with solara.Column(gap="4px", classes=["s1-tour-validate"],
                       style={"margin-top": "12px", "padding-top": "8px",
                              "border-top": "1px solid rgba(128,128,128,.3)"}):
        # Asked about the place and the change's month, not "these photos": a
        # verdict can come before any photos are fetched, or from other evidence.
        when = target.get("date_after")
        when = f" around {pd.Timestamp(when):%b %Y}" if when else ""
        with solara.Row(style={"align-items": "center"}, gap="2px"):
            solara.Text(f"What happened here{when}?", style={"font-weight": "600"})
            InfoTip(VALIDATION_INFO)
        if not photos_seen:
            solara.Text("Fetch photos with “Get photos”, or answer from other imagery or local "
                        "knowledge.", style=line)
        with solara.Row(style={"flex-wrap": "wrap"}, gap="6px"):
            for v, label in validation.VERDICTS.items():
                chosen = verdict.value == v
                solara.Button(validation.SHORT[v].capitalize(), icon_name=VERDICT_ICONS.get(v),
                              small=True, attributes={"title": label},
                              outlined=not chosen, depressed=chosen,
                              color=validation.COLOURS[v] if chosen else None,
                              dark=True if chosen else None,
                              style={"text-transform": "none", "letter-spacing": "0"},
                              on_click=lambda v=v: save(v))
        kind, text = message.value
        if kind == "error":
            solara.Error(text)
        elif kind == "ok":
            solara.Text(text, style=line)
        if refs:     # admins only (for_cube hides reference data from everyone else)
            names = sorted({r.ref_name for r in refs if r.ref_name})
            years = sorted({r.ref_year for r in refs if r.ref_year})
            solara.v.Html(tag="div", style_="font-size:13px", children=[
                _chip("ref", "#455a64"), " Matches your reference demolitions"
                + (f" ({', '.join(map(str, years))})" if years else "")
                + (f": {', '.join(names[:3])}" if names else "")
                + ". Only admins see this."])

        small = {"text-transform": "none", "letter-spacing": "0", "align-self": "flex-start"}
        solara.Button(("Hide details" if details_open.value else "Add details")
                      + " (evidence, confidence, date, link, note)", text=True, small=True,
                      icon_name="mdi-chevron-up" if details_open.value else "mdi-chevron-down",
                      style=small, on_click=lambda: details_open.set(not details_open.value))
        if details_open.value:
            ev_labels = {v: k for k, v in validation.EVIDENCE.items()}
            solara.SelectMultiple("Evidence you used",
                                  values=[validation.EVIDENCE[e] for e in evidence.value],
                                  all_values=list(ev_labels),
                                  on_value=lambda vs: evidence.set([ev_labels[v] for v in vs]))
            solara.Select("Confidence", values=list(validation.CONFIDENCE),
                          value=confidence.value or None,
                          on_value=lambda v: confidence.set(v or ""))
            solara.InputText("When it happened, if known (e.g. March 2024)", value=event_date)
            solara.InputText("Link to evidence (optional)", value=link)
            solara.InputTextArea("Note (optional)", value=note, rows=3)
            solara.Button("Save with details", color="primary", small=True,
                          disabled=not verdict.value, style={"align-self": "flex-start"},
                          on_click=lambda: save(verdict.value))
            if not verdict.value:
                solara.Text("Choose an answer above first.", style=line)

        if shown:
            solara.Button(f"{'Hide' if history_open.value else 'Show'} verdicts so far "
                          f"({len(shown)})", text=True, small=True,
                          icon_name="mdi-chevron-up" if history_open.value
                          else "mdi-chevron-down",
                          style=small, on_click=lambda: history_open.set(not history_open.value))
            if history_open.value:
                for rec in sorted(shown, key=lambda r: -r.created_at):
                    VerdictItem(rec, user, bump).key(f"verdict-{rec.id}")


def _verdict_layer(records) -> Dict:
    """Verdict outlines for the map: each place's latest counting verdict."""
    latest: Dict[str, object] = {}
    for r in sorted(records, key=lambda r: r.created_at):
        if r.source == "user" and r.counts:
            latest[json.dumps(r.geometry, sort_keys=True)] = r
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": r.geometry,
         "properties": {"colour": validation.COLOURS[r.verdict]}}
        for r in latest.values()]}


def _photo_options(results, min_change: float) -> List[Dict]:
    """Detected changes to fetch photos for, strongest first, in the words of the
    Detected changes table ("Strong drop · Mar 2023"). One option per merged event,
    anchored on its first and last image so the photos bracket every track's view."""
    opts, seen = [], {}
    for e in sorted(findings.events(results), key=lambda e: -abs(e.delta_db)):
        label = (f"{findings.strength(e.delta_db, min_change)} "
                 f"{'drop' if e.is_drop else 'rise'} · {pd.Timestamp(e.end):%b %Y}")
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label += f" ({seen[label]})"
        opts.append({"label": label, "date_before": str(e.start), "date_after": str(e.end)})
    return opts


@solara.component
def OpticalPanel(cube, cube_key, cell, results, cube_name, cache_dir,
                 default_start, default_end, footprint, min_change,
                 picked=None, records=(), user=None, photos=None):
    """Fetch + show Sentinel-2 before/after imagery for a drop (on demand), and ask
    for a verdict on it underneath (`picked`: the listed change being looked at)."""
    mode = solara.use_reactive("cell")          # "cell" | "manual"
    sel_label = solara.use_reactive("")
    man_before = solara.use_reactive(default_start)
    man_after = solara.use_reactive(default_end)
    running = solara.use_reactive(False)
    status = solara.use_reactive([])
    error = solara.use_reactive("")
    result_open = solara.use_reactive(True)
    cloud_pct = solara.use_reactive(int(round(s2_cloud_thresh() * 100)))
    win_days = solara.use_reactive(int(s2_window_days()))
    # The fetched photos and which scene of each side is shown. Held by the caller
    # when given (`photos`), so the PDF report can include what's on screen.
    own = (solara.use_reactive(None), solara.use_reactive(0), solara.use_reactive(0))
    result, before_idx, after_idx = photos or own

    opts = _photo_options(results, min_change)
    labels = [o["label"] for o in opts]

    def _fix_sel():
        if labels and sel_label.value not in labels:
            sel_label.set(labels[0])
    solara.use_effect(_fix_sel, [tuple(labels)])

    def do_fetch():
        error.set("")
        result.set(None)
        footprint.set(None)
        before_idx.set(0)
        after_idx.set(0)
        if cell is None:
            error.set("Select a location on the map first.")
            return
        if mode.value == "cell":
            if not opts:
                error.set("No change detected here — switch to “Choose dates”.")
                return
            o = opts[labels.index(sel_label.value)] if sel_label.value in labels else opts[0]
            before_a, after_a = o["date_before"], o["date_after"]
        else:
            before_a, after_a = man_before.value, man_after.value

        running.set(True)
        status.set([])

        def prog(m):
            status.set(status.value + [m])

        def worker():
            try:
                r = optical.fetch_before_after(
                    cube, cube_key=cube_key, cell=cell,
                    before_anchor=before_a, after_anchor=after_a,
                    radius_m=optical_radius_m(), window_days=win_days.value,
                    cloud_thresh=cloud_pct.value / 100.0, cloud_bucket=s2_cloud_bucket(),
                    max_probes=int(s2_max_probes()), n_candidates=int(s2_candidates()),
                    cache_dir=cache_dir, progress=prog,
                )
                # Reproject each candidate to a web-mercator overlay (off the UI thread).
                bounds = None
                for side in ("before", "after"):
                    for cand in r[side].get("candidates", []):
                        url, bounds = render.overlay_rgb(cand["rgb"], r["transform"], r["crs"])
                        cand["url"] = url
                r["bounds"] = bounds
                r["cell"] = str(cell)        # whose photos these are
                result.set(r)
                footprint.set(bounds)
            except Exception as e:  # noqa: BLE001
                error.set(f"Error: {e}")
            finally:
                running.set(False)

        threading.Thread(target=worker, daemon=True).start()

    # The change a verdict is about: the listed one picked, else the change chosen
    # for photos at the clicked spot.
    sel = next((o for o in opts if o["label"] == sel_label.value), opts[0] if opts else None)
    target = picked
    if target is None and cell is not None and sel is not None:
        target = {"row": cell[0], "col": cell[1], "date_before": sel["date_before"][:10],
                  "date_after": sel["date_after"][:10]}
    photos_seen = bool(result.value) and result.value.get("cell") == str(cell)

    # Collapsed until it has something to show: opens by itself when the selected
    # location has a detected change (with the verdict buttons for validators, before
    # any photos are fetched), closes again when it doesn't. Still openable by hand
    # for "Choose dates".
    has_change = cell is not None and bool(opts)
    panel_open = solara.use_reactive(has_change)
    solara.use_effect(lambda: panel_open.set(has_change), [str(cell), has_change])

    solara.v.Divider(style_="margin: 4px 0 8px")
    header = (panel_open, cell is not None and not opts)
    with solara.Column(classes=["s1-tour-photos"], gap="0px"):
        if panel_open.value:    # the header then heads the controls column
            OpticalBody(mode, opts, sel_label, labels, man_before, man_after, cloud_pct,
                        win_days, do_fetch, running, status, error, result, result_open,
                        before_idx, after_idx, cube_name, header=header,
                        verdict=(cube, cube_key, target, records, user, photos_seen))
        else:
            OpticalHeader(*header)


@solara.component
def OpticalHeader(panel_open, no_change=False):
    with solara.Row(style={"align-items": "center", "flex-wrap": "wrap"}, gap="4px"):
        SectionHeader("Compare satellite photos", panel_open,
                      info="Sentinel-2 true-colour images from just before and just "
                           "after the change, covering the selected location. With "
                           "“Choose dates”, enter dates as YYYY-MM-DD.")
        if no_change:
            solara.Text("· no change detected here",
                        style={"opacity": "0.6", "font-size": "13px"})


@solara.component
def OpticalBody(mode, opts, sel_label, labels, man_before, man_after, cloud_pct,
                win_days, do_fetch, running, status, error, result, result_open,
                before_idx, after_idx, cube_name, header=None, verdict=()):
    # Controls in a narrow column on the left under the panel's header, photos
    # beside them starting level with that header, so nothing sits above the
    # photos but their own title. Wraps on narrow screens.
    with solara.Row(style={"align-items": "flex-start", "flex-wrap": "wrap"}, gap="16px"):
        with solara.Column(style={"flex": "0 0 280px", "max-width": "280px"}, gap="4px"):
            if header:
                OpticalHeader(*header)
            OpticalControls(mode, opts, sel_label, labels, man_before, man_after,
                            cloud_pct, win_days, do_fetch, running, status, error)
        with solara.Column(style={"flex": "1 1 420px", "min-width": "0"}, gap="4px"):
            if result.value:
                OpticalResult(result.value, result_open, before_idx, after_idx, cube_name)
            elif not running.value:
                solara.Markdown("_Photos appear here after “Get photos”._",
                                style={"line-height": "36px"})
            if verdict:     # right under the evidence it's about
                VerdictPanel(*verdict)


@solara.component
def OpticalControls(mode, opts, sel_label, labels, man_before, man_after,
                    cloud_pct, win_days, do_fetch, running, status, error):
    small = {"font-size": "12px", "letter-spacing": "0", "padding": "0 10px"}
    with solara.ToggleButtonsSingle(value=mode, dense=True):
        solara.Button("Detected change", value="cell", style=small)
        solara.Button("Choose dates", value="manual", style=small)
    if mode.value == "cell":
        if not opts:
            with solara.Row(style={"align-items": "center"}, gap="2px"):
                solara.Text("No detected change here",
                            style={"opacity": "0.7", "font-size": "13px"})
                InfoTip("Pick another location on the map, or switch to "
                        "“Choose dates” to compare photos from any two dates.")
        else:
            solara.Select("Change", value=sel_label, values=labels)
    else:
        solara.InputText("Before date", value=man_before)
        solara.InputText("After date", value=man_after)
    solara.SliderInt("Max cloud %", value=cloud_pct, min=0, max=60)
    solara.InputInt("Search ± days", value=win_days)
    solara.Button("Get photos", on_click=do_fetch, color="primary", disabled=running.value)
    if running.value:
        solara.ProgressLinear(True)
        if status.value:
            solara.Markdown(f"`{status.value[-1]}`")
    if error.value:
        solara.Error(error.value)


@solara.component
def OpticalResult(r, result_open, before_idx, after_idx, cube_name):
    recenter_n = solara.use_reactive(0)
    with solara.Row(style={"align-items": "center"}, gap="8px"):
        SectionHeader("Before / after photos", result_open)
        if result_open.value:
            solara.Button("Recenter", text=True, small=True, icon_name="mdi-crosshairs-gps",
                          style={"text-transform": "none", "letter-spacing": "0"},
                          on_click=lambda: recenter_n.set(recenter_n.value + 1))
    if not result_open.value:
        return
    # Remount the maps per fetch so Leaflet sizes correctly; key on the first
    # candidate dates + resolution.
    def _first(side):
        cs = r[side].get("candidates", [])
        return cs[0]["date"] if cs else "na"
    key = f"opt-{_first('before')}-{_first('after')}-{r['resolution']}"
    SyncedOpticalMaps(r["before"], r["after"], r.get("bounds"),
                      before_idx, after_idx, recenter_n.value).key(key)
    # One line under the photos: what they are, and their GeoTIFF downloads.
    with solara.Row(style={"align-items": "center", "flex-wrap": "wrap"}, gap="4px"):
        solara.Text(f"Imagery at {r['resolution']:.0f} m · pan/zoom synced · ◀/▶ to "
                    "change scene · GeoTIFF:",
                    style={"font-size": "13px", "opacity": "0.8", "font-style": "italic"})
        for side, idx in (("before", before_idx), ("after", after_idx)):
            cands = r[side].get("candidates", [])
            if cands:
                c = cands[min(idx.value, len(cands) - 1)]
                pct = int(round(c["cloud"] * 100))
                solara.FileDownload(
                    (lambda p=c["tif_path"]: open(p, "rb").read()),
                    filename=(f"{cube_name}_optical_{side}_"
                              f"{c['date']}_cloud{pct}pct.tif"),
                    children=[solara.Button(side.capitalize(), icon_name="mdi-download",
                                            text=True, small=True,
                                            style={"text-transform": "none",
                                                   "letter-spacing": "0"})])


_TH = "cursor:pointer;user-select:none;white-space:nowrap"
# Sorting by confidence: settled and likely changes first, ruled out last.
CONFIDENCE_ORDER = {"Confirmed": 7, "High": 6, "Disputed": 5, "Medium": 4, "Other change": 3,
                    "Low": 2, "Too early": 1, "Ruled out": 0}


@solara.component
def SortHeader(label, key, sort, classes=""):
    """Clickable column header: first click sorts descending, the next flips it."""
    col, desc = sort.value
    arrow = (" ▼" if desc else " ▲") if col == key else ""
    th = solara.v.Html(tag="th", children=[label + arrow], style_=_TH, class_=classes)
    solara.v.use_event(th, "click",
                       lambda *_: sort.set((key, not desc if col == key else True)))
    return th


def _place_text(c, names) -> str:
    """Place name, "…" while the lookup is pending, coordinates if it failed."""
    name = names.get(places._key(c["lat"], c["lon"]))
    if name:
        return name
    if name is None:
        return "…"
    return f"{c['lat']:.4f}, {c['lon']:.4f}"


LOCAL_MARK_DB = 0.5   # mark a reported drop when the local value exceeds the average by this


def _is_local(c) -> bool:
    """Whether a candidate's reported drop is the local one, noticeably above the average."""
    return c.get("drop_db", 0.0) - c.get("avg_drop_db", c.get("drop_db", 0.0)) >= LOCAL_MARK_DB


def _ha(x: float) -> str:
    return f"{x:.0f} ha" if x >= 10 else f"{x:.1f} ha"


def _chip(text, colour, title=""):
    return solara.v.Html(
        tag="span", children=[text], attributes={"title": title},
        style_=f"display:inline-block;margin-left:6px;padding:0 6px;border-radius:9px;"
               f"font-size:0.75em;line-height:1.6;white-space:nowrap;color:#fff;"
               f"background:{colour}")


def _verdict_chips(summary) -> list:
    """Small badges after the place name: what validators said, and (admins only)
    whether it matches reference data."""
    if summary is None:
        return []
    chips = []
    if summary.verdict:
        label = ("mixed verdicts" if summary.verdict == "mixed"
                 else validation.SHORT[summary.verdict])
        mark = {"demolition": "✓ ", "none": "✗ "}.get(summary.verdict, "")
        chips.append(_chip(f"{mark}{label}" + (f" ({summary.count})" if summary.count > 1
                                                else ""),
                           validation.COLOURS[summary.verdict],
                           f"Verdicts from {summary.count} validator"
                           f"{'s' if summary.count != 1 else ''}"))
    if summary.reference:
        chips.append(_chip("ref", "#455a64", "Matches your reference demolitions "
                                             "(visible to admins only)"))
    return chips


@solara.component
def CandidateRow(c, names, on_pick, selected, summary=None, rating=None):
    """One table row; clicking anywhere on it selects the site and flies there."""
    bg = "background:rgba(255,152,0,0.22);" if selected else ""   # reads in both themes
    # Verdicts show through the confidence label; only the admins' reference
    # match keeps its own chip.
    ref = [_chip("ref", "#455a64", "Matches your reference demolitions (visible to "
                                   "admins only)")] if summary and summary.reference else []
    cells = [
        solara.v.Html(tag="td", children=[_place_text(c, names), *ref]),
        solara.v.Html(tag="td", children=[_day(c["date_after"])], style_="white-space:nowrap"),
    ]
    if "area_ha" in c:
        cells.append(solara.v.Html(tag="td", children=[_ha(c["area_ha"])], class_="s1-col-area",
                                   style_="white-space:nowrap;text-align:right"))
    drop = [f"−{c['drop_db']:.1f}"]
    tip = ""
    if _is_local(c):
        drop.append(solara.v.Html(tag="sup", children=["*"]))
        tip = (f"Around the drop ({logic.LOCAL_PASSES} images either side): "
               f"−{c['drop_db']:.1f} dB. Averaged over the whole period: "
               f"−{c['avg_drop_db']:.1f} dB.")
    cells.append(solara.v.Html(tag="td", children=drop, attributes={"title": tip},
                               style_="font-weight:600;text-align:right;white-space:nowrap"))
    cells.append(solara.v.Html(tag="td", children=[ConfidenceBadge(rating)] if rating else [],
                               style_="text-align:center"))
    tr = solara.v.Html(tag="tr", style_=f"cursor:pointer;{bg}", children=cells)
    solara.v.use_event(tr, "click", lambda *_: on_pick(c))
    return tr


@solara.component
def CandidateTable(candidates, on_pick, selected_rank, more, cache_dir, by_area,
                   min_area_ha=0.0, in_view=False, summaries=None, ratings=None):
    """Strongest changes as a sortable table of place, date, (area) and drop.

    by_area lists change areas (largest first); otherwise single pixels (strongest
    drop first), the original ranking.
    """
    default_sort = ("area", True) if by_area else ("drop", True)
    sort = solara.use_reactive(default_sort)        # (column, descending)
    solara.use_effect(lambda: sort.set(default_sort), [by_area])
    keys = tuple(places._key(c["lat"], c["lon"]) for c in candidates)
    # key -> name ("" = failed lookup); missing = still pending
    names = solara.use_reactive({})

    def resolve():
        # Cached names show at once; the rest are looked up one by one in the
        # background (Nominatim allows ~1 request/s) and fill in as they arrive.
        known = {k: n for k, c in zip(keys, candidates)
                 if (n := places.cached(cache_dir, c["lat"], c["lon"])) is not None}
        names.set(known)
        stop = threading.Event()

        def worker():
            for k, c in zip(keys, candidates):
                if stop.is_set():
                    return
                if k not in known:
                    n = places.lookup(cache_dir, c["lat"], c["lon"])
                    if not stop.is_set():
                        names.set({**names.value, k: n or ""})

        threading.Thread(target=worker, daemon=True).start()
        return stop.set
    solara.use_effect(resolve, [keys, cache_dir])

    if not candidates:
        solara.Markdown(
            f"_No areas of at least {_ha(min_area_ha)} changed together "
            f"{'in this map area' if in_view else 'in this period'}. "
            "Try a smaller minimum area or a lower minimum change"
            + (", or search a larger map area._" if in_view else "._") if by_area else
            "_No changes above the minimum "
            f"{'in this map area' if in_view else 'in this period'}. "
            "Try a lower minimum change or a longer period"
            + (", or search a larger map area._" if in_view else "._"))
        return
    col, desc = sort.value
    keyf = {
        "area": lambda c: (c.get("area_ha", 0.0), c["drop_db"]),
        "date": lambda c: c["date_after"] or "",
        "confidence": lambda c: (CONFIDENCE_ORDER.get(
            (ratings or {}).get(c["rank"]).label if (ratings or {}).get(c["rank"]) else "", 0),
            c["drop_db"]),
    }.get(col, lambda c: c["drop_db"])
    shown = sorted(candidates, key=keyf, reverse=desc)
    if col == "area":    # unconfirmed areas stay under the confirmed ones either way
        shown = ([c for c in shown if not c.get("recent")]
                 + [c for c in shown if c.get("recent")])
    with solara.v.SimpleTable(dense=True):
        with solara.v.Html(tag="thead"):
            with solara.v.Html(tag="tr"):
                solara.v.Html(tag="th", children=["Place"])
                SortHeader("Date", "date", sort)
                if by_area:
                    SortHeader("Area", "area", sort, classes="s1-col-area")
                SortHeader("Drop (dB)", "drop", sort)
                SortHeader("Confidence", "confidence", sort)
        with solara.v.Html(tag="tbody"):
            for c in shown:
                CandidateRow(c, names.value, on_pick, selected_rank == c["rank"],
                             (summaries or {}).get(c["rank"]),
                             (ratings or {}).get(c["rank"])).key(f"cand-{c['rank']}")
    solara.Text("Place names © OpenStreetMap contributors",
                style={"font-size": "0.75em", "opacity": "0.6"})
    if not more.value:
        solara.Button("Show more", text=True, on_click=lambda: more.set(True))
    else:
        solara.Button("Show fewer", text=True, on_click=lambda: more.set(False))


@solara.component
def FindingsTable(events, min_change):
    """Detected changes in words: when, how strong, and how many tracks agree."""
    if not events:
        solara.Markdown("_No sudden lasting change found here in this period._")
        return
    with solara.v.SimpleTable(dense=True):
        with solara.v.Html(tag="thead"):
            with solara.v.Html(tag="tr"):
                for h in ("When", "Strength", "Seen by"):
                    solara.v.Html(tag="th", children=[h])
        with solara.v.Html(tag="tbody"):
            for e in events:
                kind = "drop" if e.is_drop else "rise"
                with solara.v.Html(tag="tr"):
                    solara.v.Html(tag="td", children=[findings.when_text(e.start, e.end)])
                    solara.v.Html(tag="td", children=[
                        solara.v.Html(tag="b", children=[
                            f"{findings.strength(e.delta_db, min_change)} {kind}"]),
                        f" · {findings.change_text(e.delta_db)}"])
                    solara.v.Html(tag="td", style_="white-space:nowrap", children=[
                        f"{e.tracks} of {e.total_tracks} satellite "
                        f"track{'s' if e.total_tracks != 1 else ''}"])


def _same_view(a, b) -> bool:
    """Two map views equal to ~1 m (the browser reports positions with float noise)."""
    return a is not None and b is not None and np.allclose(a, b, atol=1e-5)


@solara.component
def ViewSearch(search_box, map_view):
    """"Only within the map view": rank Strongest changes in the visible area.

    Ticking it takes a snapshot of the view (outlined dashed on the map) instead of
    following the map, since picking a row zooms the map in; after a pan or zoom,
    a button searches the new view.
    """
    def toggle(on):
        search_box.set(map_view if on and map_view else None)

    with solara.Row(style={"align-items": "center", "flex-wrap": "wrap"}, gap="4px"):
        solara.Checkbox(label="Only within the map view", value=bool(search_box.value),
                        on_value=toggle, disabled=map_view is None)
        if search_box.value and not _same_view(search_box.value, map_view):
            solara.Button("Search current view", text=True, icon_name="mdi-refresh",
                          on_click=lambda: search_box.set(map_view))
        InfoTip("Ranks changes inside the part of the map you're looking at, instead "
                "of the whole area: zoom or pan to a neighbourhood first, then tick "
                "this. The searched area is outlined with a dashed line. The list "
                "doesn't follow the map as you move it (picking a row zooms in); press "
                "“Search current view” to search wherever the map is now. Areas that "
                "cross the edge of the view count only the part inside it.")


def _candidates_info(by_area: bool) -> str:
    return (
        ("An **area** is a patch of touching pixels whose radar dropped within about "
         "three weeks of each other, largest first. **Date** = typical first image after "
         "the drop; **drop** = median across the area. " if by_area else
         "Single pixels, strongest drop first. **Date** = first image after the drop. ")
        + f"**Too recent to confirm** = fewer than {logic.MIN_CONFIRM_PASSES} images per "
        "satellite track after the drop, so it can't yet be told apart from a passing "
        "change such as rain or flooding" + ("; these are listed last. " if by_area else ". ")
        + f"\n\n**Drop** is measured over the {logic.LOCAL_PASSES} images on each side of "
        "the change, or over the whole periods before and after it if that is larger. "
        "Averaging the whole period understates a drop when the site changed again later "
        "(for example, it was rebuilt), so a **\\*** marks drops where the local value is "
        "noticeably larger; hover over the number to see both. The map colours use the "
        "whole-period average.\n\n"
        "Click a row to inspect it; click a column header to sort.")


def _table_drop(c):
    """What the chart needs to mark a table row's drop, or None."""
    if not c or not c.get("date_after"):
        return None
    return {"date": c["date_after"], "drop_db": c["drop_db"],
            "avg_drop_db": c["avg_drop_db"] if _is_local(c) else None}


def _chart_info(picked, is_area: bool) -> str:
    text = ("Buildings bounce a lot of the satellite's radar back to it. When structures "
            "are removed, the reflection drops and stays low. Dots are single satellite "
            "passes" + (", each the average over the area's pixels" if is_area else "")
            + "; the line is the overall trend. The grey band is the normal ups and "
            "downs before any change. Shaded bars mark detected changes.")
    if picked:
        text += (
            "\n\n**Why the trend may not reach the table's drop:** the red dotted "
            "line is the drop listed in Strongest changes, measured from the usual level"
            + (" (the median over the area's pixels)" if is_area else "")
            + f". It compares the {logic.LOCAL_PASSES} images either side of the change. "
            "The trend line smooths over neighbouring passes"
            + (" and averages pixels that dropped by different amounts" if is_area else "")
            + ", so it usually doesn't fall as far. Where averaging the whole period "
            "understates the drop (marked * in the table), a fainter line shows that "
            "whole-period value too.")
    return text


VALIDATION_INFO = (
    "Record what actually happened at this change, from the satellite photos or other "
    "evidence. One verdict is enough for a change to count as validated; reviewers and "
    "admins can veto any verdict (it stays on record, crossed out). Saving again "
    "replaces your own earlier verdict. Verdicts are only visible to signed-in, "
    "approved users. On the map, validated places are outlined in their verdict's "
    "colour: green demolition, purple other change, grey no change, yellow can't tell.")


FINDINGS_INFO = ("Strength compares the change with your minimum change setting "
                 "(strong = at least double it). A change seen by more tracks (the "
                 "satellite views this spot from different angles) is more trustworthy.")


@solara.component
def Explore(cache_dir: str = "./cache"):
    # --- all hooks first, unconditionally (rules of hooks) ---
    dark = bool(solara.lab.use_dark_effective())
    cubes = solara.use_memo(
        lambda: list_cubes(cache_dir), [cache_dir, state.registry_version.value]
    )
    cube_labels = _cube_labels(cubes)
    keys = list(cube_labels.values())

    cube_key = solara.use_reactive("")
    pol_label = solara.use_reactive(next(iter(POL_OPTIONS)))
    orbit_label = solara.use_reactive(next(iter(ORBIT_OPTIONS)))
    layer_label = solara.use_reactive(next(iter(LAYER_OPTIONS)))
    method_label = solara.use_reactive(next(iter(METHOD_OPTIONS)))
    change_label = solara.use_reactive(next(iter(CHANGE_OPTIONS)))
    sensitivity = solara.use_reactive(1.0)
    window = solara.use_reactive(2)
    clicked = solara.use_reactive(None)
    focus = solara.use_reactive(None)               # (lat, lon, zoom) to fly the map to
    date_idx = solara.use_reactive((0, 0))
    lcz_sel = solara.use_reactive(LCZ_DEFAULT_SEL)
    min_change = solara.use_reactive(DEFAULT_MIN_CHANGE)          # applied (drives redraw)
    min_change_pending = solara.use_reactive(DEFAULT_MIN_CHANGE)  # live slider value
    neighborhood = solara.use_reactive(True)        # 3x3 neighborhood detection (10 m only)
    optical_footprint = solara.use_reactive(None)    # optical AOI box drawn on the main map
    photos = (solara.use_reactive(None), solara.use_reactive(0),    # fetched photos and
              solara.use_reactive(0))                                # the scenes shown
    plot_open = solara.use_reactive(True)            # right-panel collapse states
    drops_open = solara.use_reactive(True)
    cand_open = solara.use_reactive(True)
    cand_more = solara.use_reactive(False)
    advanced_open = solara.use_reactive(False)
    per_track = solara.use_reactive(False)           # Advanced: raw per-track dB chart
    by_pixel = solara.use_reactive(False)            # Advanced: rank single pixels, not areas
    min_area = solara.use_reactive(logic.DEFAULT_MIN_AREA_HA)            # applied
    min_area_pending = solara.use_reactive(logic.DEFAULT_MIN_AREA_HA)    # live slider
    details_open = solara.use_reactive(False)        # technical detail under the findings
    map_view = solara.use_reactive(None)             # visible map area, as last reported
    search_box = solara.use_reactive(None)           # map area Strongest changes is limited to

    def _ensure_key():
        if keys and cube_key.value not in keys:
            cube_key.set(keys[0])
            clicked.set(None)
    solara.use_effect(_ensure_key, [tuple(keys)])

    active = cube_key.value if cube_key.value in keys else (keys[0] if keys else None)
    entry = next((c for c in cubes if c.key == active), None)
    cube_name = _slug(entry.name if entry else (active or "cube"))

    cube = solara.use_memo(
        lambda: read_cube(_path(cubes, active)) if active else None,
        [active, state.registry_version.value],
    )
    dates = solara.use_memo(
        lambda: (np.unique(np.asarray(cube["time"].values).astype("datetime64[D]"))
                 if cube is not None else np.array([], dtype="datetime64[D]")),
        [active, state.registry_version.value],
    )
    n = len(dates)

    def _reset_range():
        if n:
            date_idx.set((0, n - 1))
    solara.use_effect(_reset_range, [active, n])
    # A searched map area belongs to the cube it was drawn on.
    solara.use_effect(lambda: search_box.set(None), [active])

    # A shared link (?area=…&at=…): switch to its area, then, once that area's
    # dates are known, set its period and select the change. Declared after the
    # effects above so it overrides their defaults.
    router = solara.use_router()
    pending = solara.use_reactive(solara.use_memo(lambda: share.parse(router.search), []))

    def _open_shared():
        link = pending.value
        if not link:
            return
        if link["area"] not in keys:
            pending.set(None)
            return
        if active != link["area"]:
            cube_key.set(link["area"])
            return
        if not n:
            return
        rng = share.date_range(dates, link.get("from"), link.get("to"))
        if rng:
            date_idx.set(rng)
        if "lat" in link:
            clicked.set((link["lat"], link["lon"]))
            focus.set((link["lat"], link["lon"], 16))
        pending.set(None)
    solara.use_effect(_open_shared, [active, n, tuple(keys)])

    lo, hi = date_idx.value
    if n:
        lo = max(0, min(lo, n - 1)); hi = max(lo, min(hi, n - 1))
        start, end = str(dates[lo]), str(dates[hi])
    else:
        start, end = None, None
    pol = POL_OPTIONS[pol_label.value]
    orbit_dir = ORBIT_OPTIONS[orbit_label.value]
    method = METHOD_OPTIONS[method_label.value]
    drop_dir = CHANGE_OPTIONS[change_label.value]
    layer = LAYER_OPTIONS[layer_label.value]
    lcz_classes = _classes_from_labels(lcz_sel.value)

    # One hotspot pass feeds both the map layer and the candidate list.
    # Memo deps are plain values: numpy arrays can't be compared for equality.
    hot_deps = [active, pol, start, end, orbit_dir, tuple(sorted(lcz_classes)),
                state.registry_version.value]
    # The same pass also measures every track's drop at each cell's drop date, for
    # the confidence labels' track check (no per-row reads of the cube).
    hot_all = solara.use_memo(
        lambda: (logic.hotspot_with_tracks(cube, pol=pol, start=start, end=end,
                                           direction=orbit_dir, lcz_classes=lcz_classes)
                 if cube is not None else None),
        hot_deps,
    )
    hot, track_drops = (hot_all[:4], hot_all[4]) if hot_all is not None else (None, None)
    overlay = solara.use_memo(
        lambda: (_overlay(cube, hot, pol, start, end, layer, lcz_classes, min_change.value)
                 if cube is not None else (None, [[0.0, 0.0], [0.0, 0.0]], None)),
        hot_deps + [layer, min_change.value],
    )
    url, bounds, legend = overlay
    def _rank():
        if hot is None:
            return [], None
        ranked = hot
        if search_box.value:   # rank within the searched map area only
            ranked = logic.within(hot, logic.cells_in_bounds(cube, search_box.value))
        if by_pixel.value:
            found, labels = logic.flag_recent(
                cube, logic.top_candidates(cube, *ranked, n=30, min_value=min_change.value),
                end=end, direction=orbit_dir), None
        else:
            found, labels = logic.find_clusters(cube, *ranked, min_value=min_change.value,
                                                min_area_ha=min_area.value,
                                                end=end, direction=orbit_dir)
        return [dict(c, rank=i) for i, c in enumerate(found, 1)], labels
    areas, area_labels = solara.use_memo(
        _rank, hot_deps + [min_change.value, min_area.value, by_pixel.value,
                           str(search_box.value)])
    candidates = areas[:30]

    # Verdicts on this cube's ground, for signed-in approved users (admins also get
    # reference data). Re-read after a verdict is saved or vetoed.
    user = state.user.value
    records = solara.use_memo(
        lambda: validation.for_cube(cube, user) if cube is not None else [],
        [active, user and (user.id, user.role), state.validation_version.value,
         state.registry_version.value])
    summaries = solara.use_memo(
        lambda: ({c["rank"]: sm for c in areas if (sm := validation.summarise(records, c))}
                 if records else {}),
        [id(areas), id(records)])

    # How sure we are about each listed change, for the rows on show.
    n_rows = 30 if cand_more.value else 10
    tracks = ({c["rank"]: confidence.tracks_from_steps(track_drops, c, min_change.value)
               for c in candidates[:n_rows]} if track_drops is not None else {})
    show_verdicts = user is not None and user.approved
    ratings = {c["rank"]: confidence.rate(c, min_change.value, tracks.get(c["rank"]),
                                          summaries.get(c["rank"]), show_verdicts)
               for c in candidates[:n_rows]}

    # --- guards (after all hooks) ---
    if not cubes:
        solara.Warning("No areas have been prepared yet. An admin can add one in the "
                       "Admin tab.")
        return
    if cube is None:
        solara.Info("Loading…")
        return

    has_lcz = logic.has_lcz(cube)
    resolution = float(cube.attrs.get("resolution", 100.0))
    is_native10 = resolution <= 50  # neighborhood only here
    nbhd_window = 1 if (is_native10 and neighborhood.value) else 0   # 1 => 3x3 median

    # resolve click -> cell -> series + detection (hard-restricted to allowed LCZ)
    cell = None
    highlight = None
    cell_lcz = ""
    area = None          # the selected change area (dict), when a click lands in one
    click_note = ""
    if clicked.value is not None:
        idx = cell_index(cube, clicked.value[1], clicked.value[0])
        if idx is None:
            click_note = "That point is outside this area."
        elif not logic.cell_allowed(cube, idx[0], idx[1], lcz_classes):
            click_note = ("That point is outside the area or outside the selected "
                          "land-use classes, so it isn't analysed.")
        else:
            cell = idx
            cell_lcz = logic.lcz_label(logic.lcz_at(cube, idx[0], idx[1]))
            tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
            lon_c, lat_c = tx.transform(cube["x"].values[idx[1]], cube["y"].values[idx[0]])
            highlight = (lat_c, lon_c)
            if area_labels is not None and area_labels[idx] >= 0:
                area = areas[area_labels[idx]]
    selected_rank = (area["rank"] if area else
                     next((c["rank"] for c in candidates if (c["row"], c["col"]) == cell),
                          None))
    outline = logic.area_outline(cube, area["cells"]) if area else None
    picked = area or next((c for c in candidates if c["rank"] == selected_rank), None)

    params = (
        dict(sensitivity=sensitivity.value, min_drop_db=min_change.value, direction=drop_dir)
        if method == "pelt"
        else dict(window=window.value, min_drop_db=min_change.value, direction=drop_dir)
    )

    # Reading the place's series and running the detector take most of a click's
    # time, and a click redraws the page more than once (select, then fly the map),
    # as does any later toggle: reuse the result while its inputs are unchanged.
    series, results, rows, det_key = [], [], [], None
    if cell is not None:
        where = tuple(sorted(area["cells"])) if area is not None else (cell, nbhd_window)

        def _detect():
            ser = (logic.select_area_series(cube, area["cells"], pol, orbit_dir)
                   if area is not None else
                   logic.select_series(cube, cell[0], cell[1], pol, orbit_dir,
                                       window=nbhd_window))
            return ser, logic.run_detection(ser, start=start, end=end, method=method,
                                            **params)
        det_key = ("detect", active, state.registry_version.value, where, pol, orbit_dir,
                   start, end, method, tuple(sorted(params.items())))
        series, results = _reuse(det_key, _detect)
        rows = logic.drop_rows(results)
    events = findings.events(results)

    layer_options = [k for k, v in LAYER_OPTIONS.items() if v != "lcz" or has_lcz]
    if layer_label.value not in layer_options:
        layer_label.set(layer_options[0])

    years = sorted({str(d)[:4] for d in dates})
    period_options = [ALL_DATES] + years + [CUSTOM_RANGE]
    if n and (lo, hi) == (0, n - 1):
        period = ALL_DATES
    elif n and str(dates[lo])[:4] == str(dates[hi])[:4] and \
            (lo == 0 or str(dates[lo - 1])[:4] != str(dates[lo])[:4]) and \
            (hi == n - 1 or str(dates[hi + 1])[:4] != str(dates[hi])[:4]):
        period = str(dates[lo])[:4]
    else:
        period = CUSTOM_RANGE

    def pick_period(p):
        if p == ALL_DATES:
            date_idx.set((0, n - 1))
        elif p in years:
            ix = [i for i, d in enumerate(dates) if str(d)[:4] == p]
            date_idx.set((ix[0], ix[-1]))

    def pick_candidate(c):
        clicked.set((c["lat"], c["lon"]))
        zoom = 17 if is_native10 else 16
        if "bounds" in c:   # frame the whole area, never closer than a single pixel would
            zoom = min(zoom, _fit_zoom(c["bounds"], viewport_px=500))
        focus.set((c["lat"], c["lon"], zoom))

    with solara.Sidebar():
        solara.Markdown("**Period**")
        solara.Select("Period", value=period, values=period_options, on_value=pick_period)
        with WithInfo(f"{hi - lo + 1} of {n} satellite passes fall in this range. Drag "
                      "either end to narrow the period analysed."):
            solara.SliderRangeInt("Fine-tune dates", value=date_idx, min=0,
                                  max=max(0, n - 1))
        solara.Markdown(f"**{start} → {end}**")

        solara.Markdown("---\n**Map**")
        solara.Select("Show on map", value=layer_label, values=layer_options)
        with WithInfo("How much the radar signal must drop to count as a change. "
                      "Smaller changes are hidden on the map and in the analysis. "
                      "Press Apply to update."):
            solara.SliderFloat("Minimum change (dB)", value=min_change_pending,
                               min=0.5, max=8.0, step=0.5)
        if not by_pixel.value:
            cell_ha = resolution * resolution / 10_000.0
            with WithInfo("The smallest patch of changed land listed under Strongest "
                          "changes. Set it to the size you'd count as an eviction. "
                          "Press Apply to update."):
                solara.SliderFloat("Minimum area (ha)", value=min_area_pending,
                                   min=0.0, max=25.0, step=max(cell_ha, 0.25))
            n_px = int(np.ceil(min_area_pending.value / cell_ha - 1e-9))
            solara.v.Html(tag="div", style_="font-size:0.8em;opacity:0.7;margin-bottom:8px",
                          children=[f"= {n_px} pixel{'s' if n_px != 1 else ''} at "
                                    f"{resolution:g} m"])
        solara.Button(
            "Apply", color="primary",
            on_click=lambda: (min_change.set(min_change_pending.value),
                              min_area.set(min_area_pending.value)),
            disabled=(min_change_pending.value == min_change.value
                      and min_area_pending.value == min_area.value),
        )
        solara.FileDownload(
            lambda: logic.hotspot_geojson(
                cube, pol=pol, start=start, end=end, direction=orbit_dir,
                lcz_classes=lcz_classes, min_value=min_change.value),
            filename=(f"{cube_name}_hotspot_min{min_change.value:g}dB_"
                      f"{_slug(pol)}_{start}_{end}.geojson"),
            label="Download map cells (GeoJSON)",
        )
        if user is not None and user.can_review:
            solara.FileDownload(lambda: validation.export(user, "csv"),
                                filename="s1drops_verdicts.csv",
                                label="Download all verdicts (CSV)")
            solara.FileDownload(lambda: validation.export(user, "geojson"),
                                filename="s1drops_verdicts.geojson",
                                label="Download all verdicts (GeoJSON)")
        if has_lcz:
            solara.Markdown("---\n**Land-use filter**")
            with WithInfo("Local Climate Zones (coarse, ~100 m). Areas of other types "
                          "are hidden and can't be clicked."):
                solara.SelectMultiple("Analyse only these land-use types", values=lcz_sel,
                                      all_values=LCZ_OPTIONS)

        solara.Markdown("---")
        SectionHeader("Advanced settings", advanced_open)
        if advanced_open.value:
            solara.Select("Radar channel", value=pol_label, values=list(POL_OPTIONS))
            solara.Select("Satellite passes", value=orbit_label, values=list(ORBIT_OPTIONS))
            with WithInfo("Applies to the analysis of a selected location. The map "
                          "always shows decreases."):
                solara.Select("Type of change", value=change_label,
                              values=list(CHANGE_OPTIONS))
            solara.Select("Detection method", value=method_label, values=list(METHOD_OPTIONS))
            if method == "pelt":
                with WithInfo("Higher = more, smaller changes flagged."):
                    solara.SliderFloat("Sensitivity", value=sensitivity, min=0.2, max=5.0,
                                       step=0.1)
            else:
                with WithInfo("Passes averaged on each side of a candidate change."):
                    solara.SliderInt("Window (passes)", value=window, min=1, max=5)
            if is_native10:
                solara.Checkbox(label="Smooth over 3×3 neighbouring pixels (reduces noise)",
                                value=neighborhood)
            solara.Checkbox(label="Chart: show each satellite track separately (dB)",
                            value=per_track)
            solara.Checkbox(label="Strongest changes: rank individual pixels, not areas",
                            value=by_pixel)

    with solara.Row(classes=["s1-split"], gap="0px"):
        with solara.Column(classes=["s1-left"]):
            CubeMap(url, bounds, clicked.set, highlight, height="70vh",
                    footprint=optical_footprint.value, legend_html=_legend_html(legend),
                    focus=focus.value, outline=outline, on_view=map_view.set,
                    search_box=search_box.value,
                    validated=_verdict_layer(records) if records else None)
            layout.drag_handle("--s1-map-h", target=".s1-map")
            OpticalPanel(cube=cube, cube_key=active, cell=cell, results=results,
                         cube_name=cube_name, cache_dir=cache_dir,
                         default_start=start, default_end=end,
                         footprint=optical_footprint,
                         min_change=min_change.value, picked=picked, records=records,
                         user=user, photos=photos).key("optical-panel")
        layout.drag_handle("--s1-mapcol")
        with solara.Column(classes=["s1-right"]):
            # Every direct child is keyed so collapsing a section (which changes the
            # children present) doesn't shift positions and remount its siblings.
            # Without this, toggling a collapse remounts the plot and table. Bodies are
            # wrapped in keyed columns so reconciliation is fully key-based.
            # Where to start: the area, then the list of changes under it.
            with solara.Column(classes=["s1-tour-area"], gap="0px").key("area-pick"):
                solara.Select("Area", value=[k for k, v in cube_labels.items() if v == active][0],
                              values=list(cube_labels),
                              on_value=lambda lbl: (cube_key.set(cube_labels[lbl]),
                                                    clicked.set(None)))
            if click_note:
                solara.Warning(click_note).key("click-note")
            SectionHeader(f"Strongest changes ({(start or '')[:4]}–{(end or '')[:4]}"
                          + (", map area" if search_box.value else "") + ")",
                          cand_open, info=_candidates_info(not by_pixel.value)
                          ).key("hdr-cand")
            if cand_open.value:
                # Until something is picked, a soft glow says "start here".
                cand_cls = ["s1-tour-cand"] + (["s1-nudge"] if cell is None else [])
                with solara.Column(gap="0px", classes=cand_cls).key("body-cand"):
                    Headline(areas, resolution, summaries if show_verdicts else None,
                             by_area=not by_pixel.value, in_view=bool(search_box.value))
                    ViewSearch(search_box, map_view.value)
                    shown = candidates[:n_rows]
                    CandidateTable(shown, pick_candidate, selected_rank, cand_more, cache_dir,
                                   by_area=not by_pixel.value, min_area_ha=min_area.value,
                                   in_view=bool(search_box.value), summaries=summaries,
                                   ratings=ratings)
                    if user is not None and user.is_admin and any(
                            r.source == "reference" for r in records) and shown:
                        hits = sum(1 for c in shown
                                   if (sm := summaries.get(c["rank"])) and sm.reference)
                        solara.v.Html(tag="div", style_="font-size:13px;opacity:0.85", children=[
                            _chip("ref", "#455a64"),
                            f" Reference check: {hits} of these {len(shown)} match your "
                            "reference demolitions (admins only)."])
            if cell is None:
                with solara.Row(style={"align-items": "center"}, gap="4px").key("intro"):
                    solara.Text("👆 Pick a change from the list to see what happened there.")
                    InfoTip(
                        "**How to read this map:** coloured cells are places where the "
                        "radar signal dropped sharply and stayed low in the selected "
                        "period, the signature of buildings being removed. Darker red "
                        "means a bigger drop. You can also click any spot on the map. A "
                        "drop is a lead to check, not proof of an eviction.")
            if cell is not None:
                lat_c, lon_c = highlight
                gmaps = f"https://www.google.com/maps/search/?api=1&query={lat_c:.6f},{lon_c:.6f}"
                place = places.cached(cache_dir, lat_c, lon_c)
                what = (f"**Selected area** ({_ha(area['area_ha'])}, {area['n_cells']} "
                        f"pixels, outlined on the map)" if area else "**Selected location**")
                solara.Markdown(
                    f"{what}: " + (place or f"{lat_c:.5f}, {lon_c:.5f}")
                    + (f" · {cell_lcz.split(' ', 1)[1]}" if cell_lcz else "")
                    + f" · [open in Google Maps]({gmaps})"
                ).key("sel-loc")
                # How sure we are, with the reasons on hover; the same label as the
                # list, or worked out here for a spot that isn't listed.
                sel_rating = ratings.get(picked["rank"]) if picked else None
                if sel_rating is None and picked is not None:
                    sel_rating = confidence.rate(
                        picked, min_change.value,
                        confidence.track_steps(series, picked.get("date_after"),
                                               min_change.value, start=start, end=end),
                        summaries.get(picked["rank"]) or validation.summarise(records, picked),
                        show_verdicts)
                with solara.Row(style={"align-items": "center", "flex-wrap": "wrap",
                                       "margin": "-8px 0 4px"}, gap="8px").key("sel-actions"):
                    if sel_rating is not None:
                        ConfidenceBadge(sel_rating, prefix="Confidence: ")
                    query = share.query(active, picked or {"lat": lat_c, "lon": lon_c},
                                        start, end, dates)
                    share.CopyLink.element(query=query)
                    if picked is not None and sel_rating is not None:
                        def _report(c=picked, rating=sel_rating, query=query):
                            got, bi, ai = (r.value for r in photos)
                            mine = got and got.get("cell") == str(cell)
                            public = os.environ.get("S1DROPS_PUBLIC_URL", "").rstrip("/")
                            return report.build(
                                place=place or "", area_name=next((k for k, v in cube_labels.items()
                                                       if v == active), active),
                                c=c, rating=rating, events=events,
                                signal=findings.relative_signal(results, events),
                                min_change=min_change.value, cube=cube, mag=hot[0],
                                resolution=resolution,
                                land_use=cell_lcz.split(" ", 1)[1] if cell_lcz else "",
                                link=f"{public}/?{query}" if public else "",
                                photos=dict(got, before_idx=bi, after_idx=ai) if mine else None,
                                verdicts=(validation.matching(records, c)
                                          if show_verdicts else ()))
                        solara.FileDownload(
                            _report, label="Report (PDF)",
                            filename=f"{cube_name}_change_{_slug(place or 'site')}_"
                                     f"{(picked.get('date_after') or '')[:7]}.pdf",
                            icon_name="mdi-file-pdf-box",
                            children=[solara.Button("Report (PDF)", icon_name="mdi-file-pdf-box",
                                                    text=True, small=True,
                                                    style={"text-transform": "none",
                                                           "letter-spacing": "0"})])
                if per_track.value:
                    chart_info = (
                        "Backscatter in dB, one line per satellite track. Shaded bands "
                        "mark each change; dotted lines the signal level before and after.")
                else:
                    chart_info = _chart_info(picked, area is not None)
                SectionHeader("Radar signal over time", plot_open,
                              info=chart_info).key("hdr-ts")
                if plot_open.value:
                    with solara.Column(classes=["s1-tour-chart"]).key("body-ts"):
                        # Remount the plot when its content changes: Solara's FigurePlotly
                        # otherwise leaves the previous cell's drop-span shapes on the layout.
                        plot_key = (f"plot-{cell[0]}-{cell[1]}-{area and area['rank']}-"
                                    f"{pol}-{orbit_dir}-{start}-{end}-"
                                    f"{method}-{sensitivity.value}-{min_change.value}-"
                                    f"{window.value}-{drop_dir}-{dark}-{per_track.value}")
                        fig = _reuse(
                            ("figure", det_key, dark, per_track.value, min_change.value,
                             str(_table_drop(picked))),
                            lambda: plots.figure(results, dark=dark) if per_track.value else
                            plots.simple_figure(findings.relative_signal(results, events),
                                                events, min_change=min_change.value,
                                                dark=dark, table_drop=_table_drop(picked)))
                        ResponsiveFigure(fig).key(plot_key)
                SectionHeader("Detected changes", drops_open,
                              info=FINDINGS_INFO).key("hdr-drops")
                if drops_open.value:
                    with solara.Column().key("body-drops"):
                        FindingsTable(events, min_change.value)
                        if rows:
                            solara.Button(
                                "Hide technical details" if details_open.value
                                else "Show technical details", text=True,
                                icon_name="mdi-chevron-up" if details_open.value
                                else "mdi-chevron-down",
                                on_click=lambda: details_open.set(not details_open.value))
                            if details_open.value:
                                solara.DataFrame(_display_rows(rows))
                        with solara.Row(style={"flex-wrap": "wrap"}):
                            solara.FileDownload(
                                logic.drops_csv(results, cell=cell),
                                filename=(f"{cube_name}_r{cell[0]}c{cell[1]}_{_slug(pol)}_"
                                          f"drop{min_change.value:g}dB_{start}_{end}.csv"),
                                label="Download changes (CSV)",
                            )
                            solara.FileDownload(
                                logic.series_csv(series, start=start, end=end, cell=cell),
                                filename=(f"{cube_name}_r{cell[0]}c{cell[1]}_{_slug(pol)}_"
                                          f"{start}_{end}_series.csv"),
                                label="Download signal (CSV)",
                            )


_REUSED: "OrderedDict[tuple, object]" = OrderedDict()
_REUSED_LOCK = threading.Lock()
REUSE_SIZE = 48     # recent results kept, across all sessions


def _reuse(key: tuple, make):
    """`make()`, or its result from an earlier call with the same `key` (a small
    least-recently-used store shared by all sessions). Keys name the area (and the
    registry version, which changes when a cube is re-baked) plus every input that
    changes the result."""
    with _REUSED_LOCK:
        if key in _REUSED:
            _REUSED.move_to_end(key)
            return _REUSED[key]
    value = make()
    with _REUSED_LOCK:
        _REUSED[key] = value
        while len(_REUSED) > REUSE_SIZE:
            _REUSED.popitem(last=False)
    return value


def _display_rows(rows) -> pd.DataFrame:
    """Drop-table rows with readable column names (CSV exports keep the raw names)."""
    df = pd.DataFrame(rows)
    df["direction"] = df["direction"].str.capitalize()
    df = df[["date_before", "date_after", "delta_db", "pre_db", "post_db", "gap_days",
             "direction", "relative_orbit", "pol"]]
    return df.rename(columns={
        "date_before": "Before", "date_after": "After", "delta_db": "Change (dB)",
        "pre_db": "Level before (dB)", "post_db": "Level after (dB)",
        "gap_days": "Days between", "direction": "Pass", "relative_orbit": "Track",
        "pol": "Channel",
    })


def _path(cubes, key):
    return next(c.path for c in cubes if c.key == key)


def _overlay(cube, hot, pol, start, end, kind, lcz_classes, min_change=0.0):
    """(PNG data URL, lat/lon bounds, legend spec) for the selected map layer."""
    if kind == "lcz":
        layer = logic.lcz_layer(cube)
        present = sorted({int(v) for v in np.unique(layer[np.isfinite(layer)]) if v > 0})
        items = [(LCZ_NAMES.get(c, str(c)), "#%02x%02x%02x" % LCZ_RGB[c])
                 for c in present if c in LCZ_RGB]
        url, bounds = render.overlay_lcz(layer, cube)
        return url, bounds, {"kind": "categories", "title": "Land use (LCZ)", "items": items}
    if kind == "basemap":
        layer = logic.basemap_layer(cube, pol=pol, start=start, end=end, lcz_classes=lcz_classes)
        finite = np.isfinite(layer)
        vmin = float(np.nanpercentile(layer, 2)) if finite.any() else 0.0
        vmax = float(np.nanpercentile(layer, 98)) if finite.any() else 1.0
        url, bounds = render.overlay_from_array(layer, cube, cmap="gray", vmin=vmin, vmax=vmax)
        return url, bounds, {
            "kind": "gradient", "title": "Radar brightness (median)",
            "stops": render.gradient_stops("gray"),
            "lo": f"{vmin:.0f} dB", "hi": f"{vmax:.0f} dB",
            "note": "Bright = buildings and hard surfaces",
        }
    mag = hot[0]
    # vmax from the full layer so colours stay fixed as the threshold moves; the
    # threshold only hides cells (NaN -> transparent), it doesn't rescale colour.
    vmax = float(np.nanpercentile(mag, 98)) if np.isfinite(mag).any() else 1.0
    disp = mag if min_change <= 0 else np.where(mag >= min_change, mag, np.nan)
    url, bounds = render.overlay_from_array(disp, cube, cmap=HOTSPOT_CMAP, vmin=0.0, vmax=vmax)
    return url, bounds, {
        "kind": "gradient", "title": "Drop in radar signal",
        "stops": render.gradient_stops(HOTSPOT_CMAP),
        "lo": "0 dB", "hi": f"{vmax:.1f}+ dB",
        "note": f"Showing drops ≥ {min_change:g} dB",
    }
