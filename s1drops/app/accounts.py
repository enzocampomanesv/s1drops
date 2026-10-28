"""User accounts, roles and sign-in sessions (SQLite).

Roles, least to most trusted:
  pending    signed up (or signed in with Google) but not yet approved
  validator  can record verdicts on changes and see everyone's
  reviewer   validator + can veto any verdict, and export
  admin      reviewer + manages accounts, bakes cubes, imports reference data
  disabled   account switched off; cannot sign in

Accounts are keyed by email, so one person can use Google sign-in and/or a
password on the same account. New accounts start as `pending` unless they come
from an admin's invite link (which carries a role) or their email is listed in
S1DROPS_ADMIN_EMAILS (bootstrap: the first admins).

Secrets never sit in the database in the clear: passwords are scrypt hashes and
session/invite/reset tokens are stored as SHA-256 digests, so a copy of the
database can't be used to sign in. The database lives at S1DROPS_DB_PATH
(default ./app.db), outside the cube cache.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

ROLES = ("pending", "validator", "reviewer", "admin", "disabled")
APPROVED = ("validator", "reviewer", "admin")
ROLE_LABELS = {
    "pending": "Waiting for approval", "validator": "Validator", "reviewer": "Reviewer",
    "admin": "Admin", "disabled": "Disabled",
}

SESSION_COOKIE = "s1drops_session"
SESSION_DAYS = 30
INVITE_DAYS = 7
RESET_DAYS = 2
MIN_PASSWORD = 10

# Failed password sign-ins: after MAX_FAILS within FAIL_WINDOW_S, an email is
# locked out for the rest of the window (in memory: resets on restart).
MAX_FAILS = 5
FAIL_WINDOW_S = 15 * 60
_fails: Dict[str, List[float]] = {}
_fails_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    affiliation TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'pending',
    password_hash TEXT,
    google_sub TEXT UNIQUE,
    created_at REAL NOT NULL,
    approved_by INTEGER,
    approved_at REAL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- session | invite | reset
    user_id INTEGER,
    email TEXT,
    role TEXT,
    created_by INTEGER,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
"""


def db_path() -> str:
    return os.environ.get("S1DROPS_DB_PATH", "./app.db")


