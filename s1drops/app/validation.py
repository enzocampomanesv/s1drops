"""Verdicts on detected changes, and admin-only reference data (SQLite).

A verdict records what happened at a flagged change: its outline (lat/lon
GeoJSON), the drop's before/after dates, the verdict, the evidence used and who
said so. It is stored by place and date, not by table row: areas are rebuilt
whenever the settings change, so on display each record is mapped onto the
current cube's cells (`for_cube`) and matched to candidates by shared cells and
a nearby date (`summarise`). That also carries verdicts over to a re-baked cube.

One approved user's verdict is enough to count. Reviewers and admins can veto any
verdict (and undo a veto); nothing is deleted, so the history stays auditable.
A user's new verdict on the same change supersedes their earlier one.

Reference records come from an admin's import of known demolitions (polygons with
a year). They are visible to admins only and are used to score the detector.
"""
from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import accounts

VERDICTS = {
    "demolition": "Demolition / clearance",
    "other": "Other change (construction, flooding, vegetation…)",
    "none": "No real change",
    "unsure": "Can't tell (clouds, poor imagery)",
}
SHORT = {"demolition": "demolition", "other": "other change", "none": "no change",
         "unsure": "can't tell"}
EVIDENCE = {
    "s2": "Sentinel-2 photos (in this app)",
    "highres": "High-resolution imagery",
    "field": "Field visit",
    "report": "News or NGO report",
    "community": "Community source",
}
CONFIDENCE = ("low", "medium", "high")
COLOURS = {"demolition": "#2ca02c", "other": "#9467bd", "none": "#7f7f7f",
           "unsure": "#bcbd22", "mixed": "#ff7f0e"}

MATCH_DAYS = 48   # a verdict applies to drops dated within this of its own

SCHEMA = """
CREATE TABLE IF NOT EXISTS validations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'user',     -- user | reference
    cube_key TEXT,
    kind TEXT NOT NULL DEFAULT 'area',       -- area | pixel | reference
    geometry TEXT NOT NULL,                  -- GeoJSON geometry, lon/lat
    date_before TEXT,
    date_after TEXT,
    verdict TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    event_date TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | superseded | vetoed
    vetoed_by INTEGER,
    vetoed_at REAL,
    veto_reason TEXT NOT NULL DEFAULT '',
    ref_name TEXT NOT NULL DEFAULT '',
    ref_year INTEGER,
    ref_batch TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS validations_source ON validations (source, status);
"""


@dataclass
class Record:
    id: int
    source: str
    kind: str
    geometry: Dict
    date_before: Optional[str]
    date_after: Optional[str]
    verdict: str
    evidence: List[str]
    link: str
    event_date: str
    confidence: str
    note: str
    user_id: int
    user_name: str
    created_at: float
    status: str
    vetoed_by_name: str
    veto_reason: str
    ref_name: str
    ref_year: Optional[int]
    ref_batch: str
    cells: frozenset = frozenset()        # filled in by for_cube

    @property
    def counts(self) -> bool:
        return self.status == "active"


def _fresh(user: Optional[accounts.User]) -> Optional[accounts.User]:
    """The user's current role from the database (a session's copy can be stale)."""
    return accounts.get_user(user.id) if user else None


def _require(user, check, message):
    fresh = _fresh(user)
    if fresh is None or fresh.role == "disabled" or not check(fresh):
        raise accounts.NotAllowed(message)
    return fresh


# ---- geometry ----------------------------------------------------------------

def _days(d) -> Optional[int]:
    if not d:
        return None
    return int(np.datetime64(str(d)[:10], "D").astype("int64"))


