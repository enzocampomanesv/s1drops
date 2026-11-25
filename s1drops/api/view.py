"""What the Explore tab computes, for the API.

The API keeps no session: every request names its view (area, period, radar
channel, track direction, land-use classes, minimum change and area, ranking by
area or pixel, a searched map area). The heavy parts reuse the Explore tab's own
functions and shared caches (s1drops/app/explore.py: the hotspot pass, the map
overlay, the ranking, per-place detection), so the page and the old app give
the same answers for the same settings.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from pyproj import Transformer

from ..analysis.series import cell_index
from ..app import confidence, findings, logic, places, validation
from ..app import explore as ex
from ..cube import list_cubes, read_cube
from ..cube.lcz import DEFAULT_CLASSES, LCZ_NAMES

POLS = ("vv", "vh", "vv_vh")
ORBITS = ("both", "ascending", "descending")
METHODS = ("pelt", "threshold")
DIRECTIONS = ("drop", "rise", "both")
LAYERS = ("hotspot", "basemap", "lcz")
LIST_MAX = 30


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class View:
    key: str
    pol: str = "vv"
    orbit: str = "both"
    start: Optional[str] = None      # None: from the first pass
    end: Optional[str] = None        # None: to the last pass
    min_change: float = ex.DEFAULT_MIN_CHANGE
    min_area: float = logic.DEFAULT_MIN_AREA_HA
    by_pixel: bool = False
    lcz: Tuple[int, ...] = tuple(DEFAULT_CLASSES)
    box: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None   # ((s, w), (n, e))


@dataclass
class Area:
    entry: object
    cube: object
    days: np.ndarray


_open: "OrderedDict[tuple, Area]" = OrderedDict()
_open_lock = threading.Lock()
OPEN_MAX = 3


def area_id(key: str) -> str:
    """An area's name without its dates ("Lagos_100m_nm"): unchanged by monthly updates."""
    from ..cube.refresh import area_id as _id

    return _id(key)


def open_area(cache_dir: Path, key: str) -> Area:
    """An area by its key or its stable id (what links use)."""
    cubes = list_cubes(cache_dir)
    entry = next((e for e in cubes if e.key == key), None) or next((e for e in cubes if area_id(e.key) == key), None)
    if entry is None:
        raise NotFound(f"No area {key!r}.")
    k = (entry.key, entry.created_at)
    with _open_lock:
        if k in _open:
            _open.move_to_end(k)
            return _open[k]
    cube = read_cube(entry.path)
    days = np.unique(np.asarray(cube["time"].values).astype("datetime64[D]"))
    area = Area(entry, cube, days)
    with _open_lock:
        _open[k] = area
        while len(_open) > OPEN_MAX:
            _open.popitem(last=False)
    return area


def period(a: Area, v: View) -> Tuple[Optional[str], Optional[str]]:
    """The view's dates snapped to passes the area has (the whole record if unset or empty)."""
    if not len(a.days):
        return None, None
    lo = np.datetime64(v.start, "D") if v.start else a.days[0]
    hi = np.datetime64(v.end, "D") if v.end else a.days[-1]
    inside = a.days[(a.days >= lo) & (a.days <= hi)]
    if not len(inside):
        inside = a.days
    return str(inside[0]), str(inside[-1])


def _hot_key(a: Area, v: View, start, end) -> tuple:
    return ex._hot_key(a.entry, v.pol, start, end, v.orbit, list(v.lcz))


def hotspot(a: Area, v: View):
    start, end = period(a, v)
    hot_all = ex.shared_hotspot(a.cube, a.entry, v.pol, start, end, v.orbit, list(v.lcz))
    return hot_all[:4], hot_all[4], start, end


def overlay(a: Area, v: View, layer: str) -> Dict:
    hot, _, start, end = hotspot(a, v)
    url, bounds, legend = ex._reuse(
        _hot_key(a, v, start, end) + ("overlay", layer, v.min_change),
        lambda: ex._overlay(a.cube, hot, v.pol, start, end, layer, list(v.lcz), v.min_change),
        store=ex._SHARED)
    return {"image": url, "bounds": bounds, "legend": legend}


