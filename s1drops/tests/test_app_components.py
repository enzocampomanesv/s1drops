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


def test_explore_renders_cube_and_picks_candidate(tmp_path, monkeypatch):
    import ipyvuetify as v
    import solara
    from s1drops.app import confidence
    from s1drops.app.explore import Explore
    from s1drops.cube.cache import write_cube
    from s1drops.app import places
    from s1drops.tests.test_app import _cube

    # Offline: the place lookup must never reach Nominatim from the tests.
    monkeypatch.setattr(places, "_fetch", lambda lat, lon: {"address": {"suburb": "Testville"}})
    monkeypatch.setattr(places, "MIN_INTERVAL_S", 0.0)
    cube = _cube()
    # A 4-pixel (4 ha) patch dropping together: the smallest area listed by default.
    vv = cube["vv"].values
    for y, x in [(1, 2), (2, 1), (2, 2)]:
        vv[:, y, x] = vv[:, 1, 1]
    cube["vv"] = (("time", "y", "x"), vv)
    cube.attrs.update({"name": "lagos_test", "start": "2024-01-01", "end": "2024-08-01",
                       "reduction": "native_median", "bbox_ll": [3.40, 6.45, 3.45, 6.50]})
    write_cube(cube, tmp_path / "lagos_test_2024-01-01_2024-08-01_nm.zarr")

    box, rc = solara.render(Explore(str(tmp_path)), handle_error=False)
    try:
        rows = rc.find(v.Html, tag="tr").widgets
        assert len(rows) == 2, len(rows)        # header + the one change area
        place, date, area, drop, conf = rows[1].children
        assert drop.children[0].startswith("−") and date.children[0].endswith("2024")
        assert area.children[0] == "4.0 ha"
        badge = conf.children[0]                    # confidence label, reasons on hover
        assert badge.v_slots[0]["children"][0].children[0] in confidence.LEVELS
        rows[1].fire_event("click", {})
        md = " ".join(str(w.template) + str(w.data) for w in rc.find(v.VuetifyTemplate).widgets)
        assert "Selected area" in md and "4 pixels" in md
    finally:
        rc.close()


def test_optical_maps_basemap_follows_theme():
    import ipyleaflet
    import solara
    from s1drops.app import explore

    idx = solara.reactive(0)
    box, rc = solara.render(explore.SyncedOpticalMaps(
        {}, {}, ((6.42, 3.38), (6.43, 3.40)), idx, idx), handle_error=False)
    maps = rc.find(ipyleaflet.Map).widgets

    def basemaps():
        return {tuple(lyr.name for lyr in m.layers if isinstance(lyr, ipyleaflet.TileLayer))
                for m in maps}

    try:
        solara.lab.theme.dark_effective = False
        assert basemaps() == {("Esri.WorldGrayCanvas",)}
        solara.lab.theme.dark_effective = True
        assert basemaps() == {("Esri.WorldDarkGrayCanvas",)}   # swapped, not stacked
        assert all(isinstance(m.layers[0], ipyleaflet.TileLayer) for m in maps)  # underneath
    finally:
        solara.lab.theme.dark_effective = None
        rc.close()


def test_optical_maps_label_before_and_after_in_words():
    import ipyvuetify as v
    import numpy as np
    import solara
    from s1drops.app import explore

    # Photo dates arrive as numpy strings from the fetch.
    cand = lambda d, cloud: {"date": np.str_(d), "cloud": cloud, "url": None}  # noqa: E731
    before = {"candidates": [cand("2023-12-21", 0.06), cand("2023-11-02", 0.1)]}
    after = {"candidates": []}
    idx_b, idx_a = solara.reactive(0), solara.reactive(0)
    box, rc = solara.render(explore.SyncedOpticalMaps(
        before, after, ((6.42, 3.38), (6.43, 3.40)), idx_b, idx_a), handle_error=False)
    try:
        heads = [w.children[0] for w in rc.find(v.Html, tag="b").widgets]
        assert heads == ["Before", "After"]
        spans = " ".join(str(w.children) for w in rc.find(v.Html, tag="span").widgets)
        assert "21 Dec 2023" in spans
        assert "probed" not in spans and "AOI" not in spans
    finally:
        rc.close()
