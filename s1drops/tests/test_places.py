import json

from s1drops.app import places


def test_name_inside_an_area_outline_is_plain():
    r = {"osm_type": "relation", "addresstype": "suburb", "name": "Bagong Silangan",
         "lat": "14.70", "lon": "121.10", "address": {"suburb": "Bagong Silangan",
                                                      "city": "Quezon City"}}
    assert places.name_from_result(r, 14.71, 121.11) == "Bagong Silangan, Quezon City"


def test_name_from_a_nearby_point_says_near():
    r = {"osm_type": "node", "addresstype": "suburb", "name": "Ijora",
         "lat": "6.4667", "lon": "3.3678",
         "address": {"suburb": "Ijora", "town": "Apapa", "county": "Apapa"}}
    assert places.name_from_result(r, 6.4600, 3.3700) == "near Ijora, Apapa"   # ~0.8 km


def test_name_from_a_distant_point_falls_back_to_the_admin_area():
    r = {"osm_type": "node", "addresstype": "village", "name": "Oko Agbo",
         "lat": "6.4040", "lon": "3.3822",
         "address": {"village": "Oko Agbo", "county": "Amuwo Odofin"}}
    assert places.name_from_result(r, 6.42724, 3.39112) == "Amuwo Odofin"      # ~2.8 km


def test_name_without_a_local_feature_is_the_admin_area():
    r = {"osm_type": "relation", "addresstype": "county", "name": "Apapa",
         "address": {"county": "Apapa", "state": "Lagos"}}
    assert places.name_from_result(r, 6.45, 3.37) == "Apapa"
    assert places.name_from_result({}, 0.0, 0.0) == ""


def test_lookup_caches_on_disk_and_skips_repeat_requests(tmp_path, monkeypatch):
    calls = []

    def fake(lat, lon):
        calls.append((lat, lon))
        return {"osm_type": "relation", "addresstype": "suburb", "name": "Apapa",
                "address": {"suburb": "Apapa", "city": "Lagos"}}

    monkeypatch.setattr(places, "_fetch", fake)
    monkeypatch.setattr(places, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(places, "_caches", {})
    assert places.cached(tmp_path, 6.4271, 3.3911) is None
    assert places.lookup(tmp_path, 6.4271, 3.3911) == "Apapa, Lagos"
    assert places.lookup(tmp_path, 6.4272, 3.3912) == "Apapa, Lagos"   # same ~100 m key
    assert len(calls) == 1
    saved = json.loads((tmp_path / places.CACHE_NAME).read_text(encoding="utf-8"))
    assert saved == {"6.427,3.391": "Apapa, Lagos"}

    monkeypatch.setattr(places, "_caches", {})                        # fresh process
    assert places.cached(tmp_path, 6.4271, 3.3911) == "Apapa, Lagos"


def test_lookup_failure_returns_none_and_is_not_cached(tmp_path, monkeypatch):
    def boom(lat, lon):
        raise OSError("offline")

    monkeypatch.setattr(places, "_fetch", boom)
    monkeypatch.setattr(places, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(places, "_caches", {})
    assert places.lookup(tmp_path, 1.0, 2.0) is None
    assert places.cached(tmp_path, 1.0, 2.0) is None
