"""Phase 2 of the API: downloads, verdict export, admin work (estimate, queue,
update now, delete an area), reference data and feedback."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from s1drops.api import admin as admin_work
from s1drops.api import auth, main, photos
from s1drops.app import accounts
from s1drops.cube import registry
from s1drops.tests.test_api_view import _cube, cache, centre  # noqa: F401 -- the fixture

PEOPLE = {"tok-val": ("u-val", "val@example.org", "validator"), "tok-rev": ("u-rev", "rev@example.org", "reviewer"),
          "tok-adm": ("u-adm", "adm@example.org", "admin"), "tok-new": ("u-new", "new@example.org", None)}


@pytest.fixture
def client(cache, tmp_path, monkeypatch):  # noqa: F811
    work = tmp_path / "cache"
    shutil.copytree(cache, work)
    registry.rescan(work)
    monkeypatch.setenv("S1DROPS_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("S1DROPS_CACHE_DIR", str(work))
    monkeypatch.setattr(auth, "decode_token", lambda t: {"sub": PEOPLE[t][0], "email": PEOPLE[t][1], "email_verified": True})
    for tok, (uid, email, role) in PEOPLE.items():
        if role:
            accounts.request_access(uid, email, tok, "", "testing")
            with accounts.connect() as con:
                con.execute("UPDATE users SET role = ? WHERE email = ?", (role, email))
    admin_work.bakequeue.queue.set([])
    c = TestClient(main.app)
    c.h = lambda tok: {"Authorization": f"Bearer {tok}"}
    c.cache = work
    return c


def test_area_downloads(client):
    h = client.h("tok-val")
    r = client.get("/api/areas/testtown_100m/download/changes.csv", headers=h)
    assert r.status_code == 200 and r.text.startswith("rank,place,lat,lon") and r.text.count("\n") == 2
    assert 'filename="testtown_2023-01-01_' in r.headers["content-disposition"]
    g = client.get("/api/areas/testtown_100m/download/changes.geojson", headers=h).json()
    assert len(g["features"]) == 1 and g["features"][0]["properties"]["confidence"] == "High"
    m = client.get("/api/areas/testtown_100m/download/map.geojson?minChange=4", headers=h).json()
    assert len(m["features"]) == 9                       # the 3 x 3 block
    assert client.get("/api/areas/testtown_100m/download/secrets.txt", headers=h).status_code == 404
    assert client.get("/api/areas/testtown_100m/download/changes.csv", headers=client.h("tok-new")).status_code == 403


def test_place_downloads_and_photo_tifs(client, tmp_path):
    lat, lon = centre(_cube(), 5, 6)
    h = client.h("tok-val")
    s = client.get(f"/api/areas/testtown_100m/download/signal.csv?lat={lat}&lon={lon}", headers=h)
    assert s.status_code == 200 and s.text.count("\n") == 61            # a header, then the 60 passes
    d = client.get(f"/api/areas/testtown_100m/download/drops.csv?lat={lat}&lon={lon}", headers=h)
    assert d.status_code == 200 and "delta_db" in d.text
    assert client.get("/api/areas/testtown_100m/download/signal.csv", headers=h).status_code == 404  # needs a place
    tif = tmp_path / "x.tif"
    tif.write_bytes(b"II*\x00fake")
    photos._jobs["j2"] = {"id": "j2", "uid": "u-val", "kind": "s2", "status": "done", "cell": (5, 6), "created": 1e12,
                          "messages": [], "result": {}, "error": "",
                          "raw": {"before": {"candidates": [{"date": "2023-05-01", "tif_path": str(tif)}]}, "after": {"candidates": []}}}
    r = client.get("/api/photos/j2/before.tif", headers=h)
    assert r.status_code == 200 and r.content.startswith(b"II*") and "sentinel2_before_2023-05-01.tif" in r.headers["content-disposition"]
    assert client.get("/api/photos/j2/after.tif", headers=h).status_code == 404
    assert client.get("/api/photos/j2/before.tif", headers=client.h("tok-rev")).status_code == 404   # not theirs


def test_verdict_export_for_reviewers(client):
    lat, lon = centre(_cube(), 5, 6)
    p = client.get(f"/api/areas/testtown_100m/at?lat={lat}&lon={lon}", headers=client.h("tok-val")).json()
    client.post("/api/areas/testtown_100m/verdicts", json={"target": p["target"], "verdict": "demolition"}, headers=client.h("tok-val"))
    assert client.get("/api/verdicts/export", headers=client.h("tok-val")).status_code == 403
    r = client.get("/api/verdicts/export?fmt=csv", headers=client.h("tok-rev"))
    assert r.status_code == 200 and "demolition" in r.text
    assert client.get("/api/verdicts/export?fmt=geojson", headers=client.h("tok-rev")).json()["features"]


def test_admin_estimate_and_queue(client, monkeypatch):
    h = client.h("tok-adm")
    assert client.get("/api/admin/status", headers=client.h("tok-rev")).status_code == 403
    st = client.get("/api/admin/status", headers=h).json()
    assert st["enabled"] and st["areas"][0]["key"] == "testtown_100m" and len(st["presets"]) == 3
    body = {"mode": "bbox", "bbox": [3.36, 6.50, 3.38, 6.52], "start": "2024-01-01", "end": "2025-01-01", "preset": "100-native"}
    est = client.post("/api/admin/estimate", json=body, headers=h).json()
    assert est["cells"] > 400 and est["sizeMb"] > 0 and est["problem"] is None
    big = dict(body, bbox=[3.0, 6.3, 3.6, 6.7], preset="10-native")
    assert "limit for 10 m" in client.post("/api/admin/estimate", json=big, headers=h).json()["problem"]
    assert client.post("/api/admin/estimate", json=dict(body, bbox=[500000, 1, 2, 3]), headers=h).status_code == 400

    queued = []
    monkeypatch.setattr(admin_work.bakequeue, "enqueue", lambda **kw: queued.append(kw) or 7)
    assert client.post("/api/admin/bakes", json=body, headers=h).status_code == 400          # no name
    r = client.post("/api/admin/bakes", json=dict(body, name="Ikorodu"), headers=h).json()
    assert r["id"] == 7 and queued[0]["name"] == "Ikorodu" and queued[0]["resolution"] == 100.0
    assert client.post("/api/admin/bakes", json=dict(big, name="x"), headers=h).status_code == 400  # over the 10 m limit
    ext = {"mode": "existing", "key": "testtown_100m", "start": "2023-01-01", "end": "2024-06-30", "preset": "100-native"}
    client.post("/api/admin/bakes", json=ext, headers=h)
    assert queued[1]["name"] == "testtown" and queued[1]["bbox"] == tuple(st and registry.list_cubes(client.cache)[0].bbox_ll)
    geo = {"type": "Polygon", "coordinates": [[[3.36, 6.50], [3.38, 6.50], [3.38, 6.52], [3.36, 6.50]]]}
    client.post("/api/admin/bakes", json=dict(body, mode="geojson", geojson=json.dumps(geo), name="drawn"), headers=h)
    assert queued[2]["geom"] is not None and queued[2]["name"] == "drawn"

    ups = []
    monkeypatch.setattr(admin_work.bakequeue, "enqueue_update", lambda cache_dir, trigger: ups.append(trigger) or 9)
    assert client.post("/api/admin/update-now", headers=h).json() == {"id": 9} and ups == ["manual"]


def test_bakes_off_in_this_process(client, monkeypatch):
    monkeypatch.setenv("S1DROPS_BAKES", "0")
    body = {"mode": "bbox", "bbox": [3.36, 6.50, 3.38, 6.52], "start": "2024-01-01", "end": "2025-01-01", "preset": "100-native", "name": "x"}
    assert client.post("/api/admin/bakes", json=body, headers=client.h("tok-adm")).status_code == 400
    assert client.get("/api/admin/status", headers=client.h("tok-adm")).json()["enabled"] is False
    assert admin_work.started_updater(client.cache) is False


def test_delete_an_area(client):
    h = client.h("tok-adm")
    assert client.delete("/api/areas/testtown_100m", headers=client.h("tok-rev")).status_code == 403
    assert client.delete("/api/areas/testtown_100m?confirm=wrong", headers=h).status_code == 409
    admin_work.bakequeue.queue.set([{"id": 1, "label": "x", "status": "running", "log": [], "params": {}}])
    assert "idle" in client.delete("/api/areas/testtown_100m?confirm=Testtown", headers=h).json()["detail"]
    admin_work.bakequeue.queue.set([])
    assert client.delete("/api/areas/testtown_100m?confirm=Testtown", headers=h).status_code == 200
    assert client.get("/api/areas", headers=h).json() == []
    assert client.delete("/api/areas/testtown_100m?confirm=Testtown", headers=h).status_code == 404


def test_reference_data(client):
    h = client.h("tok-adm")
    poly = {"type": "Polygon", "coordinates": [[[3.3655, 6.5076], [3.3685, 6.5076], [3.3685, 6.5104], [3.3655, 6.5104], [3.3655, 6.5076]]]}
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": poly, "properties": {"demo_year": 2023, "Name": "Block A"}}]}
    assert client.post("/api/admin/references", json={"geojson": json.dumps(fc), "batch": "ngo-2024"}, headers=client.h("tok-rev")).status_code == 403
    assert client.post("/api/admin/references", json={"geojson": json.dumps(fc), "batch": "ngo-2024"}, headers=h).json() == {"imported": 1}
    assert client.post("/api/admin/references", json={"geojson": "{}", "batch": "x"}, headers=h).status_code == 400
    b = client.get("/api/admin/references", headers=h).json()
    assert b[0]["ref_batch"] == "ngo-2024" and b[0]["n"] == 1
    assert client.delete("/api/admin/references/ngo-2024", headers=h).status_code == 200
    assert client.get("/api/admin/references", headers=h).json() == []


def test_feedback(client):
    assert client.post("/api/feedback", json={"kind": "bug", "message": "The chart is empty", "context": "#x"},
                       headers=client.h("tok-new")).status_code == 200      # anyone signed in
    assert client.post("/api/feedback", json={"kind": "bug", "message": ""}, headers=client.h("tok-val")).status_code == 422
    inbox = client.get("/api/admin/feedback", headers=client.h("tok-adm")).json()
    assert inbox[0]["message"] == "The chart is empty" and inbox[0]["kind"] == "Something's not working"
    assert client.get("/api/admin/feedback", headers=client.h("tok-val")).status_code == 403
    client.post(f"/api/admin/feedback/{inbox[0]['id']}", json={"done": True}, headers=client.h("tok-adm"))
    assert client.get("/api/admin/feedback", headers=client.h("tok-adm")).json() == []
    assert len(client.get("/api/admin/feedback?all=true", headers=client.h("tok-adm")).json()) == 1


def test_areas_by_stable_id_and_tracks(client):
    h = client.h("tok-val")
    a = client.get("/api/areas", headers=h).json()[0]
    assert a["id"] == "testtown_100m"                       # no dates in this key; a dated one loses them
    from s1drops.api import view
    assert view.area_id("Lagos_100m_2017-01-01_2026-10-01_nm") == "Lagos_100m_nm"
    lat, lon = centre(_cube(), 5, 6)
    p = client.get(f"/api/areas/{a['id']}/at?lat={lat}&lon={lon}", headers=h).json()
    assert len(p["tracks"]) == 2 and len(p["tracks"][0]["dates"]) == len(p["tracks"][0]["db"]) == 30
    assert p["tracks"][0]["drops"] and p["tracks"][0]["drops"][0]["pre"] > p["tracks"][0]["drops"][0]["post"]


def test_place_search_through_the_server(client, monkeypatch):
    import requests

    asked = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return [{"display_name": "Makoko, Lagos", "lat": "6.49", "lon": "3.39", "boundingbox": ["6.48", "6.50", "3.38", "3.40"]}]

    monkeypatch.setattr(requests, "get", lambda url, params, timeout, headers: asked.update(params) or R())
    r = client.get("/api/search?q=makoko&bounds=3.3,6.4,3.5,6.6", headers=client.h("tok-val")).json()
    assert r[0]["name"] == "Makoko, Lagos" and asked["bounded"] == "1" and asked["viewbox"] == "3.3,6.6,3.5,6.4"
    assert client.get("/api/search?q=makoko", headers=client.h("tok-new")).status_code == 403


def test_reference_marks_for_admins(client):
    poly = {"type": "Polygon", "coordinates": [[[3.3655, 6.5076], [3.3685, 6.5076], [3.3685, 6.5104], [3.3655, 6.5104], [3.3655, 6.5076]]]}
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": poly, "properties": {"demo_year": 2023}}]}
    client.post("/api/admin/references", json={"geojson": json.dumps(fc), "batch": "b"}, headers=client.h("tok-adm"))
    adm = client.get("/api/areas/testtown_100m/changes", headers=client.h("tok-adm")).json()
    val = client.get("/api/areas/testtown_100m/changes", headers=client.h("tok-val")).json()
    assert adm["referenceMatches"] == 1 and adm["items"][0]["reference"] is True
    assert val["referenceMatches"] is None and val["items"][0]["reference"] is False      # admins only


def test_preapprove_by_email(client):
    h = client.h("tok-adm")
    assert client.post("/api/users", json={"email": "Zoe@Example.org", "role": "reviewer"}, headers=client.h("tok-val")).status_code == 403
    assert client.post("/api/users", json={"email": "zoe@example.org", "role": "emperor"}, headers=h).status_code == 400
    assert client.post("/api/users", json={"email": "Zoe@Example.org", "role": "reviewer", "name": "Zoe"}, headers=h).json()["role"] == "reviewer"
    assert client.post("/api/users", json={"email": "zoe@example.org", "role": "validator"}, headers=h).status_code == 400   # exists
    # Zoe signs in with that email: unverified, nothing; verified, she is a reviewer
    PEOPLE["tok-zoe"] = ("u-zoe", "zoe@example.org", None)
    try:
        import s1drops.api.auth as a
        a_decode = a.decode_token
        a.decode_token = lambda t: {"sub": "u-zoe", "email": "zoe@example.org", "email_verified": False}
        assert client.get("/api/me", headers=client.h("tok-zoe")).json()["role"] is None
        a.decode_token = lambda t: {"sub": "u-zoe", "email": "zoe@example.org", "email_verified": True}
        assert client.get("/api/me", headers=client.h("tok-zoe")).json()["role"] == "reviewer"
    finally:
        a.decode_token = a_decode
        PEOPLE.pop("tok-zoe", None)


def test_old_address_forwards_to_space4all(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "https://space4all.web.app/tools/demolitions/"
    r = client.get("/?area=Lagos_100m_2017-01-01_2026-10-01_nm&at=6.49012,3.39054&from=2023-01-01&to=2023-12-31", follow_redirects=False)
    assert r.headers["location"] == "https://space4all.web.app/tools/demolitions/#Lagos_100m_nm/6.49012,3.39054/2023-01-01,2023-12-31"
    assert client.get("/auth/login", follow_redirects=False).headers["location"].endswith("/tools/demolitions/")
    assert client.get("/api/nothing-here").status_code == 404
    assert client.get("/api/health").json() == {"ok": True}