def ranked(a: Area, v: View):
    """(changes, labels) as Strongest changes lists them: change areas (labels gives
    each cell its area's index) or single pixels (labels None)."""
    hot, _, start, end = hotspot(a, v)

    def rank():
        r = hot
        if v.box:
            r = logic.within(hot, logic.cells_in_bounds(a.cube, v.box))
        if v.by_pixel:
            found, labels = logic.flag_recent(
                a.cube, logic.top_candidates(a.cube, *r, n=LIST_MAX, min_value=v.min_change),
                end=end, direction=v.orbit), None
        else:
            found, labels = logic.find_clusters(a.cube, *r, min_value=v.min_change, min_area_ha=v.min_area,
                                                end=end, direction=v.orbit)
        return [dict(c, rank=i) for i, c in enumerate(found, 1)], labels

    # The same key as the Explore tab's, so either warms the other's cache (box: str()).
    box = None if v.box is None else [list(v.box[0]), list(v.box[1])]
    return ex._reuse(_hot_key(a, v, start, end) + ("rank", v.min_change, v.min_area, v.by_pixel, str(box)),
                     rank, store=ex._SHARED)


# ---- JSON shapes ----

def _rating(r: Optional[confidence.Rating]) -> Optional[Dict]:
    if r is None:
        return None
    return {"label": r.label, "colour": r.colour,
            "factors": [{"name": f.name, "level": f.level, "text": f.text} for f in r.factors]}


def _summary(s: Optional[validation.Summary]) -> Optional[Dict]:
    if s is None or not getattr(s, "verdict", ""):
        return None
    return {"verdict": s.verdict, "label": "Disagree" if s.verdict == "mixed" else validation.SHORT[s.verdict],
            "count": s.count, "colour": validation.COLOURS[s.verdict]}


def _reference(s: Optional[validation.Summary]) -> bool:
    """Whether a change matches the admins' reference demolitions (admins only see them)."""
    return bool(s and s.reference)


def _land(code) -> str:
    return LCZ_NAMES.get(int(code), "") if code is not None and int(code) > 0 else ""


def change_json(c: Dict, *, min_change: float, cache_dir, rating=None, summary=None) -> Dict:
    before, after = c.get("date_before"), c.get("date_after")
    drop = -abs(float(c["drop_db"]))
    out = {
        "rank": c["rank"], "lat": c["lat"], "lon": c["lon"], "row": c["row"], "col": c["col"],
        "dropDb": c["drop_db"], "avgDropDb": c["avg_drop_db"], "dateBefore": before, "dateAfter": after,
        "when": findings.when_text(before or after, after) if after else "",
        "strength": findings.strength(drop, min_change), "changeText": findings.change_text(drop),
        "recent": bool(c.get("recent")), "passesAfter": c.get("passes_after"),
        "land": _land(c.get("lcz")), "place": places.cached(cache_dir, c["lat"], c["lon"]),
        "rating": _rating(rating), "verdict": _summary(summary), "reference": _reference(summary),
    }
    if "cells" in c:
        out.update(areaHa=c["area_ha"], cells=c["n_cells"], maxDropDb=c["max_drop_db"], bounds=c["bounds"])
    return out


def records(a: Area, user) -> List[validation.Record]:
    return validation.for_cube(a.cube, user)


