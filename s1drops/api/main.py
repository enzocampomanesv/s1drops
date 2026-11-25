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
from typing import List

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..app import accounts
from ..cube import list_cubes
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
