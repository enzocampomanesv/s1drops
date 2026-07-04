import numpy as np
import pandas as pd
import xarray as xr

from s1drops.cube.build import assemble_tracks, tag_track
from s1drops.cube.cache import cache_key, read_cube, write_cube


def _fake_track(times, ny=4, nx=5):
    """A minimal loaded-track Dataset: vv/vh on (time, y, x)."""
    t = pd.to_datetime(times)
    shape = (len(t), ny, nx)
    rng = np.random.default_rng(0)
    return xr.Dataset(
        {
            "vv": (("time", "y", "x"), rng.random(shape).astype("float32")),
            "vh": (("time", "y", "x"), rng.random(shape).astype("float32")),
        },
        coords={"time": t, "y": np.arange(ny), "x": np.arange(nx)},
    )


def test_tag_and_assemble_interleaves_by_time():
    asc = tag_track(_fake_track(["2024-01-05", "2024-01-17"]), 1, "ascending")
    desc = tag_track(_fake_track(["2024-01-02", "2024-01-14", "2024-01-26"]), 95, "descending")
    cube = assemble_tracks([asc, desc])
    # 5 passes, sorted chronologically, orbit metadata carried per time
    assert cube.sizes["time"] == 5
    assert list(cube["time"].values) == sorted(cube["time"].values.tolist())
    first = cube.isel(time=0)
    assert str(first["orbit_state"].values) == "descending"  # 2024-01-02 is earliest
    assert int(first["relative_orbit"].values) == 95
    # both relative orbits present
    assert set(np.unique(cube["relative_orbit"].values).tolist()) == {1, 95}


def _fake_cube():
    asc = tag_track(_fake_track(["2024-01-05", "2024-01-17"]), 1, "ascending")
    desc = tag_track(_fake_track(["2024-01-02", "2024-01-14"]), 95, "descending")
    cube = assemble_tracks([asc, desc])
    ny, nx = cube.sizes["y"], cube.sizes["x"]
    mask = np.ones((ny, nx), dtype=bool)
    mask[0, 0] = False
    cube["aoi_mask"] = (("y", "x"), mask)
    cube.attrs.update({"crs": "EPSG:32631", "units": "linear_power_gamma0", "n_passes": 4})
    return cube


def test_zarr_roundtrip_preserves_coords_mask_attrs(tmp_path):
    cube = _fake_cube()
    path = write_cube(cube, tmp_path / "c.zarr")
    back = read_cube(path)
    # string orbit coordinate survives
    assert back["orbit_state"].values[0] in ("ascending", "descending")
    assert set(np.unique(back["orbit_state"].values).tolist()) == {"ascending", "descending"}
    assert int(back["relative_orbit"].values.max()) == 95
    # mask + attrs survive
    assert back["aoi_mask"].dtype == bool
    assert bool(back["aoi_mask"].values[0, 0]) is False
    assert back.attrs["crs"] == "EPSG:32631"
    assert back.attrs["units"] == "linear_power_gamma0"
    # values intact
    np.testing.assert_allclose(back["vv"].values, cube["vv"].values)


def test_coarsen_median_returns_block_medians():
    from s1drops.cube.build import coarsen_median

    rng = np.random.default_rng(1)
    data = rng.random((1, 20, 20)).astype("float32")
    ds = xr.Dataset(
        {"vv": (("time", "y", "x"), data)},
        coords={"time": [0], "y": np.arange(20), "x": np.arange(20)},
    )
    out = coarsen_median(ds, 10)["vv"].isel(time=0).values
    assert out.shape == (2, 2)
    for i in range(2):
        for j in range(2):
            block = data[0, i * 10 : (i + 1) * 10, j * 10 : (j + 1) * 10]
            assert abs(out[i, j] - np.median(block)) < 1e-5


def test_cache_key_includes_reduction_and_name():
    bbox = (3.35, 6.40, 3.50, 6.50)
    k1 = cache_key(bbox, "2024-01-01", "2025-01-01")  # default reduction
    assert k1.startswith("aoi_") and k1.endswith("_nm")
    # different range -> different key
    assert cache_key(bbox, "2024-01-01", "2024-06-01") != k1
    # different reduction -> different key (prevents stale-cube collisions)
    k_ov = cache_key(bbox, "2024-01-01", "2025-01-01", reduction="overview_med")
    assert k_ov != k1 and k_ov.endswith("_ov")
    # name overrides the hash
    assert (
        cache_key(bbox, "2024-01-01", "2025-01-01", name="lagos")
        == "lagos_2024-01-01_2025-01-01_nm"
    )


def test_usable_metas_filters_by_band():
    from s1drops.cube.build import usable_metas
    from s1drops.cube.stac import ItemMeta

    def m(pols):
        return ItemMeta(item=None, datetime="2024-01-01", orbit_state="ascending",
                        relative_orbit=1, pols=pols)

    metas = [m(("vv", "vh")), m(("vv",)), m(("hh", "hv")), m(("vv", "vh"))]
    keep = usable_metas(metas, ("vv", "vh"))
    assert len(keep) == 2                      # only the dual VV/VH scenes
    assert usable_metas(metas, ("vv",)) and len(usable_metas(metas, ("vv",))) == 3
    assert usable_metas([m(("hh", "hv"))], ("vv", "vh")) == []