def changes(a: Area, v: View, user, cache_dir, limit: int = LIST_MAX) -> Dict:
    """Strongest changes: the totals line and the first `limit` rows, rated."""
    found, _ = ranked(a, v)
    _, track_drops, start, end = hotspot(a, v)
    recs = records(a, user)
    res = float(a.cube.attrs.get("resolution", 100.0))
    cell_ha = res * res / 10_000.0
    rows = []
    for c in found[:limit]:
        sm = validation.summarise(recs, c) if recs else None
        tracks = confidence.tracks_from_steps(track_drops, c, v.min_change) if track_drops is not None else None
        rating = confidence.rate(c, v.min_change, tracks, sm, True)
        rows.append(change_json(c, min_change=v.min_change, cache_dir=cache_dir, rating=rating, summary=sm))
    confirmed = sum(1 for c in found if (sm := validation.summarise(recs, c)) and sm.verdict == "demolition") \
        if recs else 0
    refs = sum(1 for r in rows[:10] if r["reference"]) if any(r.source == "reference" for r in recs) else None
    return {
        "start": start, "end": end, "byPixel": v.by_pixel, "referenceMatches": refs,
        "total": len(found), "totalHa": round(sum(c.get("area_ha", cell_ha) for c in found), 2),
        "recent": sum(1 for c in found if c.get("recent")), "confirmed": confirmed,
        "items": rows,
    }


@dataclass(frozen=True)
class Detect:
    method: str = "pelt"
    sensitivity: float = 1.0
    window: int = 2
    direction: str = "drop"
    neighborhood: bool = True       # 3x3 median, 10 m areas only


def target_json(c: Dict) -> Dict:
    """What a verdict is about: the change's cells (or pixel) and dates."""
    out = {"row": int(c["row"]), "col": int(c["col"]),
           "date_before": c.get("date_before"), "date_after": c.get("date_after")}
    if c.get("cells"):
        out["cells"] = [[int(y), int(x)] for y, x in c["cells"]]
    return out


def record_json(r: validation.Record, viewer_id: int) -> Dict:
    return {
        "id": r.id, "verdict": r.verdict, "label": validation.VERDICTS.get(r.verdict, r.verdict),
        "colour": validation.COLOURS.get(r.verdict), "evidence": [validation.EVIDENCE.get(e, e) for e in r.evidence],
        "link": r.link, "eventDate": r.event_date, "confidence": r.confidence, "note": r.note,
        "userName": r.user_name, "createdAt": r.created_at, "status": r.status,
        "vetoedBy": r.vetoed_by_name, "vetoReason": r.veto_reason, "mine": r.user_id == viewer_id,
        "source": r.source,
    }


class Outside(ValueError):
    pass