def cells_in_geometry(cube, geometry: Dict) -> frozenset:
    """Cube cells (row, col) whose centre lies inside a lon/lat geometry."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform

    geom = shape(geometry)
    if geom.is_empty:
        return frozenset()
    tx = Transformer.from_crs("EPSG:4326", cube.attrs["crs"], always_xy=True)
    geom = transform(tx.transform, geom)
    xs = np.asarray(cube["x"].values, dtype="float64")
    ys = np.asarray(cube["y"].values, dtype="float64")
    half = float(cube.attrs.get("resolution", 100.0)) / 2.0
    minx, miny, maxx, maxy = geom.bounds
    ci = np.nonzero((xs >= minx - half) & (xs <= maxx + half))[0]
    ri = np.nonzero((ys >= miny - half) & (ys <= maxy + half))[0]
    if not ci.size or not ri.size:
        return frozenset()
    gx, gy = np.meshgrid(xs[ci], ys[ri])
    inside = shapely.contains_xy(geom, gx, gy)
    if not inside.any():
        # A shape smaller than a cell covers no centre: take the cell under it.
        p = geom.representative_point()
        r = int(np.argmin(np.abs(ys - p.y))); k = int(np.argmin(np.abs(xs - p.x)))
        if abs(ys[r] - p.y) <= half and abs(xs[k] - p.x) <= half:
            return frozenset({(r, k)})
        return frozenset()
    rr, cc = np.nonzero(inside)
    return frozenset(zip(ri[rr].tolist(), ci[cc].tolist()))


def candidate_cells(c: Dict) -> frozenset:
    return frozenset(map(tuple, c["cells"])) if "cells" in c else frozenset({(c["row"], c["col"])})


def candidate_geometry(cube, c: Dict) -> Dict:
    from .logic import area_outline
    return area_outline(cube, sorted(candidate_cells(c)))["geometry"]


# ---- reading -----------------------------------------------------------------

_SELECT = """
SELECT v.*, COALESCE(NULLIF(u.name, ''), u.email) AS user_name,
       COALESCE(NULLIF(w.name, ''), w.email, '') AS vetoed_by_name
