import pytest

from s1drops.app import accounts


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S1DROPS_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("S1DROPS_ADMIN_EMAILS", "boss@example.org")
    accounts._fails.clear()


def _admin():
    return accounts.sign_up("Boss@Example.org", "Boss", "correct horse battery")


def test_passwords_are_hashed_and_checked():
    h = accounts.hash_password("correct horse battery")
    assert "correct" not in h and h.startswith("scrypt$")
    assert accounts.verify_password("correct horse battery", h)
    assert not accounts.verify_password("wrong", h)
    assert not accounts.verify_password("x", None)


def test_sign_up_is_pending_except_bootstrap_admins():
    u = accounts.sign_up("ada@example.org", "Ada", "a long password", "NGO", "help")
    assert u.role == "pending" and not u.approved
    assert _admin().role == "admin"                     # listed in S1DROPS_ADMIN_EMAILS
    with pytest.raises(ValueError, match="already exists"):
        accounts.sign_up("ADA@example.org", "Ada", "a long password")
    with pytest.raises(ValueError, match="at least"):
        accounts.sign_up("bob@example.org", "Bob", "short")


def test_sessions_follow_the_account():
    u = accounts.sign_up("ada@example.org", "Ada", "a long password")
    token = accounts.start_session(u.id)
    assert accounts.user_from_cookies({accounts.SESSION_COOKIE: token}).email == u.email
    assert accounts.user_for_session("forged") is None
    admin = _admin()
    accounts.set_role(admin, u.id, "disabled")            # disabling signs them out
    assert accounts.user_for_session(token) is None
    token = accounts.start_session(admin.id)
    accounts.end_session(token)
    assert accounts.user_for_session(token) is None


def test_password_sign_in_locks_out_after_repeated_failures():
    accounts.sign_up("ada@example.org", "Ada", "a long password")
    assert accounts.password_sign_in(" ADA@example.org ", "a long password").name == "Ada"
    for _ in range(accounts.MAX_FAILS):
        with pytest.raises(ValueError, match="Wrong"):
            accounts.password_sign_in("ada@example.org", "nope")
    with pytest.raises(ValueError, match="Too many"):
        accounts.password_sign_in("ada@example.org", "a long password")


def test_invites_give_an_approved_role_once():
    admin = _admin()
    token = accounts.create_invite(admin, "Cleo@example.org", "reviewer")
    assert accounts.invite_details(token) == {"email": "cleo@example.org", "role": "reviewer"}
    u = accounts.accept_invite(token, "Cleo", "a long password")
    assert u.role == "reviewer" and u.approved
    with pytest.raises(ValueError, match="expired"):
        accounts.accept_invite(token, "Cleo", "a long password")
    pending = accounts.sign_up("dan@example.org", "Dan", "a long password")
    with pytest.raises(accounts.NotAllowed):
        accounts.create_invite(pending, "x@example.org", "admin")
    with pytest.raises(ValueError):
        accounts.create_invite(admin, "x@example.org", "pending")


def test_google_sign_in_links_by_email_and_honours_invites():
    ada = accounts.sign_up("ada@example.org", "Ada", "a long password")
    assert accounts.google_sign_in("g-1", "ada@example.org", "Ada L").id == ada.id
    assert accounts.google_sign_in("g-1", "renamed@example.org", "").id == ada.id
    stranger = accounts.google_sign_in("g-2", "eve@example.org", "Eve")
    assert stranger.role == "pending"
    accounts.create_invite(_admin(), "fay@example.org", "validator")
    assert accounts.google_sign_in("g-3", "fay@example.org", "Fay").role == "validator"


def test_reset_links_set_a_new_password_and_end_sessions():
    admin = _admin()
    u = accounts.sign_up("ada@example.org", "Ada", "a long password")
    old = accounts.start_session(u.id)
    token = accounts.create_reset(admin, u.id)
    assert accounts.reset_details(token).id == u.id
    accounts.accept_reset(token, "another long one")
    assert accounts.user_for_session(old) is None
    assert accounts.password_sign_in("ada@example.org", "another long one").id == u.id
    assert accounts.reset_details(token) is None


def test_only_admins_change_roles_and_not_their_own():
    admin = _admin()
    u = accounts.sign_up("ada@example.org", "Ada", "a long password")
    with pytest.raises(accounts.NotAllowed):
        accounts.set_role(u, u.id, "admin")
    accounts.set_role(admin, u.id, "validator")
    assert accounts.get_user(u.id).role == "validator"
    with pytest.raises(ValueError, match="own admin"):
        accounts.set_role(admin, admin.id, "validator")
    # A demoted admin's stale session object can't keep acting as admin.
    accounts.set_role(admin, u.id, "admin")
    accounts.set_role(accounts.get_user(u.id), admin.id, "validator")
    with pytest.raises(accounts.NotAllowed):
        accounts.set_role(admin, u.id, "pending")
    assert [r["role"] for r in accounts.list_users()].count("admin") == 1