def place_at(a: Area, v: View, d: Detect, lat: float, lon: float, user, cache_dir) -> Dict:
    """The analysis of a place (the page's selection and the report share it):
    raises Outside for a point that isn't analysed."""
    cube = a.cube
    idx = cell_index(cube, lon, lat)
    if idx is None:
        raise Outside("That point is outside this area.")
    if not logic.cell_allowed(cube, idx[0], idx[1], list(v.lcz)):
        raise Outside("That point is outside the selected land-use classes, so it isn't analysed.")
    found, labels = ranked(a, v)
    _, track_drops, start, end = hotspot(a, v)
    area = found[labels[idx]] if labels is not None and labels[idx] >= 0 else None
    picked = area or next((c for c in found[:LIST_MAX] if (c["row"], c["col"]) == idx), None)

    res = float(cube.attrs.get("resolution", 100.0))
    window = 1 if (res <= 50 and d.neighborhood) else 0
    params = (dict(sensitivity=d.sensitivity, min_drop_db=v.min_change, direction=d.direction)
              if d.method == "pelt" else dict(window=d.window, min_drop_db=v.min_change, direction=d.direction))
    where = tuple(sorted(area["cells"])) if area is not None else (idx, window)

    def detect():
        ser = (logic.select_area_series(cube, area["cells"], v.pol, v.orbit) if area is not None
               else logic.select_series(cube, idx[0], idx[1], v.pol, v.orbit, window=window))
        return ser, logic.run_detection(ser, start=start, end=end, method=d.method, **params)

    det_key = ("detect", a.entry.key, a.entry.created_at, where, v.pol, v.orbit, start, end, d.method,
               tuple(sorted(params.items())))
    series, results = ex._reuse(det_key, detect)
    events = findings.events(results)
    # As in Explore: a change picked from the list is always among the detected
    # changes, even when this place's stricter detector doesn't flag it alone.
    if picked and picked.get("date_after") and not any(
            e.is_drop and abs((e.end - np.datetime64(picked["date_after"][:10], "D"))
                              / np.timedelta64(1, "D")) <= validation.MATCH_DAYS for e in events):
        seen_total = (confidence.tracks_from_steps(track_drops, picked, v.min_change)
                      if track_drops is not None else None) or (1, 1)
        events = sorted(events + [findings.Event(
            start=np.datetime64((picked.get("date_before") or picked["date_after"])[:10], "D"),
            end=np.datetime64(picked["date_after"][:10], "D"), delta_db=-abs(float(picked["drop_db"])),
            tracks=max(seen_total[0], 1), total_tracks=max(seen_total[1], 1))], key=lambda e: e.start)

    recs = records(a, user)
    rating, summary = None, None
    if picked is not None:
        summary = validation.summarise(recs, picked) if recs else None
        # Rated as in the list when it is listed; from this place's own series otherwise.
        tracks = (confidence.tracks_from_steps(track_drops, picked, v.min_change)
                  if track_drops is not None and picked["rank"] <= LIST_MAX else None)
        if tracks is None:
            tracks = confidence.track_steps(series, picked.get("date_after"), v.min_change, start=start, end=end)
        rating = confidence.rate(picked, v.min_change, tracks, summary, True)

    tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
    lon_c, lat_c = tx.transform(cube["x"].values[idx[1]], cube["y"].values[idx[0]])
    sig = findings.relative_signal(results, events)
    place = places.cached(cache_dir, lat_c, lon_c)
    photo = ex._photo_options(events, v.min_change, (place or "").split(",")[0])
    # The change a verdict is about: the listed one, else the strongest change here.
    target = picked
    if target is None and photo:
        target = {"row": idx[0], "col": idx[1], "date_before": photo[0]["date_before"][:10],
                  "date_after": photo[0]["date_after"][:10]}
    return {
        "idx": idx, "lat": float(lat_c), "lon": float(lon_c), "area": area, "picked": picked, "start": start,
        "end": end, "results": results, "events": events, "signal": sig, "rating": rating, "summary": summary,
        "records": recs, "place": place, "photo": photo, "target": target, "track_drops": track_drops,
        "land": (logic.lcz_label(logic.lcz_at(cube, idx[0], idx[1])).split(" ", 1)[-1]
                 if logic.has_lcz(cube) else ""),
    }


def at(a: Area, v: View, d: Detect, lat: float, lon: float, user, cache_dir) -> Dict:
    """A place on the map, clicked or picked from the list: its change area (if
    any), the radar signal and detected changes there, how sure we are, the
    changes photos can be fetched for, and the verdicts on it."""
    try:
        p = place_at(a, v, d, lat, lon, user, cache_dir)
    except Outside as e:
        return {"note": str(e)}
    cube, idx, area, picked, events, sig = a.cube, p["idx"], p["area"], p["picked"], p["events"], p["signal"]
    rating, summary, recs, target, photo = p["rating"], p["summary"], p["records"], p["target"], p["photo"]
    mine_id = user.id if user else -1
    return {
        "cell": [int(idx[0]), int(idx[1])], "lat": p["lat"], "lon": p["lon"],
        "land": p["land"], "place": p["place"], "start": p["start"], "end": p["end"],
        "area": (dict(change_json(area, min_change=v.min_change, cache_dir=cache_dir),
                      outline=logic.area_outline(cube, area["cells"])) if area is not None else None),
        "listed": (change_json(picked, min_change=v.min_change, cache_dir=cache_dir, rating=rating, summary=summary)
                   if picked is not None else None),
        "rating": _rating(rating),
        "events": [{
            "start": str(e.start), "end": str(e.end), "deltaDb": round(float(e.delta_db), 2), "isDrop": e.is_drop,
            "tracks": e.tracks, "totalTracks": e.total_tracks, "strength": findings.strength(e.delta_db, v.min_change),
            "changeText": findings.change_text(e.delta_db), "when": findings.when_text(e.start, e.end),
        } for e in events],
        "signal": {
            "dates": [str(x) for x in sig.dates], "pct": [round(float(x), 1) for x in sig.pct],
            "trend": [round(float(x), 1) for x in sig.trend], "band": sig.band, "hover": sig.hover,
        },
        "rows": [{k: (v_.item() if hasattr(v_, "item") else v_) for k, v_ in r.items()}
                 for r in logic.drop_rows(p["results"])],
        "tracks": [_track(r) for r in p["results"] if len(r.series)],
        "photoOptions": [{"label": o["label"], "dateBefore": o["date_before"][:10], "dateAfter": o["date_after"][:10]}
                         for o in photo],
        "target": target_json(target) if target is not None else None,
        "verdicts": [record_json(r, mine_id) for r in (validation.matching(recs, target) if target else [])],
        "summary": _summary(summary),
    }


