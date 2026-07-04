"""Tab 2 — Build / Admin (gated, server-enforced).

Login with the admin password (env S1DROPS_ADMIN_PASSWORD). Privileged actions
call auth.guard_admin() before any work, so the gate is enforced in the handler,
not by hiding the UI. Bakes run on a background thread with a live progress log.
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

import ipyleaflet
import solara
from ipywidgets import Layout

from ..cube import delete_cube, list_cubes
from ..cube.geobox import aoi_to_geobox
from ..cube.lcz import lcz_status
from ..config import est_passes_per_year, native10_max_km2
from . import auth, bakequeue, maputil, state

REDUCTIONS = ["native_median", "overview_med"]
AOI_MODES = ["registry", "bbox", "upload", "draw"]

# label -> (resolution_m, reduction). Presets keep the 10 m + reduction combo sane.
BAKE_PRESETS = {
    "100 m · native median": (100.0, "native_median"),
    "100 m · overview (fast)": (100.0, "overview_med"),
    "10 m · native (small AOI)": (10.0, "native_median"),
}


def _preset_for(resolution, reduction) -> str:
    for label, (r, red) in BAKE_PRESETS.items():
        if round(r) == round(resolution or 100) and red == (reduction or "native_median"):
            return label
    return "100 m · native median"


def _days_between(a: str, b: str) -> int:
    from datetime import date
    try:
        return max((date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days, 0)
    except ValueError:
        return 0


def _valid_lonlat(bbox) -> Optional[str]:
    minx, miny, maxx, maxy = bbox
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        return ("Coordinates must be WGS84 lon/lat (EPSG:4326), lon in [-180,180], "
                "lat in [-90,90], min < max. Projected/UTM input is not accepted.")
    return None


def _geom_from_geojson(text: str):
    from shapely.geometry import shape
    gj = json.loads(text)
    if gj.get("type") == "FeatureCollection":
        geom = shape(gj["features"][0]["geometry"])
    elif gj.get("type") == "Feature":
        geom = shape(gj["geometry"])
    else:
        geom = shape(gj)
    return geom


@solara.component
def Login():
    pw = solara.use_reactive("")
    msg = solara.use_reactive("")

    def submit():
        if auth.check_password(pw.value):
            state.is_admin.set(True)
            msg.set("")
        else:
            msg.set("Incorrect password.")

    if not auth.admin_password_configured():
        solara.Error("Admin is disabled: set the S1DROPS_ADMIN_PASSWORD env var to enable it.")
        return
    solara.Markdown("### Admin login")
    solara.InputText("Password", value=pw, password=True)
    solara.Button("Sign in", on_click=submit, color="primary")
    if msg.value:
        solara.Error(msg.value)


@solara.component
def DrawAOI(on_geom):
    map_el = ipyleaflet.Map.element(layout=Layout(height="560px", width="100%"))

    def setup():
        m = solara.get_widget(map_el)
        m.center = (6.5, 3.4)   # set once; not a live prop, so draws won't reset it
        m.zoom = 9
        dc = ipyleaflet.DrawControl(
            rectangle={"shapeOptions": {}}, polygon={"shapeOptions": {}},
            circle={}, circlemarker={}, polyline={},
        )

        def handle(self, action, geo_json):
            if action in ("created", "edited"):
                on_geom(json.dumps(geo_json["geometry"]))

        dc.on_draw(handle)
        m.add(dc)
        maputil.add_search(m)
    solara.use_effect(setup, [])
    return map_el


@solara.component
def BuildForm(cache_dir: str):
    name = solara.use_reactive("")
    mode = solara.use_reactive("bbox")
    minx = solara.use_reactive(3.10)
    miny = solara.use_reactive(6.35)
    maxx = solara.use_reactive(3.70)
    maxy = solara.use_reactive(6.75)
    start = solara.use_reactive("2024-01-01")
    end = solara.use_reactive("2025-01-01")
    bake_type = solara.use_reactive("100 m · native median")
    geojson_text = solara.use_reactive("")  # from upload or draw
    chosen = solara.use_reactive("")         # existing-cube key (registry mode)

    cubes = solara.use_memo(
        lambda: list_cubes(cache_dir), [cache_dir, state.registry_version.value]
    )

    result = solara.use_reactive("")  # immediate enqueue feedback (per-job state is in the queue)

    def _prefill_from_chosen():
        if mode.value != "registry" or not chosen.value:
            return
        e = next((c for c in cubes if c.key == chosen.value), None)
        if e and len(e.bbox_ll) == 4:
            name.set(e.name)
            start.set(e.start)
            end.set(e.end)
            bake_type.set(_preset_for(e.resolution, e.reduction))
            mnx, mny, mxx, mxy = e.bbox_ll
            minx.set(mnx); miny.set(mny); maxx.set(mxx); maxy.set(mxy)
    solara.use_effect(_prefill_from_chosen, [chosen.value, mode.value])

    def resolve_aoi() -> Tuple[Optional[tuple], object, Optional[str]]:
        if mode.value == "registry":
            e = next((c for c in cubes if c.key == chosen.value), None)
            if e is None:
                return None, None, "Pick an existing AOI from the registry."
            return tuple(e.bbox_ll), None, _valid_lonlat(tuple(e.bbox_ll))
        if mode.value == "bbox":
            bbox = (minx.value, miny.value, maxx.value, maxy.value)
            return bbox, None, _valid_lonlat(bbox)
        if not geojson_text.value:
            return None, None, "No geometry provided (upload a GeoJSON or draw one)."
        try:
            geom = _geom_from_geojson(geojson_text.value)
        except Exception as e:
            return None, None, f"Could not parse GeoJSON: {e}"
        bbox = tuple(geom.bounds)
        return bbox, geom, _valid_lonlat(bbox)

    def do_enqueue():
        try:
            auth.guard_admin(state.is_admin.value)  # gate is enforced at enqueue time
        except auth.NotAuthorized as e:
            result.set(str(e))
            return
        bbox, geom, err = resolve_aoi()
        if err:
            result.set(err)
            return
        res, red = BAKE_PRESETS[bake_type.value]
        gbox, _ = aoi_to_geobox(bbox, res)
        area_km2 = int(gbox.shape.y) * int(gbox.shape.x) * res * res / 1e6
        if res <= 50 and area_km2 > native10_max_km2():
            result.set(
                f"Error: AOI {area_km2:.1f} km² exceeds the {native10_max_km2():.0f} km² "
                "cap for 10 m bakes. Shrink the AOI or pick a 100 m preset."
            )
            return
        label = f"{name.value or 'cube'} · {int(round(res))} m · {start.value}..{end.value}"
        bakequeue.enqueue(
            label=label, bbox=bbox, geom=geom, start=start.value, end=end.value,
            name=name.value or None, reduction=red, resolution=res, cache_dir=cache_dir,
        )
        result.set(f"Queued: {label}")

    solara.Markdown("### Build / extend a cube")
    _lcz_state, _lcz_p = lcz_status()
    if _lcz_state == "enabled":
        solara.Success(f"LCZ enabled — new bakes include the LCZ layer.  ({_lcz_p})")
    elif _lcz_state == "missing":
        solara.Error(f"S1DROPS_LCZ_PATH is set but the file is not found: {_lcz_p}")
    else:
        solara.Info("LCZ not configured — cubes will bake without LCZ filtering. "
                    "Set S1DROPS_LCZ_PATH and restart to enable it.")
    solara.InputText("Name (e.g. lagos)", value=name)
    solara.Select("AOI source", value=mode, values=AOI_MODES)
    if mode.value == "registry":
        if not cubes:
            solara.Info("No existing cubes yet — use bbox, upload, or draw.")
        else:
            solara.Select(
                "Existing AOI", value=chosen, values=[c.key for c in cubes]
            )
            solara.Markdown(
                "_Reuses this cube's footprint. Widen the date range below to "
                "extend it; the original AOI mask is preserved._"
            )
    elif mode.value == "bbox":
        with solara.Row():
            solara.InputFloat("min lon", value=minx)
            solara.InputFloat("min lat", value=miny)
            solara.InputFloat("max lon", value=maxx)
            solara.InputFloat("max lat", value=maxy)
    elif mode.value == "upload":
        def on_file(f):
            geojson_text.set(f["data"].decode("utf-8"))
        solara.FileDrop(label="Drop a GeoJSON (WGS84 lon/lat)", on_file=on_file, lazy=False)
        if geojson_text.value:
            solara.Success("Geometry loaded.")
    else:
        DrawAOI(geojson_text.set)
        if geojson_text.value:
            solara.Success("Geometry captured from the map.")

    with solara.Row():
        solara.InputText("Start (YYYY-MM-DD)", value=start)
        solara.InputText("End (YYYY-MM-DD)", value=end)
    solara.Select("Bake type", value=bake_type, values=list(BAKE_PRESETS))
    _res, _red = BAKE_PRESETS[bake_type.value]
    _bbox_rd, _geom_rd, _err_rd = resolve_aoi()
    if not _err_rd and _bbox_rd is not None:
        _g, _ = aoi_to_geobox(_bbox_rd, _res)
        _ny, _nx = int(_g.shape.y), int(_g.shape.x)
        _cells = _ny * _nx
        _area = _cells * _res * _res / 1e6
        _yrs = max(_days_between(start.value, end.value), 1) / 365.0
        _passes = est_passes_per_year() * _yrs
        _est_gb = _cells * _passes * 2 * 4 * 1.3 / 1e9
        solara.Markdown(
            f"_AOI ≈ **{_area:.2f} km²** · grid {_ny}×{_nx} = {_cells:,} cells · "
            f"est. size ~{_est_gb:.2f} GB (≈{_passes:.0f} passes)_"
        )
        if _res <= 50 and _area > native10_max_km2():
            solara.Error(
                f"AOI {_area:.1f} km² exceeds the {native10_max_km2():.0f} km² cap for "
                "10 m bakes — shrink the AOI or pick a 100 m preset before queuing."
            )
    solara.Button("Add to queue", on_click=do_enqueue, color="primary")
    if result.value:
        (solara.Success if result.value.startswith("Queued") else solara.Error)(result.value)
    BakeQueuePanel()


def _status_tag(status: str) -> str:
    return {"pending": "⏳ pending", "running": "⟳ running",
            "done": "✓ done", "failed": "✗ failed"}.get(status, status)


@solara.component
def BakeQueuePanel():
    jobs = bakequeue.queue.value
    solara.Markdown("### Bake queue")
    if not jobs:
        solara.Markdown("_Queue is empty._")
        return
    n_run = sum(j["status"] == "running" for j in jobs)
    n_pend = sum(j["status"] == "pending" for j in jobs)
    solara.Markdown(f"_{n_run} running · {n_pend} pending · {len(jobs)} total. "
                    "Jobs run one at a time._")
    for j in jobs:
        with solara.Card():
            with solara.Row(style={"align-items": "center", "justify-content": "space-between"}):
                solara.Markdown(f"**{_status_tag(j['status'])}** — {j['label']}")
                if j["status"] == "pending":
                    solara.Button("Remove", text=True,
                                  on_click=lambda jid=j["id"]: bakequeue.remove(jid))
            if j["status"] == "running":
                solara.ProgressLinear(True)
                if j["log"]:
                    solara.Markdown("```\n" + "\n".join(j["log"][-10:]) + "\n```")
            elif j["result"]:
                (solara.Success if j["status"] == "done" else solara.Error)(j["result"])
    if any(j["status"] in ("done", "failed") for j in jobs):
        solara.Button("Clear finished", text=True, on_click=bakequeue.clear_finished)


@solara.component
def ManageCubes(cache_dir: str):
    cubes = solara.use_memo(
        lambda: list_cubes(cache_dir), [cache_dir, state.registry_version.value]
    )
    confirm = solara.use_reactive("")  # key pending confirmation
    solara.Markdown("### Cubes")
    if not cubes:
        solara.Markdown("_(none)_")
        return
    for e in cubes:
        with solara.Row(style={"align-items": "center"}):
            solara.Markdown(
                f"**{e.name}** · `{e.key}` · {e.start}..{e.end} · "
                f"{e.n_passes} passes · {e.reduction}"
            )
            if confirm.value == e.key:
                def really_delete(key=e.key):
                    try:
                        auth.guard_admin(state.is_admin.value)
                        delete_cube(cache_dir, key)
                        state.registry_version.set(state.registry_version.value + 1)
                    finally:
                        confirm.set("")
                solara.Button("Confirm delete", on_click=really_delete, color="error")
                solara.Button("Cancel", on_click=lambda: confirm.set(""))
            else:
                solara.Button("Delete", on_click=lambda key=e.key: confirm.set(key), text=True)


@solara.component
def Admin(cache_dir: str = "./cache"):
    if not state.is_admin.value:
        Login()
        return
    with solara.Column():
        with solara.Row(style={"justify-content": "space-between"}):
            solara.Markdown("**Admin** — signed in")
            solara.Button("Sign out", on_click=lambda: state.is_admin.set(False), text=True)
        BuildForm(cache_dir)
        solara.Markdown("---")
        ManageCubes(cache_dir)
