from s1drops.app import highres

# 40 monthly releases; the imagery at the place was captured three times.
RELEASES = [{"num": 100 + i, "date": f"{2020 + i // 12}-{i % 12 + 1:02d}-15",
             "tiles": f"https://tiles/{100 + i}/{{z}}/{{y}}/{{x}}", "meta": f"m{i}"}
            for i in range(40)]
CAPTURES = [("2020-01-15", "2019-06-01"), ("2021-03-15", "2021-02-10"),
            ("2022-11-15", "2022-10-20")]       # (first release showing it, capture date)


def _capture(release, lat, lon):
    shown = [cap for first, cap in CAPTURES if first <= release["date"]]
    return {"date": shown[-1], "res_m": 0.31, "source": "Vantor"} if shown else None


def test_finds_newest_image_before_and_oldest_after(tmp_path, monkeypatch):
    monkeypatch.setattr(highres, "releases", lambda: RELEASES)
    calls = []
    monkeypatch.setattr(highres, "capture",
                        lambda r, la, lo: calls.append(r["num"]) or _capture(r, la, lo))
    pair = highres.find_pair(tmp_path, 6.5, 3.4, "2021-06-01", "2021-07-01")
    assert pair["before"]["date"] == "2021-02-10"
    assert pair["after"]["date"] == "2022-10-20"
    assert len(calls) < 25                       # searched, not all 40 asked
    assert "swipeWidget=" in pair["link"] and str(pair["after"]["num"]) in pair["link"]
    # Saved: asking again doesn't call Esri.
    calls.clear()
    assert highres.find_pair(tmp_path, 6.5, 3.4, "2021-06-01", "2021-07-01") == pair
    assert calls == []


def test_recent_change_has_no_after_image_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(highres, "releases", lambda: RELEASES)
    monkeypatch.setattr(highres, "capture", _capture)
    highres._caches.clear()
    pair = highres.find_pair(tmp_path, 6.5, 3.4, "2023-01-01", "2023-02-01")
    assert pair["before"]["date"] == "2022-10-20" and pair["after"] is None
    assert highres.describe(pair["before"]) == "20 Oct 2022 · 31 cm · Vantor"