PAGE_URL = "https://space4all.web.app/tools/demolitions/"


def share_link(a: Area, lat: float, lon: float, start, end) -> str:
    """The page's link to this place (its hash: area/lat,lon/start,end)."""
    return f"{PAGE_URL}#{area_id(a.entry.key)}/{lat:.5f},{lon:.5f}/{start},{end}"


def report_pdf(a: Area, v: View, d: Detect, lat: float, lon: float, user, cache_dir,
               photos: Optional[Dict] = None) -> Tuple[bytes, str]:
    """The one-page case sheet for the listed change at this place (the old
    app's report), with its Sentinel-2 photos when the caller fetched them, and
    a file name. ValueError if there is no listed change here."""
    from ..app import report

    p = place_at(a, v, d, lat, lon, user, cache_dir)
    c = p["picked"]
    if c is None or p["rating"] is None:
        raise ValueError("Reports are for changes in Strongest changes: pick one from the list, or click inside one.")
    hot, _, start, end = hotspot(a, v)
    res = float(a.cube.attrs.get("resolution", 100.0))
    pdf = report.build(
        place=p["place"] or "", area_name=a.entry.name.replace("_", " ").title(), c=c, rating=p["rating"],
        events=p["events"], signal=p["signal"], min_change=v.min_change, cube=a.cube, mag=hot[0], resolution=res,
        land_use=p["land"], link=share_link(a, c["lat"], c["lon"], start, end), photos=photos,
        verdicts=validation.matching(p["records"], c))
    name = ((p["place"] or "site").split(",")[0]).strip()
    slug = ex._slug(f"{a.entry.name}_{name}_{(c.get('date_after') or '')[:7]}")
    return pdf, f"{slug}_report.pdf"


AREA_DOWNLOADS = ("changes.csv", "changes.geojson", "map.geojson")
PLACE_DOWNLOADS = ("signal.csv", "drops.csv")


def area_download(a: Area, v: View, item: str, user, cache_dir) -> Tuple[str, str, str]:
    """(content, file name, media type): every change found with these settings
    (table or outlines), or every pixel of the drop map, as the old Download button."""
    from ..app import downloads

    found, _ = ranked(a, v)
    _, track_drops, start, end = hotspot(a, v)
    recs = records(a, user)
    summaries = {c["rank"]: sm for c in found if (sm := validation.summarise(recs, c))} if recs else {}
    kw = dict(min_change=v.min_change, track_drops=track_drops, summaries=summaries, show_verdicts=True,
              cache_dir=cache_dir)
    tag = f"{ex._slug(a.entry.name)}_{start}_{end}"
    if item == "changes.csv":
        return downloads.changes_csv(found, **kw), f"{tag}_changes.csv", "text/csv"
    if item == "changes.geojson":
        return downloads.changes_geojson(a.cube, found, **kw), f"{tag}_changes.geojson", "application/geo+json"
    if item == "map.geojson":
        return (logic.hotspot_geojson(a.cube, pol=v.pol, start=start, end=end, direction=v.orbit,
                                      lcz_classes=list(v.lcz), min_value=v.min_change),
                f"{tag}_map_min{v.min_change:g}dB_{v.pol}.geojson", "application/geo+json")
    raise ValueError("Unknown download.")


