"""Sign-in pages and Google sign-in, as plain web routes beside the Solara app.

They live outside Solara because signing in has to set an HttpOnly cookie,
which only an HTTP response can do (Solara talks over a websocket and can only
read cookies). The app then reads the session cookie to know who is signed in.

Routes:
  /auth/login            email + password, or "Sign in with Google"
  /auth/signup           request an account (pending until an admin approves)
  /auth/invite/{token}   accept an admin's invite: set a password (or use Google)
  /auth/reset/{token}    set a new password from an admin's reset link
  /auth/google           start Google sign-in; /auth/google/callback finishes it
  /auth/logout

Google sign-in is on when GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set. The
callback URL Google must know is <S1DROPS_PUBLIC_URL>/auth/google/callback.
Forms carry a CSRF token that must match a cookie (double-submit).
"""
from __future__ import annotations

import hmac
import html
import os
import secrets
from typing import Optional
from urllib.parse import parse_qs, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import accounts

CSRF_COOKIE = "s1drops_csrf"
STATE_COOKIE = "s1drops_oauth_state"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def google_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def _base_url(request: Request) -> str:
    return (os.environ.get("S1DROPS_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def _secure(request: Request) -> bool:
    return _base_url(request).startswith("https://")


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


# ---- page rendering ----------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; --bg:#f5f5f5; --card:#fff; --fg:#222; --muted:#666;
        --line:#ccc; --accent:#1976d2; --err:#c62828; --ok:#2e7d32; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#121212; --card:#1e1e1e; --fg:#eee; --muted:#aaa; --line:#444;
          --accent:#64b5f6; --err:#ef9a9a; --ok:#a5d6a7; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 Roboto, system-ui, -apple-system, Segoe UI, sans-serif; }
main { max-width:420px; margin:48px auto; padding:0 16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:24px; }
h1 { font-size:20px; margin:0 0 4px; } p.sub { color:var(--muted); margin:0 0 20px; }
label { display:block; font-size:13px; color:var(--muted); margin:12px 0 4px; }
input, textarea { width:100%; padding:9px 10px; border:1px solid var(--line); border-radius:4px;
       background:transparent; color:var(--fg); font:inherit; }
textarea { min-height:70px; }
button, .btn { display:block; width:100%; margin-top:18px; padding:10px; border:0;
       border-radius:4px; background:var(--accent); color:#fff; font:inherit;
       font-weight:600; cursor:pointer; text-align:center; text-decoration:none; }
.btn.google { background:transparent; color:var(--fg); border:1px solid var(--line); }
.or { text-align:center; color:var(--muted); margin:18px 0 0; font-size:13px; }
.err { color:var(--err); margin:12px 0 0; } .ok { color:var(--ok); margin:12px 0 0; }
.links { margin-top:18px; font-size:14px; text-align:center; }
a { color:var(--accent); }
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)} · Rapid change detection tool</title><style>{_CSS}</style>"
        f"</head><body><main><div class='card'>{body}</div>"
        f"<p class='links'><a href='/'>Back to the map</a></p></main></body></html>",
        status_code=status)


def _csrf(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)


def _with_csrf(resp: Response, request: Request, token: str) -> Response:
    if request.cookies.get(CSRF_COOKIE) != token:
        resp.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="lax",
                        secure=_secure(request), path="/auth")
    return resp


async def _form(request: Request) -> Optional[dict]:
    """The POSTed form, or None when its CSRF token doesn't match the cookie."""
    # The forms post urlencoded bodies; parsed here so Starlette's form parser
    # (and its python-multipart dependency) isn't needed.
    body = (await request.body())[:64_000].decode("utf-8", "replace")
    form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not hmac.compare_digest(cookie, str(form.get("csrf", ""))):
        return None
    return form


def _field(name, label, kind="text", value="", extra="") -> str:
    if kind == "textarea":
        return (f"<label for='{name}'>{label}</label>"
                f"<textarea id='{name}' name='{name}' {extra}>{_esc(value)}</textarea>")
    return (f"<label for='{name}'>{label}</label>"
            f"<input id='{name}' name='{name}' type='{kind}' value='{_esc(value)}' {extra}>")


def _google_button(label="Sign in with Google") -> str:
    if not google_enabled():
        return ""
    return f"<a class='btn google' href='/auth/google'>{label}</a><p class='or'>or</p>"


def _signed_in(request: Request, user: accounts.User, to: str = "/") -> Response:
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(accounts.SESSION_COOKIE, accounts.start_session(user.id),
                    max_age=accounts.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=_secure(request), path="/")
    return resp


# ---- pages -------------------------------------------------------------------

