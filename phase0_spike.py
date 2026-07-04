"""
Phase 0 spike — Sentinel-1 RTC ingestion de-risk.

What this proves (the parts that need live Planetary Computer access):
  1. RTC coverage density over a small AOI for a date range.
  2. The asc/desc and relative-orbit split (drives per-orbit handling).
  3. Polarizations actually present per acquisition.
  4. End-to-end build of a 100m median cube via odc.stac.load, with timing.
  5. A sanity check that values are linear-power gamma0 (dB range looks right).

Prereqs (all free):
  pip install odc-stac pystac-client planetary-computer rioxarray xarray rasterio numpy
  A Planetary Computer subscription key (https://planetarycomputer.microsoft.com/account/request):
      export PC_SDK_SUBSCRIPTION_KEY=...    # RTC assets need a SAS token

Run:
  python phase0_spike.py
"""

import os


def _fix_proj_env() -> None:
    """Windows fix: stop GDAL/PROJ from loading PostgreSQL/PostGIS's old proj.db.

    A system-wide PROJ_LIB/PROJ_DATA (set by the PostGIS install) points GDAL at
    a pre-v6-layout proj.db that modern rasterio refuses to read. We clear those
    pointers and aim them at rasterio's own bundled data. Must run BEFORE rasterio
    or pyproj initialize their PROJ context, so it's the first thing in the file.
    """
    import importlib.util
    for v in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
        os.environ.pop(v, None)
    spec = importlib.util.find_spec("rasterio")  # locates without importing
    if not (spec and spec.submodule_search_locations):
        return
    base = list(spec.submodule_search_locations)[0]
    proj = os.path.join(base, "proj_data")
    gdal = os.path.join(base, "gdal_data")
    if os.path.isdir(proj):
        os.environ["PROJ_DATA"] = proj
        os.environ["PROJ_LIB"] = proj
    if os.path.isdir(gdal):
        os.environ["GDAL_DATA"] = gdal


_fix_proj_env()

import time
import math

import numpy as np
import planetary_computer as pc
import pystac_client
import odc.stac

# ---- Test AOI: small Lagos bbox (minx, miny, maxx, maxy in lon/lat). ----
# Swap to Manila to test the second city: (120.95, 14.50, 121.10, 14.65)
BBOX = (3.35, 6.40, 3.50, 6.50)          # ~16 x 11 km around Lagos
START, END = "2024-01-01", "2024-03-31"   # ~3 months
BANDS = ["vv", "vh"]
RESOLUTION_M = 100


def utm_epsg(lon: float, lat: float) -> str:
    """Local UTM EPSG from a lon/lat (auto-detect the zone)."""
    zone = int(math.floor((lon + 180) / 6) % 60) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def main() -> None:
    has_key = bool(os.environ.get("PC_SDK_SUBSCRIPTION_KEY"))
    print(f"Subscription key present: {has_key} "
          f"({'using it' if has_key else 'trying anonymously — RTC may 403 without one'})\n")

    cx = 0.5 * (BBOX[0] + BBOX[2])
    cy = 0.5 * (BBOX[1] + BBOX[3])
    crs = utm_epsg(cx, cy)
    print(f"AOI center ({cx:.3f}, {cy:.3f}) -> {crs}")

    # ---- 1. STAC search ----
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=pc.sign_inplace,
    )
    t0 = time.time()
    search = catalog.search(
        collections=["sentinel-1-rtc"],
        bbox=BBOX,
        datetime=f"{START}/{END}",
    )
    items = list(search.items())
    print(f"\nFound {len(items)} items in {time.time() - t0:.1f}s")
    if not items:
        print("No coverage for this AOI/date range — try a wider window or check the bbox.")
        return

    # ---- 2/3. Coverage breakdown: orbit direction, relative orbit, pols ----
    from collections import Counter
    dirs = Counter()
    rel_orbits = Counter()
    pol_combos = Counter()
    for it in items:
        p = it.properties
        d = p.get("sat:orbit_state", "?")
        ro = p.get("sat:relative_orbit", "?")
        dirs[d] += 1
        rel_orbits[(d, ro)] += 1
        present = tuple(sorted(b for b in BANDS if b in it.assets))
        pol_combos[present] += 1

    print("\nBy orbit direction:", dict(dirs))
    print("By (direction, relative_orbit):")
    for k, v in sorted(rel_orbits.items()):
        print(f"   {k}: {v} acquisitions")
    print("Polarization availability:", {("+".join(k) or "none"): v for k, v in pol_combos.items()})

    # ---- 4. Build a 100m median cube (lazy), then realize one cell's series ----
    t0 = time.time()
    cube = odc.stac.load(
        items,
        bands=BANDS,
        crs=crs,
        resolution=RESOLUTION_M,
        resampling="med",          # <-- the spatial median we proved offline
        bbox=BBOX,
        chunks={"time": 1, "x": 512, "y": 512},  # lazy / dask
        groupby="solar_day",
    )
    print(f"\nLazy cube built in {time.time() - t0:.1f}s")
    print("dims:", dict(cube.sizes))
    print("crs:", cube.rio.crs if hasattr(cube, "rio") else crs)

    # Pull the center cell timeseries for vv and convert to dB to sanity-check units.
    t0 = time.time()
    try:
        center = cube.isel(
            x=cube.sizes["x"] // 2,
            y=cube.sizes["y"] // 2,
        ).compute()
    except Exception as e:
        msg = str(e)
        print(f"\nFailed to read pixel data: {type(e).__name__}: {msg[:300]}")
        if "403" in msg or "AuthenticationFailed" in msg or "Forbidden" in msg:
            print("\n>>> This looks like an auth failure on the RTC blobs.")
            print(">>> RTC needs a Planetary Computer subscription key. Set it and re-run:")
            print(">>>   export PC_SDK_SUBSCRIPTION_KEY=...   (see walkthrough)")
        return
    print(f"Realized 1-cell series in {time.time() - t0:.1f}s")

    vv = center["vv"].values.astype("float64")
    vv = vv[np.isfinite(vv) & (vv > 0)]
    if vv.size:
        db = 10 * np.log10(vv)
        print(f"\nVV linear range: {vv.min():.4f} .. {vv.max():.4f}")
        print(f"VV dB range:     {db.min():.1f} .. {db.max():.1f} dB "
              f"(expect roughly -25..0 dB if linear-power gamma0)")
        print(f"n finite samples at this cell: {vv.size}")
    else:
        print("No finite VV samples at the center cell (edge/coverage gap?).")


if __name__ == "__main__":
    main()
