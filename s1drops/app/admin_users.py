"""Admin tab sections for accounts and reference data (signed-in admins only).

Every action re-checks the admin role in the database (accounts/validation
functions do), so reaching these components is not enough to change anything.
"""
from __future__ import annotations

import os
import time

import solara

from . import accounts, state, validation


def _link(path: str) -> str:
    base = os.environ.get("S1DROPS_PUBLIC_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


def _ago(ts) -> str:
    if not ts:
        return "never"
    return time.strftime("%d %b %Y", time.gmtime(ts))


@solara.component
def UserRow(u, admin, on_change):
    error = solara.use_reactive("")
    reset_link = solara.use_reactive("")
    labels = {accounts.ROLE_LABELS[r]: r for r in accounts.ROLES}

    def set_role(label):
        try:
            accounts.set_role(admin, u["id"], labels[label])
            error.set("")
            on_change()
        except (accounts.NotAllowed, ValueError) as e:
            error.set(str(e))

    def make_reset():
        try:
            reset_link.set(_link(f"/auth/reset/{accounts.create_reset(admin, u['id'])}"))
        except accounts.NotAllowed as e:
            error.set(str(e))

    methods = " + ".join(m for m, on in (("password", u["has_password"]),
                                         ("Google", u["has_google"])) if on) or "none"
    with solara.Column(gap="2px", style={"padding": "8px 0",
                                         "border-bottom": "1px solid rgba(128,128,128,.25)"}):
        with solara.Row(style={"align-items": "center", "flex-wrap": "wrap"}):
            solara.v.Html(tag="div", style_="flex:1 1 260px;font-size:14px", children=[
                solara.v.Html(tag="b", children=[u["name"] or u["email"]]),
                f" · {u['email']}" if u["name"] else "",
                solara.v.Html(tag="div", style_="opacity:0.75", children=[
                    (f"{u['affiliation']} · " if u["affiliation"] else "")
                    + f"signs in with {methods} · joined {_ago(u['created_at'])} · "
                    f"last sign-in {_ago(u['last_login'])}"]),
                solara.v.Html(tag="div", style_="opacity:0.75;white-space:pre-wrap",
                              children=[f"“{u['reason']}”"]) if u["reason"] else "",
            ])
            solara.Select("Role", values=list(labels), value=accounts.ROLE_LABELS[u["role"]],
                          on_value=set_role, style={"max-width": "200px"})
            if u["has_password"]:
                solara.Button("Reset link", text=True, small=True, on_click=make_reset)
        if reset_link.value:
            solara.Markdown("Send this password-reset link to them (valid "
                            f"{accounts.RESET_DAYS} days, works once):")
            solara.v.Html(tag="code", children=[reset_link.value],
                          style_="word-break:break-all")
        if error.value:
            solara.Error(error.value)


@solara.component
def ManageUsers():
    admin = state.user.value
    version = solara.use_reactive(0)
    email = solara.use_reactive("")
    role_label = solara.use_reactive(accounts.ROLE_LABELS["validator"])
    invite_link = solara.use_reactive("")
    error = solara.use_reactive("")
    users = solara.use_memo(accounts.list_users, [version.value])

    solara.Markdown("### Accounts")
    pending = [u for u in users if u["role"] == "pending"]
    if pending:
        solara.Info(f"{len(pending)} account request{'s' if len(pending) != 1 else ''} "
                    "waiting for approval (listed first). Set a role to approve one.")

    with solara.Card("Invite someone"):
        solara.Markdown("_Creates a one-time link (valid "
                        f"{accounts.INVITE_DAYS} days) for that email, with the role "
                        "already approved. Send it to them yourself; they set a password "
                        "or use Google with the same email._")
        approved = {accounts.ROLE_LABELS[r]: r for r in accounts.APPROVED}
        with solara.Row(style={"flex-wrap": "wrap", "align-items": "center"}):
            solara.InputText("Email", value=email)
            solara.Select("Role", values=list(approved), value=role_label)

        def make_invite():
            try:
                token = accounts.create_invite(admin, email.value, approved[role_label.value])
                invite_link.set(_link(f"/auth/invite/{token}"))
                error.set("")
            except (accounts.NotAllowed, ValueError) as e:
                error.set(str(e))
        solara.Button("Create invite link", color="primary", on_click=make_invite,
                      disabled=not email.value)
        if invite_link.value:
            solara.v.Html(tag="code", children=[invite_link.value],
                          style_="word-break:break-all")
        if error.value:
            solara.Error(error.value)

    for u in users:
        UserRow(u, admin, lambda: version.set(version.value + 1)).key(f"user-{u['id']}")


@solara.component
def ReferenceData():
    admin = state.user.value
    version = solara.use_reactive(0)
    upload = solara.use_reactive(None)      # (filename, text)
    batch = solara.use_reactive("")
    year_field = solara.use_reactive("demo_year")
    name_field = solara.use_reactive("Name")
    message = solara.use_reactive(("", ""))
    batches = solara.use_memo(lambda: validation.reference_batches(admin), [version.value])

    solara.Markdown("### Reference demolitions (admins only)")
    solara.Markdown(
        "_Known demolitions to score the detector against. They are stored with the "
        "verdicts but only admins ever see them: a “ref” badge in Strongest changes and "
        "a reference check under the list. They are never drawn on the map. Upload "
        "polygons in lon/lat (GeoJSON) with a year per polygon._")

    def on_file(f):
        upload.set((f["name"], f["data"].decode("utf-8", "replace")))
        batch.set(f["name"].rsplit(".", 1)[0])

    solara.FileDrop(label="Drop a GeoJSON file here", on_file=on_file, lazy=False)
    if upload.value:
        with solara.Row(style={"flex-wrap": "wrap"}):
            solara.InputText("Name for this batch", value=batch)
            solara.InputText("Year field", value=year_field)
            solara.InputText("Name field", value=name_field)

        def do_import():
            try:
                n = validation.import_reference(admin, upload.value[1],
                                                batch.value or upload.value[0],
                                                year_field=year_field.value,
                                                name_field=name_field.value)
                message.set(("ok", f"Imported {n} polygons."))
                upload.set(None)
                version.set(version.value + 1)
                state.validation_version.set(state.validation_version.value + 1)
            except (accounts.NotAllowed, ValueError) as e:
                message.set(("error", str(e)))
        solara.Button(f"Import {upload.value[0]}", color="primary", on_click=do_import)
    kind, text = message.value
    if kind:
        (solara.Success if kind == "ok" else solara.Error)(text)

    for b in batches:
        with solara.Row(style={"align-items": "center"}):
            years = f"{b['y0']}–{b['y1']}" if b["y0"] != b["y1"] else f"{b['y0']}"
            solara.Text(f"{b['ref_batch']}: {b['n']} polygons, {years}")

            def remove(name=b["ref_batch"]):
                validation.delete_reference_batch(admin, name)
                version.set(version.value + 1)
                state.validation_version.set(state.validation_version.value + 1)
            solara.Button("Delete", text=True, small=True, color="error", on_click=remove)