def _login_form(csrf, email="", error="") -> str:
    return (
        "<h1>Sign in</h1><p class='sub'>Validators, reviewers and admins sign in here. "
        "Browsing the map needs no account.</p>"
        + _google_button()
        + f"<form method='post'><input type='hidden' name='csrf' value='{_esc(csrf)}'>"
        + _field("email", "Email", "email", email, "required autocomplete='email'")
        + _field("password", "Password", "password", "",
                 "required autocomplete='current-password'")
        + (f"<p class='err'>{_esc(error)}</p>" if error else "")
        + "<button type='submit'>Sign in</button></form>"
        "<p class='links'>No account yet? <a href='/auth/signup'>Request one</a><br>"
        "Forgot your password? Ask an admin for a reset link.</p>")


async def login(request: Request) -> Response:
    csrf = _csrf(request)
    if request.method == "POST":
        form = await _form(request)
        if form is None:
            return _with_csrf(_page("Sign in", _login_form(csrf, error="Your session "
                              "expired. Please try again."), 400), request, csrf)
        try:
            user = await run_in_threadpool(accounts.password_sign_in, form.get("email", ""),
                                           form.get("password", ""))
        except ValueError as e:
            return _with_csrf(_page("Sign in", _login_form(csrf, form.get("email", ""),
                                                           str(e)), 400), request, csrf)
        return _signed_in(request, user)
    return _with_csrf(_page("Sign in", _login_form(csrf)), request, csrf)


def _signup_form(csrf, f=None, error="") -> str:
    f = f or {}
    return (
        "<h1>Request an account</h1><p class='sub'>Accounts are for people validating "
        "detected changes. An admin will review your request; you can sign in meanwhile "
        "but can't record verdicts until it's approved.</p>"
        + _google_button("Continue with Google")
        + f"<form method='post'><input type='hidden' name='csrf' value='{_esc(csrf)}'>"
        + _field("name", "Your name", "text", f.get("name"), "required autocomplete='name'")
        + _field("email", "Email", "email", f.get("email"), "required autocomplete='email'")
        + _field("affiliation", "Organisation or community (optional)", "text",
                 f.get("affiliation"))
        + _field("reason", "How you'd like to help (optional)", "textarea", f.get("reason"))
        + _field("password", f"Password (at least {accounts.MIN_PASSWORD} characters)",
                 "password", "", "required autocomplete='new-password'")
        + _field("password2", "Password again", "password", "",
                 "required autocomplete='new-password'")
        + (f"<p class='err'>{_esc(error)}</p>" if error else "")
        + "<button type='submit'>Request account</button></form>"
        "<p class='links'>Already have one? <a href='/auth/login'>Sign in</a></p>")


async def signup(request: Request) -> Response:
    csrf = _csrf(request)
    if request.method == "POST":
        form = await _form(request)
        if form is None:
            return _with_csrf(_page("Request an account", _signup_form(
                csrf, error="Your session expired. Please try again."), 400), request, csrf)
        error = ("The two passwords don't match."
                 if form.get("password") != form.get("password2") else "")
        if not error:
            try:
                user = await run_in_threadpool(
                    accounts.sign_up, form.get("email", ""), form.get("name", ""),
                    form.get("password", ""), form.get("affiliation", ""),
                    form.get("reason", ""))
                return _signed_in(request, user)
            except ValueError as e:
                error = str(e)
        return _with_csrf(_page("Request an account", _signup_form(csrf, form, error), 400),
                          request, csrf)
    return _with_csrf(_page("Request an account", _signup_form(csrf)), request, csrf)


def _invite_form(csrf, details, name="", error="") -> str:
    role = accounts.ROLE_LABELS.get(details["role"], details["role"])
    return (
        f"<h1>You're invited</h1><p class='sub'>Account for <b>{_esc(details['email'])}</b> "
        f"as <b>{_esc(role)}</b>. Set a password, or use Google with this same email.</p>"
        + _google_button("Continue with Google")
        + f"<form method='post'><input type='hidden' name='csrf' value='{_esc(csrf)}'>"
        + _field("name", "Your name", "text", name, "required autocomplete='name'")
        + _field("password", f"Password (at least {accounts.MIN_PASSWORD} characters)",
                 "password", "", "required autocomplete='new-password'")
        + _field("password2", "Password again", "password", "",
                 "required autocomplete='new-password'")
        + (f"<p class='err'>{_esc(error)}</p>" if error else "")
        + "<button type='submit'>Create account</button></form>")


_EXPIRED = ("<h1>Link not valid</h1><p class='sub'>This link has expired or was already "
            "used. Ask an admin for a new one.</p>")


async def invite(request: Request) -> Response:
    token = request.path_params["token"]
    details = await run_in_threadpool(accounts.invite_details, token)
    if details is None:
        return _page("Invite", _EXPIRED, 404)
    csrf = _csrf(request)
    if request.method == "POST":
        form = await _form(request)
        error = "Your session expired. Please try again." if form is None else (
            "The two passwords don't match."
            if form.get("password") != form.get("password2") else "")
        if not error:
            try:
                user = await run_in_threadpool(accounts.accept_invite, token,
                                               form.get("name", ""), form.get("password", ""))
                return _signed_in(request, user)
            except ValueError as e:
                error = str(e)
        return _with_csrf(_page("Invite", _invite_form(csrf, details, (form or {}).get(
            "name", ""), error), 400), request, csrf)
    return _with_csrf(_page("Invite", _invite_form(csrf, details)), request, csrf)


