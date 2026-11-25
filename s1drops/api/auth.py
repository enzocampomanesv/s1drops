"""Who is calling: a Space4All (Firebase) sign-in, checked here, and the tool's
own account and role for it (s1drops/app/accounts.py).

The page sends the user's Firebase ID token as `Authorization: Bearer <token>`.
It is a JWT signed by Google: checked against Google's published certificates
(fetched and kept until they expire), for the Space4All project
(S1DROPS_FIREBASE_PROJECT). No service account is needed for that.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests
from fastapi import Depends, Header, HTTPException
from google.auth import jwt

from ..app import accounts

CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"

_certs: Dict[str, object] = {"until": 0.0, "certs": {}}
_certs_lock = threading.Lock()


def project() -> str:
    return os.environ.get("S1DROPS_FIREBASE_PROJECT", "livability-mapping-prod")


def _google_certs() -> Dict[str, str]:
    with _certs_lock:
        if time.time() < float(_certs["until"]):
            return _certs["certs"]  # type: ignore[return-value]
        r = requests.get(CERTS_URL, timeout=10)
        r.raise_for_status()
        max_age = 3600
        for part in r.headers.get("Cache-Control", "").split(","):
            if part.strip().startswith("max-age="):
                max_age = int(part.strip()[8:])
        _certs.update(until=time.time() + max_age, certs=r.json())
        return _certs["certs"]  # type: ignore[return-value]


def decode_token(token: str) -> dict:
    """The token's claims, or ValueError if it isn't a valid Space4All sign-in."""
    claims = jwt.decode(token, certs=_google_certs(), audience=project())
    if claims.get("iss") != f"https://securetoken.google.com/{project()}" or not claims.get("sub"):
        raise ValueError("not a Space4All sign-in")
    return claims


@dataclass(frozen=True)
class Caller:
    uid: str
    email: str
    email_verified: bool
    user: Optional[accounts.User]   # None: no account in the tool yet

    @property
    def approved(self) -> bool:
        return self.user is not None and self.user.approved


def caller(authorization: str = Header("")) -> Caller:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in with your Space4All account.")
    try:
        claims = decode_token(authorization.split(" ", 1)[1].strip())
    except Exception:  # noqa: BLE001 -- any failure is the same answer
        raise HTTPException(status_code=401, detail="Your sign-in has expired; sign in again.")
    email = claims.get("email", "")
    verified = bool(claims.get("email_verified"))
    return Caller(claims["sub"], accounts.normalise_email(email), verified,
                  accounts.space4all_user(claims["sub"], email, verified))


def approved(c: Caller = Depends(caller)) -> Caller:
    """Only approved users (validator, reviewer, admin) see anything of the tool."""
    if not c.approved:
        raise HTTPException(status_code=403, detail="You don't have access to this tool yet.")
    return c


def admin(c: Caller = Depends(approved)) -> Caller:
    if not c.user.is_admin:
        raise HTTPException(status_code=403, detail="Only the tool's admins can do this.")
    return c
