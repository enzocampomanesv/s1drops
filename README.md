# s1drops — cube layer (Phase 1)

Ingests Sentinel-1 RTC (Planetary Computer) into a cached 100 m data cube for a
given AOI and date range. This is the framework-agnostic data backbone; the
analysis (drop detection) and Solara UI layers build on top of it.

## What it does

- Searches `sentinel-1-rtc` over a lon/lat AOI + date range (anonymous signing).
- Picks a local UTM zone, snaps a 100 m grid, builds one cube per relative orbit,
  tags each pass with `orbit_state` + `relative_orbit`, and concatenates to a
  long-form cube `(time, y, x)` for `vv`/`vh` in linear-power gamma0.
- Spatial reduction to 100 m is selectable:
  - `native_median` (default): reads native ~10 m pixels and takes the true
    spatial median of each 100 m cell. Robust to transient bright scatterers and
    reproducible. Slower to bake (reads ~100x more data; cached once).
  - `overview_med`: GDAL `med` straight to 100 m, may read COG overviews. ~1 dB
    different from native (and biased ~+0.5 dB); fast, for exploration only.
- Carries a boolean `aoi_mask` (cells inside the AOI polygon) for the UI to grey
  out / disable cells outside an irregular AOI.
- Caches to zarr, keyed on (snapped bbox, dates, resolution) or a readable name.

Ascending and descending passes are never mosaicked together: loading per
relative orbit keeps look geometries separate, which the detection layer needs.

## Install

```bash
python -m venv s1env
# Windows: s1env\Scripts\activate   |  macOS/Linux: source s1env/bin/activate
pip install -r requirements.txt
pip install -e .          # optional; or run from the repo root
```

The package applies a PROJ/GDAL bootstrap on import (`cube/proj_env.py`) so a
PostGIS-installed `proj.db` can't shadow rasterio's — no manual env fiddling.

## Run the tests (offline, no network)

```bash
python -m pytest s1drops/tests/ -q
```

## Build a cube

Small smoke test (the Phase 0 bbox, ~3 months — fast):

```bash
python -m s1drops.cli build --bbox 3.35 6.40 3.50 6.50 --name lagos_test \
    --start 2024-01-01 --end 2024-04-01
```

Full Lagos year (default range is Jan 2024 – Jan 2025):

```bash
python -m s1drops.cli build --aoi lagos.geojson --name lagos
```

Output: a `./cache/<key>.zarr` cube plus a printed summary (dims, passes by
orbit direction and relative orbit, CRS). The date range is a CLI argument, so
the UI's date-range selector maps straight onto `--start/--end`.

## Programmatic use

```python
from s1drops.cube import build_or_load
cube, path, built = build_or_load(
    (3.35, 6.40, 3.50, 6.50), "2024-01-01", "2025-01-01", name="lagos",
)
# cube: xarray Dataset (time, y, x) with vv, vh, aoi_mask;
#       coords time, orbit_state, relative_orbit, orbit_code.
```

## Analysis layer (Phase 2)

`s1drops.analysis` extracts per-(polarisation x relative-orbit) dB series at a
clicked cell (VV/VH derived on the fly), filters to a date window with padding,
and detects sharp drops two ways:

- `detect_pelt` — ruptures changepoints, penalty auto-scaled to per-series noise
  via a single `sensitivity` knob; a changepoint is a drop only if pre->post
  falls >= `min_drop_db` (default 2.5).
- `detect_threshold` — sliding pre/post median windows; flag where the level
  falls >= `min_drop_db` (default 3.0); consecutive flags merged.

Both report `[date_before, date_after]` windows with the real day-gap. Try it on
a cached cube:

```bash
python demo_analysis.py --cube cache/lagos_2024-01-01_2025-01-01_nm.zarr \
    --lon 3.38 --lat 6.46 --pol vv --method pelt
```

## Cube management (registry + bake/extend)

Cubes are tracked in `cache/registry.json`, rebuildable from disk at any time
(`s1drops rescan`). `build` auto-detects time-extension: re-running the same AOI
(same snapped grid) with a wider range bakes only the missing dates and merges.

```bash
s1drops build --aoi lagos.geojson --name lagos --start 2015-01-01 --end 2026-06-01
s1drops build --bbox 3.10 6.35 3.70 6.75 --name lagos --start 2014-01-01 --end 2015-01-01  # extends backward
s1drops list
s1drops delete --key lagos_2015-01-01_2026-06-01_nm   # admin only
```

(`s1drops` = `python -m s1drops.cli`.) A *different* footprint is always a new,
independent cube — in-place spatial growth is deliberately not supported.

## Run the app

```bash
export S1DROPS_ADMIN_PASSWORD='choose-one'   # enables the gated Admin tab
export S1DROPS_CACHE_DIR='./cache'           # optional; defaults to ./cache
export S1DROPS_LCZ_PATH='/data/lcz_filter_v1.tif'  # optional; enables LCZ filtering
solara run s1drops.app.main
```

### LCZ filtering (optional)

To restrict the analysis to specific Local Climate Zone classes (and hide
water / non-urban cells), download the global LCZ map once and point the app at
it before baking:

1. Download `lcz_filter_v1.tif` (1.4 GB) from https://zenodo.org/records/6364594
2. Set `S1DROPS_LCZ_PATH` to its path.
3. Bake a cube — the LCZ layer is sampled onto the cube grid and stored in the
   zarr (one static 2018 epoch, applied across all years). Cubes baked without
   the variable set keep working; the LCZ controls just don't appear for them.

In Explore, a class multiselect (default 2, 3, 6, 7, 9) hard-restricts the
analysis surface: non-allowed cells are hidden, not clickable, and excluded from
the hotspot. An "lcz" map layer shows the zones. Tip: converting the file to a
COG once (`gdal_translate ... -of COG`) speeds up per-bake window reads.

Opens a two-tab app: **Explore** (pick a cube, the map shows a hotspot/basemap
overlay, click a cell → timeseries + drop detection + table + CSV) and **Admin**
(password-gated build/extend/delete with live progress). The admin password is
enforced server-side in the bake/delete handlers, not just hidden.

## Next

UI is in place — run it and calibrate detection defaults against the live map.
