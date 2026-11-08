"""Feedback from anyone using the app: bug reports, ideas and questions.

Stored in the accounts database (no email server is needed) and read by admins in
the Admin tab. Anyone can send it, signed in or not; a signed-in sender is
recorded, a visitor may leave an email address to be answered at. What the
sender was looking at (the page's share link) is attached so a report can be
reproduced.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from . import accounts

KINDS = {"bug": "Something's not working", "idea": "An idea or request",
         "question": "A question", "other": "Something else"}
MAX_LEN = 4000
PER_SESSION = 5     # messages one browser session may send

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',        -- the page the sender was on
    user_id INTEGER REFERENCES users(id),
    created_at REAL NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Message:
    id: int
    kind: str
    message: str
    email: str
    context: str
    sender: str
    created_at: float
    done: bool


def send(user: Optional[accounts.User], kind: str, message: str, email: str = "",
         context: str = "") -> int:
    message = (message or "").strip()
    if not message:
        raise ValueError("Please write a message.")
    if kind not in KINDS:
        raise ValueError("Choose what kind of feedback this is.")
    email = (email or "").strip()[:200]
    if email and ("@" not in email or " " in email):
        raise ValueError("That email address doesn't look right.")
    with accounts.connect() as con:
        return con.execute(
            "INSERT INTO feedback (kind, message, email, context, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, message[:MAX_LEN], email, (context or "")[:500],
             user.id if user else None, time.time())).lastrowid


def inbox(admin: accounts.User, include_done: bool = False) -> List[Message]:
    accounts._require_admin(admin)
    with accounts.connect() as con:
        rows = con.execute(
            "SELECT f.*, COALESCE(NULLIF(u.name, ''), u.email, '') AS sender FROM feedback f "
            "LEFT JOIN users u ON u.id = f.user_id "
            + ("" if include_done else "WHERE f.done = 0 ")
            + "ORDER BY f.created_at DESC").fetchall()
    return [Message(r["id"], r["kind"], r["message"], r["email"], r["context"],
                    r["sender"], r["created_at"], bool(r["done"])) for r in rows]


def mark_done(admin: accounts.User, feedback_id: int, done: bool = True) -> None:
    accounts._require_admin(admin)
    with accounts.connect() as con:
        con.execute("UPDATE feedback SET done = ? WHERE id = ?", (int(done), feedback_id))
