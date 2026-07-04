import numpy as np

from s1drops.analysis.detect import detect_pelt, detect_threshold
from s1drops.analysis.series import Series


def make_series(levels, n_each, *, sigma=0.3, step_days=12, seed=0, start="2024-01-01"):
    rng = np.random.default_rng(seed)
    vals = np.concatenate([np.full(n, lv, dtype="float64") for lv, n in zip(levels, n_each)])
    vals = vals + rng.normal(0, sigma, vals.size)
    dates = np.datetime64(start) + np.arange(vals.size) * np.timedelta64(step_days, "D")
    return Series("vv", 1, "ascending", dates, vals, 0, 0)


def test_pelt_recovers_injected_drop():
    s = make_series([-7.0, -12.0], [15, 15], sigma=0.3, seed=1)
    res = detect_pelt(s, sensitivity=1.0, min_drop_db=2.5)
    assert len(res.drops) == 1
    drop = res.drops[0]
    assert -6.5 < drop.delta_db < -4.0          # ~ -5 dB
    assert drop.date_after == s.dates[15]        # change between 14 and 15
    assert drop.gap_days == 12


def test_threshold_recovers_injected_drop():
    s = make_series([-7.0, -12.0], [15, 15], sigma=0.2, seed=2)
    res = detect_threshold(s, window=2, min_drop_db=3.0)
    assert len(res.drops) == 1
    assert res.drops[0].date_after == s.dates[15]


def test_no_false_positive_on_flat_series():
    s = make_series([-7.0], [30], sigma=0.3, seed=3)
    assert len(detect_pelt(s, sensitivity=1.0, min_drop_db=2.5).drops) == 0
    assert len(detect_threshold(s, window=2, min_drop_db=3.0).drops) == 0


def test_direction_filter_excludes_rises():
    s = make_series([-12.0, -7.0], [15, 15], sigma=0.3, seed=4)  # a rise
    assert len(detect_pelt(s, direction="drop").drops) == 0
    rises = detect_pelt(s, direction="rise").drops
    assert len(rises) == 1 and rises[0].delta_db > 0


def test_threshold_merges_consecutive_steps():
    # sharp two-sample descent at indices 10 and 11 -> one merged event
    vals = np.concatenate([
        np.full(10, -6.0), np.array([-9.0, -12.0]), np.full(10, -12.0)
    ])
    rng = np.random.default_rng(5)
    vals = vals + rng.normal(0, 0.05, vals.size)
    dates = np.datetime64("2024-01-01") + np.arange(vals.size) * np.timedelta64(12, "D")
    s = Series("vv", 1, "ascending", dates, vals, 0, 0)
    res = detect_threshold(s, window=2, min_drop_db=2.0)
    assert len(res.drops) == 1
    d = res.drops[0]
    assert d.delta_db < -4.5                      # spans full -6 dB descent
    assert d.date_before == s.dates[9] and d.date_after == s.dates[11]
    assert d.gap_days == 24                        # two 12-day steps


def test_pelt_segments_exposed_for_plotting():
    s = make_series([-7.0, -12.0], [15, 15], sigma=0.3, seed=6)
    res = detect_pelt(s, min_drop_db=2.5)
    assert res.seg_bounds[0] == 0 and res.seg_bounds[-1] == len(s)
    assert len(res.seg_means) == len(res.seg_bounds) - 1
    assert res.sigma_db is not None and res.sigma_db > 0
