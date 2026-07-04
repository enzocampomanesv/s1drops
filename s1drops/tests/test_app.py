import numpy as np
import xarray as xr

from s1drops.app import logic, plots, render
from s1drops.analysis.detect import detect_pelt
from s1drops.analysis.series import Series
from s1drops.cube.geobox import aoi_to_geobox

SMALL_BBOX = (3.40, 6.45, 3.45, 6.50)


def _cube(ny=3, nx=3, n=20, drop_cell=(1, 1)):
    gbox, utm = aoi_to_geobox(SMALL_BBOX, 100.0)
    # force a known small shape for the test grid
    ny, nx = 3, 3
    t = gbox.transform
    xs = t.c + 100 * (np.arange(nx) + 0.5)
    ys = t.f - 100 * (np.arange(ny) + 0.5)
    times = np.datetime64("2024-01-01") + np.arange(n) * np.timedelta64(12, "D")

    vv = np.full((n, ny, nx), 0.2, dtype="float32")  # ~ -7 dB flat
    yi, xi = drop_cell
    vv[:n // 2, yi, xi] = 10 ** (-6 / 10)   # -6 dB
    vv[n // 2:, yi, xi] = 10 ** (-12 / 10)  # -12 dB (a clear drop)

    ds = xr.Dataset(
        {"vv": (("time", "y", "x"), vv), "vh": (("time", "y", "x"), vv / 2)},
        coords={
            "time": times, "x": xs, "y": ys,
            "relative_orbit": ("time", np.full(n, 1, dtype="int32")),
            "orbit_state": ("time", np.array(["ascending"] * n, dtype="<U10")),
        },
    )
    mask = np.ones((ny, nx), dtype=bool)
    mask[0, 0] = False  # one cell outside AOI
    ds["aoi_mask"] = (("y", "x"), mask)
    tr = gbox.transform
    ds.attrs.update({
        "crs": str(utm), "resolution": 100.0,
        "grid_transform": [tr.a, tr.b, tr.c, tr.d, tr.e, tr.f],
        "grid_shape": [ny, nx],
    })
    return ds


def test_to_webmercator_bounds_near_aoi():
    cube = _cube()
    arr = np.random.rand(cube.sizes["y"], cube.sizes["x"])
    merc, bounds = render.to_webmercator(arr, cube)
    (south, west), (north, east) = bounds
    assert 3.3 < west < 3.6 and 6.4 < south < 6.6
    assert north > south and east > west
    assert merc.shape[0] > 0 and merc.shape[1] > 0


def test_colorize_alpha_from_nan():
    arr = np.array([[1.0, np.nan], [2.0, 3.0]])
    rgba = render.colorize(arr, cmap="viridis")
    assert rgba.shape == (2, 2, 4)
    assert rgba[0, 1, 3] == 0          # NaN -> transparent
    assert rgba[0, 0, 3] == 255        # finite -> opaque


def test_overlay_returns_data_url():
    cube = _cube()
    url, bounds = render.overlay_from_array(np.random.rand(3, 3), cube, cmap="gray")
    assert url.startswith("data:image/png;base64,")
    assert len(bounds) == 2 and len(bounds[0]) == 2


def test_basemap_layer_median_and_aoi_mask():
    cube = _cube()
    layer = logic.basemap_layer(cube, pol="vv")
    assert layer.shape == (3, 3)
    assert np.isnan(layer[0, 0])                      # masked cell
    assert abs(layer[2, 2] - 10 * np.log10(0.2)) < 0.1  # flat cell ~ -7 dB


def test_hotspot_peaks_at_drop_cell():
    cube = _cube(drop_cell=(1, 1))
    hot = logic.hotspot_layer(cube, pol="vv")
    assert np.isnan(hot[0, 0])                        # masked
    assert hot[1, 1] > 4.0                            # ~6 dB drop
    # flat cells have near-zero stepdown
    flat = hot[2, 2]
    assert np.isfinite(flat) and abs(flat) < 1.0
    assert np.nanargmax(hot) == np.ravel_multi_index((1, 1), hot.shape)


def test_drop_rows_and_csv_roundtrip():
    dates = np.datetime64("2024-01-01") + np.arange(20) * np.timedelta64(12, "D")
    vals = np.concatenate([np.full(10, -7.0), np.full(10, -12.0)]) + \
        np.random.default_rng(0).normal(0, 0.2, 20)
    s = Series("vv", 1, "ascending", dates, vals, 1, 1)
    res = detect_pelt(s, min_drop_db=2.5)
    rows = logic.drop_rows([res])
    assert len(rows) == 1
    assert set(rows[0]) >= {"date_before", "date_after", "delta_db", "gap_days", "pol"}
    csv_text = logic.drops_csv([res], cell=(12, 34))
    head = csv_text.splitlines()[0]
    assert head.startswith("cell_id,cell_row,cell_col,") and "date_before" in head
    assert csv_text.splitlines()[1].startswith("12_34,12,34,")
    sc = logic.series_csv([s], cell=(12, 34))
    assert sc.splitlines()[0].startswith("cell_id,cell_row,cell_col,date,series")
    assert len(sc.splitlines()) == 21  # header + 20 rows
    assert sc.splitlines()[1].startswith("12_34,12,34,")


def test_plot_figure_has_traces_and_spans():
    dates = np.datetime64("2024-01-01") + np.arange(20) * np.timedelta64(12, "D")
    vals = np.concatenate([np.full(10, -7.0), np.full(10, -12.0)])
    s = Series("vv", 1, "ascending", dates, vals, 1, 1)
    res = detect_pelt(s, min_drop_db=2.5)
    fig = plots.figure([res])
    assert plots.count_traces(fig) >= 1        # at least the data trace
    assert plots.count_drop_spans(fig) >= 1    # the shaded drop span
