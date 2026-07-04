"""Tab 1 — Explore (public).

Pick a cube, see a basemap/hotspot image overlay on the map, click a cell to get
its per-(pol x orbit) dB series, filter the date range, and run drop detection on
the filtered+padded view with a live plot, table, and CSV export.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

import ipyleaflet
import numpy as np
import pandas as pd
import solara
from ipywidgets import Layout
from pyproj import Transformer

from ..analysis.series import cell_index
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


@solara.component
def CubeMap(overlay_url, bounds, on_click, highlight: Optional[Tuple[float, float]], height: str = "80vh"):
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
        CubeMap(url, bounds, clicked.set, highlight, height="82vh")
        with solara.Column():
            if cell is None:
                msg = "Click a cell on the map to plot its backscatter timeseries."
                if has_lcz:
                    msg += " Only cells in the selected LCZ classes are clickable."
                solara.Info(msg)
            else:
                title = f"cell ({cell[0]}, {cell[1]})" + (f" · LCZ {cell_lcz}" if cell_lcz else "")
                # Remount the plot when its content changes: Solara's FigurePlotly
                # otherwise leaves the previous cell's drop-span shapes on the layout.
                plot_key = (f"plot-{cell[0]}-{cell[1]}-{pol}-{orbit_dir.value}-{start}-{end}-"
                            f"{method.value}-{sensitivity.value}-{min_drop.value}-"
                            f"{window.value}-{drop_dir.value}")
                solara.FigurePlotly(plots.figure(results, title=title)).key(plot_key)
                n_drops = sum(len(r.drops) for r in results)
                solara.Markdown(f"**{n_drops} drop(s)** across {len(series)} series in this window.")
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
    return render.overlay_from_array(disp, cube, cmap="cool", vmin=0.0, vmax=vmax)
