"""The API's Explore endpoints on a small synthetic area: a 12 x 12 grid at 100 m
(Lagos, UTM 31N), two tracks every 12 days through 2023, and a 3 x 3 block whose
signal drops by 8 dB in mid-2023 (a demolition)."""
import time

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from s1drops.api import auth, main, photos, view
from s1drops.app import accounts
from s1drops.cube import registry
from s1drops.cube.cache import write_cube

NY = NX = 12
BLOCK = [(y, x) for y in (4, 5, 6) for x in (5, 6, 7)]


def _cube():
    res = 100.0
    xs = 540_000 + res * (np.arange(NX) + 0.5)
    ys = 720_000 - res * (np.arange(NY) + 0.5)
    t = np.datetime64("2023-01-01") + np.arange(30) * np.timedelta64(12, "D")
    times = np.sort(np.concatenate([t, t + np.timedelta64(6, "D")]))
    n = len(times)
    rng = np.random.default_rng(1)
    db = -7 + rng.normal(0, 0.3, (n, NY, NX))
    change = times >= np.datetime64("2023-06-15")
    for y, x in BLOCK:
        db[change, y, x] -= 8
    vv = (10 ** (db / 10)).astype("float32")
    ro = np.tile([1, 2], 30)
    ds = xr.Dataset(
        {"vv": (("time", "y", "x"), vv), "vh": (("time", "y", "x"), vv / 3)},
        coords={"time": times.astype("datetime64[ns]"), "x": xs, "y": ys,
                "relative_orbit": ("time", ro.astype("int32")),
                "orbit_state": ("time", np.where(ro == 1, "ascending", "descending").astype("<U10"))})
    ds["aoi_mask"] = (("y", "x"), np.ones((NY, NX), bool))
    ds.attrs.update({"crs": "EPSG:32631", "resolution": res, "name": "testtown", "bbox_ll": [3.36, 6.5, 3.37, 6.51],
                     "start": "2023-01-01", "end": "2023-12-31", "reduction": "native_median", "n_passes": n,
                     "grid_transform": [res, 0.0, 540_000.0, 0.0, -res, 720_000.0], "grid_shape": [NY, NX]})
    return ds


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    d = tmp_path_factory.mktemp("cache")
    write_cube(_cube(), d / "testtown_100m.zarr")
    registry.rescan(d)
    return d