def _reset_form(csrf, user, error="") -> str:
    return (
        f"<h1>Set a new password</h1><p class='sub'>For <b>{_esc(user.email)}</b>. This "
        "signs you out everywhere else.</p>"
        f"<form method='post'><input type='hidden' name='csrf' value='{_esc(csrf)}'>"
        + _field("password", f"New password (at least {accounts.MIN_PASSWORD} characters)",
                 "password", "", "required autocomplete='new-password'")
        + _field("password2", "New password again", "password", "",
                 "required autocomplete='new-password'")
        + (f"<p class='err'>{_esc(error)}</p>" if error else "")
        + "<button type='submit'>Save password</button></form>")


async def reset(request: Request) -> Response:
    token = request.path_params["token"]
    user = await run_in_threadpool(accounts.reset_details, token)
    if user is None:
        return _page("Reset password", _EXPIRED, 404)
    csrf = _csrf(request)
    if request.method == "POST":
        form = await _form(request)
        error = "Your session expired. Please try again." if form is None else (
            "The two passwords don't match."
            if form.get("password") != form.get("password2") else "")
        if not error:
            try:
                user = await run_in_threadpool(accounts.accept_reset, token,
                                               form.get("password", ""))
                return _signed_in(request, user)
            except ValueError as e:
                error = str(e)
        return _with_csrf(_page("Reset password", _reset_form(csrf, user, error), 400),
                          request, csrf)
    return _with_csrf(_page("Reset password", _reset_form(csrf, user)), request, csrf)


async def logout(request: Request) -> Response:
    token = request.cookies.get(accounts.SESSION_COOKIE)
    if token:
        await run_in_threadpool(accounts.end_session, token)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(accounts.SESSION_COOKIE, path="/")
    return resp


# ---- Google ------------------------------------------------------------------

def _callback_url(request: Request) -> str:
    return _base_url(request) + "/auth/google/callback"


async def google_start(request: Request) -> Response:
    if not google_enabled():
        return _page("Sign in", "<h1>Google sign-in is not set up</h1>"
                     "<p class='sub'>Use your email and password instead.</p>", 404)
    state = secrets.token_urlsafe(24)
    query = urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": _callback_url(request),
        "response_type": "code", "scope": "openid email profile", "state": state,
        "prompt": "select_account",
    })
    resp = RedirectResponse(f"{GOOGLE_AUTH}?{query}", status_code=303)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=_secure(request), path="/auth")
    return resp


def _google_identity(code: str, redirect_uri: str) -> dict:
    import requests

    tok = requests.post(GOOGLE_TOKEN, data={
        "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=15)
    tok.raise_for_status()
    info = requests.get(GOOGLE_USERINFO, timeout=15, headers={
        "Authorization": f"Bearer {tok.json()['access_token']}"})
    info.raise_for_status()
    return info.json()


async def google_callback(request: Request) -> Response:
    if not google_enabled():
        return RedirectResponse("/auth/login", status_code=303)
    state, expected = request.query_params.get("state", ""), request.cookies.get(STATE_COOKIE, "")
    code = request.query_params.get("code")
    if not code or not expected or not hmac.compare_digest(state, expected):
        return _page("Sign in", "<h1>Sign-in didn't complete</h1><p class='sub'>Please "
                     "<a href='/auth/login'>try again</a>.</p>", 400)
    try:
        info = await run_in_threadpool(_google_identity, code, _callback_url(request))
        if not info.get("email_verified") or not info.get("email") or not info.get("sub"):
            raise ValueError("Google didn't confirm an email address for this account.")
        user = await run_in_threadpool(accounts.google_sign_in, info["sub"], info["email"],
                                       info.get("name", ""))
    except ValueError as e:
        return _page("Sign in", f"<h1>Can't sign in</h1><p class='err'>{_esc(e)}</p>", 403)
    except Exception:
        return _page("Sign in", "<h1>Google sign-in failed</h1><p class='sub'>Please "
                     "<a href='/auth/login'>try again</a> in a moment.</p>", 502)
    resp = _signed_in(request, user)
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    return resp


routes = [
    Route("/auth/login", login, methods=["GET", "POST"]),
    Route("/auth/signup", signup, methods=["GET", "POST"]),
    Route("/auth/invite/{token}", invite, methods=["GET", "POST"]),
    Route("/auth/reset/{token}", reset, methods=["GET", "POST"]),
    Route("/auth/logout", logout),
    Route("/auth/google", google_start),
    Route("/auth/google/callback", google_callback),
]
