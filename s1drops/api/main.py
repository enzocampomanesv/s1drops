"""The JSON API behind Space4All's Demolition detection page
(space4all.web.app/tools/demolitions/). Runs next to the Solara app, on the
same cube cache and database:

    uvicorn s1drops.api.main:app --port 8766

Everything is under /api. Only signed-in users the tool has approved see any
of it; /api/me and /api/access are for finding out where one stands and asking.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..app import accounts, validation
from ..cube import list_cubes
from . import photos as photo_jobs
from . import view
from .auth import Caller, admin, approved, caller

app = FastAPI(title="Demolition detection API", docs_url=None, redoc_url=None, openapi_url=None)


def _origins() -> List[str]:
    raw = os.environ.get("S1DROPS_API_ORIGINS", "https://space4all.web.app")
    return [o.strip() for o in raw.split(",") if o.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(),
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=3600,
)


def cache_dir() -> Path:
    return Path(os.environ.get("S1DROPS_CACHE_DIR", "./cache"))


@app.get("/api/health")
def health():
    return {"ok": True}


# ---- where one stands ----

@app.get("/api/me")
def me(c: Caller = Depends(caller)):
    """For the page's gate: verify your email, ask for access, waiting, or in."""
    u = c.user
    return {
        "email": c.email,
        "emailVerified": c.email_verified,
        "role": u.role if u else None,
        "roleLabel": accounts.ROLE_LABELS.get(u.role) if u else None,
        "approved": c.approved,
        "name": u.name if u else "",
        "affiliation": u.affiliation if u else "",
        "canReview": bool(u and u.approved and u.can_review),
        "isAdmin": bool(u and u.approved and u.is_admin),
    }


class AccessIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    affiliation: str = Field("", max_length=300)
    reason: str = Field(..., min_length=1, max_length=2000)


@app.post("/api/access")
def ask_for_access(body: AccessIn, c: Caller = Depends(caller)):
    if not c.email_verified:
        raise HTTPException(status_code=403, detail="Verify your email first.")
    try:
        u = accounts.request_access(c.uid, c.email, body.name, body.affiliation, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"role": u.role, "roleLabel": accounts.ROLE_LABELS[u.role], "approved": u.approved}


# ---- areas ----

@app.get("/api/areas")
def areas(_: Caller = Depends(approved)):
    """The baked areas (cubes), newest last, as the area picker lists them."""
    out = []
    for e in list_cubes(cache_dir()):
        out.append({
            "key": e.key, "name": e.name.replace("_", " ").title(), "bounds": e.bbox_ll,
            "start": str(e.start)[:10], "end": str(e.end)[:10], "resolution": e.resolution,
            "fast": e.reduction == "overview_med", "passes": e.n_passes,
        })
    return sorted(out, key=lambda a: (a["name"], a["resolution"], a["start"]))


# ---- one area: its map, its changes, a place on it ----

def view_of(
    key: str,
    pol: str = Query("vv"), orbit: str = Query("both"),
    start: Optional[str] = Query(None), end: Optional[str] = Query(None),
    minChange: float = Query(view.ex.DEFAULT_MIN_CHANGE, ge=0.5, le=8.0),
    minArea: float = Query(view.logic.DEFAULT_MIN_AREA_HA, ge=0.0, le=25.0),
    byPixel: bool = Query(False),
    lcz: Optional[str] = Query(None, description="land-use classes, comma-separated; empty: all"),
    box: Optional[str] = Query(None, description="searched map area: south,west,north,east"),
) -> view.View:
    if pol not in view.POLS or orbit not in view.ORBITS:
        raise HTTPException(status_code=400, detail="Unknown radar channel or track direction.")
    try:
        for d in (start, end):
            if d:
                view.np.datetime64(d, "D")
        classes = tuple(view.DEFAULT_CLASSES) if lcz is None else tuple(
            sorted({int(c) for c in lcz.split(",") if c.strip()} & set(view.LCZ_NAMES)))
        b = None
        if box:
            s, w, n, e = (float(x) for x in box.split(","))
            b = ((s, w), (n, e))
    except ValueError:
        raise HTTPException(status_code=400, detail="Check the dates, land-use classes and map area.")
    if lcz is not None and not classes:
        classes = tuple(sorted(view.LCZ_NAMES))     # none ticked: everything
    return view.View(key, pol, orbit, start, end, minChange, minArea, byPixel, classes, b)


def detect_of(method: str = Query("pelt"), sensitivity: float = Query(1.0, ge=0.2, le=5.0),
              window: int = Query(2, ge=1, le=5), direction: str = Query("drop"),
              neighborhood: bool = Query(True)) -> view.Detect:
    if method not in view.METHODS or direction not in view.DIRECTIONS:
        raise HTTPException(status_code=400, detail="Unknown detection method or type of change.")
    return view.Detect(method, sensitivity, window, direction, neighborhood)


def _area(key: str) -> view.Area:
    try:
        return view.open_area(cache_dir(), key)
    except view.NotFound:
        raise HTTPException(status_code=404, detail="No such area.")


@app.get("/api/areas/{key}")
def area(key: str, _: Caller = Depends(approved)):
    return view.area_json(_area(key))


