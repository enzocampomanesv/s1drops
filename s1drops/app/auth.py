"""Admin authentication — server-side enforced.

Mirrors the control.html principle (gated login, enforced in the backend, not by
hiding the UI) translated to a non-Firebase stack: privileged actions call
`guard_admin()` before doing any work, and the only way to flip the session to
admin is a constant-time password check against an env var. Reaching the admin
component is therefore not sufficient to bake or delete.

Configure with:  export S1DROPS_ADMIN_PASSWORD='...'
"""
from __future__ import annotations

import hmac
import os

ENV_VAR = "S1DROPS_ADMIN_PASSWORD"


class NotAuthorized(PermissionError):
    pass


def admin_password_configured() -> bool:
    return bool(os.environ.get(ENV_VAR))


def check_password(password: str) -> bool:
    """Constant-time compare against the configured admin password."""
    expected = os.environ.get(ENV_VAR, "")
    if not expected or not password:
        return False
    return hmac.compare_digest(str(password), expected)


def guard_admin(is_admin: bool) -> None:
    """Raise unless the caller is an authenticated admin. Call inside every
    privileged action (bake / extend / delete) for server-side enforcement."""
    if not is_admin:
        raise NotAuthorized("Admin authentication is required for this action.")