FROM validations v
JOIN users u ON u.id = v.user_id
LEFT JOIN users w ON w.id = v.vetoed_by
"""


def _record(row) -> Record:
    return Record(
        id=row["id"], source=row["source"], kind=row["kind"],
        geometry=json.loads(row["geometry"]), date_before=row["date_before"],
        date_after=row["date_after"], verdict=row["verdict"],
        evidence=[e for e in row["evidence"].split(",") if e], link=row["link"],
        event_date=row["event_date"], confidence=row["confidence"], note=row["note"],
        user_id=row["user_id"], user_name=row["user_name"], created_at=row["created_at"],
        status=row["status"], vetoed_by_name=row["vetoed_by_name"],
        veto_reason=row["veto_reason"], ref_name=row["ref_name"], ref_year=row["ref_year"],
        ref_batch=row["ref_batch"])


def _bbox_ll(cube) -> Tuple[float, float, float, float]:
    from pyproj import Transformer
    xs = np.asarray(cube["x"].values, dtype="float64")
    ys = np.asarray(cube["y"].values, dtype="float64")
    tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
    lon, lat = tx.transform([xs.min(), xs.max(), xs.min(), xs.max()],
                            [ys.min(), ys.min(), ys.max(), ys.max()])
    return min(lon), min(lat), max(lon), max(lat)


def for_cube(cube, viewer: Optional[accounts.User]) -> List[Record]:
    """Every verdict on this cube's ground the viewer may see, with its cells.

    Approved users see user verdicts (including vetoed and superseded ones, for
    the history); admins also see reference records. Others see nothing.
    """
    if viewer is None or not viewer.approved:
        return []
    from shapely.geometry import box, shape

    sources = ("user", "reference") if viewer.is_admin else ("user",)
    with accounts.connect() as con:
        rows = con.execute(_SELECT + f" WHERE v.source IN ({','.join('?' * len(sources))})"
                           " ORDER BY v.created_at", sources).fetchall()
    frame = box(*_bbox_ll(cube))
    out = []
    for row in rows:
        rec = _record(row)
        if not shape(rec.geometry).intersects(frame):
            continue
        rec.cells = cells_in_geometry(cube, rec.geometry)
        if rec.cells:
            out.append(rec)
    return out


def _date_matches(rec: Record, date_after: Optional[str]) -> bool:
    d = _days(date_after)
    if d is None:
        return False
    if rec.source == "reference":
        if rec.ref_year is None:
            return True
        # Demolitions late in a year show up in the first images of the next.
        lo = _days(f"{rec.ref_year}-01-01"); hi = _days(f"{rec.ref_year + 1}-03-31")
        return lo <= d <= hi
    ref = _days(rec.date_after)
    return ref is not None and abs(d - ref) <= MATCH_DAYS


def matching(records: Sequence[Record], c: Dict) -> List[Record]:
    """Records about the same change as candidate `c`: shared cells, nearby date."""
    cells = candidate_cells(c)
    return [r for r in records if r.cells & cells and _date_matches(r, c.get("date_after"))]


@dataclass
class Summary:
    verdict: str          # a VERDICTS key, or "mixed"
    count: int            # people whose verdict counts
    reference: bool       # matches admin reference data


def summarise(records: Sequence[Record], c: Dict) -> Optional[Summary]:
    """What the counting verdicts say about candidate `c` (None if nothing)."""
    found = matching(records, c)
    users = [r for r in found if r.source == "user" and r.counts]
    reference = any(r.source == "reference" for r in found)
    if not users and not reference:
        return None
    latest: Dict[int, str] = {}
    for r in sorted(users, key=lambda r: r.created_at):
        latest[r.user_id] = r.verdict
    kinds = set(latest.values())
    verdict = (kinds.pop() if len(kinds) == 1 else "mixed") if kinds else ""
    return Summary(verdict, len(latest), reference)


# ---- writing -----------------------------------------------------------------

def record(user: accounts.User, cube, c: Dict, *, cube_key: str, verdict: str,
           evidence: Sequence[str] = (), link: str = "", event_date: str = "",
           confidence: str = "", note: str = "") -> int:
    """Store `user`'s verdict on candidate `c`; supersedes their earlier one on it."""
    user = _require(user, lambda u: u.approved, "Only approved validators can do this.")
    if verdict not in VERDICTS:
        raise ValueError("Choose a verdict.")
    evidence = [e for e in evidence if e in EVIDENCE]
    if confidence and confidence not in CONFIDENCE:
        raise ValueError("Unknown confidence level.")
    link = link.strip()
    if link and not link.lower().startswith(("http://", "https://")):
        raise ValueError("Links must start with http:// or https://.")
    mine = [r for r in for_cube(cube, user) if r.source == "user" and r.user_id == user.id
            and r.status == "active"]
    replaced = [r.id for r in matching(mine, c)]
    with accounts.connect() as con:
        if replaced:
            con.execute(f"UPDATE validations SET status = 'superseded' WHERE id IN "
                        f"({','.join('?' * len(replaced))})", replaced)
        return con.execute(
            "INSERT INTO validations (source, cube_key, kind, geometry, date_before, "
            "date_after, verdict, evidence, link, event_date, confidence, note, user_id, "
            "created_at) VALUES ('user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cube_key, "area" if "cells" in c else "pixel",
             json.dumps(candidate_geometry(cube, c)), c.get("date_before"),
             c.get("date_after"), verdict, ",".join(evidence), link, event_date.strip(),
             confidence, note.strip()[:4000], user.id, time.time())).lastrowid


def veto(user: accounts.User, record_id: int, reason: str = "") -> None:
    user = _require(user, lambda u: u.can_review, "Only reviewers and admins can veto.")
    with accounts.connect() as con:
        con.execute("UPDATE validations SET status = 'vetoed', vetoed_by = ?, vetoed_at = ?, "
                    "veto_reason = ? WHERE id = ? AND source = 'user'",
                    (user.id, time.time(), reason.strip()[:1000], record_id))


def undo_veto(user: accounts.User, record_id: int) -> None:
    _require(user, lambda u: u.can_review, "Only reviewers and admins can undo a veto.")
    with accounts.connect() as con:
        con.execute("UPDATE validations SET status = 'active', vetoed_by = NULL, "
                    "vetoed_at = NULL, veto_reason = '' WHERE id = ? AND status = 'vetoed'",
                    (record_id,))