@app.get("/api/areas/{key}/overlay")
def overlay(key: str, layer: str = Query("hotspot"), v: view.View = Depends(view_of),
            _: Caller = Depends(approved)):
    """The map layer as a PNG data URL with its lat/lon bounds and legend."""
    a = _area(key)
    if layer not in view.LAYERS or (layer == "lcz" and not view.logic.has_lcz(a.cube)):
        raise HTTPException(status_code=400, detail="Unknown map layer.")
    return view.overlay(a, v, layer)


@app.get("/api/areas/{key}/changes")
def changes(key: str, v: view.View = Depends(view_of), c: Caller = Depends(approved)):
    return view.changes(_area(key), v, c.user, cache_dir())


@app.get("/api/areas/{key}/at")
def place(key: str, lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
          v: view.View = Depends(view_of), d: view.Detect = Depends(detect_of), c: Caller = Depends(approved)):
    return view.at(_area(key), v, d, lat, lon, c.user, cache_dir())


@app.get("/api/areas/{key}/verdicts")
def verdict_layer(key: str, c: Caller = Depends(approved)):
    """Validated places as GeoJSON, coloured by their latest verdict."""
    return view.verdict_layer(_area(key), c.user)


@app.get("/api/place")
def place_name(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
               _: Caller = Depends(approved)):
    """A place's name (OpenStreetMap, cached; at most one new lookup a second)."""
    return {"name": view.places.lookup(cache_dir(), lat, lon)}


# ---- photos ----

class PhotosIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    dateBefore: str
    dateAfter: str
    source: str = "s2"                # "s2" | "highres"
    cloudPct: Optional[float] = Field(None, ge=0, le=100)
    windowDays: Optional[int] = Field(None, ge=1, le=365)


@app.post("/api/areas/{key}/photos")
def photos_start(key: str, body: PhotosIn, c: Caller = Depends(approved)):
    a = _area(key)
    try:
        before, after = (str(view.np.datetime64(x[:10], "D")) for x in (body.dateBefore, body.dateAfter))
    except ValueError:
        raise HTTPException(status_code=400, detail="Check the dates.")
    if body.source == "highres":
        return photo_jobs.start_highres(c.uid, body.lat, body.lon, before, after, cache_dir())
    if body.source != "s2":
        raise HTTPException(status_code=400, detail="Unknown photo source.")
    cell = view.cell_index(a.cube, body.lon, body.lat)
    if cell is None:
        raise HTTPException(status_code=400, detail="That point is outside this area.")
    return photo_jobs.start_s2(c.uid, a, cell, before, after, cache_dir(), body.cloudPct, body.windowDays)


@app.get("/api/photos/{job_id}")
def photos_job(job_id: str, c: Caller = Depends(approved)):
    job = photo_jobs.get(job_id, c.uid)
    if job is None:
        raise HTTPException(status_code=404, detail="No such photo request (they are kept 30 minutes).")
    return job


# ---- verdicts ----

class VerdictIn(BaseModel):
    target: dict
    verdict: str
    evidence: List[str] = []
    link: str = Field("", max_length=1000)
    eventDate: str = Field("", max_length=100)
    confidence: str = ""
    note: str = Field("", max_length=4000)


@app.post("/api/areas/{key}/verdicts")
def verdict_record(key: str, body: VerdictIn, c: Caller = Depends(approved)):
    a = _area(key)
    try:
        target = view.target_from(a, body.target)
        rid = validation.record(c.user, a.cube, target, cube_key=a.entry.key, verdict=body.verdict,
                                evidence=body.evidence, link=body.link, event_date=body.eventDate,
                                confidence=body.confidence, note=body.note)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e) or "Check the verdict.")
    except accounts.NotAllowed as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"id": rid, "verdicts": [view.record_json(r, c.user.id) for r in
                                    validation.matching(view.records(a, c.user), target)]}


class VetoIn(BaseModel):
    reason: str = Field("", max_length=1000)


@app.post("/api/verdicts/{record_id}/veto")
def verdict_veto(record_id: int, body: VetoIn, c: Caller = Depends(approved)):
    try:
        validation.veto(c.user, record_id, body.reason)
    except accounts.NotAllowed as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"id": record_id, "status": "vetoed"}


@app.post("/api/verdicts/{record_id}/undo-veto")
def verdict_undo_veto(record_id: int, c: Caller = Depends(approved)):
    try:
        validation.undo_veto(c.user, record_id)
    except accounts.NotAllowed as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"id": record_id, "status": "active"}


@app.get("/api/verdict-options")
def verdict_options(_: Caller = Depends(approved)):
    """The choices a verdict offers, in the old app's words."""
    return {"verdicts": [{"key": k, "label": v, "colour": validation.COLOURS[k]} for k, v in validation.VERDICTS.items()],
            "evidence": [{"key": k, "label": v} for k, v in validation.EVIDENCE.items()],
            "confidence": list(validation.CONFIDENCE)}


# ---- users (the tool's admins) ----

@app.get("/api/users")
def users(_: Caller = Depends(admin)):
    keep = ("id", "email", "name", "affiliation", "reason", "role", "created_at", "last_login")
    return [{k: u[k] for k in keep} for u in accounts.list_users()]


class RoleIn(BaseModel):
    role: str


@app.put("/api/users/{user_id}/role")
def set_role(user_id: int, body: RoleIn, c: Caller = Depends(admin)):
    if accounts.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="No such user.")
    try:
        accounts.set_role(c.user, user_id, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except accounts.NotAllowed as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"id": user_id, "role": body.role}
