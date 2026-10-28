import numpy as np
import pytest
import xarray as xr

from s1drops.app import logic
from s1drops.analysis.series import extract_area_series

NAT = np.datetime64("NaT", "D")


def _grid(ny=8, nx=8, res=100.0):
    """A bare UTM grid (Lagos, zone 31N) with the attrs find_clusters needs, and
    passes on two tracks every 12 days through 2023 (to count passes after a drop)."""
    xs = 540_000 + res * (np.arange(nx) + 0.5)
    ys = 720_000 - res * (np.arange(ny) + 0.5)
    t = np.datetime64("2023-01-01") + np.arange(30) * np.timedelta64(12, "D")
    times = np.sort(np.concatenate([t, t + np.timedelta64(6, "D")]))
    ro = np.tile([1, 2], 30)
    return xr.Dataset(coords={
        "x": xs, "y": ys, "time": times.astype("datetime64[ns]"),
        "relative_orbit": ("time", ro),
        "orbit_state": ("time", np.where(ro == 1, "ascending", "descending")),
    }, attrs={"crs": "EPSG:32631", "resolution": res})


def _layers(ny=8, nx=8):
    mag = np.full((ny, nx), np.nan)
    before = np.full((ny, nx), NAT, dtype="datetime64[D]")
    after = np.full((ny, nx), NAT, dtype="datetime64[D]")
    return mag, before, after


def _mark(layers, cells, drop, day):
    mag, before, after = layers
    for y, x in cells:
        mag[y, x] = drop
        after[y, x] = np.datetime64(day, "D")
        before[y, x] = after[y, x] - np.timedelta64(12, "D")


BLOCK = [(1, 1), (1, 2), (2, 1), (2, 2), (3, 2)]      # 5 touching cells


def test_touching_cells_form_one_area_and_lone_cells_are_dropped():
    cube, lay = _grid(), _layers()
    _mark(lay, BLOCK, 6.0, "2023-03-01")
    _mark(lay, [(6, 6)], 12.0, "2023-03-01")               # strong but alone
    areas, labels = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)
    assert len(areas) == 1
    a = areas[0]
    assert a["n_cells"] == 5 and a["area_ha"] == pytest.approx(5.0)
    assert sorted(a["cells"]) == sorted(BLOCK)
    assert a["drop_db"] == pytest.approx(6.0) and a["date_after"] == "2023-03-01"
    assert labels[1, 1] == 0 and labels[6, 6] == -1
    assert (a["row"], a["col"]) in BLOCK                  # placed on a member cell
    (s, w), (n, e) = a["bounds"]
    assert s < a["lat"] < n and w < a["lon"] < e


def test_area_beats_magnitude_and_min_area_is_respected():
    cube, lay = _grid(), _layers()
    _mark(lay, BLOCK, 4.0, "2023-03-01")                   # bigger, weaker
    _mark(lay, [(6, 5), (6, 6)], 9.0, "2023-03-01")        # smaller, stronger
    areas, _ = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=1.0)
    assert [a["n_cells"] for a in areas] == [5, 2]
    areas, _ = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)
    assert [a["n_cells"] for a in areas] == [5]
    areas, _ = logic.find_clusters(cube, *lay, min_value=5.0, min_area_ha=1.0)
    assert [a["n_cells"] for a in areas] == [2]            # weak block below the minimum


def test_neighbours_at_different_times_are_separate_areas():
    cube, lay = _grid(), _layers()
    _mark(lay, [(1, 1), (1, 2), (2, 1), (2, 2)], 6.0, "2023-03-01")
    _mark(lay, [(1, 3), (2, 3), (1, 4), (2, 4)], 5.0, "2023-09-01")   # touching, months later
    areas, labels = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)
    assert len(areas) == 2 and labels[1, 2] != labels[1, 3]
    # Within the matching window they merge.
    _mark(lay, [(1, 3), (2, 3), (1, 4), (2, 4)], 5.0, "2023-03-13")
    areas, _ = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)
    assert [a["n_cells"] for a in areas] == [8]


