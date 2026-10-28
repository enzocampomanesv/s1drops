from types import SimpleNamespace

from s1drops.app import explore, layout, tour


def test_steps_point_at_marked_parts_of_the_page():
    steps = tour.steps_for(None)
    assert steps[0].get("target") is None                    # welcome card, centred
    src = open(explore.__file__, encoding="utf-8").read() + layout.CSS
    for s in steps:
        assert s["title"] and s["body"]
        t = s.get("target", "")
        if t.startswith(".s1-") and t != ".s1-tour-help":
            assert t[1:] in src, t                            # class exists on the page


def test_validation_step_depends_on_who_is_signed_in():
    anon = tour.steps_for(None)
    member = tour.steps_for(SimpleNamespace(approved=True))
    pick = lambda st: next(s["body"] for s in st if s.get("target") == ".s1-tour-validate")
    assert "sign in" in pick(anon) and "tap" in pick(member)
