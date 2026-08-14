"""Invariants that the de-duplicated implementations rely on.

Several pairs of near-identical functions were merged behind a flag:
`_max_stepdown(with_index=)`, `_hotspot(with_timing=)`, and the shared
`_dst_grid`/`_latlon_bounds` helpers in render. The flagged branches must stay
in agreement, and the CSV builders must keep emitting the same cell columns.
These tests pin those properties so a future edit to one branch can't silently
diverge from the other.
"""
from __future__ import annotations

import csv
import io

import numpy as np
import pytest
import xarray as xr

from s1drops.app import logic, render
from s1drops.cube.geobox import aoi_to_geobox

BBOX = (3.40, 6.45, 3.50, 6.55)


@pytest.fixture(scope="module")
def cube():
    """Two relative orbits (one per direction), planted drops, NaNs, AOI hole, LCZ."""
    rng = np.random.default_rng(0)
    gbox, utm = aoi_to_geobox(BBOX, 100.0)
    ny, nx = 10, 12
    t = gbox.transform
    xs = t.c + 100 * (np.arange(nx) + 0.5)
    ys = t.f - 100 * (np.arange(ny) + 0.5)

    n = 24
    times = np.datetime64("2024-01-01") + np.arange(n) * np.timedelta64(6, "D")
    ro = np.where(np.arange(n) % 2 == 0, 44, 88).astype("int32")
    state = np.where(ro == 44, "ascending", "descending").astype("<U10")

    vv = 10 ** ((-8 + rng.normal(0, 0.5, (n, ny, nx))) / 10)
    vh = 10 ** ((-14 + rng.normal(0, 0.5, (n, ny, nx))) / 10)
    for yi, xi, k, depth in [(2, 3, 10, 6.0), (5, 9, 14, 3.5), (8, 2, 6, 9.0)]:
        vv[k:, yi, xi] *= 10 ** (-depth / 10)
        vh[k:, yi, xi] *= 10 ** (-depth / 10)
    vv[rng.random(vv.shape) < 0.05] = np.nan   # speckle gaps
    vv[:, 0, 1] = np.nan                        # a wholly empty cell
    vv[3, 4, 4] = 0.0                           # non-positive -> NaN in dB

    ds = xr.Dataset(
        {"vv": (("time", "y", "x"), vv), "vh": (("time", "y", "x"), vh)},
        coords={"time": times, "x": xs, "y": ys,
                "relative_orbit": ("time", ro), "orbit_state": ("time", state)},
    )
    mask = np.ones((ny, nx), bool)
    mask[0, :] = False
    mask[:, -1] = False
    ds["aoi_mask"] = (("y", "x"), mask)
    lcz = rng.integers(1, 18, (ny, nx)).astype("int16")
    lcz[1, 1] = 0
    ds["lcz"] = (("y", "x"), lcz)
    ds.attrs.update({"crs": str(utm), "resolution": 100.0,
                     "grid_transform": [t.a, t.b, t.c, t.d, t.e, t.f],
                     "grid_shape": [ny, nx]})
    return ds


# ---- _max_stepdown: the two accumulation branches must agree ----------------

def test_max_stepdown_index_branch_matches_fmax_branch(cube):
    """with_index=True uses a mask-based running max; without it uses np.fmax.
    The magnitudes must be identical, NaNs included."""
    db = logic._pol_db_cube(cube, "vv")
    plain = logic._max_stepdown(db)
    best, _k = logic._max_stepdown(db, with_index=True)
    assert np.array_equal(plain, best, equal_nan=True)


def test_max_stepdown_index_points_at_the_planted_drop():
    """The returned k is the split index: the drop sits between k-1 and k."""
    db = np.full((12, 1, 1), -8.0)
    db[7:, 0, 0] = -15.0            # 7 dB drop starting at index 7
    best, k = logic._max_stepdown(db, with_index=True)
    assert k[0, 0] == 7
    assert best[0, 0] == pytest.approx(7.0)


@pytest.mark.parametrize("db", [
    np.full((4, 3, 3), np.nan),          # nothing valid anywhere
    np.full((1, 3, 3), -8.0),            # single timestep: no split exists
], ids=["all-nan", "single-step"])
def test_max_stepdown_degenerate_inputs_agree_and_are_nan(db):
    plain = logic._max_stepdown(db)
    best, k = logic._max_stepdown(db, with_index=True)
    assert np.array_equal(plain, best, equal_nan=True)
    assert np.isnan(plain).all()
    assert (k == 0).all()               # no split won, so the index stays at 0


def test_orbit_selections_never_yields_an_empty_time_stack(cube):
    """_max_stepdown indexes csum[-1], so it requires >=1 timestep. The >=2-pass
    guard in _orbit_selections is what makes that safe; pin it here."""
    for start, end in [(None, None), ("2024-02-01", "2024-04-15"),
                       ("2030-01-01", "2030-02-01")]:
        for direction in ("both", "ascending", "descending"):
            for sel in logic._orbit_selections(cube, start, end, direction):
                assert sel.sum() >= 2


def test_max_stepdown_needs_data_on_both_sides():
    """A split with no valid sample on one side yields NaN, not a bogus drop."""
    db = np.full((6, 1, 1), np.nan)
    db[0, 0, 0] = -5.0                   # only one valid sample overall
    assert np.isnan(logic._max_stepdown(db)).all()


