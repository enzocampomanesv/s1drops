import numpy as np
import pytest

from s1drops.analysis.detect import DetectionResult, Drop
from s1drops.analysis.series import Series
from s1drops.app import findings, plots

D = np.datetime64


def _res(orbit, dates, vals, drops=()):
    s = Series("vv", orbit, "ascending" if orbit % 2 else "descending",
               np.asarray(dates, dtype="datetime64[ns]"), np.asarray(vals, float), 0, 0)
    return DetectionResult(s, list(drops), "pelt")


def _drop(before, after, delta, pre=-7.0):
    return Drop(D(before), D(after), delta, pre, pre + delta, 12, "pelt")


def test_same_event_on_two_tracks_merges():
    a = _res(1, [], [], [_drop("2023-02-15", "2023-02-27", -10.0)])
    b = _res(2, [], [], [_drop("2023-02-21", "2023-03-05", -8.0)])
    c = _res(3, [], [], [_drop("2024-08-01", "2024-08-13", -4.0)])
    for r in (a, b, c):   # give each track some data so it counts as "with data"
        r.series.dates = np.array([D("2023-01-01")], dtype="datetime64[ns]")
        r.series.values_db = np.array([-7.0])
    ev = findings.events([a, b, c])
    assert len(ev) == 2
    assert (ev[0].tracks, ev[0].total_tracks) == (2, 3)
    assert ev[0].delta_db == pytest.approx(-9.0)          # mean across tracks
    assert ev[0].start == D("2023-02-15") and ev[0].end == D("2023-03-05")
    assert ev[1].tracks == 1


def test_drops_and_rises_stay_separate():
    r = _res(1, ["2023-01-01"], [-7.0], [_drop("2023-02-15", "2023-02-27", -6.0),
                                         _drop("2023-03-01", "2023-03-13", 5.0)])
    ev = findings.events([r])
    assert [e.is_drop for e in ev] == [True, False]


def test_words():
    assert findings.change_text(-10.0) == "~90% weaker"
    assert findings.change_text(-3.0) == "~50% weaker"
    assert findings.change_text(1.0) == "~25% stronger"
    assert findings.change_text(5.0) == "~3.2× stronger"
    assert findings.strength(-5.0, 2.5) == "Strong"
    assert findings.strength(-4.0, 2.5) == "Moderate"
    assert findings.strength(-2.6, 2.5) == "Slight"
    assert findings.when_text("2023-03-02", "2023-03-14") == "Mar 2023"
    assert findings.when_text("2023-02-25", "2023-03-05") == "late Feb – early Mar 2023"
    assert findings.when_text("2023-12-28", "2024-01-09") == "late Dec 2023 – early Jan 2024"


def test_relative_signal_puts_tracks_on_one_scale():
    dates = D("2023-01-01") + np.arange(10) * np.timedelta64(12, "D")
    # Two tracks at different absolute levels, both dropping 10 dB at pass 5.
    a = _res(1, dates, [-5.0] * 5 + [-15.0] * 5)
    b = _res(2, dates + np.timedelta64(6, "D"), [-9.0] * 5 + [-19.0] * 5)
    ev = [findings.Event(D(dates[4], "D"), D(dates[5], "D"), -10.0, 2, 2)]
    sig = findings.relative_signal([a, b], ev)
    assert len(sig.dates) == 20 and np.all(np.diff(sig.dates) >= np.timedelta64(0, "D"))
    assert np.allclose(sig.pct[:9], 100.0)          # before: both at their usual level
    assert np.allclose(sig.pct[-9:], 10.0)          # after: a tenth of it
    assert sig.band == pytest.approx((100.0, 100.0))
    assert "evening pass" in sig.hover[0] and "% of usual level" in sig.hover[0]


def test_simple_figure_labels_events_without_overlap():
    dates = D("2023-01-01") + np.arange(30) * np.timedelta64(12, "D")
    r = _res(1, dates, [-5.0] * 10 + [-15.0] * 20)
    ev = [findings.Event(D("2023-04-01"), D("2023-04-13"), -10.0, 1, 1),
          findings.Event(D("2023-05-01"), D("2023-05-13"), -4.0, 1, 1)]
    fig = plots.simple_figure(findings.relative_signal([r], ev), ev, min_change=2.5)
    labels = [a for a in fig.layout.annotations if "drop" in (a.text or "")]
    assert len(labels) == 2 and labels[0].y != labels[1].y    # stacked, not overprinted
    assert "Strong drop" in labels[0].text and "~90% weaker" in labels[0].text
    assert fig.layout.yaxis.ticksuffix == "%"


def test_photo_options_name_each_event_once_in_words():
    from s1drops.app.explore import _photo_options

    a = _res(1, ["2023-01-01"], [-7.0], [_drop("2023-02-15", "2023-02-27", -10.0)])
    b = _res(2, ["2023-01-01"], [-7.0], [_drop("2023-02-21", "2023-03-05", -8.0),
                                         _drop("2024-08-01", "2024-08-13", -3.0)])
    opts = _photo_options(findings.events([a, b]), 2.5)
    assert [o["label"] for o in opts] == ["Strong drop · Mar 2023", "Slight drop · Aug 2024"]
    assert (opts[0]["date_before"], opts[0]["date_after"]) == ("2023-02-15", "2023-03-05")


def test_simple_figure_marks_the_table_drop():
    dates = D("2023-01-01") + np.arange(10) * np.timedelta64(12, "D")
    r = _res(1, dates, [-5.0] * 5 + [-8.0] * 5)
    ev = [findings.Event(D(dates[4], "D"), D(dates[5], "D"), -3.0, 1, 1)]
    sig = findings.relative_signal([r], ev)
    fig = plots.simple_figure(sig, ev, min_change=2.5,
                              table_drop={"date": "2023-03-02", "drop_db": 3.0,
                                          "avg_drop_db": None})
    assert [t.name for t in fig.data][-1] == "Drop in table (−3.0 dB)"
    assert fig.data[-1].y[0] == pytest.approx(50.1, abs=0.1)        # -3 dB = half
    fig = plots.simple_figure(sig, ev, min_change=2.5,
                              table_drop={"date": "2023-03-02", "drop_db": 6.0,
                                          "avg_drop_db": 3.0})
    assert [t.name for t in fig.data][-2:] == ["Drop in table (−6.0 dB)",
                                               "Whole-period average (−3.0 dB)"]
