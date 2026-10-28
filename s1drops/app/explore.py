"""Tab 1 — Explore (public).

Pick a cube, see a basemap/hotspot image overlay on the map, click a cell to get
its per-(pol x orbit) dB series, filter the date range, and run drop detection on
the filtered+padded view with a live plot, table, and CSV export.
"""
from __future__ import annotations

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
from . import findings, layout, logic, maputil, places, plots, render, state

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
            outline=None):
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
            if isinstance(lyr, ipyleaflet.Rectangle):
                m.remove(lyr)
        if footprint:
            rect = ipyleaflet.Rectangle(
                bounds=footprint, color="#ffcc00", weight=2, fill=False,
            )
            rect.name = "Optical AOI"
            m.add(rect)
    solara.use_effect(update_footprint, [str(footprint)])

    def update_outline():
        # The selected change area's outline is the map's only GeoJSON layer.
        m = solara.get_widget(map_el)
        for lyr in list(m.layers):
            if isinstance(lyr, ipyleaflet.GeoJSON):
                m.remove(lyr)
        if outline:
            m.add(ipyleaflet.GeoJSON(data=outline, name="Selected area", style={
                "color": "#00e5ff", "weight": 3, "fill": False}))
    solara.use_effect(update_outline, [str(outline)])

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
def SyncedOpticalMaps(before, after, bounds, before_idx, after_idx):
    """Two before/after mini-maps (drag-resizable height) with pan/zoom synced via jslink.

    Each side offers up to N candidate scenes cycled with ◀/▶; flipping swaps only the
    ImageOverlay on the same geobox, so framing (and your zoom/pan) never changes.
    Images sit over a muted gray basemap (dark in dark mode) with the fetch-footprint
    rectangle.
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

    def recenter():
        for el in (m1, m2):
            w = solara.get_widget(el)
            w.center = center
            w.zoom = fit_zoom

    with solara.Row():
        solara.Button("Recenter", on_click=recenter, text=True, icon_name="mdi-crosshairs-gps")
    with solara.Columns([1, 1]):
        with solara.Column():
            m1 = ipyleaflet.Map.element(scroll_wheel_zoom=True,
                                        layout=Layout(height="420px", width="100%"))
            controls("before", before, before_idx)
        with solara.Column():
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
    solara.use_effect(lambda: swap(m1, before, before_idx), [before_idx.value])
    solara.use_effect(lambda: swap(m2, after, after_idx), [after_idx.value])


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
                 default_start, default_end, footprint, min_change):
    """Fetch + show Sentinel-2 before/after imagery for a drop (on demand)."""
    mode = solara.use_reactive("cell")          # "cell" | "manual"
    sel_label = solara.use_reactive("")
    man_before = solara.use_reactive(default_start)
    man_after = solara.use_reactive(default_end)
    running = solara.use_reactive(False)
    status = solara.use_reactive([])
    result = solara.use_reactive(None)
    error = solara.use_reactive("")
    result_open = solara.use_reactive(True)
    cloud_pct = solara.use_reactive(int(round(s2_cloud_thresh() * 100)))
    win_days = solara.use_reactive(int(s2_window_days()))
    before_idx = solara.use_reactive(0)          # which candidate scene is shown
    after_idx = solara.use_reactive(0)

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
                result.set(r)
                footprint.set(bounds)
            except Exception as e:  # noqa: BLE001
                error.set(f"Error: {e}")
            finally:
                running.set(False)

        threading.Thread(target=worker, daemon=True).start()

    solara.Markdown("---")
    with solara.Row(style={"align-items": "center"}, gap="4px"):
        solara.Markdown("**Compare satellite photos (Sentinel-2)**")
        InfoTip("True-colour images from just before and just after the change, "
                "covering the selected location. With “Choose dates”, enter dates as "
                "YYYY-MM-DD.")
    with solara.ToggleButtonsSingle(value=mode):
        solara.Button("Use detected change", value="cell")
        solara.Button("Choose dates", value="manual")
    if mode.value == "cell":
        if not opts:
            solara.Info("No change detected at this location — pick another, "
                        "or switch to “Choose dates”.")
        else:
            solara.Select("Change", value=sel_label, values=labels)
    else:
        with solara.Row():
            solara.InputText("Before date", value=man_before)
            solara.InputText("After date", value=man_after)
    with solara.Row():
        solara.SliderInt("Max cloud %", value=cloud_pct, min=0, max=60)
        solara.InputInt("Search ± days", value=win_days)
    solara.Button("Get photos", on_click=do_fetch, color="primary", disabled=running.value)
    if running.value:
        solara.ProgressLinear(True)
        if status.value:
            solara.Markdown(f"`{status.value[-1]}`")
    if error.value:
        solara.Error(error.value)

    if result.value:
        r = result.value
        SectionHeader("Before / after photos", result_open)
        if result_open.value:
            # Remount the maps per fetch so Leaflet sizes correctly; key on the first
            # candidate dates + resolution.
            def _first(side):
                cs = r[side].get("candidates", [])
                return cs[0]["date"] if cs else "na"
            key = f"opt-{_first('before')}-{_first('after')}-{r['resolution']}"
            SyncedOpticalMaps(r["before"], r["after"], r.get("bounds"),
                              before_idx, after_idx).key(key)
            with solara.Row():
                for side, idx in (("before", before_idx), ("after", after_idx)):
                    cands = r[side].get("candidates", [])
                    if cands:
                        c = cands[min(idx.value, len(cands) - 1)]
                        pct = int(round(c["cloud"] * 100))
                        solara.FileDownload(
                            (lambda p=c["tif_path"]: open(p, "rb").read()),
                            filename=(f"{cube_name}_optical_{side}_"
                                      f"{c['date']}_cloud{pct}pct.tif"),
                            label=f"Download {side} GeoTIFF ({c['date']})",
                        )
            solara.Markdown(f"_Imagery at {r['resolution']:.0f} m · pan/zoom synced · "
                            "◀/▶ to change scene._")


_TH = "cursor:pointer;user-select:none;white-space:nowrap"


@solara.component
def SortHeader(label, key, sort):
    """Clickable column header: first click sorts descending, the next flips it."""
    col, desc = sort.value
    arrow = (" ▼" if desc else " ▲") if col == key else ""
    th = solara.v.Html(tag="th", children=[label + arrow], style_=_TH)
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


@solara.component
def CandidateRow(c, names, on_pick, selected):
    """One table row; clicking anywhere on it selects the site and flies there."""
    bg = "background:rgba(255,152,0,0.22);" if selected else ""   # reads in both themes
    when = [_day(c["date_after"])]
    if c.get("recent"):
        when += [solara.v.Html(tag="br"), solara.v.Html(
            tag="span", children=["too recent to confirm"],
            style_="font-size:0.8em;opacity:0.7;white-space:nowrap")]
    cells = [
        solara.v.Html(tag="td", children=[_place_text(c, names)]),
        solara.v.Html(tag="td", children=when, style_="white-space:nowrap"),
    ]
    if "area_ha" in c:
        cells.append(solara.v.Html(tag="td", children=[_ha(c["area_ha"])],
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
    tr = solara.v.Html(tag="tr", style_=f"cursor:pointer;{bg}", children=cells)
    solara.v.use_event(tr, "click", lambda *_: on_pick(c))
    return tr


@solara.component
def CandidateTable(candidates, on_pick, selected_rank, more, cache_dir, by_area,
                   min_area_ha=0.0):
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
            f"_No areas of at least {_ha(min_area_ha)} changed together in this period. "
            "Try a smaller minimum area or a lower minimum change._" if by_area else
            "_No changes above the minimum in this period. "
            "Try a lower minimum change or a longer period._")
        return
    col, desc = sort.value
    keyf = {
        "area": lambda c: (c.get("area_ha", 0.0), c["drop_db"]),
        "date": lambda c: c["date_after"] or "",
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
                    SortHeader("Area", "area", sort)
                SortHeader("Drop (dB)", "drop", sort)
        with solara.v.Html(tag="tbody"):
            for c in shown:
                CandidateRow(c, names.value, on_pick,
                             selected_rank == c["rank"]).key(f"cand-{c['rank']}")
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
    hot = solara.use_memo(
        lambda: (logic.hotspot_with_local(cube, pol=pol, start=start, end=end,
                                          direction=orbit_dir, lcz_classes=lcz_classes)
                 if cube is not None else None),
        hot_deps,
    )
    overlay = solara.use_memo(
        lambda: (_overlay(cube, hot, pol, start, end, layer, lcz_classes, min_change.value)
                 if cube is not None else (None, [[0.0, 0.0], [0.0, 0.0]], None)),
        hot_deps + [layer, min_change.value],
    )
    url, bounds, legend = overlay
    def _rank():
        if hot is None:
            return [], None
        if by_pixel.value:
            found, labels = logic.flag_recent(
                cube, logic.top_candidates(cube, *hot, n=30, min_value=min_change.value),
                end=end, direction=orbit_dir), None
        else:
            found, labels = logic.find_clusters(cube, *hot, min_value=min_change.value,
                                                min_area_ha=min_area.value,
                                                end=end, direction=orbit_dir)
        return [dict(c, rank=i) for i, c in enumerate(found, 1)], labels
    areas, area_labels = solara.use_memo(
        _rank, hot_deps + [min_change.value, min_area.value, by_pixel.value])
    candidates = areas[:30]

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

    params = (
        dict(sensitivity=sensitivity.value, min_drop_db=min_change.value, direction=drop_dir)
        if method == "pelt"
        else dict(window=window.value, min_drop_db=min_change.value, direction=drop_dir)
    )

    series, results, rows = [], [], []
    if area is not None:
        series = logic.select_area_series(cube, area["cells"], pol, orbit_dir)
    elif cell is not None:
        series = logic.select_series(cube, cell[0], cell[1], pol, orbit_dir,
                                     window=nbhd_window)
    if cell is not None:
        results = logic.run_detection(series, start=start, end=end, method=method, **params)
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
        solara.Markdown("**Area and period**")
        solara.Select("Area", value=[k for k, v in cube_labels.items() if v == active][0],
                      values=list(cube_labels),
                      on_value=lambda lbl: (cube_key.set(cube_labels[lbl]), clicked.set(None)))
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
                    focus=focus.value, outline=outline)
            layout.drag_handle("--s1-map-h", target=".s1-map")
            OpticalPanel(cube=cube, cube_key=active, cell=cell, results=results,
                         cube_name=cube_name, cache_dir=cache_dir,
                         default_start=start, default_end=end,
                         footprint=optical_footprint,
                         min_change=min_change.value).key("optical-panel")
        layout.drag_handle("--s1-mapcol")
        with solara.Column(classes=["s1-right"]):
            # Every direct child is keyed so collapsing a section (which changes the
            # children present) doesn't shift positions and remount its siblings.
            # Without this, toggling a collapse remounts the plot and table. Bodies are
            # wrapped in keyed columns so reconciliation is fully key-based.
            if cell is None:
                with solara.Row(style={"align-items": "center"}, gap="4px").key("intro"):
                    solara.Text("Pick a place from the list or click the map.")
                    InfoTip(
                        "**How to read this map:** coloured cells are places where the "
                        "radar signal dropped sharply and stayed low in the selected "
                        "period, the signature of buildings being removed. Darker red "
                        "means a bigger drop. Pick a place to see its history and compare "
                        "satellite photos. A drop is a lead to check, not proof of an "
                        "eviction.")
            if click_note:
                solara.Warning(click_note).key("click-note")
            SectionHeader(f"Strongest changes ({(start or '')[:4]}–{(end or '')[:4]})",
                          cand_open, info=_candidates_info(not by_pixel.value)
                          ).key("hdr-cand")
            if cand_open.value:
                with solara.Column(gap="0px").key("body-cand"):
                    CandidateTable(candidates[:30 if cand_more.value else 10],
                                   pick_candidate, selected_rank, cand_more, cache_dir,
                                   by_area=not by_pixel.value, min_area_ha=min_area.value)
            if cell is not None:
                lat_c, lon_c = highlight
                gmaps = f"https://www.google.com/maps/search/?api=1&query={lat_c:.6f},{lon_c:.6f}"
                place = places.cached(cache_dir, lat_c, lon_c)
                what = (f"**Selected area** ({_ha(area['area_ha'])}, {area['n_cells']} "
                        f"pixels, outlined on the map)" if area else "**Selected location**")
                solara.Markdown(
                    f"{what}: " + (f"{place} · " if place else "")
                    + f"{lat_c:.5f}, {lon_c:.5f}"
                    + (f" · {cell_lcz.split(' ', 1)[1]}" if cell_lcz else "")
                    + f" · [open in Google Maps]({gmaps})"
                ).key("sel-loc")
                if area and area["recent"]:
                    solara.Warning(
                        f"Too recent to confirm: only {area['passes_after']} image"
                        f"{'s' if area['passes_after'] != 1 else ''} per satellite track "
                        "after this drop. Check again later, or compare satellite photos."
                    ).key("recent-note")
                picked = area or next(
                    (c for c in candidates if c["rank"] == selected_rank), None)
                if per_track.value:
                    chart_info = (
                        "Backscatter in dB, one line per satellite track. Shaded bands "
                        "mark each change; dotted lines the signal level before and after.")
                else:
                    chart_info = _chart_info(picked, area is not None)
                SectionHeader("Radar signal over time", plot_open,
                              info=chart_info).key("hdr-ts")
                if plot_open.value:
                    with solara.Column().key("body-ts"):
                        # Remount the plot when its content changes: Solara's FigurePlotly
                        # otherwise leaves the previous cell's drop-span shapes on the layout.
                        plot_key = (f"plot-{cell[0]}-{cell[1]}-{area and area['rank']}-"
                                    f"{pol}-{orbit_dir}-{start}-{end}-"
                                    f"{method}-{sensitivity.value}-{min_change.value}-"
                                    f"{window.value}-{drop_dir}-{dark}-{per_track.value}")
                        if per_track.value:
                            fig = plots.figure(results, dark=dark)
                        else:
                            fig = plots.simple_figure(
                                findings.relative_signal(results, events), events,
                                min_change=min_change.value, dark=dark,
                                table_drop=_table_drop(picked))
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
