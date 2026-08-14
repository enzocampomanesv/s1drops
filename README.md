# s1drops

A Sentinel-1 SAR change-detection tool for spotting **sudden, sustained drops in
radar backscatter** — the signature left behind when buildings are demolished or
an informal settlement is cleared. Radar sees through cloud and works at night,
so unlike optical imagery it gives an uninterrupted ~6–12 day timeline of a site.

The app bakes a Sentinel-1 RTC data cube for an AOI, draws a map of where
backscatter fell, lets you click any cell to see and validate the drop in its
timeseries, and pulls matching Sentinel-2 before/after optical imagery for visual
confirmation.

```
Planetary Computer (STAC)
        │
        ├── sentinel-1-rtc ──► cube layer ──► zarr cube + registry   (bake, once)
        │                       (s1drops.cube)
        │                            │
        │                            ├──► analysis layer  (per-cell series + drop detection)
        │                            │      (s1drops.analysis)
        │                            │
        │                            └──► Solara app  (map, hotspot, plots, exports)
        │                                   (s1drops.app)
        │                                        │
        └── sentinel-2-l2a ──────────────────────┘  (on-demand optical before/after)
                                                       (s1drops.optical)
```

---

## Contents

- [Install](#install)
- [Run the app](#run-the-app)
- [Explore tab](#explore-tab)
- [Admin tab](#admin-tab)
- [LCZ filtering](#lcz-filtering-optional)
- [Optical before/after](#optical-beforeafter-sentinel-2)
- [CLI](#cli)
- [Programmatic use](#programmatic-use)
- [Cube format](#cube-format)
- [Configuration](#configuration)
- [Docker](#docker)
- [Tests](#tests)
- [Design notes and limits](#design-notes-and-limits)

---

## Install

```bash
python -m venv s1env
# Windows: s1env\Scripts\activate   |  macOS/Linux: source s1env/bin/activate
pip install -r requirements.txt
pip install -e .          # optional; or run from the repo root
```

Python ≥ 3.10. No API keys are needed — Planetary Computer access is anonymous,
and asset URLs are signed lazily at read time.

The package applies a PROJ/GDAL bootstrap on import (`s1drops/proj_env.py`) so a
PostGIS-installed `proj.db` can't shadow rasterio's — no manual env fiddling.

## Run the app

```bash
export S1DROPS_ADMIN_PASSWORD='choose-one'          # enables the gated Admin tab
export S1DROPS_CACHE_DIR='./cache'                  # optional; defaults to ./cache
export S1DROPS_LCZ_PATH='/data/lcz_filter_v1.tif'   # optional; enables LCZ filtering
solara run s1drops.app.main
```

Two tabs: **Explore** (public — read-only analysis of already-baked cubes) and
**Admin** (password-gated — build, extend, and delete cubes).

`run_s1drops_demo.bat` is a Windows convenience launcher: it refuses to start
without an admin password set, waits for the port to accept connections, then
opens a Cloudflare quick tunnel for sharing. Edit the two paths at the top first.

---

## Explore tab

Pick a cube from the sidebar; the map shows an image overlay of the whole AOI and
clicking a cell drills into its timeseries.

**Map**

- Esri World Imagery satellite basemap, layer control, and an OSM place-search
  box (geocoding runs from the browser, not the server).
- Three selectable overlay layers:
  - **hotspot** — per-cell largest sustained drop in dB over the visible date
    range, red-blue colour scale. This is a *cheap proxy* (best single-split
    pre/post level change, vectorised over the whole grid) so it stays fast over
    a full AOI; clicking a cell runs the real detector there.
  - **basemap** — temporal-median dB backscatter, for orientation.
  - **lcz** — Local Climate Zone classes in the standard WUDAPT palette (only
    for cubes baked with LCZ enabled).
- Overlays are rendered server-side as a single reprojected EPSG:3857 PNG, not as
  per-cell features — a ~300k-cell grid would choke the browser as vectors.
- A yellow rectangle marks the optical fetch footprint once you request imagery.

**Sidebar controls**

| Control | Effect |
| --- | --- |
| Cube | Which baked cube to analyse |
| Polarisation | `VV`, `VH`, or `VV/VH` (derived as VV_dB − VH_dB) |
| Orbit direction | `both`, `ascending`, `descending` |
| Map layer | hotspot / basemap / lcz |
| Hotspot min (dB) | Hides cells below the threshold (0–12 dB); colours stay fixed so the threshold only filters, never rescales |
| Date range | Dual slider over the cube's actual pass dates |
| LCZ classes | Hard restriction of the analysis surface (see below) |
| 3×3 neighborhood | Detect on the spatial median of the 3×3 block instead of one pixel — shown only for 10 m cubes, where single-pixel speckle is severe |
| Method | `pelt` (changepoints) or `threshold` (sliding windows) |
| Sensitivity / Window | Method-specific knob |
| Min drop (dB) | Minimum pre→post level change to report (0.5–8) |
| Direction | `drop`, `rise`, or `both` |

**Detection.** Clicking a cell extracts one dB series per
(polarisation × relative orbit) — ascending and descending look geometries are
never mixed — and runs the chosen detector on the visible window plus 2 passes of
padding on each side, so a drop at the window edge still has a before and after.

- **PELT** (default): `ruptures` mean-shift changepoints with an l2 cost. The
  penalty is auto-scaled to each series' own noise (MAD of first differences), so
  one `sensitivity` knob works across cells with different backscatter levels. A
  changepoint is reported only if the pre→post level falls by at least
  `min_drop_db` (default 2.5).
- **Threshold**: sliding pre/post median windows of width `window`; flag where
  the level falls by ≥ `min_drop_db`. Consecutive flags merge into one event.

Detection runs on sample index; the real day-gap of each event is reported
separately, because Sentinel-1 sampling is irregular.

**Panels and exports** (each collapsible)

- **Timeseries** — Plotly chart with one trace per series, shaded
  `[date_before, date_after]` spans for each detected drop, and dotted horizontal
  lines showing the PELT segment levels the detector actually fitted.
- **Detected drops** — table of series, orbit, direction, before/after dates,
  gap in days, pre/post dB, delta, and method.
- **Downloads**:
  - `drops CSV` — the detection table, with cell id/row/col.
  - `series CSV` — the raw per-pass dB values for the cell.
  - `Hotspot GeoJSON` — one WGS84 polygon per shown hotspot cell, carrying
    `drop_db`, `date_before`, `date_after`, `gap_days`, `year_before`,
    `month_before`, `lcz`, and cell indices. This is the artefact for GIS
    validation against eviction records; it exports exactly the cells drawn on
    the map (same LCZ filter, same dB threshold).
  - `before`/`after` GeoTIFFs of the fetched Sentinel-2 scenes.

Filenames encode the cube, cell, polarisation, threshold, and date range, so
exports stay self-describing after download.

---

## Admin tab

Password login (`S1DROPS_ADMIN_PASSWORD`, constant-time compare). The gate is
enforced **server-side**: `auth.guard_admin()` is called inside the bake and
delete handlers, so reaching the component is not enough to trigger either.

**Build / extend form**

- **AOI source**: `registry` (reuse an existing cube's exact footprint),
  `bbox` (four lon/lat fields), `upload` (drop a GeoJSON), or `draw` (rectangle
  or polygon drawn on a map, with place search).
- **Bake presets** — resolution and spatial reduction are chosen as one unit:
  - `100 m · native median` — reads native ~10 m pixels and takes the true
    spatial median of each 100 m cell. Robust to transient bright scatterers and
    reproducible. Slower to bake (reads ~100× more data; cached once).
  - `100 m · overview (fast)` — GDAL `med` straight to 100 m, may read COG
    overviews. ~1 dB different from native (biased ~+0.5 dB); exploration only.
  - `10 m · native (small AOI)` — full-resolution cube, capped by area.
- **Live size estimate** — the form shows AOI area, grid shape, cell count, and
  estimated cube size before you commit, and blocks a 10 m bake over the area cap.
- **Bake queue** — jobs run one at a time on a single background worker, so
  back-to-back bakes never fetch in parallel or collide on a zarr write. Each job
  shows a live progress log; pending jobs can be removed, finished ones cleared.
  A failed job is isolated and the worker keeps draining. Queue state is
  in-memory and shared across sessions (lost on server restart).
- Baking is **incremental in time**: re-running the same footprint with a wider
  date range bakes only the missing sub-ranges and merges them into the existing
  cube, de-duplicating overlapping passes.

**Cubes** — a list of every registered cube with its key, span, pass count, and
reduction, each with a two-step confirm delete.

**Guards.** Two budget checks protect the server: the AOI area cap for 10 m bakes
(`S1DROPS_NATIVE10_MAX_KM2`, default 10 km²), and a hard ceiling on estimated
cube RAM (`S1DROPS_MAX_CUBE_BYTES`, default 2 GB) checked *before* any pixels are
loaded — and again before a merge is materialised, since `write_cube()` loads the
whole cube into memory.

---

## LCZ filtering (optional)

Restrict the analysis to specific Local Climate Zone classes so water, vegetation
and non-urban cells are excluded from the map, the hotspot, and clicking.

1. Download `lcz_filter_v1.tif` (~1.4 GB) from
   <https://zenodo.org/records/6364594> (Demuzere et al. 2022, global 100 m,
   nominal 2018).
2. Set `S1DROPS_LCZ_PATH` to its path and restart the app.
3. Bake a cube — the LCZ layer is window-read, reprojected onto the cube grid,
   and stored inside the zarr. Cubes baked without the variable set keep working;
   the LCZ controls simply don't appear for them.

In Explore, the class multiselect (default `3, 6, 7, 8` — compact low-rise, open
low-rise, lightweight low-rise, large low-rise) **hard-restricts** the analysis
surface: non-allowed cells are hidden, not clickable, and excluded from both the
hotspot layer and its GeoJSON export.

One static epoch is used deliberately. A time-varying urban mask would remove a
settlement once it is demolished — hiding the very event being detected.

Tip: converting the source to a COG once
(`gdal_translate lcz_filter_v1.tif lcz_cog.tif -of COG`) speeds up per-bake reads.

---

## Optical before/after (Sentinel-2)

On demand — never at bake time — the Explore panel fetches true-colour Sentinel-2
L2A imagery around a clicked cell to visually confirm a detected drop.

- **Anchoring**: `cell` mode uses a detected drop's before/after dates (picked
  from a dropdown, largest drop first); `manual` mode takes two dates you type,
  using the clicked cell's box without needing a detection.
- **Cloud screening**: scene-level cloud cover is meaningless for a 2 km box, so
  the AOI cloud fraction is computed from the SCL band at 60 m (cheap) for every
  candidate scene in the window. Only scenes at or under the threshold are
  eligible — a hard gate.
- **Ranking**: eligible scenes are sorted by cloud rounded into 5 % buckets, then
  by proximity to the anchor date, so a meaningfully clearer scene beats a nearer
  one while similarly-clear scenes prefer the nearer date. The top N per side
  (default 3) are offered.
- **Display**: two responsive-square maps with pan/zoom linked, over a muted grey
  basemap, with the fetch footprint drawn. ◀/▶ cycles the candidate scenes on
  each side — swapping only the image overlay, so your zoom and pan are preserved.
  Each caption shows the scene date, AOI cloud %, and how many scenes were probed.
- **Caching**: only the winning scenes are read at 10 m, written to
  `<cache>/optical/<cube_key>/<date>_<hash>.tif`, and keyed by the scene itself —
  so a scene reused across drops or a repeat fetch loads instantly. Both are
  downloadable as GeoTIFFs.

The fetch runs on a background thread with a live progress log so the UI stays
responsive.

---

## CLI

`s1drops` = `python -m s1drops.cli`. All subcommands take `--cache-dir`
(default `./cache`).

```bash
# Bake (or time-extend) a cube
s1drops build --bbox 3.35 6.40 3.50 6.50 --name lagos_test \
    --start 2024-01-01 --end 2024-04-01
s1drops build --aoi lagos.geojson --name lagos \
    --start 2015-01-01 --end 2026-06-01        # full archive: slow, once
s1drops build --bbox 3.10 6.35 3.70 6.75 --name lagos \
    --start 2014-01-01 --end 2015-01-01        # extends the same cube backward

s1drops list                                   # registered cubes
s1drops rescan                                 # rebuild registry.json from disk
s1drops delete --key lagos_2015-01-01_2026-06-01_nm [--yes]
```

`build` also accepts `--res` (default 100) and
`--reduction {native_median,overview_med}`. It auto-detects time-extension: same
snapped grid + wider range bakes only the missing dates and merges. A *different*
footprint is always a new, independent cube — in-place spatial growth is
deliberately not supported.

`demo_analysis.py` runs the analysis layer end-to-end against a cached cube from
the command line:

```bash
python demo_analysis.py --cube cache/lagos_2024-01-01_2025-01-01_nm.zarr \
    --lon 3.38 --lat 6.46 --pol vv --method pelt --min-drop 2.5
```

## Programmatic use

```python
from s1drops.cube import build_or_load, list_cubes, read_cube
from s1drops.analysis import cell_index, extract_series, detect

cube, path, built = build_or_load(
    (3.35, 6.40, 3.50, 6.50), "2024-01-01", "2025-01-01", name="lagos",
)

yi, xi = cell_index(cube, 3.38, 6.46)                 # lon/lat -> grid cell
series = extract_series(cube, yi, xi, pols=("vv",))   # one per relative orbit
for s in series:
    result = detect(s.filter("2024-03-01", "2024-10-01", pad=2), method="pelt")
    print(s.label, [(d.date_before, d.date_after, d.delta_db) for d in result.drops])
```

The map layers and exports are available headlessly too, via
`s1drops.app.logic`: `hotspot_layer`, `basemap_layer`, `hotspot_geojson`,
`drops_csv`, `series_csv`.

---

## Cube format

One zarr store per cube at `<cache_dir>/<key>.zarr`, plus a `registry.json`
manifest that is rebuildable from the stores at any time.

**Variables** — `vv`, `vh` on dims `(time, y, x)` in **linear-power gamma0**
(convert with `to_db`); `aoi_mask` on `(y, x)` marking cells inside the AOI
polygon; `lcz` on `(y, x)` when LCZ was enabled at bake time.

**Coordinates** — `time`; `relative_orbit` and `orbit_state` along time (stored
on disk as a small int `orbit_code` for portability, decoded on read); `x`/`y`
cell centres in a local UTM CRS.

**Attributes** — `crs`, `grid_transform`, `grid_shape`, `bbox_ll`, `start`,
`end`, `resolution`, `reduction`, `n_passes`, `units`, `collection`,
`scenes_skipped_polarization`, and the LCZ provenance fields.

**Keying** — `<name>_<start>_<end>_<tag>` where tag is `nm`/`ov` (plus the
resolution when not 100 m, e.g. `nm10`). Without a `--name`, a hash of the
snapped bbox is used instead. Chunking is `{time: -1, y: 256, x: 256}` — the full
time axis in one chunk, so a single cell's whole series reads from few chunks,
which is exactly the click pattern.

**Grid** — the AOI's four corners are projected into the local UTM zone, the
envelope is snapped to the resolution grid, and a north-up GeoBox is built. The
same AOI therefore always yields the same grid, which is what makes cache keys
and cell indices stable across bakes.

---

## Configuration

All read live from the environment; no code changes needed to tune them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `S1DROPS_ADMIN_PASSWORD` | — | Enables the Admin tab. Unset ⇒ admin disabled entirely |
| `S1DROPS_CACHE_DIR` | `./cache` | Where cubes and optical tiles live (app only; the CLI uses `--cache-dir`) |
| `S1DROPS_LCZ_PATH` | — | Path to `lcz_filter_v1.tif`; enables LCZ at bake time |
| `S1DROPS_NATIVE10_MAX_KM2` | `10` | Max AOI area (km²) for a 10 m bake |
| `S1DROPS_MAX_CUBE_BYTES` | `2e9` | Hard ceiling on estimated cube RAM |
| `S1DROPS_EST_PASSES_PER_YEAR` | `40` | Passes/year used only for the form's live size estimate |
| `S1DROPS_S2_WINDOW_DAYS` | `45` | Half-window (days) each side of a drop to search for S2 |
| `S1DROPS_S2_CLOUD_THRESH` | `0.10` | Max AOI cloud fraction for a scene to be eligible |
| `S1DROPS_S2_MAX_PROBES` | `24` | Ceiling on scenes per side whose AOI cloud is evaluated |
| `S1DROPS_S2_CANDIDATES` | `3` | Scenes offered per side in the ◀/▶ cycler |
| `S1DROPS_S2_CLOUD_BUCKET` | `5` | Cloud band width (%) for clarity-vs-proximity ranking |
| `S1DROPS_OPTICAL_RADIUS_M` | `1000` | Half-size (m) of the optical box around a clicked cell |

## Docker

The Dockerfile copies `requirements.txt` from the **repository root**, so build
from there:

```bash
docker build -f s1drops/Dockerfile -t s1drops .
docker run -p 8765:8765 \
  -e S1DROPS_ADMIN_PASSWORD='choose-one' \
  -e S1DROPS_CACHE_DIR=/data/cache \
  -v /host/cache:/data/cache \
  s1drops
```

The image runs `solara run … --host 0.0.0.0 --port 8765 --production`
(`--production` disables the dev file-watcher, which has no business inside a
container). Mount the cache so baked cubes survive container restarts.

`.dockerignore` lives at the repository root — Docker only reads it there — and
keeps `s1env/`, `cache/`, and large local rasters out of the build context.

## Tests

```bash
python -m pytest s1drops/tests/ -q
```

Fully offline — no network, no Planetary Computer access. STAC search, scene
loading, and the S2 SCL/RGB readers are all injectable, so the geometry, cache
keying, registry, merge, detection, cloud-ranking, and queue logic are tested
against synthetic data. Solara components are exercised headlessly.

---

## Design notes and limits

- **Orbits are never mosaicked.** Ascending and descending passes can share a
  solar day, so grouping the whole search by day would silently blend two look
  geometries into one time slice. Each *relative orbit* is loaded separately
  (a relative orbit revisits only every ~12 days, so day-grouping within it is
  safe and correctly mosaics multi-frame passes), tagged, then concatenated.
- **The hotspot layer is a proxy, not the detector.** It is the largest single
  pre/post split per cell, computed with cumulative sums as whole-grid array ops.
  Use it to find candidates; click to get the real, orbit-separated detection.
- **Scenes lacking VV/VH are skipped.** Sentinel-1 images some regions (often
  maritime/coastal) in HH/HV or VV-only. Those scenes are filtered out, and the
  count is recorded in `scenes_skipped_polarization`. If *no* scene in an AOI
  carries both bands, the bake fails with an explicit message rather than a
  cryptic asset error.
- **Signing happens at read time**, not at search time. SAS tokens last ~1 h, so
  signing every item up front means a long bake can outlive its tokens (403s) and
  a full-archive search can trip the anonymous rate limit. Read-time signing mints
  and caches one token per storage container.
- **Spatial growth is not supported.** "Add a bigger area" means "bake a bigger
  AOI as a new cube". Mosaic-in-place is a separate, fragile problem.
- **Windows cache locations matter.** Zarr's atomic renames fail under OneDrive
  sync or antivirus scanning; `write_cube` retries with backoff and, if it still
  fails, tells you to point `S1DROPS_CACHE_DIR` at a local non-synced path.
- **The bake queue is in-memory.** Queued and running jobs are lost on server
  restart; completed cubes on disk are not.
- **Backscatter drops are evidence, not proof.** A sustained drop is consistent
  with demolition, but also with other surface changes. The optical before/after
  panel and the GeoJSON export exist so each candidate can be checked against
  imagery and ground records.
