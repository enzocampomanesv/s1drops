import pytest

from s1drops.app import accounts, feedback


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S1DROPS_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("S1DROPS_ADMIN_EMAILS", "boss@example.org")


def test_anyone_can_send_only_admins_read():
    admin = accounts.sign_up("boss@example.org", "Boss", "a long password")
    val = accounts.sign_up("val@example.org", "Val", "a long password")
    feedback.send(None, "bug", "  The map is <b>blank</b>  ", "me@example.org",
                  "area=lagos&at=6.5,3.4")
    feedback.send(val, "idea", "Add Manila")
    with pytest.raises(ValueError):
        feedback.send(None, "bug", "   ")
    with pytest.raises(ValueError):
        feedback.send(None, "bug", "hi", "not an email")
    with pytest.raises(accounts.NotAllowed):
        feedback.inbox(val)
    msgs = feedback.inbox(admin)
    assert [m.kind for m in msgs] == ["idea", "bug"]          # newest first
    assert msgs[0].sender == "Val" and msgs[1].sender == ""
    assert msgs[1].message == "The map is <b>blank</b>"      # stored as typed, shown as text
    assert msgs[1].context == "area=lagos&at=6.5,3.4"
    feedback.mark_done(admin, msgs[1].id)
    assert [m.kind for m in feedback.inbox(admin)] == ["idea"]
    assert len(feedback.inbox(admin, include_done=True)) == 2