@pytest.fixture
def client(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("S1DROPS_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("S1DROPS_CACHE_DIR", str(cache))
    people = {"tok-val": ("u-val", "val@example.org"), "tok-rev": ("u-rev", "rev@example.org"),
              "tok-new": ("u-new", "new@example.org")}

    def decode(token):
        uid, email = people[token]
        return {"sub": uid, "email": email, "email_verified": True}

    monkeypatch.setattr(auth, "decode_token", decode)
    for tok, role in (("tok-val", "validator"), ("tok-rev", "reviewer")):
        uid, email = people[tok]
        accounts.request_access(uid, email, tok, "", "testing")
        with accounts.connect() as con:
            con.execute("UPDATE users SET role = ? WHERE email = ?", (role, email))
    c = TestClient(main.app)
    c.h = lambda tok: {"Authorization": f"Bearer {tok}"}
    return c


def centre(cube, y, x):
    from pyproj import Transformer
    tx = Transformer.from_crs("EPSG:32631", "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(cube["x"].values[x], cube["y"].values[y])
    return lat, lon


def test_only_approved_users(client):
    for path in ("/api/areas/testtown_100m", "/api/areas/testtown_100m/changes", "/api/areas/testtown_100m/overlay"):
        assert client.get(path, headers=client.h("tok-new")).status_code == 403
    assert client.get("/api/areas/nowhere", headers=client.h("tok-val")).status_code == 404


def test_area_and_overlay(client):
    a = client.get("/api/areas/testtown_100m", headers=client.h("tok-val")).json()
    assert a["name"] == "Testtown" and len(a["days"]) == 60 and a["days"][0] == "2023-01-01"
    assert a["hasLcz"] is False and a["defaults"]["minChange"] == 2.5 and a["neighborhood"] is False
    o = client.get("/api/areas/testtown_100m/overlay", headers=client.h("tok-val")).json()
    assert o["image"].startswith("data:image/png;base64,")
    (s, w), (n, e) = o["bounds"]
    assert s < n and w < e and o["legend"]["title"] == "Drop in radar signal"
    assert client.get("/api/areas/testtown_100m/overlay?layer=lcz", headers=client.h("tok-val")).status_code == 400
    assert client.get("/api/areas/testtown_100m/overlay?pol=xx", headers=client.h("tok-val")).status_code == 400


def test_strongest_changes(client):
    r = client.get("/api/areas/testtown_100m/changes", headers=client.h("tok-val")).json()
    assert r["total"] == 1 and r["totalHa"] == 9.0 and r["confirmed"] == 0
    c = r["items"][0]
    assert c["rank"] == 1 and c["cells"] == 9 and c["areaHa"] == 9.0
    assert c["dropDb"] > 6 and c["strength"] == "Strong" and c["changeText"].endswith("weaker")
    assert "2023" in c["when"] and not c["recent"]
    assert c["rating"]["label"] == "High" and c["verdict"] is None
    assert {f["name"] for f in c["rating"]["factors"]} >= {"Strength", "Satellite tracks", "Time since", "Verdicts"}
    # stricter settings: nothing; by pixel: up to one row per neighbourhood
    assert client.get("/api/areas/testtown_100m/changes?minArea=10", headers=client.h("tok-val")).json()["total"] == 0
    px = client.get("/api/areas/testtown_100m/changes?byPixel=true", headers=client.h("tok-val")).json()
    assert px["byPixel"] and px["total"] >= 1 and "cells" not in px["items"][0]
    # a searched map area away from the change: nothing there
    far = client.get("/api/areas/testtown_100m/changes?box=6.0,3.0,6.01,3.01", headers=client.h("tok-val")).json()
    assert far["total"] == 0


def test_a_place_and_its_verdicts(client):
    cube = _cube()
    lat, lon = centre(cube, 5, 6)
    p = client.get(f"/api/areas/testtown_100m/at?lat={lat}&lon={lon}", headers=client.h("tok-val")).json()
    assert p["cell"] == [5, 6] and p["area"]["cells"] == 9 and p["area"]["outline"]["type"] == "Feature"
    drops = [e for e in p["events"] if e["isDrop"]]
    assert drops and drops[0]["strength"] == "Strong" and drops[0]["totalTracks"] == 2
    assert len(p["signal"]["dates"]) == len(p["signal"]["pct"]) == 60 and p["signal"]["band"]
    assert p["rating"]["label"] == "High" and p["photoOptions"] and p["rows"]
    assert p["target"]["cells"] and p["verdicts"] == [] and p["summary"] is None

    # a validator records a verdict; it shows on the place, the list and the map layer
    body = {"target": p["target"], "verdict": "demolition", "evidence": ["s2", "nonsense"], "confidence": "high",
            "note": "Cleared"}
    r = client.post("/api/areas/testtown_100m/verdicts", json=body, headers=client.h("tok-val")).json()
    assert r["verdicts"][0]["label"] == "Demolition / clearance" and r["verdicts"][0]["mine"]
    assert r["verdicts"][0]["evidence"] == ["Sentinel-2 photos (in this app)"]
    ch = client.get("/api/areas/testtown_100m/changes", headers=client.h("tok-val")).json()
    assert ch["confirmed"] == 1 and ch["items"][0]["rating"]["label"] == "Confirmed"
    assert ch["items"][0]["verdict"]["verdict"] == "demolition"
    layer = client.get("/api/areas/testtown_100m/verdicts", headers=client.h("tok-val")).json()
    assert len(layer["features"]) == 1
    # saving again replaces your own; a reviewer vetoes, and undoes it
    body["verdict"] = "other"
    client.post("/api/areas/testtown_100m/verdicts", json=body, headers=client.h("tok-val"))
    p = client.get(f"/api/areas/testtown_100m/at?lat={lat}&lon={lon}", headers=client.h("tok-rev")).json()
    assert [v["status"] for v in p["verdicts"]] == ["superseded", "active"] and not p["verdicts"][1]["mine"]
    rid = p["verdicts"][1]["id"]
    assert client.post(f"/api/verdicts/{rid}/veto", json={"reason": "x"}, headers=client.h("tok-val")).status_code == 403
    assert client.post(f"/api/verdicts/{rid}/veto", json={"reason": "Wrong place"}, headers=client.h("tok-rev")).status_code == 200
    p = client.get(f"/api/areas/testtown_100m/at?lat={lat}&lon={lon}", headers=client.h("tok-val")).json()
    assert p["verdicts"][1]["status"] == "vetoed" and p["verdicts"][1]["vetoReason"] == "Wrong place" and p["summary"] is None
    client.post(f"/api/verdicts/{rid}/undo-veto", headers=client.h("tok-rev"))
    assert client.get(f"/api/areas/testtown_100m/at?lat={lat}&lon={lon}",
                      headers=client.h("tok-val")).json()["summary"]["verdict"] == "other"


def test_bad_verdicts_and_places(client):
    cube = _cube()
    lat, lon = centre(cube, 5, 6)
    assert "outside" in client.get("/api/areas/testtown_100m/at?lat=0&lon=0", headers=client.h("tok-val")).json()["note"]
    bad = {"target": {"row": 99, "col": 0, "date_after": "2023-06-20"}, "verdict": "demolition"}
    assert client.post("/api/areas/testtown_100m/verdicts", json=bad, headers=client.h("tok-val")).status_code == 400
    bad = {"target": {"row": 5, "col": 6, "date_after": "2023-06-20"}, "verdict": "maybe"}
    assert client.post("/api/areas/testtown_100m/verdicts", json=bad, headers=client.h("tok-val")).status_code == 400
    bad = {"target": {"row": 5, "col": 6, "date_after": "2023-06-20"}, "verdict": "none", "link": "javascript:x"}
    assert client.post("/api/areas/testtown_100m/verdicts", json=bad, headers=client.h("tok-val")).status_code == 400
    # a quiet cell: no change there, nothing to record a verdict on
    lat0, lon0 = centre(cube, 0, 0)
    q = client.get(f"/api/areas/testtown_100m/at?lat={lat0}&lon={lon0}", headers=client.h("tok-val")).json()
    assert q["area"] is None and q["listed"] is None and q["target"] is None and q["photoOptions"] == []


def test_photo_jobs_belong_to_who_started_them(client, monkeypatch):
    def fake_pair(cache_dir, lat, lon, before, after, progress=None):
        progress("looking")
        side = {"date": "2023-05-01", "res_m": 0.3, "source": "Vantor", "tiles": "https://t/{z}/{y}/{x}", "release": 1}
        return {"before": side, "after": dict(side, date="2023-08-01"), "link": "https://wayback"}

    monkeypatch.setattr(photos.highres, "find_pair", fake_pair)
    cube = _cube()
    lat, lon = centre(cube, 5, 6)
    body = {"lat": lat, "lon": lon, "dateBefore": "2023-06-09", "dateAfter": "2023-06-21", "source": "highres"}
    job = client.post("/api/areas/testtown_100m/photos", json=body, headers=client.h("tok-val")).json()
    for _ in range(50):
        j = client.get(f"/api/photos/{job['id']}", headers=client.h("tok-val")).json()
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "done" and j["result"]["after"]["date"] == "2023-08-01" and "looking" in j["messages"]
    assert j["result"]["before"]["describe"].startswith("01 May 2023")
    assert client.get(f"/api/photos/{job['id']}", headers=client.h("tok-rev")).status_code == 404
    body["source"] = "drone"
    assert client.post("/api/areas/testtown_100m/photos", json=body, headers=client.h("tok-val")).status_code == 400


def test_warm_up_fills_what_the_api_reads(cache):
    """The start-up warm-up (Explore's own) computes the default view under the
    same keys the API asks for: the first visitor's map and list are ready."""
    from s1drops.app import explore as ex

    ex._SHARED.items.clear()
    ex.warm_up(str(cache))
    warmed = set(ex._SHARED.items)
    a = view.open_area(cache, "testtown_100m")
    view.overlay(a, view.View("testtown_100m"), "hotspot")
    view.ranked(a, view.View("testtown_100m"))
    assert set(ex._SHARED.items) == warmed


def test_report_for_a_listed_change(client):
    cube = _cube()
    lat, lon = centre(cube, 5, 6)
    r = client.get(f"/api/areas/testtown_100m/report?lat={lat}&lon={lon}", headers=client.h("tok-val"))
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "_report.pdf" in r.headers["content-disposition"]
    # someone else's (or unknown) photo job is ignored, not an error
    assert client.get(f"/api/areas/testtown_100m/report?lat={lat}&lon={lon}&photos=nope",
                      headers=client.h("tok-val")).status_code == 200
    lat0, lon0 = centre(cube, 0, 0)
    q = client.get(f"/api/areas/testtown_100m/report?lat={lat0}&lon={lon0}", headers=client.h("tok-val"))
    assert q.status_code == 400 and "Strongest changes" in q.json()["detail"]
    assert client.get(f"/api/areas/testtown_100m/report?lat={lat}&lon={lon}", headers=client.h("tok-new")).status_code == 403


def test_report_photos_only_from_your_own_job(monkeypatch):
    rgb = np.zeros((4, 4, 3), "uint8")
    raw = {"before": {"candidates": [{"date": "2023-05-01", "cloud": 0.0, "rgb": rgb}]},
           "after": {"candidates": [{"date": "2023-08-01", "cloud": 0.1, "rgb": rgb}]}, "transform": None, "crs": None}
    photos._jobs["j1"] = {"id": "j1", "uid": "me", "kind": "s2", "status": "done", "cell": (5, 6), "raw": raw,
                          "created": time.time(), "messages": [], "result": {}, "error": ""}
    assert photos.for_report("j1", "me", (5, 6), 0, 0)["after"]["candidates"][0]["date"] == "2023-08-01"
    assert photos.for_report("j1", "someone", (5, 6), 0, 0) is None
    assert photos.for_report("j1", "me", (1, 1), 0, 0) is None      # fetched for another place