def test_area_size_scales_with_resolution():
    cube, lay = _grid(ny=30, nx=30, res=10.0), _layers(30, 30)
    cells = [(y, x) for y in range(5, 25) for x in range(5, 25)]       # 400 cells = 4 ha
    _mark(lay, cells, 6.0, "2023-03-01")
    areas, _ = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)
    assert len(areas) == 1 and areas[0]["area_ha"] == pytest.approx(4.0)
    lay[0][5, 5] = np.nan                                              # 399 cells
    assert logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)[0] == []


def test_area_outline_is_the_union_of_cells():
    cube = _grid()
    gj = logic.area_outline(cube, BLOCK)
    assert gj["geometry"]["type"] == "Polygon"             # touching squares merge
    lon, lat = gj["geometry"]["coordinates"][0][0]
    assert 2.5 < lon < 3.5 and 6.0 < lat < 7.0


def test_area_reports_the_local_drop_where_it_is_larger():
    cube, lay = _grid(), _layers()
    _mark(lay, BLOCK, 4.0, "2023-03-01")
    local = np.where(np.isfinite(lay[0]), 3.0, np.nan)     # smaller locally: keep 4.0
    local[1, 1] = local[1, 2] = local[2, 1] = 7.0          # larger in 3 of 5 cells
    (a,), _ = logic.find_clusters(cube, *lay, local, min_value=2.5, min_area_ha=4.0)
    assert a["drop_db"] == pytest.approx(7.0)              # median of 7, 7, 7, 4, 4
    assert a["avg_drop_db"] == pytest.approx(4.0)
    (a,), _ = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)
    assert a["drop_db"] == a["avg_drop_db"] == pytest.approx(4.0)


def test_local_step_uses_the_passes_next_to_the_split():
    # Drops 6 dB at index 6, then recovers by 4 dB at index 10: the whole-period
    # step understates the drop, the local one sees all of it.
    s = np.array([0.0] * 6 + [-6.0] * 4 + [-2.0] * 6)[:, None, None]
    mag, k = logic._max_stepdown(s, with_index=True)
    assert k[0, 0] == 6
    assert mag[0, 0] < 4.0
    assert logic._local_step(s, k)[0, 0] == pytest.approx(6.0)
    s[4, 0, 0] = np.nan                                    # gaps are skipped, not zeros
    assert logic._local_step(s, k)[0, 0] == pytest.approx(6.0)


def test_recent_areas_are_flagged_and_ranked_last():
    cube, lay = _grid(), _layers()
    _mark(lay, [(y, x) for y in range(4) for x in range(4)], 6.0, "2023-12-01")   # big, late
    _mark(lay, [(6, 5), (6, 6), (7, 5), (7, 6)], 5.0, "2023-03-01")               # small, early
    areas, labels = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0)
    assert [a["n_cells"] for a in areas] == [4, 16]
    assert [a["recent"] for a in areas] == [False, True]
    assert areas[1]["passes_after"] == 2
    assert labels[0, 0] == 1 and labels[6, 6] == 0         # labels follow the new order
    # Cut the period short and even the early drop is unconfirmed.
    areas, _ = logic.find_clusters(cube, *lay, min_value=2.5, min_area_ha=4.0,
                                   end="2023-03-20")
    assert all(a["recent"] for a in areas)


def test_passes_after_counts_the_best_covered_track():
    cube = _grid()
    assert logic.passes_after(cube, "2023-11-01") == 5     # track 2: Nov 3 .. Dec 21
    assert logic.passes_after(cube, "2023-11-01", direction="ascending") == 4
    flagged = logic.flag_recent(cube, [{"date_after": "2023-10-01"},
                                       {"date_after": "2023-12-20"}, {"date_after": None}])
    assert [c["recent"] for c in flagged] == [False, True, True]


def test_area_series_is_the_mean_in_linear_power():
    from s1drops.tests.test_app import _cube
    cube = _cube()                     # cell (1, 1) drops to -12 dB, others flat at ~-7
    (s,) = extract_area_series(cube, [(1, 1), (1, 2)], pols=("vv",))
    expected = 10 * np.log10((10 ** (-1.2) + 0.2) / 2)
    assert s.values_db[-1] == pytest.approx(expected, abs=1e-4)
    assert (s.yi, s.xi) == (1, 1)