_init_lock = threading.Lock()
_initialised: set = set()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A short-lived connection (one per call keeps threads independent)."""
    path = db_path()
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        with _init_lock:
            if path not in _initialised:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
                from . import validation          # its tables share the database
                con.executescript(validation.SCHEMA)
                _initialised.add(path)
        yield con
        con.commit()
    finally:
        con.close()


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    role: str
    affiliation: str = ""

    @property
    def approved(self) -> bool:
        return self.role in APPROVED

    @property
    def can_review(self) -> bool:
        return self.role in ("reviewer", "admin")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def display(self) -> str:
        return self.name or self.email


def _user(row) -> Optional[User]:
    if row is None:
        return None
    return User(row["id"], row["email"], row["name"], row["role"], row["affiliation"])


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def admin_emails() -> set:
    raw = os.environ.get("S1DROPS_ADMIN_EMAILS", "")
    return {normalise_email(e) for e in raw.replace(";", ",").split(",") if e.strip()}


# ---- passwords ---------------------------------------------------------------

_SCRYPT = dict(n=2 ** 14, r=8, p=1)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not password:
        return False
    try:
        _algo, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=32,
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def password_problem(password: str) -> Optional[str]:
    if len(password or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return None


# ---- tokens ------------------------------------------------------------------

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token(con, kind: str, *, days: float, user_id=None, email=None, role=None,
               created_by=None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    con.execute(
        "INSERT INTO tokens (token_hash, kind, user_id, email, role, created_by, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_digest(token), kind, user_id, email, role, created_by, now, now + days * 86400))
    return token


def _live_token(con, token: str, kind: str):
    row = con.execute("SELECT * FROM tokens WHERE token_hash = ? AND kind = ?",
                      (_digest(token or ""), kind)).fetchone()
    if row is None or row["used_at"] is not None or row["expires_at"] < time.time():
        return None
    return row


def _use_token(con, token: str) -> None:
    con.execute("UPDATE tokens SET used_at = ? WHERE token_hash = ?",
                (time.time(), _digest(token)))


# ---- sessions ----------------------------------------------------------------

def start_session(user_id: int) -> str:
    """A new sign-in session; the returned token goes in the session cookie."""
    with connect() as con:
        con.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), user_id))
        return _new_token(con, "session", days=SESSION_DAYS, user_id=user_id)


def end_session(token: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM tokens WHERE token_hash = ? AND kind = 'session'",
                    (_digest(token or ""),))


def user_for_session(token: Optional[str]) -> Optional[User]:
    """The signed-in user behind a session cookie, or None (disabled users too)."""
    if not token:
        return None
    with connect() as con:
        row = _live_token(con, token, "session")
        if row is None:
            return None
        user = _user(con.execute("SELECT * FROM users WHERE id = ?",
                                 (row["user_id"],)).fetchone())
    return user if user and user.role != "disabled" else None


def user_from_cookies(cookies: Optional[Dict[str, str]]) -> Optional[User]:
    return user_for_session((cookies or {}).get(SESSION_COOKIE))


# ---- accounts ----------------------------------------------------------------

def get_user(user_id: int) -> Optional[User]:
    with connect() as con:
        return _user(con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def find_by_email(email: str) -> Optional[User]:
    with connect() as con:
        return _user(con.execute("SELECT * FROM users WHERE email = ?",
                                 (normalise_email(email),)).fetchone())


def _initial_role(email: str, role: Optional[str] = None) -> str:
    return "admin" if normalise_email(email) in admin_emails() else (role or "pending")


def sign_up(email: str, name: str, password: str, affiliation: str = "",
            reason: str = "") -> User:
    """A self-service account, pending until an admin approves it."""
    email = normalise_email(email)
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    if "@" not in email:
        raise ValueError("Enter a valid email address.")
    with connect() as con:
        if con.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise ValueError("An account with this email already exists. Sign in instead.")
        cur = con.execute(
            "INSERT INTO users (email, name, affiliation, reason, role, password_hash, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, name.strip(), affiliation.strip(), reason.strip(), _initial_role(email),
             hash_password(password), time.time()))
        return _user(con.execute("SELECT * FROM users WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())


def _too_many_fails(email: str) -> bool:
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(email, []) if now - t < FAIL_WINDOW_S]
        _fails[email] = recent
        return len(recent) >= MAX_FAILS


def _note_fail(email: str) -> None:
    with _fails_lock:
        _fails.setdefault(email, []).append(time.time())


def password_sign_in(email: str, password: str) -> User:
    email = normalise_email(email)
    if _too_many_fails(email):
        raise ValueError("Too many failed attempts. Try again in 15 minutes.")
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        _note_fail(email)
        raise ValueError("Wrong email or password.")
    if row["role"] == "disabled":
        raise ValueError("This account has been disabled.")
    return _user(row)


def google_sign_in(sub: str, email: str, name: str) -> User:
    """The account for a verified Google identity: matched by Google ID, then by
    email (linking it), else created pending (or with a waiting invite's role)."""
    email = normalise_email(email)
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE google_sub = ?", (sub,)).fetchone()
        if row is None:
            row = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row is not None:
                con.execute("UPDATE users SET google_sub = ? WHERE id = ?", (sub, row["id"]))
            else:
                invite = con.execute(
                    "SELECT * FROM tokens WHERE kind = 'invite' AND email = ? AND used_at "
                    "IS NULL AND expires_at > ? ORDER BY created_at DESC",
                    (email, time.time())).fetchone()
                role = _initial_role(email, invite["role"] if invite else None)
                cur = con.execute(
                    "INSERT INTO users (email, name, role, google_sub, created_at, "
                    "approved_by, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (email, name.strip(), role, sub, time.time(),
                     invite["created_by"] if invite else None,
                     time.time() if role != "pending" else None))
                if invite:
                    con.execute("UPDATE tokens SET used_at = ? WHERE token_hash = ?",
                                (time.time(), invite["token_hash"]))
                row = con.execute("SELECT * FROM users WHERE id = ?",
                                  (cur.lastrowid,)).fetchone()
        else:
            row = con.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    if row["role"] == "disabled":
        raise ValueError("This account has been disabled.")
    return _user(row)


