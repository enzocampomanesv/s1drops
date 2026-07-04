import numpy as np
import xarray as xr

from s1drops.cube import manage
from s1drops.cube.cache import write_cube
from s1drops.cube.geobox import aoi_to_geobox
from s1drops.cube.manage import bake_or_extend, merge_time, missing_ranges
from s1drops.cube.registry import (
    delete_cube,
    grid_signature,
    list_cubes,
    register_cube,
    scan_cubes,
)

SMALL_BBOX = (3.40, 6.45, 3.42, 6.47)  # tiny -> small grid


def _synth_cube(bbox, times, name, start, end, reduction="native_median"):
    gbox, utm = aoi_to_geobox(bbox, 100.0)
    ny, nx = gbox.shape.y, gbox.shape.x
    t = np.array(times, dtype="datetime64[ns]")
    n = len(t)
    rng = np.random.default_rng(0)
    vv = rng.random((n, ny, nx)).astype("float32") + 0.1
    ds = xr.Dataset(
        {"vv": (("time", "y", "x"), vv), "vh": (("time", "y", "x"), vv / 2)},
        coords={
            "time": t,
            "y": np.asarray(gbox.coordinates["y"].values) if hasattr(gbox, "coordinates") else np.arange(ny),
            "x": np.asarray(gbox.coordinates["x"].values) if hasattr(gbox, "coordinates") else np.arange(nx),
            "relative_orbit": ("time", np.full(n, 1, dtype="int32")),
            "orbit_state": ("time", np.array(["ascending"] * n, dtype="<U10")),
        },
    )
    ds["aoi_mask"] = (("y", "x"), np.ones((ny, nx), dtype=bool))
    tr = gbox.transform
    ds.attrs.update({
        "collection": "sentinel-1-rtc", "name": name, "bbox_ll": list(bbox),
        "start": start, "end": end, "resolution": 100.0, "crs": str(gbox.crs),
        "grid_transform": [tr.a, tr.b, tr.c, tr.d, tr.e, tr.f],
        "grid_shape": [ny, nx], "units": "linear_power_gamma0",
        "reduction": reduction, "n_passes": n,
    })
    return ds


def test_grid_signature_match_and_mismatch():
    g1, _ = aoi_to_geobox(SMALL_BBOX, 100.0)
    g2, _ = aoi_to_geobox(SMALL_BBOX, 100.0)
    t1, t2 = g1.transform, g2.transform
    s1 = grid_signature(g1.crs, [t1.a, t1.b, t1.c, t1.d, t1.e, t1.f], [g1.shape.y, g1.shape.x], 100, "native_median")
    s2 = grid_signature(g2.crs, [t2.a, t2.b, t2.c, t2.d, t2.e, t2.f], [g2.shape.y, g2.shape.x], 100, "native_median")
    assert s1 == s2
    # different reduction -> different signature
    s3 = grid_signature(g1.crs, [t1.a, t1.b, t1.c, t1.d, t1.e, t1.f], [g1.shape.y, g1.shape.x], 100, "overview_med")
    assert s3 != s1


def test_missing_ranges_cases():
    assert missing_ranges("2024-01-01", "2024-12-31", "2024-03-01", "2024-09-01") == []
    assert missing_ranges("2024-01-01", "2024-12-31", "2023-06-01", "2024-06-01") == [("2023-06-01", "2023-12-31")]
    assert missing_ranges("2024-01-01", "2024-12-31", "2024-06-01", "2025-06-01") == [("2025-01-01", "2025-06-01")]
    both = missing_ranges("2024-01-01", "2024-12-31", "2023-06-01", "2025-06-01")
    assert both == [("2023-06-01", "2023-12-31"), ("2025-01-01", "2025-06-01")]


def test_merge_time_dedup_and_sort():
    a = _synth_cube(SMALL_BBOX, ["2024-01-02", "2024-01-14", "2024-01-26"], "x", "2024-01-01", "2024-02-01")
    b = _synth_cube(SMALL_BBOX, ["2024-01-26", "2024-02-07"], "x", "2024-01-26", "2024-02-10")
    merged = merge_time([a, b])
    assert merged.sizes["time"] == 4  # shared 2024-01-26 de-duplicated
    assert np.all(np.diff(merged["time"].values) > np.timedelta64(0))  # strictly ascending
    assert "aoi_mask" in merged and merged["aoi_mask"].dtype == bool


def test_registry_scan_register_and_delete(tmp_path):
    c1 = _synth_cube(SMALL_BBOX, ["2024-03-01", "2024-06-01"], "lagos", "2024-01-01", "2024-12-31")
    p1 = write_cube(c1, tmp_path / "lagos_2024-01-01_2024-12-31_nm.zarr")
    c2 = _synth_cube((121.0, 14.5, 121.02, 14.52), ["2024-03-01"], "manila", "2024-01-01", "2024-12-31")
    p2 = write_cube(c2, tmp_path / "manila_2024-01-01_2024-12-31_nm.zarr")

    register_cube(tmp_path, p1)
    register_cube(tmp_path, p2)
    cubes = {e.key: e for e in list_cubes(tmp_path)}
    assert len(cubes) == 2
    assert cubes["lagos_2024-01-01_2024-12-31_nm"].name == "lagos"

    assert delete_cube(tmp_path, "manila_2024-01-01_2024-12-31_nm") is True
    assert not p2.exists()
    assert len(list_cubes(tmp_path)) == 1

    # rescan from disk reproduces the remaining cube
    assert len(scan_cubes(tmp_path)) == 1


def test_bake_or_extend_noop_when_covered(tmp_path):
    c = _synth_cube(SMALL_BBOX, ["2024-03-01", "2024-09-01"], "lagos", "2024-01-01", "2024-12-31")
    p = write_cube(c, tmp_path / "lagos_2024-01-01_2024-12-31_nm.zarr")
    register_cube(tmp_path, p)
    entry, changed = bake_or_extend(
        SMALL_BBOX, "2024-04-01", "2024-08-01", name="lagos", cache_dir=tmp_path
    )
    assert changed is False
    assert entry.start == "2024-01-01" and entry.end == "2024-12-31"


def test_bake_or_extend_extends_backward(tmp_path, monkeypatch):
    orig = _synth_cube(SMALL_BBOX, ["2024-03-01", "2024-09-01"], "lagos", "2024-01-01", "2024-12-31")
    p = write_cube(orig, tmp_path / "lagos_2024-01-01_2024-12-31_nm.zarr")
    register_cube(tmp_path, p)

    def fake_build(bbox, start, end, **kw):  # stand in for the network bake
        cube = _synth_cube(bbox, ["2023-08-01", "2023-11-01"], kw.get("name", ""), start, end)
        return cube, str(cube.attrs["crs"])

    monkeypatch.setattr(manage, "build_cube", fake_build)
    entry, changed = bake_or_extend(
        SMALL_BBOX, "2023-06-01", "2024-12-31", name="lagos", cache_dir=tmp_path
    )
    assert changed is True
    assert entry.start == "2023-06-01" and entry.end == "2024-12-31"
    assert entry.n_passes == 4  # 2 original + 2 backfilled
    # the superseded store is gone; only the merged cube remains for this grid
    keys = [e.key for e in list_cubes(tmp_path)]
    assert "lagos_2024-01-01_2024-12-31_nm" not in keys
    assert "lagos_2023-06-01_2024-12-31_nm" in keys
