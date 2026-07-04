import numpy as np
import xarray as xr
from pyproj import Transformer

from s1drops.analysis.series import (
    Series,
    cell_index,
    extract_series,
    to_db,
)


def test_to_db_guards_nonpositive():
    out = to_db([1.0, 0.0, -0.5, 10.0])
    assert abs(out[0] - 0.0) < 1e-9          # 10log10(1) = 0
    assert np.isnan(out[1]) and np.isnan(out[2])
    assert abs(out[3] - 10.0) < 1e-9         # 10log10(10) = 10


def test_series_filter_with_padding():
    dates = np.datetime64("2024-01-01") + np.arange(10) * np.timedelta64(12, "D")
    s = Series("vv", 1, "ascending", dates, np.arange(10.0), 0, 0)
    # window covering samples 3..6, pad 1 -> samples 2..7
    f = s.filter(str(dates[3].astype("datetime64[D]")), str(dates[6].astype("datetime64[D]")), pad=1)
    assert f.values_db.tolist() == [2, 3, 4, 5, 6, 7]
    # pad clipped at the left edge
    f0 = s.filter(str(dates[0].astype("datetime64[D]")), str(dates[1].astype("datetime64[D]")), pad=2)
    assert f0.values_db[0] == 0.0


def _fake_index_cube():
    nx, ny = 10, 8
    xs = 500000 + 100 * (np.arange(nx) + 0.5)
    ys = 700000 - 100 * (np.arange(ny) + 0.5)
    ds = xr.Dataset(coords={"x": ("x", xs.astype("float64")), "y": ("y", ys.astype("float64"))})
    ds.attrs.update({"crs": "EPSG:32631", "resolution": 100.0})
    return ds


def test_cell_index_nearest_and_out_of_bounds():
    cube = _fake_index_cube()
    xs, ys = cube["x"].values, cube["y"].values
    tx = Transformer.from_crs("EPSG:32631", "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(xs[4], ys[3])
    assert cell_index(cube, lon, lat) == (3, 4)
    # far away -> None
    assert cell_index(cube, 0.0, 0.0) is None


def _fake_data_cube():
    # 4 passes: ro=1 ascending (2), ro=95 descending (2); 2x2 cells
    times = np.array(
        ["2024-01-02", "2024-01-05", "2024-01-14", "2024-01-17"], dtype="datetime64[ns]"
    )
    ro = np.array([95, 1, 95, 1], dtype="int32")
    state = np.array(["descending", "ascending", "descending", "ascending"], dtype="<U10")
    vv = np.arange(4 * 2 * 2, dtype="float32").reshape(4, 2, 2) + 1.0
    vh = vv / 2.0
    ds = xr.Dataset(
        {"vv": (("time", "y", "x"), vv), "vh": (("time", "y", "x"), vh)},
        coords={
            "time": times, "y": [0, 1], "x": [0, 1],
            "relative_orbit": ("time", ro), "orbit_state": ("time", state),
        },
    )
    ds.attrs["resolution"] = 100.0
    return ds


def test_extract_series_splits_by_pol_and_orbit():
    cube = _fake_data_cube()
    series = extract_series(cube, yi=0, xi=0)
    # 3 pols x 2 relative orbits = 6 series
    assert len(series) == 6
    keys = {(s.pol, s.relative_orbit) for s in series}
    assert keys == {(p, o) for p in ("vv", "vh", "vv_vh") for o in (1, 95)}
    # within a track, dates are ascending
    for s in series:
        assert list(s.dates) == sorted(s.dates)
    # VV/VH equals VV_dB - VH_dB
    vv = next(s for s in series if s.pol == "vv" and s.relative_orbit == 1)
    vh = next(s for s in series if s.pol == "vh" and s.relative_orbit == 1)
    ratio = next(s for s in series if s.pol == "vv_vh" and s.relative_orbit == 1)
    np.testing.assert_allclose(ratio.values_db, vv.values_db - vh.values_db)