def place_download(a: Area, v: View, d: Detect, lat: float, lon: float, item: str, user, cache_dir) -> Tuple[str, str, str]:
    """(content, file name, media type): the radar signal at a place, or its detected changes."""
    p = place_at(a, v, d, lat, lon, user, cache_dir)
    name = ex._slug(f"{a.entry.name}_{(p['place'] or 'site').split(',')[0]}")
    if item == "signal.csv":
        series = [r.series for r in p["results"]]
        return (logic.series_csv(series, start=p["start"], end=p["end"], cell=p["idx"]),
                f"{name}_signal_{p['start']}_{p['end']}.csv", "text/csv")
    if item == "drops.csv":
        return logic.drops_csv(p["results"], cell=p["idx"]), f"{name}_changes.csv", "text/csv"
    raise ValueError("Unknown download.")


def _track(r) -> Dict:
    """One satellite track's radar series at a place (dB) and the drops found in it."""
    s = r.series
    ok = np.isfinite(s.values_db)
    return {"label": s.label, "orbit": s.orbit_state,
            "dates": [str(d) for d in s.dates[ok].astype("datetime64[D]")],
            "db": [round(float(x), 2) for x in s.values_db[ok]],
            "drops": [{"before": str(np.datetime64(d.date_before, "D")), "after": str(np.datetime64(d.date_after, "D")),
                       "pre": round(float(d.pre_db), 2), "post": round(float(d.post_db), 2)} for d in r.drops]}


def verdict_layer(a: Area, user) -> Dict:
    return ex._verdict_layer(records(a, user))


def area_json(a: Area) -> Dict:
    e, cube = a.entry, a.cube
    has_lcz = logic.has_lcz(cube)
    res = float(cube.attrs.get("resolution", e.resolution))
    return {
        "key": e.key, "id": area_id(e.key), "name": e.name.replace("_", " ").title(), "bounds": e.bbox_ll, "resolution": res,
        "days": [str(x) for x in a.days], "hasLcz": has_lcz,
        "lcz": [{"code": c, "name": n, "default": c in DEFAULT_CLASSES} for c, n in sorted(LCZ_NAMES.items())]
        if has_lcz else [],
        "defaults": {"pol": "vv", "orbit": "both", "minChange": ex.DEFAULT_MIN_CHANGE,
                     "minArea": logic.DEFAULT_MIN_AREA_HA, "method": "pelt", "sensitivity": 1.0, "window": 2,
                     "direction": "drop"},
        "neighborhood": res <= 50,
    }


def target_from(a: Area, t: Dict) -> Dict:
    """A verdict's target from the page, checked against the area's grid."""
    ny, nx = a.cube.sizes["y"], a.cube.sizes["x"]
    row, col = int(t["row"]), int(t["col"])
    cells = [(int(y), int(x)) for y, x in t.get("cells") or []]
    if not (0 <= row < ny and 0 <= col < nx) or any(not (0 <= y < ny and 0 <= x < nx) for y, x in cells):
        raise ValueError("That place is outside this area.")
    if len(cells) > 50_000:
        raise ValueError("That change is too large.")
    c = {"row": row, "col": col, "date_before": (t.get("date_before") or None),
         "date_after": (t.get("date_after") or None)}
    for k in ("date_before", "date_after"):
        if c[k]:
            np.datetime64(c[k][:10], "D")     # a valid date, or ValueError
            c[k] = c[k][:10]
    if cells:
        c["cells"] = cells
    if not c["date_after"]:
        raise ValueError("A verdict needs the change's dates.")
    return c
