import json

import numpy as np
import pytest

from s1drops.app import accounts, logic, validation
from s1drops.tests.test_clusters import _grid


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S1DROPS_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("S1DROPS_ADMIN_EMAILS", "boss@example.org")


@pytest.fixture
def people():
    admin = accounts.sign_up("boss@example.org", "Boss", "a long password")
    val = accounts.sign_up("val@example.org", "Val", "a long password")
    rev = accounts.sign_up("rev@example.org", "Rev", "a long password")
    pend = accounts.sign_up("pen@example.org", "Pen", "a long password")
    accounts.set_role(admin, val.id, "validator")
    accounts.set_role(admin, rev.id, "reviewer")
    return {"admin": admin, "val": accounts.get_user(val.id),
            "rev": accounts.get_user(rev.id), "pending": pend}


def _area(cells, day="2023-03-01", rank=1):
    return {"rank": rank, "cells": cells, "date_before": "2023-02-17", "date_after": day,
            "row": cells[0][0], "col": cells[0][1]}


BLOCK = [(1, 1), (1, 2), (2, 1), (2, 2)]


def test_a_verdict_sticks_to_the_place_and_date(people):
    cube = _grid()
    validation.record(people["val"], cube, _area(BLOCK), cube_key="k", verdict="demolition",
                      evidence=["s2", "bogus"], note="<b>cleared</b>")
    recs = validation.for_cube(cube, people["val"])
    assert len(recs) == 1 and recs[0].cells == frozenset(BLOCK)
    assert recs[0].evidence == ["s2"] and recs[0].note == "<b>cleared</b>"
    # Different settings give a different area over the same ground: still matched.
    s = validation.summarise(recs, _area([(2, 2), (2, 3), (3, 3)], "2023-03-13"))
    assert (s.verdict, s.count, s.reference) == ("demolition", 1, False)
    assert validation.summarise(recs, _area([(5, 5)])) is None           # elsewhere
    assert validation.summarise(recs, _area(BLOCK, "2023-09-01")) is None  # later change
    pixel = {"rank": 2, "row": 1, "col": 1, "date_after": "2023-03-01"}
    assert validation.summarise(recs, pixel).verdict == "demolition"


def test_only_approved_users_see_and_record(people):
    cube = _grid()
    with pytest.raises(accounts.NotAllowed):
        validation.record(people["pending"], cube, _area(BLOCK), cube_key="k",
                          verdict="none")
    validation.record(people["val"], cube, _area(BLOCK), cube_key="k", verdict="none")
    assert validation.for_cube(cube, people["pending"]) == []
    assert validation.for_cube(cube, None) == []
    with pytest.raises(ValueError):
        validation.record(people["val"], cube, _area(BLOCK), cube_key="k", verdict="maybe")
    with pytest.raises(ValueError, match="http"):
        validation.record(people["val"], cube, _area(BLOCK), cube_key="k", verdict="none",
                          link="javascript:alert(1)")


def test_new_verdict_replaces_own_and_others_make_it_mixed(people):
    cube = _grid()
    for v in ("unsure", "demolition"):
        validation.record(people["val"], cube, _area(BLOCK), cube_key="k", verdict=v)
    recs = validation.for_cube(cube, people["val"])
    assert [r.status for r in recs] == ["superseded", "active"]
    assert validation.summarise(recs, _area(BLOCK)).verdict == "demolition"
    validation.record(people["rev"], cube, _area(BLOCK), cube_key="k", verdict="none")
    s = validation.summarise(validation.for_cube(cube, people["val"]), _area(BLOCK))
    assert (s.verdict, s.count) == ("mixed", 2)


def test_reviewers_veto_and_undo(people):
    cube = _grid()
    vid = validation.record(people["val"], cube, _area(BLOCK), cube_key="k",
                            verdict="demolition")
    with pytest.raises(accounts.NotAllowed):
        validation.veto(people["val"], vid, "mine")
    validation.veto(people["rev"], vid, "photos show a market")
    recs = validation.for_cube(cube, people["val"])
    assert recs[0].status == "vetoed" and recs[0].vetoed_by_name == "Rev"
    assert validation.summarise(recs, _area(BLOCK)) is None             # no longer counts
    validation.undo_veto(people["admin"], vid)
    assert validation.for_cube(cube, people["val"])[0].counts


def _ref_geojson(cube, cells, year):
    geom = logic.area_outline(cube, cells)["geometry"]
    return json.dumps({"type": "FeatureCollection",
                       "crs": {"type": "name", "properties": {
                           "name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                       "features": [{"type": "Feature", "geometry": geom,
                                     "properties": {"Name": "Site A", "demo_year": year}}]})


def test_reference_data_is_admin_only_and_matched_by_year(people):
    cube = _grid()
    with pytest.raises(accounts.NotAllowed):
        validation.import_reference(people["rev"], _ref_geojson(cube, BLOCK, 2023), "b")
    n = validation.import_reference(people["admin"], _ref_geojson(cube, BLOCK, 2023), "lagos")
    assert n == 1
    assert validation.for_cube(cube, people["rev"]) == []               # hidden
    recs = validation.for_cube(cube, people["admin"])
    assert validation.summarise(recs, _area(BLOCK, "2023-11-20")).reference
    assert validation.summarise(recs, _area(BLOCK, "2024-02-10")).reference  # next Jan-Mar
    assert validation.summarise(recs, _area(BLOCK, "2024-06-01")) is None
    assert validation.reference_batches(people["admin"])[0]["n"] == 1
    validation.delete_reference_batch(people["admin"], "lagos")
    assert validation.for_cube(cube, people["admin"]) == []


def test_export_for_reviewers(people):
    cube = _grid()
    validation.record(people["val"], cube, _area(BLOCK), cube_key="k", verdict="other",
                      note="new road")
    with pytest.raises(accounts.NotAllowed):
        validation.export(people["val"])
    text = validation.export(people["rev"], "csv")
    assert "new road" in text and "Val" in text
    gj = json.loads(validation.export(people["admin"], "geojson"))
    assert gj["features"][0]["properties"]["verdict"] == "other"


def test_cells_in_geometry_small_shapes_take_the_cell_under_them():
    cube = _grid()
    from pyproj import Transformer
    tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
    x, y = float(cube["x"].values[3]), float(cube["y"].values[4])
    lon0, lat0 = tx.transform(x - 10, y - 10)
    lon1, lat1 = tx.transform(x + 10, y + 10)
    tiny = {"type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1],
                                                [lon0, lat1], [lon0, lat0]]]}
    assert validation.cells_in_geometry(cube, tiny) == frozenset({(4, 3)})
    far = {"type": "Point", "coordinates": [10.0, 10.0]}
    assert validation.cells_in_geometry(cube, far) == frozenset()
