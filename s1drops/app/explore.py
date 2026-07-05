"""Tab 1 — Explore (public).

Pick a cube, see a basemap/hotspot image overlay on the map, click a cell to get
its per-(pol x orbit) dB series, filter the date range, and run drop detection on
the filtered+padded view with a live plot, table, and CSV export.
"""
from __future__ import annotations

import math
import re
import threading
from typing import Optional, Tuple

import ipyleaflet
import numpy as np
import pandas as pd
import solara
from ipywidgets import Layout, jslink
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
from ..cube.lcz import DEFAULT_CLASSES, LCZ_NAMES
from . import logic, maputil, plots, render, state

POL_LABELS = {"VV": "vv", "VH": "vh", "VV/VH": "vv_vh"}
ORBIT_DIRS = ["both", "ascending", "descending"]
DROP_DIRS = ["drop", "rise", "both"]
OVERLAYS = ["hotspot", "basemap"]

LCZ_OPTIONS = [f"{c} {LCZ_NAMES[c]}" for c in sorted(LCZ_NAMES)]
LCZ_DEFAULT_SEL = [f"{c} {LCZ_NAMES[c]}" for c in DEFAULT_CLASSES]


def _classes_from_labels(labels):
    return [int(s.split()[0]) for s in labels]


def _slug(s: str) -> str:
    """Filesystem-safe token for download filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s)).strip("-") or "cube"


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
            height: str = "80vh", footprint=None):
    map_el = ipyleaflet.Map.element(
        scroll_wheel_zoom=True, layout=Layout(height=height, width="100%"),
    )

    def setup_base():
        m = solara.get_widget(map_el)
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
def SectionHeader(title, opened):
    """Clickable collapse/expand header bound to a boolean reactive."""
    with solara.Row(style={"align-items": "center"}):
        solara.Button(
            icon=True,
            icon_name="mdi-chevron-down" if opened.value else "mdi-chevron-right",
            on_click=lambda: opened.set(not opened.value),
        )
        solara.Text(title, style={"font-weight": "600"})


@solara.component
def SyncedOpticalMaps(before, after, bounds, before_idx, after_idx):
    """Two responsive-square before/after mini-maps with pan/zoom synced via jslink.

    Each side offers up to N candidate scenes cycled with ◀/▶; flipping swaps only the
    ImageOverlay on the same geobox, so framing (and your zoom/pan) never changes.
    Images sit over a muted gray basemap with the fetch-footprint rectangle.
    """
    square = {"width": "100%", "aspect-ratio": "1 / 1"}
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
            with solara.Column(style=square):
                m1 = ipyleaflet.Map.element(scroll_wheel_zoom=True,
                                            layout=Layout(height="100%", width="100%"))
            controls("before", before, before_idx)
        with solara.Column():
            with solara.Column(style=square):
                m2 = ipyleaflet.Map.element(scroll_wheel_zoom=True,
                                            layout=Layout(height="100%", width="100%"))
            controls("after", after, after_idx)

    def setup():
        w1, w2 = solara.get_widget(m1), solara.get_widget(m2)
        for w in (w1, w2):
            w.add(ipyleaflet.basemap_to_tiles(ipyleaflet.basemaps.CartoDB.Positron))
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


@solara.component
def OpticalPanel(cube, cube_key, cell, results, cube_name, cache_dir,
                 default_start, default_end, footprint):
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

    opts = logic.drop_options(results) if results else []
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
            error.set("Click a cell on the map to place the imagery box.")
            return
        if mode.value == "cell":
            if not opts:
                error.set("This cell has no detected drop — switch to manual dates.")
                return
            o = opts[labels.index(sel_label.value)] if sel_label.value in labels else opts[0]
            before_a = np.datetime_as_string(o["date_before"], unit="D")
            after_a = np.datetime_as_string(o["date_after"], unit="D")
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

    solara.Markdown("---\n**Optical before/after (Sentinel-2)**")
    solara.ToggleButtonsSingle(value=mode, values=["cell", "manual"])
    if mode.value == "cell":
        if not opts:
            solara.Info("Click a cell with a detected drop to anchor the imagery.")
        else:
            solara.Select("Drop", value=sel_label, values=labels)
    else:
        with solara.Row():
            solara.InputText("Before date", value=man_before)
            solara.InputText("After date", value=man_after)
        solara.Markdown("_Uses the clicked cell's box (no drop needed) — click a cell, "
                        "then set dates._")
    with solara.Row():
        solara.SliderInt("Max cloud %", value=cloud_pct, min=0, max=60)
        solara.InputInt("Window (days)", value=win_days)
    solara.Button("Fetch optical", on_click=do_fetch, color="primary", disabled=running.value)
    if running.value:
        solara.ProgressLinear(True)
        if status.value:
            solara.Markdown(f"`{status.value[-1]}`")
    if error.value:
        solara.Error(error.value)

    if result.value:
        r = result.value
        SectionHeader("Before / after imagery", result_open)
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
                            filename=(f"{_slug(cube_name)}_optical_{side}_"
                                      f"{c['date']}_cloud{pct}pct.tif"),
                            label=f"Download {side} GeoTIFF ({c['date']})",
                        )
            solara.Markdown(f"_Imagery at {r['resolution']:.0f} m · pan/zoom synced · "
                            "◀/▶ to change scene._")


@solara.component
def Explore(cache_dir: str = "./cache"):
    # --- all hooks first, unconditionally (rules of hooks) ---
    cubes = solara.use_memo(
        lambda: list_cubes(cache_dir), [cache_dir, state.registry_version.value]
    )
    keys = [c.key for c in cubes]

    cube_key = solara.use_reactive("")
    pol_label = solara.use_reactive("VV")
    orbit_dir = solara.use_reactive("both")
    overlay_kind = solara.use_reactive("hotspot")
    method = solara.use_reactive("pelt")
    sensitivity = solara.use_reactive(1.0)
    min_drop = solara.use_reactive(2.5)
    window = solara.use_reactive(2)
    drop_dir = solara.use_reactive("drop")
    clicked = solara.use_reactive(None)
    date_idx = solara.use_reactive((0, 0))
    lcz_sel = solara.use_reactive(LCZ_DEFAULT_SEL)
    hotspot_min = solara.use_reactive(0.0)          # applied (drives redraw)
    hotspot_min_pending = solara.use_reactive(0.0)  # live slider value
    neighborhood = solara.use_reactive(True)        # 3x3 neighborhood detection (10 m only)
    optical_footprint = solara.use_reactive(None)    # optical AOI box drawn on the main map
    plot_open = solara.use_reactive(True)            # right-panel collapse states
    drops_open = solara.use_reactive(True)

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
    pol = POL_LABELS[pol_label.value]
    lcz_classes = _classes_from_labels(lcz_sel.value)

    overlay = solara.use_memo(
        lambda: (_overlay(cube, pol, start, end, orbit_dir.value, overlay_kind.value,
                          lcz_classes, hotspot_min.value)
                 if cube is not None else (None, [[0.0, 0.0], [0.0, 0.0]])),
        [active, pol, start, end, orbit_dir.value, overlay_kind.value,
         tuple(sorted(lcz_classes)), hotspot_min.value, state.registry_version.value],
    )
    url, bounds = overlay

    # --- guards (after all hooks) ---
    if not cubes:
        solara.Warning("No cubes found. Build one in the Admin tab.")
        return
    if cube is None:
        solara.Info("Loading cube…")
        return

    has_lcz = logic.has_lcz(cube)
    is_native10 = float(cube.attrs.get("resolution", 100.0)) <= 50  # neighborhood only here
    nbhd_window = 1 if (is_native10 and neighborhood.value) else 0   # 1 => 3x3 median

    # resolve click -> cell -> series + detection (hard-restricted to allowed LCZ)
    cell = None
    highlight = None
    cell_lcz = ""
    if clicked.value is not None:
        idx = cell_index(cube, clicked.value[1], clicked.value[0])
        if idx is not None and logic.cell_allowed(cube, idx[0], idx[1], lcz_classes):
            cell = idx
            cell_lcz = logic.lcz_label(logic.lcz_at(cube, idx[0], idx[1]))
            tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
            lon_c, lat_c = tx.transform(cube["x"].values[idx[1]], cube["y"].values[idx[0]])
            highlight = (lat_c, lon_c)

    params = (
        dict(sensitivity=sensitivity.value, min_drop_db=min_drop.value, direction=drop_dir.value)
        if method.value == "pelt"
        else dict(window=window.value, min_drop_db=min_drop.value, direction=drop_dir.value)
    )

    series, results, rows = [], [], []
    if cell is not None:
        series = logic.select_series(cube, cell[0], cell[1], pol, orbit_dir.value,
                                     window=nbhd_window)
        results = logic.run_detection(series, start=start, end=end, method=method.value, **params)
        rows = logic.drop_rows(results)

    layer_options = OVERLAYS + (["lcz"] if has_lcz else [])
    if overlay_kind.value not in layer_options:
        overlay_kind.set(layer_options[0])

    with solara.Sidebar():
        solara.Select("Cube", value=cube_key, values=keys)
        solara.Select("Polarisation", value=pol_label, values=list(POL_LABELS))
        solara.Select("Orbit direction", value=orbit_dir, values=ORBIT_DIRS)
        solara.Select("Map layer", value=overlay_kind, values=layer_options)
        if overlay_kind.value == "hotspot":
            solara.SliderFloat("Hotspot min (dB)", value=hotspot_min_pending,
                               min=0.0, max=12.0, step=0.5)
            with solara.Row():
                solara.Button(
                    "Apply filter", color="primary",
                    on_click=lambda: hotspot_min.set(hotspot_min_pending.value),
                    disabled=hotspot_min_pending.value == hotspot_min.value,
                )
                solara.FileDownload(
                    lambda: logic.hotspot_geojson(
                        cube, pol=pol, start=start, end=end, direction=orbit_dir.value,
                        lcz_classes=lcz_classes, min_value=hotspot_min.value),
                    filename=(f"{cube_name}_hotspot_min{hotspot_min.value:g}dB_"
                              f"{_slug(pol)}_{start}_{end}.geojson"),
                    label="Hotspot GeoJSON",
                )
            applied = f"≥ {hotspot_min.value:.1f} dB" if hotspot_min.value > 0 else "all cells"
            solara.Markdown(f"_Showing {applied}. GeoJSON exports the shown cells._")
        solara.SliderRangeInt("Date range", value=date_idx, min=0, max=max(0, n - 1))
        solara.Markdown(f"**{start} → {end}**  ({hi - lo + 1} of {n} dates)")
        if has_lcz:
            solara.Markdown("---\n**LCZ filter** (allowed classes)")
            solara.SelectMultiple("Classes", values=lcz_sel, all_values=LCZ_OPTIONS)
            solara.Markdown(
                f"_{len(lcz_classes)} class(es). Cells outside are hidden and not clickable._"
            )
        solara.Markdown("---\n**Detection**")
        if is_native10:
            solara.Checkbox(label="Detect on 3×3 neighborhood (10 m speckle)",
                            value=neighborhood)
        solara.Select("Method", value=method, values=["pelt", "threshold"])
        if method.value == "pelt":
            solara.SliderFloat("Sensitivity", value=sensitivity, min=0.2, max=5.0, step=0.1)
        else:
            solara.SliderInt("Window", value=window, min=1, max=5)
        solara.SliderFloat("Min drop (dB)", value=min_drop, min=0.5, max=8.0, step=0.5)
        solara.Select("Direction", value=drop_dir, values=DROP_DIRS)

    with solara.Columns([3, 2]):
        CubeMap(url, bounds, clicked.set, highlight, height="82vh",
                footprint=optical_footprint.value)
        with solara.Column():
            # Every direct child is keyed so collapsing a section (which changes the
            # children present) doesn't shift positions and remount its siblings.
            # Without this, toggling a collapse remounts OpticalPanel and wipes the
            # fetched imagery. Bodies are wrapped in keyed columns so reconciliation
            # is fully key-based.
            if cell is None:
                msg = "Click a cell on the map to plot its backscatter timeseries."
                if has_lcz:
                    msg += " Only cells in the selected LCZ classes are clickable."
                solara.Info(msg).key("no-cell-info")
            else:
                title = f"cell ({cell[0]}, {cell[1]})" + (f" · LCZ {cell_lcz}" if cell_lcz else "")
                SectionHeader("Timeseries", plot_open).key("hdr-ts")
                if plot_open.value:
                    with solara.Column().key("body-ts"):
                        # Remount the plot when its content changes: Solara's FigurePlotly
                        # otherwise leaves the previous cell's drop-span shapes on the layout.
                        plot_key = (f"plot-{cell[0]}-{cell[1]}-{pol}-{orbit_dir.value}-{start}-{end}-"
                                    f"{method.value}-{sensitivity.value}-{min_drop.value}-"
                                    f"{window.value}-{drop_dir.value}")
                        solara.FigurePlotly(plots.figure(results, title=title)).key(plot_key)
                        n_drops = sum(len(r.drops) for r in results)
                        solara.Markdown(f"**{n_drops} drop(s)** across {len(series)} series "
                                        "in this window.")
                SectionHeader("Detected drops", drops_open).key("hdr-drops")
                if drops_open.value:
                    with solara.Column().key("body-drops"):
                        if rows:
                            solara.DataFrame(pd.DataFrame(rows))
                        with solara.Row():
                            solara.FileDownload(
                                logic.drops_csv(results, cell=cell),
                                filename=(f"{cube_name}_r{cell[0]}c{cell[1]}_{_slug(pol)}_"
                                          f"drop{min_drop.value:g}dB_{start}_{end}.csv"),
                                label="Download drops CSV",
                            )
                            solara.FileDownload(
                                logic.series_csv(series, start=start, end=end, cell=cell),
                                filename=(f"{cube_name}_r{cell[0]}c{cell[1]}_{_slug(pol)}_"
                                          f"{start}_{end}_series.csv"),
                                label="Download series CSV",
                            )
            OpticalPanel(cube=cube, cube_key=active, cell=cell, results=results,
                         cube_name=cube_name, cache_dir=cache_dir,
                         default_start=start, default_end=end,
                         footprint=optical_footprint).key("optical-panel")


def _path(cubes, key):
    return next(c.path for c in cubes if c.key == key)


def _overlay(cube, pol, start, end, orbit_dir, kind, lcz_classes, hotspot_min=0.0):
    if kind == "lcz":
        return render.overlay_lcz(logic.lcz_layer(cube), cube)
    if kind == "basemap":
        layer = logic.basemap_layer(cube, pol=pol, start=start, end=end, lcz_classes=lcz_classes)
        return render.overlay_from_array(layer, cube, cmap="gray")
    layer = logic.hotspot_layer(
        cube, pol=pol, start=start, end=end, direction=orbit_dir, lcz_classes=lcz_classes
    )
    # vmax from the full layer so colours stay fixed as the threshold moves; the
    # threshold only hides cells (NaN -> transparent), it doesn't rescale colour.
    vmax = float(np.nanpercentile(layer, 98)) if np.isfinite(layer).any() else 1.0
    disp = layer if hotspot_min <= 0 else np.where(layer >= hotspot_min, layer, np.nan)
    return render.overlay_from_array(disp, cube, cmap="RdBu_r", vmin=0.0, vmax=vmax)
