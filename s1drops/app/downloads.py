"""One Download button for everything: tick what you want, get one file or a ZIP.

The page lists what can be downloaded as `Item`s, grouped by what they describe
(this area, or the selected change). Items are built only when downloaded, and
an item that can't be made right now (no change selected, no photos fetched)
shows greyed out with the reason.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import solara

from . import confidence, logic, places, validation

Data = Union[bytes, str]


@dataclass
class Item:
    key: str
    group: str              # heading it's listed under
    label: str
    filename: str
    make: Optional[Callable[[], Data]]      # None: not available now
    why_not: str = ""       # shown when unavailable
    default: bool = False   # ticked when the dialog opens


def bundle(items: Sequence[Item], zip_name: str) -> Tuple[str, bytes]:
    """(filename, bytes): the file itself for one item, else a ZIP of them all."""
    def raw(it):
        data = it.make()
        return data.encode("utf-8") if isinstance(data, str) else data
    if len(items) == 1:
        return items[0].filename, raw(items[0])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for it in items:
            z.writestr(it.filename, raw(it))
    return zip_name, buf.getvalue()


# ---- the area's changes, as a table and as outlines ------------------------------

def _rows(areas, *, min_change, track_drops, summaries, show_verdicts, cache_dir):
    for c in areas:
        tracks = (confidence.tracks_from_steps(track_drops, c, min_change)
                  if track_drops is not None else None)
        summary = summaries.get(c["rank"]) if summaries else None
        rating = confidence.rate(c, min_change, tracks, summary, show_verdicts)
        row = {
            "rank": c["rank"],
            "place": places.cached(cache_dir, c["lat"], c["lon"]) or "",
            "lat": round(c["lat"], 6), "lon": round(c["lon"], 6),
            "date_before": c.get("date_before"), "date_after": c.get("date_after"),
            "drop_db": c.get("drop_db"), "avg_drop_db": c.get("avg_drop_db"),
            "area_ha": c.get("area_ha"), "pixels": c.get("n_cells", 1),
            "images_after": c.get("passes_after"), "too_recent": c.get("recent"),
            "confidence": rating.label,
            "confidence_reasons": "; ".join(f"{f.name}: {f.text}" for f in rating.factors),
        }
        if show_verdicts:
            row["verdict"] = (summary.verdict if summary else "") or ""
            row["validators"] = summary.count if summary else 0
        yield c, row


def changes_csv(areas, **kw) -> str:
    rows = [row for _, row in _rows(areas, **kw)]
    if not rows:
        return ""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def changes_geojson(cube, areas, **kw) -> str:
    feats = []
    for c, row in _rows(areas, **kw):
        cells = c.get("cells") or [(c["row"], c["col"])]
        feats.append({"type": "Feature", "properties": row,
                      "geometry": logic.area_outline(cube, cells)["geometry"]})
    return json.dumps({"type": "FeatureCollection", "features": feats})


# ---- the button ------------------------------------------------------------------

@solara.component
def DownloadButton(items: List[Item], zip_name: str):
    """A Download button opening a list of tick boxes, grouped, with one Download
    at the bottom (one file, or a ZIP when several are ticked)."""
    open_ = solara.use_reactive(False)
    chosen = solara.use_reactive(frozenset())
    usable = {it.key for it in items if it.make is not None}

    def _defaults():
        if open_.value:
            chosen.set(frozenset(it.key for it in items if it.default and it.key in usable))
    solara.use_effect(_defaults, [open_.value])

    picked = [it for it in items if it.key in chosen.value and it.key in usable]
    name = picked[0].filename if len(picked) == 1 else zip_name

    def toggle(key, on):
        chosen.set(chosen.value | {key} if on else chosen.value - {key})

    btn = solara.v.Btn(text=True, small=True, v_on="dl.on",
                       style_="text-transform:none;letter-spacing:0",
                       children=[solara.v.Icon(small=True, left=True, children=["mdi-download"]),
                                 "Download"])
    groups: Dict[str, List[Item]] = {}
    for it in items:
        groups.setdefault(it.group, []).append(it)
    body = []
    for group, its in groups.items():
        body.append(solara.v.Html(tag="div", style_="font-weight:600;margin:8px 0 2px",
                                  children=[group]))
        for it in its:
            body.append(solara.v.Checkbox(
                label=it.label + (f" ({it.why_not})" if it.make is None and it.why_not else ""),
                v_model=it.key in chosen.value, disabled=it.make is None, dense=True,
                hide_details=True, class_="mt-0",
                on_v_model=lambda on, k=it.key: toggle(k, bool(on))))
    body.append(solara.v.Html(tag="div", style_="margin-top:12px", children=[
        solara.FileDownload(
            lambda: bundle(picked, zip_name)[1], filename=name,
            children=[solara.Button(
                "Download " + ("1 file" if len(picked) == 1 else f"{len(picked)} files (ZIP)")
                if picked else "Tick what you want", color="primary", disabled=not picked,
                small=True)]) if picked else
        solara.Button("Tick what you want", disabled=True, small=True)]))
    return solara.v.Menu(
        v_model=open_.value, on_v_model=open_.set, close_on_content_click=False,
        offset_y=True, left=True, z_index=2000, max_width=420,
        v_slots=[{"name": "activator", "variable": "dl", "children": [btn]}],
        children=[solara.v.Card(children=[solara.v.CardText(children=body)])],
    )
