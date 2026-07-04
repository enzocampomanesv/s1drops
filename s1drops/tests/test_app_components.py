import pytest

from s1drops.app import auth


def test_check_password_unset(monkeypatch):
    monkeypatch.delenv(auth.ENV_VAR, raising=False)
    assert auth.admin_password_configured() is False
    assert auth.check_password("anything") is False


def test_check_password_set(monkeypatch):
    monkeypatch.setenv(auth.ENV_VAR, "s3cret")
    assert auth.admin_password_configured() is True
    assert auth.check_password("s3cret") is True
    assert auth.check_password("wrong") is False
    assert auth.check_password("") is False


def test_guard_admin():
    with pytest.raises(auth.NotAuthorized):
        auth.guard_admin(False)
    auth.guard_admin(True)  # no raise


def test_components_import():
    # Defining components must not execute rendering or require a server.
    from s1drops.app import admin, explore, main  # noqa: F401
    assert hasattr(explore, "Explore")
    assert hasattr(admin, "Admin")
    assert hasattr(main, "Page")


def test_explore_renders_empty_cache(tmp_path):
    import solara
    from s1drops.app.explore import Explore
    # Empty cache -> early Warning, no map widget; should construct without error.
    box, rc = solara.render(Explore(str(tmp_path)), handle_error=False)
    rc.close()


def test_admin_login_renders(monkeypatch):
    import solara
    from s1drops.app.admin import Admin
    from s1drops.app import state
    monkeypatch.setenv(auth.ENV_VAR, "pw")
    state.is_admin.set(False)
    box, rc = solara.render(Admin("./cache"), handle_error=False)
    rc.close()
