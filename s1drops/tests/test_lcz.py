import numpy as np
import rasterio
import xarray as xr
from rasterio.transform import from_bounds

from s1drops.app import logic
from s1drops.cube.geobox import aoi_to_geobox
from s1drops.cube.lcz import allowed_mask, lcz_for_geobox
from s1drops.cube.manage import merge_time

BBOX = (3.40, 6.45, 3.45, 6.50)


def _write_lcz_tif(path, ny=50, nx=50):
    """Synthetic EPSG:4326 LCZ raster: left half class 3, right half class 17 (water)."""
    arr = np.full((ny, nx), 3, dtype="uint8")
    arr[:, nx // 2:] = 17
    transform = from_bounds(BBOX[0], BBOX[1], BBOX[2], BBOX[3], nx, ny)
    with rasterio.open(
        path, "w", driver="GTiff", height=ny, width=nx, count=1,
        dtype="uint8", crs="EPSG:4326", transform=transform, nodata=0,
    ) as dst:
        dst.write(arr, 1)


def test_lcz_for_geobox_aligns_and_preserves_classes(tmp_path):
    tif = tmp_path / "lcz.tif"
    _write_lcz_tif(str(tif))
    gbox, utm = aoi_to_geobox(BBOX, 100.0)
    out = lcz_for_geobox(gbox, utm, str(tif))
    assert out.shape == (gbox.shape.y, gbox.shape.x)
    vals = set(np.unique(out).tolist())
    assert vals <= {0, 3, 17}      # nearest-neighbour: no invented classes
    assert 3 in vals and 17 in vals  # both halves survive the reprojection
    # left side should be mostly built (3), right side mostly water (17)
    assert (out[:, :out.shape[1] // 4] == 3).mean() > 0.8
    assert (out[:, 3 * out.shape[1] // 4:] == 17).mean() > 0.8


def _cube_with_lcz():
    gbox, utm = aoi_to_geobox(BBOX, 100.0)
    ny, nx = 3, 3
    xs = gbox.transform.c + 100 * (np.arange(nx) + 0.5)
    ys = gbox.transform.f - 100 * (np.arange(ny) + 0.5)
    n = 12
    times = np.datetime64("2024-01-01") + np.arange(n) * np.timedelta64(12, "D")
    vv = np.full((n, ny, nx), 0.2, dtype="float32")
    vv[n // 2:, 1, 1] = 10 ** (-12 / 10)  # a drop at the centre cell
    ds = xr.Dataset(
        {"vv": (("time", "y", "x"), vv), "vh": (("time", "y", "x"), vv / 2)},
        coords={
            "time": times, "x": xs, "y": ys,
            "relative_orbit": ("time", np.full(n, 1, "int32")),
            "orbit_state": ("time", np.array(["ascending"] * n, "<U10")),
        },
    )
    ds["aoi_mask"] = (("y", "x"), np.ones((ny, nx), bool))
    lcz = np.full((ny, nx), 17, dtype="int16")  # default: water
    lcz[1, 1] = 3                                 # centre is compact low-rise (allowed)
    ds["lcz"] = (("y", "x"), lcz)
    ds.attrs.update({"crs": str(utm), "resolution": 100.0,
                     "grid_transform": [gbox.transform.a, gbox.transform.b, gbox.transform.c,
                                        gbox.transform.d, gbox.transform.e, gbox.transform.f]})
    return ds


def test_effective_mask_and_cell_allowed():
    cube = _cube_with_lcz()
    m = logic.effective_mask(cube, [2, 3, 6, 7, 9])
    assert m[1, 1] and not m[0, 0]                 # only the class-3 centre is allowed
    assert logic.cell_allowed(cube, 1, 1, [2, 3, 6, 7, 9])
    assert not logic.cell_allowed(cube, 0, 0, [2, 3, 6, 7, 9])
    # no filter -> whole AOI allowed
    assert logic.effective_mask(cube, None).all()


def test_hotspot_respects_lcz_filter():
    cube = _cube_with_lcz()
    hot = logic.hotspot_layer(cube, pol="vv", lcz_classes=[2, 3, 6, 7, 9])
    assert np.isfinite(hot[1, 1])                  # allowed cell retained
    assert np.isnan(hot[0, 0])                     # water cell removed
    # without the filter, the water cells are not NaN'd by LCZ
    hot_all = logic.hotspot_layer(cube, pol="vv", lcz_classes=None)
    assert np.isfinite(hot_all[0, 0])


def test_lcz_at_and_label():
    cube = _cube_with_lcz()
    assert logic.lcz_at(cube, 1, 1) == 3
    assert logic.lcz_at(cube, 0, 0) == 17
    assert logic.lcz_label(3) == "3 Compact low-rise"
    assert logic.lcz_label(None) == ""


def test_merge_time_preserves_lcz():
    a = _cube_with_lcz()
    b = _cube_with_lcz()
    b = b.assign_coords(time=b["time"] + np.timedelta64(365, "D"))
    merged = merge_time([a, b])
    assert "lcz" in merged
    assert int(merged["lcz"].values[1, 1]) == 3
