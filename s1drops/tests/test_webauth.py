"""The /auth pages, driven through a minimal in-process ASGI client."""
import asyncio
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import pytest
from starlette.applications import Starlette

from s1drops.app import accounts, webauth

APP = Starlette(routes=webauth.routes)


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S1DROPS_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    accounts._fails.clear()


def call(method, path, cookies=None, form=None):
    """(status, headers, body, set-cookies) for one request."""
    body = urlencode(form).encode() if form is not None else b""
    headers = [(b"host", b"testserver")]
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    if form is not None:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": headers, "scheme": "http",
             "server": ("testserver", 80), "client": ("127.0.0.1", 1), "root_path": "",
             "http_version": "1.1"}
    sent, out = [False], {"status": 0, "headers": [], "body": b""}

    async def receive():
        if sent[0]:
            return {"type": "http.disconnect"}
        sent[0] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"], out["headers"] = msg["status"], msg["headers"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(APP(scope, receive, send))
    jar = {}
    for k, v in out["headers"]:
        if k == b"set-cookie":
            c = SimpleCookie(v.decode())
            jar.update({name: m.value for name, m in c.items()})
    hdrs = {k.decode(): v.decode() for k, v in out["headers"]}
    return out["status"], hdrs, out["body"].decode(), jar


def test_sign_up_then_sign_in_sets_a_session_cookie():
    status, _, body, jar = call("GET", "/auth/signup")
    assert status == 200 and "Request an account" in body
    csrf = jar[webauth.CSRF_COOKIE]
    form = {"csrf": csrf, "name": "Ada <script>", "email": "ada@example.org",
            "affiliation": "", "reason": "", "password": "a long password",
            "password2": "a long password"}
    status, hdrs, _, jar2 = call("POST", "/auth/signup", {webauth.CSRF_COOKIE: csrf}, form)
    assert status == 303 and hdrs["location"] == "/"
    user = accounts.user_for_session(jar2[accounts.SESSION_COOKIE])
    assert user.name == "Ada <script>" and user.role == "pending"

    _, _, _, jar = call("GET", "/auth/login")
    csrf = jar[webauth.CSRF_COOKIE]
    status, _, body, _ = call("POST", "/auth/login", {webauth.CSRF_COOKIE: csrf},
                              {"csrf": csrf, "email": "ada@example.org", "password": "nope"})
    assert status == 400 and "Wrong email or password" in body
    status, _, _, jar = call("POST", "/auth/login", {webauth.CSRF_COOKIE: csrf},
                             {"csrf": csrf, "email": "ada@example.org",
                              "password": "a long password"})
    assert status == 303 and accounts.SESSION_COOKIE in jar


def test_forms_without_a_matching_csrf_token_are_refused():
    accounts.sign_up("ada@example.org", "Ada", "a long password")
    status, _, body, jar = call("POST", "/auth/login", {webauth.CSRF_COOKIE: "a"},
                                {"csrf": "b", "email": "ada@example.org",
                                 "password": "a long password"})
    assert status == 400 and accounts.SESSION_COOKIE not in jar


def test_invite_page_escapes_and_logout_ends_the_session(monkeypatch):
    monkeypatch.setenv("S1DROPS_ADMIN_EMAILS", "boss@example.org")
    admin = accounts.sign_up("boss@example.org", "Boss", "a long password")
    token = accounts.create_invite(admin, "x'><b>@example.org", "validator")
    status, _, body, _ = call("GET", f"/auth/invite/{token}")
    assert status == 200 and "<b>@example" not in body and "&lt;b&gt;" in body
    assert call("GET", "/auth/invite/nonsense")[0] == 404
    session = accounts.start_session(admin.id)
    status, hdrs, _, _ = call("GET", "/auth/logout", {accounts.SESSION_COOKIE: session})
    assert status == 303 and accounts.user_for_session(session) is None


def test_google_routes_are_off_without_credentials():
    assert call("GET", "/auth/google")[0] == 404
    status, _, body, _ = call("GET", "/auth/login")
    assert "Sign in with Google" not in body