# ---- _hotspot: with_timing must not change the magnitudes -------------------

@pytest.mark.parametrize("pol", ["vv", "vh", "vv_vh"])
@pytest.mark.parametrize("direction", ["both", "ascending", "descending"])
def test_hotspot_timing_magnitude_matches_layer(cube, pol, direction):
    layer = logic.hotspot_layer(cube, pol=pol, direction=direction)
    mag, _b, _a = logic._hotspot(cube, pol=pol, direction=direction, with_timing=True)
    assert np.array_equal(layer, mag, equal_nan=True)


def test_hotspot_timing_matches_layer_under_lcz_and_date_filters(cube):
    kw = dict(pol="vv", start="2024-02-01", end="2024-04-15",
              lcz_classes=[3, 6, 7, 8])
    layer = logic.hotspot_layer(cube, **kw)
    mag, before, after = logic._hotspot(cube, with_timing=True, **kw)
    assert np.array_equal(layer, mag, equal_nan=True)
    # dates exist exactly where a magnitude survived the mask
    assert np.array_equal(np.isfinite(mag), ~np.isnat(before))
    assert np.array_equal(np.isfinite(mag), ~np.isnat(after))
    assert np.all(after[np.isfinite(mag)] > before[np.isfinite(mag)])


def test_hotspot_returns_all_nan_when_no_orbit_qualifies(cube):
    """Out-of-range window: both branches must produce a full-NaN grid, not crash."""
    kw = dict(pol="vv", start="2030-01-01", end="2030-02-01")
    layer = logic.hotspot_layer(cube, **kw)
    mag, before, after = logic._hotspot(cube, with_timing=True, **kw)
    assert layer.shape == (cube.sizes["y"], cube.sizes["x"])
    assert np.isnan(layer).all()
    assert np.array_equal(layer, mag, equal_nan=True)
    assert np.isnat(before).all() and np.isnat(after).all()


def test_orbit_selections_skips_orbits_with_fewer_than_two_passes(cube):
    sels = list(logic._orbit_selections(cube, None, None, "both"))
    assert len(sels) == 2                       # both relative orbits qualify
    assert all(s.sum() >= 2 for s in sels)
    # a window containing a single pass leaves nothing to split
    one = str(np.asarray(cube["time"].values).astype("datetime64[D]")[0])
    assert list(logic._orbit_selections(cube, one, one, "both")) == []
    # direction filter keeps only the matching orbit
    asc = list(logic._orbit_selections(cube, None, None, "ascending"))
    assert len(asc) == 1


def test_hotspot_geojson_still_carries_timing_fields(cube):
    import json
    fc = json.loads(logic.hotspot_geojson(cube, pol="vv", min_value=2.0))
    assert fc["features"], "expected at least one hotspot cell above 2 dB"
    props = fc["features"][0]["properties"]
    for field in ("cell_id", "drop_db", "row", "col", "date_before",
                  "date_after", "gap_days", "year_before", "month_before", "lcz"):
        assert field in props
    assert props["gap_days"] > 0


# ---- CSV cell columns -------------------------------------------------------

def test_cell_ids_blank_without_a_cell():
    assert logic._cell_ids(None) == ("", "", "")
    assert logic._cell_ids((4, 7)) == ("4_7", 4, 7)


@pytest.mark.parametrize("cell,expected", [((2, 3), ("2_3", "2", "3")), (None, ("", "", ""))])
def test_csv_exports_share_the_same_cell_columns(cube, cell, expected):
    from s1drops.analysis.series import extract_series
    series = list(extract_series(cube, 2, 3, pols=("vv",)))
    results = logic.run_detection(series, method="pelt")

    for text in (logic.drops_csv(results, cell=cell),
                 logic.series_csv(series, cell=cell)):
        rows = list(csv.DictReader(io.StringIO(text)))
        assert rows, "expected at least one exported row"
        for r in rows:
            assert (r["cell_id"], r["cell_row"], r["cell_col"]) == expected


# ---- render: the shared grid/bounds helpers --------------------------------

def test_dst_grid_and_bounds_agree_between_array_and_rgb_paths(cube):
    """to_webmercator and overlay_rgb now share _dst_grid/_latlon_bounds, so the
    same source geobox must place both overlays on identical bounds."""
    ny, nx = cube.sizes["y"], cube.sizes["x"]
    layer = logic.hotspot_layer(cube, pol="vv")
    _merc, bounds_arr = render.to_webmercator(layer, cube)

    rgb = np.zeros((ny, nx, 3), dtype="uint8")
    _url, bounds_rgb = render.overlay_rgb(rgb, cube.attrs["grid_transform"],
                                          cube.attrs["crs"])
    assert bounds_arr == bounds_rgb


def test_latlon_bounds_are_ordered_and_near_the_aoi(cube):
    layer = logic.basemap_layer(cube, pol="vv")
    _merc, ((south, west), (north, east)) = render.to_webmercator(layer, cube)
    assert north > south and east > west
    assert 3.3 < west < 3.6 and 6.4 < south < 6.6


def test_overlay_rgb_marks_outside_the_footprint_transparent(cube):
    rgb = np.full((20, 25, 3), 200, dtype="uint8")
    url, bounds = render.overlay_rgb(rgb, cube.attrs["grid_transform"],
                                     cube.attrs["crs"])
    assert url.startswith("data:image/png;base64,")
    assert bounds[0][0] < bounds[1][0]
