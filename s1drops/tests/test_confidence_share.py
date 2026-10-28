import numpy as np

from s1drops.app import confidence, share, validation


def _c(drop=6.0, passes=8, recent=False):
    return {"rank": 1, "drop_db": drop, "passes_after": passes, "recent": recent,
            "lat": 6.5, "lon": 3.4, "date_after": "2024-03-01"}


def test_strength_and_track_agreement_score_the_label():
    assert confidence.rate(_c(6.0), 2.5, (2, 2)).label == "High"          # strong, all
    assert confidence.rate(_c(4.0), 2.5, (1, 2)).label == "Medium"        # moderate, half
    assert confidence.rate(_c(2.6), 2.5, (0, 2)).label == "Low"           # slight, none
    single = confidence.rate(_c(6.0), 2.5, (1, 1))                        # only one track
    assert single.label == "High" and "only one track" in single.explain()


def test_recent_changes_are_too_early_unless_a_verdict_settles_them():
    r = confidence.rate(_c(passes=2, recent=True), 2.5, (2, 2))
    assert r.label == "Too early" and "too recent" in r.explain()
    done = validation.Summary("demolition", 2, False)
    assert confidence.rate(_c(recent=True), 2.5, (2, 2), done, True).label == "Confirmed"
    no = validation.Summary("none", 1, False)
    assert confidence.rate(_c(), 2.5, (2, 2), no, True).label == "Ruled out"


def test_verdicts_stay_out_for_viewers_who_cant_see_them():
    done = validation.Summary("demolition", 1, False)
    r = confidence.rate(_c(), 2.5, (2, 2), done, show_verdicts=False)
    assert r.label == "High" and "Verdicts" not in r.explain()
    assert [f.name for f in r.factors] == ["Strength", "Satellite tracks", "Time since"]


def test_share_links_round_trip():
    dates = np.array(["2023-01-03", "2023-06-01", "2024-01-05", "2024-12-30"],
                     dtype="datetime64[D]")
    q = share.query("lagos_x", {"lat": 6.481234, "lon": 3.391156}, "2023-06-01",
                    "2024-01-05", dates)
    link = share.parse(q)
    assert link["area"] == "lagos_x" and (link["lat"], link["lon"]) == (6.48123, 3.39116)
    assert share.date_range(dates, link["from"], link["to"]) == (1, 2)
    whole = share.query("lagos_x", {"lat": 1, "lon": 2}, "2023-01-03", "2024-12-30", dates)
    assert "from" not in whole
    assert share.parse("") is None
    assert "lat" not in share.parse("area=x&at=999,1")      # nonsense location ignored


def test_report_is_a_one_page_pdf_without_names(tmp_path):
    from s1drops.app import findings, report
    from s1drops.tests.test_clusters import _grid
    cube = _grid()
    c = {"rank": 1, "row": 2, "col": 2, "cells": [(2, 2), (2, 3)], "lat": 6.5, "lon": 3.4,
         "drop_db": 6.0, "area_ha": 2.0, "n_cells": 2, "date_after": "2023-03-01",
         "passes_after": 8, "recent": False}
    rating = confidence.rate(c, 2.5, (2, 2))
    sig = findings.RelativeSignal(dates=np.array(["2023-01-01", "2023-04-01"],
                                                 dtype="datetime64[D]"),
                                  pct=np.array([100.0, 30.0]), hover=["", ""],
                                  trend=np.array([100.0, 30.0]), band=(80.0, 120.0))
    rec = validation.Record.__new__(validation.Record)
    vals = dict(source="user", verdict="demolition", evidence=["s2"], status="active",
                user_name="Secret Person")
    rec.__dict__.update(vals)
    pdf = report.build(place="Test place", area_name="Test", c=c, rating=rating, events=[],
                       signal=sig, min_change=2.5, cube=cube,
                       mag=np.full((10, 10), 3.0), resolution=100.0, verdicts=[rec],
                       photos={"before": {"candidates": [{"rgb": np.zeros((20, 20, 3), "uint8"),
                                                          "date": "2023-01-01", "cloud": 0.1}]},
                               "after": {"candidates": []}, "before_idx": 0, "after_idx": 0})
    assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") <= 1
    assert b"Secret Person" not in pdf


def test_fast_pelt_matches_ruptures_exactly():
    import ruptures as rpt
    from s1drops.analysis.detect import pelt_l2
    rng = np.random.default_rng(1)
    for _ in range(200):
        n = int(rng.integers(5, 400))
        x = rng.normal(0, rng.uniform(0.2, 2), n)
        for k in rng.integers(0, n, rng.integers(0, 4)):
            x[k:] += rng.normal(0, 4)
        pen = float(rng.uniform(0.5, 30))
        assert pelt_l2(x, pen) == rpt.Pelt(model="l2", min_size=2).fit(x).predict(pen=pen)


def test_track_agreement_comes_from_each_tracks_own_drop():
    # Three tracks over a 4x4 grid; the change covers two cells.
    steps = np.full((3, 4, 4), np.nan, dtype="float32")
    steps[0, 1, 1:3] = [5.0, 4.0]       # fell on track 1
    steps[1, 1, 1:3] = [1.0, 0.5]       # barely moved on track 2
    c = {"cells": [(1, 1), (1, 2)], "row": 1, "col": 1}
    assert confidence.tracks_from_steps(steps, c, 2.5) == (1, 2)   # track 3: no data
    assert confidence.tracks_from_steps(steps, {"row": 0, "col": 0}, 2.5) is None


def test_hotspot_with_tracks_measures_every_track():
    from s1drops.app import logic
    from s1drops.tests.test_clusters import _grid
    cube = _grid()
    t, ny, nx = cube.sizes["time"], cube.sizes["y"], cube.sizes["x"]
    vv = np.full((t, ny, nx), 0.1) * np.random.default_rng(0).uniform(0.9, 1.1, (t, ny, nx))
    ro = cube["relative_orbit"].values
    vv[30:, 2:4, 2:4] *= 0.1                      # -10 dB on both tracks from mid-year
    vv[30:, 6, 6] = np.where(ro[30:] == 1, 0.01, 0.1)   # only track 1 sees this one
    cube["vv"] = (("time", "y", "x"), vv)
    mag, before, after, local, steps = logic.hotspot_with_tracks(cube)
    assert steps.shape == (2, ny, nx)
    both = {"cells": [(2, 2), (2, 3), (3, 2), (3, 3)], "row": 2, "col": 2}
    assert confidence.tracks_from_steps(steps, both, 3.0) == (2, 2)
    assert confidence.tracks_from_steps(steps, {"row": 6, "col": 6}, 3.0) == (1, 2)
