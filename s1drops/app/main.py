"""Solara entrypoint — Explore (public) and, for signed-in admins, an Admin tab.

Run (the server module adds the sign-in pages in front of Solara):
    export S1DROPS_ADMIN_EMAILS='you@example.org'   # these accounts get the Admin tab
    export S1DROPS_CACHE_DIR='./cache'              # optional; defaults to ./cache
    uvicorn s1drops.app.server:app --port 8765
"""
from __future__ import annotations

import os

import solara
import solara.server.settings

from . import accounts, admin, explore, feedback, layout, state, tour

CACHE_DIR = os.environ.get("S1DROPS_CACHE_DIR", "./cache")

# Dark by default. The page template reads this on every request; a visitor's own
# choice from the toggle is kept in their browser and wins over it.
solara.server.settings.theme.variant = "dark"


@solara.component
def Page():
    solara.Title("Rapid change detection tool")
    solara.Style(layout.CSS)
    layout.Resizer.element()
    # The toggle starts from the server-side theme state, which is unset until the
    # browser reports its theme; seed it so the first click switches to light.
    dark = bool(solara.lab.use_dark_effective())
    if solara.lab.theme.dark is None:
        solara.lab.theme.dark = dark
    # Render the active tab's content conditionally (not as hidden Tabs panels):
    # the Explore map must mount into a full-size container, otherwise Leaflet
    # caches a zero/stale size while hidden and paints in the corner on return.
    # The tabs sit in the app bar's extension row (Solara puts Tabs there) so the
    # sidebar, which is clipped below the app bar, can never cover them. White
    # with a rule underneath, so they read as their own row, not part of the title.
    # Who is signed in comes from the session cookie the /auth pages set. Signing
    # in or out reloads the page, so reading it once per session is enough.
    token = (solara.lab.cookies.value or {}).get(accounts.SESSION_COOKIE)
    user = solara.use_memo(lambda: accounts.user_for_session(token), [token])

    def _share_user():
        state.user.set(user)
        if user is not None and user.is_admin:
            state.is_admin.set(True)
    solara.use_effect(_share_user, [user])

    # Only signed-in admins get the Admin tab; everyone else sees Explore alone.
    show_admin = user is not None and user.is_admin
    tab = solara.use_reactive(0)
    tour_run = solara.use_reactive(0)          # bumped by the Tutorial button
    tour.Tour.element(steps=tour.steps_for(user), run=tour_run.value)
    with solara.AppBar():
        solara.Button("Tutorial", icon_name="mdi-help-circle-outline", text=True,
                      dark=True, classes=["s1-tour-help", "s1-compact"],
                      style={"text-transform": "none"},
                      on_click=lambda: tour_run.set(tour_run.value + 1))
        FeedbackButton(user)
        UserMenu(user)
        solara.lab.ThemeToggle(enable_auto=False)
        if show_admin:
            with solara.lab.Tabs(value=tab, align="left",
                                 background_color="#1e1e1e" if dark else "white",
                                 color="primary", slider_color="primary"):
                solara.lab.Tab("Explore")
                solara.lab.Tab("Admin")
    if tab.value == 0 or not show_admin:
        explore.Explore(CACHE_DIR)
    else:
        admin.Admin(CACHE_DIR)


@solara.component
def FeedbackButton(user):
    """Feedback in the title bar: a short form saved for the admins to read."""
    open_ = solara.use_reactive(False)
    kind = solara.use_reactive("bug")
    message = solara.use_reactive("")
    email = solara.use_reactive("")
    status = solara.use_reactive(("", ""))      # (kind, text)
    sent = solara.use_reactive(0)

    def send():
        if sent.value >= feedback.PER_SESSION:
            status.set(("error", "Thanks! You've sent several messages already; please "
                                 "try again later."))
            return
        try:
            feedback.send(user, kind.value, message.value, email.value,
                          state.page_link.value)
        except ValueError as e:
            status.set(("error", str(e)))
            return
        sent.set(sent.value + 1)
        message.set("")
        status.set(("ok", "Sent. Thank you!"))

    labels = {v: k for k, v in feedback.KINDS.items()}
    btn = solara.v.Btn(text=True, dark=True, class_="s1-compact", v_on="fb.on",
                       style_="text-transform:none",
                       children=[solara.v.Icon(left=True, children=["mdi-message-text-outline"]),
                                 "Feedback"])
    with solara.v.Dialog(v_model=open_.value, on_v_model=open_.set, max_width=480,
                         v_slots=[{"name": "activator", "variable": "fb", "children": [btn]}]):
        with solara.Card("Send feedback"):
            solara.Markdown("Tell us about a problem, an idea or a question. It goes to "
                            "the team running this site, with a link to what you're "
                            "looking at now.")
            solara.Select("What is it about?", value=feedback.KINDS[kind.value],
                          values=list(labels), on_value=lambda v: kind.set(labels[v]))
            solara.InputTextArea("Message", value=message, rows=5, continuous_update=True)
            if user is None:
                solara.InputText("Your email, if you'd like an answer (optional)",
                                 value=email)
            k, text = status.value
            if k == "error":
                solara.Error(text)
            elif k == "ok":
                solara.Success(text)
            with solara.Row(justify="end"):
                solara.Button("Close", text=True,
                              on_click=lambda: (open_.set(False), status.set(("", ""))))
                solara.Button("Send", color="primary", on_click=send,
                              disabled=not message.value.strip())


@solara.component
def UserMenu(user):
    """Sign in, or who is signed in (with their role) and Sign out. Plain links:
    signing in and out happen on the /auth pages, which set the cookie."""
    link = dict(text=True, dark=True, style_="text-transform:none")
    if user is None:
        solara.v.Btn(href="/auth/login", class_="s1-compact", children=[
            solara.v.Icon(left=True, children=["mdi-account-circle-outline"]), "Sign in"],
            **link)
        return
    with solara.Row(style={"align-items": "center", "background": "transparent"}, gap="0px"):
        solara.v.Html(tag="span", class_="s1-hide-phone",
                      style_="font-size:14px;margin-right:4px;white-space:nowrap",
                      children=[f"{user.display} · {accounts.ROLE_LABELS[user.role]}"])
        solara.v.Btn(href="/auth/logout", class_="s1-compact", attributes={"title": "Sign out"},
                     children=[solara.v.Icon(left=True, children=["mdi-logout"]), "Sign out"],
                     **link)


@solara.component
def Layout(children=[]):
    # Sidebar closed at first: the area picker and the list of changes on the right
    # are where to start; the sidebar's settings are for fine-tuning.
    return solara.AppLayout(children=children, sidebar_open=False)
