"""Diagnose the 100 m spatial reduction.

Question: does the default `resampling="med"` at 100 m read native 10 m pixels,
or does GDAL pull from COG overviews? If overviews are used, our 100 m value is
NOT the median of the 100 native 10 m pixels we specified.

Method: for one real pass, compare
  (a) the current path  -> odc load at 100 m, resampling="med"
  (b) the explicit path -> odc load at native 10 m, then coarsen-median to 100 m
on the SAME grid, and report value differences (in dB) and timings.

Run from your project root (where the s1drops package lives):
    python diagnose_resampling.py
"""
from __future__ import annotations

import time

import numpy as np
import odc.stac

from s1drops import proj_env  # noqa: F401  (applies PROJ fix on import)
from s1drops.cube.geobox import aoi_to_geobox
from s1drops.cube.stac import open_catalog, search_items
from odc.geo.geobox import GeoBox

# Small ~2 km AOI so the native-10 m read stays cheap.
BBOX = (3.400, 6.450, 3.420, 6.470)
START, END = "2024-01-01", "2024-02-15"
BAND = "vv"


def main() -> None:
    cat = open_catalog()
    metas = search_items(BBOX, START, END, catalog=cat)
    if not metas:
        print("No items; widen the window.")
        return
    m = metas[0]
    item = m.item
    print(f"Using one pass: {m.datetime}  {m.orbit_state}  rel_orbit={m.relative_orbit}")

    gbox100, utm = aoi_to_geobox(BBOX, 100.0)
    # Build a 10 m grid on the EXACT same extent so coarsen(10) lands on gbox100.
    t = gbox100.transform
    minx, maxy = t.c, t.f
    maxx = minx + gbox100.shape.x * 100.0
    miny = maxy - gbox100.shape.y * 100.0
    gbox10 = GeoBox.from_bbox((minx, miny, maxx, maxy), utm, resolution=10, tight=True)
    print(f"grid: 100 m {tuple(gbox100.shape)}  |  10 m {tuple(gbox10.shape)}  CRS {utm}")

    # (a) current path: 100 m median (may use overviews)
    t0 = time.time()
    a = (
        odc.stac.load([item], bands=[BAND], geobox=gbox100, resampling="med",
                      groupby="solar_day", chunks={})[BAND]
        .isel(time=0).compute().values.astype("float64")
    )
    ta = time.time() - t0

    # (b) explicit path: native 10 m, then median-coarsen to 100 m
    t0 = time.time()
    nat = (
        odc.stac.load([item], bands=[BAND], geobox=gbox10, resampling="nearest",
                      groupby="solar_day", chunks={})[BAND]
        .isel(time=0)
    )
    b = nat.coarsen(x=10, y=10, boundary="trim").median().compute().values.astype("float64")
    tb = time.time() - t0

    print(f"\nTimings: (a) 100 m-med = {ta:.2f}s   (b) native-10 m+median = {tb:.2f}s")
    print(f"  -> (b) is {tb/ta:.1f}x slower" if ta > 0 else "")

    # Compare on the overlapping shape.
    hy, hx = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:hy, :hx], b[:hy, :hx]
    ok = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    da, db = 10 * np.log10(a[ok]), 10 * np.log10(b[ok])
    diff = da - db
    print(f"\nCompared {ok.sum()} cells (dB):")
    print(f"  mean |a-b| = {np.mean(np.abs(diff)):.3f} dB")
    print(f"  median|a-b|= {np.median(np.abs(diff)):.3f} dB")
    print(f"  max  |a-b| = {np.max(np.abs(diff)):.3f} dB")
    print(f"  bias (a-b) = {np.mean(diff):+.3f} dB")
    print(f"  corr       = {np.corrcoef(da, db)[0,1]:.4f}")
    print("\nIf (b) is many x slower AND |a-b| is non-trivial (> ~0.3 dB typical),")
    print("the default path is using overviews and changing the reduction.")


if __name__ == "__main__":
    main()