def create_invite(admin: User, email: str, role: str) -> str:
    """A one-time link token that creates (or upgrades) an account with `role`."""
    _require_admin(admin)
    if role not in APPROVED:
        raise ValueError("Invites must give an approved role.")
    email = normalise_email(email)
    if "@" not in email:
        raise ValueError("Enter a valid email address.")
    with connect() as con:
        return _new_token(con, "invite", days=INVITE_DAYS, email=email, role=role,
                          created_by=admin.id)


def invite_details(token: str) -> Optional[Dict]:
    with connect() as con:
        row = _live_token(con, token, "invite")
        return {"email": row["email"], "role": row["role"]} if row else None


def accept_invite(token: str, name: str, password: str) -> User:
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    with connect() as con:
        row = _live_token(con, token, "invite")
        if row is None:
            raise ValueError("This invite link has expired or was already used.")
        email, role = row["email"], _initial_role(row["email"], row["role"])
        existing = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        now = time.time()
        if existing:
            con.execute(
                "UPDATE users SET name = COALESCE(NULLIF(?, ''), name), password_hash = ?, "
                "role = ?, approved_by = ?, approved_at = ? WHERE id = ?",
                (name.strip(), hash_password(password), role, row["created_by"], now,
                 existing["id"]))
            uid = existing["id"]
        else:
            uid = con.execute(
                "INSERT INTO users (email, name, role, password_hash, created_at, "
                "approved_by, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, name.strip(), role, hash_password(password), now, row["created_by"],
                 now)).lastrowid
        _use_token(con, token)
        return _user(con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def create_reset(admin: User, user_id: int) -> str:
    """A one-time link token to set a new password (no email server: the admin
    passes the link on)."""
    _require_admin(admin)
    with connect() as con:
        return _new_token(con, "reset", days=RESET_DAYS, user_id=user_id, created_by=admin.id)


def reset_details(token: str) -> Optional[User]:
    with connect() as con:
        row = _live_token(con, token, "reset")
        if row is None:
            return None
        return _user(con.execute("SELECT * FROM users WHERE id = ?",
                                 (row["user_id"],)).fetchone())


def accept_reset(token: str, password: str) -> User:
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    with connect() as con:
        row = _live_token(con, token, "reset")
        if row is None:
            raise ValueError("This reset link has expired or was already used.")
        con.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                    (hash_password(password), row["user_id"]))
        # A new password signs out every existing session.
        con.execute("DELETE FROM tokens WHERE kind = 'session' AND user_id = ?",
                    (row["user_id"],))
        _use_token(con, token)
        return _user(con.execute("SELECT * FROM users WHERE id = ?",
                                 (row["user_id"],)).fetchone())


# ---- administration ----------------------------------------------------------

class NotAllowed(PermissionError):
    pass


def _require_admin(user: Optional[User]) -> None:
    # Re-read the role: a session's User can be stale after a demotion.
    fresh = get_user(user.id) if user else None
    if fresh is None or not fresh.is_admin:
        raise NotAllowed("Only admins can do this.")


def list_users() -> List[Dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT id, email, name, affiliation, reason, role, created_at, last_login, "
            "password_hash IS NOT NULL AS has_password, google_sub IS NOT NULL AS has_google "
            "FROM users ORDER BY role = 'pending' DESC, created_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_role(admin: User, user_id: int, role: str) -> None:
    _require_admin(admin)
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}.")
    if user_id == admin.id and role != "admin":
        raise ValueError("You can't remove your own admin role.")
    with connect() as con:
        con.execute("UPDATE users SET role = ?, approved_by = ?, approved_at = ? WHERE id = ?",
                    (role, admin.id, time.time(), user_id))
        if role == "disabled":
            con.execute("DELETE FROM tokens WHERE kind = 'session' AND user_id = ?",
                        (user_id,))