# ---- export ------------------------------------------------------------------

def export(user: accounts.User, fmt: str = "csv") -> str:
    """Every user verdict (with its status) as CSV or GeoJSON, for reviewers."""
    _require(user, lambda u: u.can_review, "Only reviewers and admins can export.")
    with accounts.connect() as con:
        rows = con.execute(_SELECT + " WHERE v.source = 'user' ORDER BY v.created_at"
                           ).fetchall()
    recs = [_record(r) for r in rows]
    fields = ["id", "cube_key", "kind", "date_before", "date_after", "verdict", "evidence",
              "link", "event_date", "confidence", "note", "user_name", "created",
              "status", "vetoed_by_name", "veto_reason"]

    def props(r: Record, cube_key) -> Dict:
        return {"id": r.id, "cube_key": cube_key, "kind": r.kind,
                "date_before": r.date_before, "date_after": r.date_after,
                "verdict": r.verdict, "evidence": ",".join(r.evidence), "link": r.link,
                "event_date": r.event_date, "confidence": r.confidence, "note": r.note,
                "user_name": r.user_name,
                "created": time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.created_at)),
                "status": r.status, "vetoed_by_name": r.vetoed_by_name,
                "veto_reason": r.veto_reason}

    keys = {r["id"]: r["cube_key"] for r in rows}
    if fmt == "geojson":
        return json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": r.geometry, "properties": props(r, keys[r.id])}
            for r in recs]})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in recs:
        w.writerow(props(r, keys[r.id]))
    return buf.getvalue()


# ---- reference data (admin only) -----------------------------------------------

def import_reference(user: accounts.User, geojson_text: str, batch: str, *,
                     year_field: str = "demo_year", name_field: str = "Name") -> int:
    """Load known demolitions (polygons, lon/lat) as admin-only reference records."""
    user = _require(user, lambda u: u.is_admin, "Only admins can import reference data.")
    from shapely.geometry import mapping, shape

    data = json.loads(geojson_text)
    crs = (data.get("crs") or {}).get("properties", {}).get("name", "")
    if crs and not any(k in crs for k in ("CRS84", "4326")):
        raise ValueError(f"Reference data must be in lon/lat (EPSG:4326); found {crs}.")
    rows = []
    for f in data.get("features", []):
        geom = f.get("geometry")
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        props = f.get("properties") or {}
        year = props.get(year_field)
        try:
            year = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        rows.append((json.dumps(mapping(shape(geom))), str(props.get(name_field) or ""),
                     year))
    if not rows:
        raise ValueError("No polygons found in the file.")
    now = time.time()
    with accounts.connect() as con:
        con.executemany(
            "INSERT INTO validations (source, kind, geometry, verdict, user_id, created_at, "
            "ref_name, ref_year, ref_batch) VALUES ('reference', 'reference', ?, "
            "'demolition', ?, ?, ?, ?, ?)",
            [(g, user.id, now, n, y, batch) for g, n, y in rows])
    return len(rows)


def reference_batches(user: accounts.User) -> List[Dict]:
    _require(user, lambda u: u.is_admin, "Only admins can see reference data.")
    with accounts.connect() as con:
        rows = con.execute(
            "SELECT ref_batch, COUNT(*) AS n, MIN(ref_year) AS y0, MAX(ref_year) AS y1, "
            "MIN(created_at) AS t FROM validations WHERE source = 'reference' "
            "GROUP BY ref_batch ORDER BY t").fetchall()
    return [dict(r) for r in rows]


def delete_reference_batch(user: accounts.User, batch: str) -> None:
    _require(user, lambda u: u.is_admin, "Only admins can delete reference data.")
    with accounts.connect() as con:
        con.execute("DELETE FROM validations WHERE source = 'reference' AND ref_batch = ?",
                    (batch,))
