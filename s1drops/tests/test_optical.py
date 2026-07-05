from datetime import datetime
from types import SimpleNamespace

import numpy as np

from s1drops import optical


def _item(idx, day):
    return SimpleNamespace(id=f"s{idx}", datetime=datetime(2024, 1, day, 10, 0))


def _scl_frac(frac, n=100):
    """SCL array (class 9 = cloud, 4 = veg) with the given cloud fraction."""
    a = np.full(n, 4, dtype="uint8")
    a[: int(round(frac * n))] = 9
    return a


def _loader(clouds):
    """Injected load_scl mapping item.id -> desired AOI cloud fraction."""
    return lambda it, gbox: _scl_frac(clouds[it.id])


# ---- cloud fraction ----

def test_aoi_cloud_fraction_excludes_nodata_and_respects_mask():
    scl = np.array([0, 4, 9, 9])                     # nodata, veg, cloud, cloud
    assert optical.aoi_cloud_fraction(scl) == 1.0 * 2 / 3        # 2 cloud of 3 valid
    mask = np.array([True, True, True, False])       # drop the last cloud
    assert optical.aoi_cloud_fraction(scl, mask) == 0.5          # 1 cloud of 2 valid
    assert optical.aoi_cloud_fraction(np.zeros(5)) == 1.0        # all nodata -> unusable


# ---- ranking / candidates ----

def _rank(items, anchor, side, clouds, thresh=0.1, bucket=5, cap=24, n=3):
    return optical.rank_scenes(items, anchor, side, None, None, 45, thresh, bucket, cap, n,
                               _loader(clouds))


def test_rank_respects_sidedness():
    items = [_item(1, 5), _item(2, 10), _item(3, 20)]
    clouds = {"s1": 0.0, "s2": 0.0, "s3": 0.0}
    picks, _ = _rank(items, "2024-01-15", "before", clouds)
    assert all(p.date <= np.datetime64("2024-01-15") for p in picks)   # never Jan20
    assert picks[0].date == np.datetime64("2024-01-10")                # nearest first (same bucket)
    picks, _ = _rank(items, "2024-01-15", "after", clouds)
    assert picks[0].date == np.datetime64("2024-01-20")


def test_rank_clearer_bucket_beats_nearer():
    # 8% one day away vs 2% ten days away, both under 10%: the clearer bucket wins.
    items = [_item(1, 14), _item(2, 5)]
    clouds = {"s1": 0.08, "s2": 0.02}
    picks, _ = _rank(items, "2024-01-15", "before", clouds, thresh=0.15)
    assert picks[0].date == np.datetime64("2024-01-05")   # 2% (band 0) over nearer 8% (band 2)
    assert picks[1].date == np.datetime64("2024-01-14")


def test_rank_same_bucket_prefers_nearer():
    # 3% one day away vs 4% ten days away: same 5% bucket -> nearer wins.
    items = [_item(1, 14), _item(2, 5)]
    clouds = {"s1": 0.03, "s2": 0.04}
    picks, _ = _rank(items, "2024-01-15", "before", clouds)
    assert picks[0].date == np.datetime64("2024-01-14")


def test_rank_hard_gate_excludes_over_threshold():
    items = [_item(1, 5), _item(2, 10), _item(3, 13)]
    clouds = {"s1": 0.30, "s2": 0.05, "s3": 0.50}    # only Jan10 under 10%
    picks, _ = _rank(items, "2024-01-15", "before", clouds, thresh=0.1, n=3)
    assert [p.date for p in picks] == [np.datetime64("2024-01-10")]   # cloudy ones excluded
    assert all(p.cloud <= 0.1 for p in picks)


def test_rank_none_under_threshold_returns_empty():
    items = [_item(1, 5), _item(2, 10)]
    clouds = {"s1": 0.30, "s2": 0.40}                # nothing under 10%
    picks, probed = _rank(items, "2024-01-15", "before", clouds, thresh=0.1)
    assert picks == [] and probed == 2               # probed both, none qualified


def test_rank_probe_cap_bounds_evaluations():
    items = [_item(1, 14), _item(2, 12), _item(3, 5)]
    clouds = {"s1": 0.5, "s2": 0.5, "s3": 0.0}
    picks, probed = _rank(items, "2024-01-15", "before", clouds, cap=2, n=3)
    assert probed == 2                                    # far clear Jan05 not probed
    assert picks == []                                   # the two probed are over threshold


def test_rank_empty_window():
    picks, probed = _rank([_item(1, 5)], "2024-06-15", "before", {"s1": 0.0})
    assert picks == [] and probed == 0


# ---- rendering + options ----

def test_stretch_and_true_colour_shape():
    band = np.linspace(0, 3000, 100, dtype="float32").reshape(10, 10)
    out = optical.stretch_band(band)
    assert out.dtype == np.uint8 and out.min() == 0 and out.max() == 255
    rgb = optical.to_true_colour(band, band, band)
    assert rgb.shape == (10, 10, 3) and rgb.dtype == np.uint8


def test_drop_options_sorted_by_magnitude():
    from s1drops.app import logic

    def drop(delta, b, a):
        return SimpleNamespace(delta_db=delta,
                               date_before=np.datetime64(b), date_after=np.datetime64(a))

    results = [SimpleNamespace(drops=[drop(-1.5, "2024-01-01", "2024-01-13"),
                                      drop(-4.2, "2024-03-01", "2024-03-13")])]
    opts = logic.drop_options(results)
    assert [o["delta_db"] for o in opts] == [-4.2, -1.5]     # largest magnitude first
    assert "-4.2 dB" in opts[0]["label"] and "2024-03-01 → 2024-03-13" in opts[0]["label"]


def test_overlay_rgb_reprojects_with_ordered_bounds():
    from s1drops.app import render
    rgb = np.zeros((10, 10, 3), dtype="uint8")
    rgb[:, :, 0] = 200
    transform = (10.0, 0.0, 500000.0, 0.0, -10.0, 700000.0)  # UTM 31N, 10 m pixels
    url, bounds = render.overlay_rgb(rgb, transform, "EPSG:32631")
    assert url.startswith("data:image/png;base64,")
    (s, w), (n, e) = bounds
    assert s < n and w < e                       # [[south, west], [north, east]]
    assert 0 < w < 6 and 5 < s < 8               # ~3E, ~6.3N for that UTM origin


def test_fit_zoom_frames_smaller_aoi_tighter():
    from s1drops.app.explore import _fit_zoom
    z_small = _fit_zoom([[6.30, 3.30], [6.32, 3.32]])   # ~2 km box
    z_large = _fit_zoom([[6.0, 3.0], [6.5, 3.5]])        # ~0.5 deg AOI
    assert 1 <= z_large < z_small <= 19                  # smaller AOI -> higher zoom
