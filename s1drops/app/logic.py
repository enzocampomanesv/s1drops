"""UI-agnostic glue between the cube/analysis layers and the components.

Holds the heavier non-UI work so the Solara components stay thin: selecting
series for a click, running detection on the filtered+padded view, building the
drop table and CSV exports, and computing the two map layers (temporal-median
basemap and the vectorized drop hotspot). The hotspot is a cheap per-cell
proxy (largest single-split level drop), NOT per-cell ruptures, so it stays
fast over a whole AOI; clicking a hotspot runs the real detector.
"""
from __future__ import annotations

import csv
import io
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import xarray as xr

from ..analysis.detect import DetectionResult, detect
from ..analysis.series import Series, extract_area_series, extract_series, to_db
from ..cube.lcz import LCZ_NAMES, allowed_mask

DETECT_PAD = 2  # passes beyond the visible window for edge context
MIN_PRE_PASSES = 3   # hotspot: valid passes needed before a split (see _max_stepdown)
MIN_POST_PASSES = 2  # hotspot: valid passes needed after a split
CLUSTER_DAYS = 24     # cells join a change area if their drop is this close in time
DEFAULT_MIN_AREA_HA = 4.0   # 4 cells at 100 m, 400 at 10 m
# A drop needs this many passes per track after it before it counts as lasting.
# The hotspot accepts MIN_POST_PASSES, so a drop in the last weeks of the period
# (a wet spell, a flood) can't yet be told from a lasting one: it is flagged.
MIN_CONFIRM_PASSES = 4
# The hotspot measures a drop between the whole periods before and after it, which
# understates it when the site changes again later (e.g. rebuilt). The local drop
# compares only this many passes on each side; reported drops use the larger.
LOCAL_PASSES = 4


# ---- series selection + detection -----------------------------------------

def select_series(
    cube: xr.Dataset, yi: int, xi: int, pol: str, direction: str = "both", window: int = 0
) -> List[Series]:
    """All (relative-orbit) series at a cell for one pol, filtered by direction.

    window>=1 detects on the spatial-neighbourhood median instead of the single
    pixel (speckle reduction for 10 m cubes).
    """
    series = [s for s in extract_series(cube, yi, xi, pols=(pol,), window=window)]
    if direction in ("ascending", "descending"):
        series = [s for s in series if s.orbit_state == direction]
    return series


def select_area_series(
    cube: xr.Dataset, cells: Sequence[Tuple[int, int]], pol: str, direction: str = "both"
) -> List[Series]:
    """Like select_series, but the mean signal over a change area's cells."""
    series = extract_area_series(cube, cells, pols=(pol,))
    if direction in ("ascending", "descending"):
        series = [s for s in series if s.orbit_state == direction]
    return series


def run_detection(
    series_list: Sequence[Series],
    *,
    start=None,
    end=None,
    method: str = "pelt",
    pad: int = DETECT_PAD,
    **params,
) -> List[DetectionResult]:
    """Filter each series to [start, end] (+pad) and detect."""
    out = []
    for s in series_list:
        view = s.filter(start, end, pad=pad)
        out.append(detect(view, method=method, **params))
    return out


# ---- tables + CSV ----------------------------------------------------------

def drop_options(results: Sequence[DetectionResult]) -> List[Dict]:
    """Detected drops as selector options for optical anchoring, largest first.

    Each option exposes only magnitude and the before/after dates (what the user
    chooses on), plus the raw datetime64 anchors the fetch needs.
    """
    opts: List[Dict] = []
    for res in results:
        for d in res.drops:
            opts.append({
                "delta_db": round(float(d.delta_db), 2),
                "date_before": d.date_before,   # np.datetime64 (anchor)
                "date_after": d.date_after,      # np.datetime64 (anchor)
                "label": (f"{d.delta_db:+.1f} dB · "
                          f"{np.datetime_as_string(d.date_before, unit='D')} → "
                          f"{np.datetime_as_string(d.date_after, unit='D')}"),
            })
    opts.sort(key=lambda o: abs(o["delta_db"]), reverse=True)
    return opts


def drop_rows(results: Sequence[DetectionResult]) -> List[Dict]:
    rows: List[Dict] = []
    for res in results:
        s = res.series
        for d in res.drops:
            rows.append({
                "series": s.label, "pol": s.pol, "relative_orbit": s.relative_orbit,
                "direction": s.orbit_state,
                "date_before": np.datetime_as_string(d.date_before, unit="D"),
                "date_after": np.datetime_as_string(d.date_after, unit="D"),
                "gap_days": d.gap_days, "pre_db": round(d.pre_db, 2),
                "post_db": round(d.post_db, 2), "delta_db": round(d.delta_db, 2),
                "method": d.method,
            })
    rows.sort(key=lambda r: (r["pol"] != "vv", r["date_after"]))  # VV first, then by date
    return rows


def _cell_ids(cell) -> Tuple[str, object, object]:
    """(cell_id, row, col) columns for an export; blanks when no cell is given."""
    if cell is None:
        return "", "", ""
    return f"{cell[0]}_{cell[1]}", cell[0], cell[1]


def drops_csv(results: Sequence[DetectionResult], *, cell=None) -> str:
    rows = drop_rows(results)
    cid, crow, ccol = _cell_ids(cell)
    for r in rows:
        r["cell_id"], r["cell_row"], r["cell_col"] = cid, crow, ccol
    fields = ["cell_id", "cell_row", "cell_col", "series", "pol", "relative_orbit",
              "direction", "date_before", "date_after", "gap_days", "pre_db", "post_db",
              "delta_db", "method"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def series_csv(series_list: Sequence[Series], *, start=None, end=None, cell=None) -> str:
    cid, crow, ccol = _cell_ids(cell)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cell_id", "cell_row", "cell_col", "date", "series", "pol",
                "relative_orbit", "direction", "value_db"])
    for s in series_list:
        v = s.filter(start, end, pad=0)
        for dt, val in zip(v.dates, v.values_db):
            w.writerow([
                cid, crow, ccol,
                np.datetime_as_string(dt, unit="D"), s.label, s.pol,
                s.relative_orbit, s.orbit_state,
                "" if not np.isfinite(val) else round(float(val), 3),
            ])
    return buf.getvalue()


# ---- map layers ------------------------------------------------------------

def hotspot_geojson(
    cube: xr.Dataset, *, pol: str = "vv", start=None, end=None, direction: str = "both",
    lcz_classes: Optional[Sequence[int]] = None, min_value: float = 0.0,
) -> str:
    """Hotspot cells as a WGS84 GeoJSON FeatureCollection (one polygon per 100 m
    cell), for GIS validation against eviction records. Each cell carries the max
    drop (`drop_db`) and when it occurred: `date_before` (last pre-drop pass) and
    `date_after` (first post-drop pass), with `gap_days` between them, and the
    drop over the LOCAL_PASSES passes either side of it (`drop_local_db`).

    Respects the LCZ filter and the display minimum, so the export matches what's
    drawn on the map. Each cell is its square footprint reprojected to lon/lat.
    """
    import json

    from pyproj import Transformer

    mag, before, after, local = _hotspot(
        cube, pol=pol, start=start, end=end, direction=direction,
        lcz_classes=lcz_classes, with_local=True,
    )
    if min_value and min_value > 0:
        mag = np.where(mag >= min_value, mag, np.nan)
    yy, xx = np.where(np.isfinite(mag))
    if yy.size == 0:
        return json.dumps({"type": "FeatureCollection", "features": []})

    xs = np.asarray(cube["x"].values, dtype="float64")
    ys = np.asarray(cube["y"].values, dtype="float64")
    half = float(cube.attrs.get("resolution", 100.0)) / 2.0
    cx, cy = xs[xx], ys[yy]
    corners_x = np.stack([cx - half, cx + half, cx + half, cx - half], axis=1)
    corners_y = np.stack([cy - half, cy - half, cy + half, cy + half], axis=1)
    tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(corners_x.ravel(), corners_y.ravel())
    lon = np.asarray(lon).reshape(-1, 4)
    lat = np.asarray(lat).reshape(-1, 4)

    vals = mag[yy, xx]
    bvals = before[yy, xx]
    avals = after[yy, xx]
    lcz = cube["lcz"].values if "lcz" in cube else None
    feats = []
    for i in range(yy.size):
        ring = [[round(float(lon[i, j]), 6), round(float(lat[i, j]), 6)] for j in range(4)]
        ring.append(ring[0])
        props = {"cell_id": f"{int(yy[i])}_{int(xx[i])}", "drop_db": round(float(vals[i]), 3),
                 "row": int(yy[i]), "col": int(xx[i])}
        if np.isfinite(local[yy[i], xx[i]]):
            props["drop_local_db"] = round(float(local[yy[i], xx[i]]), 3)
        b, a = bvals[i], avals[i]
        if not np.isnat(b) and not np.isnat(a):
            db_str = np.datetime_as_string(b, unit="D")
            props["date_before"] = db_str
            props["date_after"] = np.datetime_as_string(a, unit="D")
            props["gap_days"] = int((a - b) / np.timedelta64(1, "D"))
            y, m, _ = db_str.split("-")          # split out for year/month joins
            props["year_before"] = int(y)
            props["month_before"] = int(m)
        if lcz is not None:
            props["lcz"] = int(lcz[yy[i], xx[i]])
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return json.dumps({"type": "FeatureCollection", "features": feats})


def _pol_db_cube(cube: xr.Dataset, pol: str) -> np.ndarray:
    vv = to_db(cube["vv"].values)
    if pol == "vv":
        return vv
    vh = to_db(cube["vh"].values)
    return vh if pol == "vh" else (vv - vh)


def has_lcz(cube: xr.Dataset) -> bool:
    return "lcz" in cube


def lcz_at(cube: xr.Dataset, yi: int, xi: int) -> Optional[int]:
    if "lcz" not in cube:
        return None
    return int(cube["lcz"].values[yi, xi])


def lcz_label(code: Optional[int]) -> str:
    if code is None:
        return ""
    return f"{code} {LCZ_NAMES.get(code, 'unclassified')}"


def effective_mask(cube: xr.Dataset, lcz_classes: Optional[Sequence[int]] = None) -> np.ndarray:
    """AOI mask AND (LCZ in allowed classes), when both are available.

    This is the hard analysis surface: cells outside it are not rendered, not
    clickable, and excluded from the hotspot.
    """
    ny, nx = cube.sizes["y"], cube.sizes["x"]
    mask = cube["aoi_mask"].values if "aoi_mask" in cube else np.ones((ny, nx), bool)
    if lcz_classes is not None and "lcz" in cube:
        mask = mask & allowed_mask(cube["lcz"].values, lcz_classes)
    return mask


def cell_allowed(cube: xr.Dataset, yi: int, xi: int, lcz_classes: Optional[Sequence[int]] = None) -> bool:
    return bool(effective_mask(cube, lcz_classes)[yi, xi])


def _time_mask(cube: xr.Dataset, start, end) -> np.ndarray:
    days = np.asarray(cube["time"].values).astype("datetime64[D]")
    lo = np.datetime64(start, "D") if start is not None else days.min()
    hi = np.datetime64(end, "D") if end is not None else days.max()
    return (days >= lo) & (days <= hi)


def _mask_nan(cube: xr.Dataset, layer: np.ndarray, lcz_classes: Optional[Sequence[int]]) -> np.ndarray:
    return np.where(effective_mask(cube, lcz_classes), layer, np.nan)


def basemap_layer(
    cube: xr.Dataset, *, pol: str = "vv", start=None, end=None,
    lcz_classes: Optional[Sequence[int]] = None,
) -> np.ndarray:
    """Temporal-median dB backscatter per cell over the date range (orientation basemap)."""
    db = _pol_db_cube(cube, pol)
    tm = _time_mask(cube, start, end)
    with np.errstate(all="ignore"):
        layer = np.nanmedian(db[tm], axis=0)
    return _mask_nan(cube, layer, lcz_classes)


def lcz_layer(cube: xr.Dataset) -> np.ndarray:
    """LCZ class codes as a float layer over the AOI (NaN outside AOI). For display."""
    ny, nx = cube.sizes["y"], cube.sizes["x"]
    if "lcz" not in cube:
        return np.full((ny, nx), np.nan)
    arr = cube["lcz"].values.astype("float64")
    arr = np.where(arr > 0, arr, np.nan)  # 0 = no class -> transparent
    aoi = cube["aoi_mask"].values if "aoi_mask" in cube else np.ones((ny, nx), bool)
    return np.where(aoi, arr, np.nan)


def _max_stepdown(db: np.ndarray, *, with_index: bool = False):
    """Per-cell largest single-split level drop (pre_mean - post_mean), in dB.

    Vectorised over (y, x) via cumulative sums along time; O(T) passes, each a
    whole-grid array op. Positive = a drop. A split only counts with at least
    MIN_PRE_PASSES valid passes before it and MIN_POST_PASSES after (NaN otherwise):
    with a single pass on one side, one unusually bright image at the edge of the
    window reads as a huge "drop". The pre side asks for more because the drop is
    measured against an established level, while a real drop should be flagged
    soon after it happens.

    with_index=True also returns the split index k of each cell's best drop (the
    drop sits between time index k-1 and k), for callers that need to date it.
    The index-tracking branch costs an extra whole-grid write per split, so the
    plain form keeps the cheaper `fmax` accumulation for the map redraw path.
    """
    t = db.shape[0]
    best = np.full(db.shape[1:], np.nan)
    best_k = np.zeros(db.shape[1:], dtype=int)
    if t < 2:
        # No split exists, so every cell is NaN regardless of the data. Returning
        # here also keeps the function total for an empty time axis, where the
        # cumulative sums below would have nothing to index.
        return (best, best_k) if with_index else best

    valid = np.isfinite(db)
    filled = np.where(valid, db, 0.0)
    csum = np.cumsum(filled, axis=0)
    ccnt = np.cumsum(valid, axis=0)
    total, totcnt = csum[-1], ccnt[-1]
    for k in range(1, t):
        pre_sum, pre_cnt = csum[k - 1], ccnt[k - 1]
        post_sum, post_cnt = total - pre_sum, totcnt - pre_cnt
        with np.errstate(invalid="ignore", divide="ignore"):
            drop = pre_sum / pre_cnt - post_sum / post_cnt
        cand = np.where((pre_cnt >= MIN_PRE_PASSES) & (post_cnt >= MIN_POST_PASSES),
                        drop, np.nan)
        if with_index:
            # Equivalent to fmax, but records which split won.
            better = np.isfinite(cand) & (~np.isfinite(best) | (cand > best))
            best = np.where(better, cand, best)
            best_k = np.where(better, k, best_k)
        else:
            best = np.fmax(best, cand)
    return (best, best_k) if with_index else best


def _local_step(db: np.ndarray, k: np.ndarray, n: int = LOCAL_PASSES) -> np.ndarray:
    """Per-cell drop at split k from the (up to) n passes on each side: mean of
    db[k-n:k] minus mean of db[k:k+n], ignoring missing passes."""
    t = db.shape[0]
    valid = np.isfinite(db)
    zero = np.zeros((1,) + db.shape[1:])
    cs = np.concatenate([zero, np.cumsum(np.where(valid, db, 0.0), axis=0)])
    cc = np.concatenate([zero, np.cumsum(valid, axis=0)])

    def at(a, i):
        return np.take_along_axis(a, np.clip(i, 0, t)[None], axis=0)[0]

    lo, hi = k - n, k + n
    with np.errstate(invalid="ignore", divide="ignore"):
        pre = (at(cs, k) - at(cs, lo)) / (at(cc, k) - at(cc, lo))
        post = (at(cs, hi) - at(cs, k)) / (at(cc, hi) - at(cc, k))
    return pre - post


def _orbit_selections(cube: xr.Dataset, start, end, direction: str):
    """Yield the per-relative-orbit time masks that a hotspot reduction runs over.

    Orbits are kept separate so look geometries are never mixed; an orbit with
    fewer than two passes in the window has no split to measure and is skipped.
    """
    tm = _time_mask(cube, start, end)
    ro = np.asarray(cube["relative_orbit"].values)
    state = np.asarray(cube["orbit_state"].values)
    for ro_val in np.unique(ro):
        sel = tm & (ro == ro_val)
        if direction in ("ascending", "descending"):
            sel = sel & (state == direction)
        if sel.sum() >= 2:
            yield sel


def _hotspot(
    cube: xr.Dataset, *, pol: str = "vv", start=None, end=None, direction: str = "both",
    lcz_classes: Optional[Sequence[int]] = None, with_timing: bool = False,
    with_local: bool = False,
):
    """Per-cell max sustained drop (dB), reduced across matching relative orbits.

    with_timing=True additionally returns the bracketing pass dates of each cell's
    winning drop — the last pre-drop and first post-drop pass of whichever orbit
    produced that maximum. Returns `magnitude`, or (magnitude, before, after).
    with_local=True (implies timing) appends the local drop at that same split
    (see `_local_step`).
    """
    with_timing = with_timing or with_local
    db = _pol_db_cube(cube, pol)
    shape = db.shape[1:]
    best = np.full(shape, np.nan)
    if not with_timing:
        for sel in _orbit_selections(cube, start, end, direction):
            best = np.fmax(best, _max_stepdown(db[sel]))
        return _mask_nan(cube, best, lcz_classes)

    days = np.asarray(cube["time"].values).astype("datetime64[D]")
    nat = np.datetime64("NaT", "D")
    before = np.full(shape, nat, dtype="datetime64[D]")
    after = np.full(shape, nat, dtype="datetime64[D]")
    local = np.full(shape, np.nan)
    for sel in _orbit_selections(cube, start, end, direction):
        d_sel = days[sel]
        mag, k = _max_stepdown(db[sel], with_index=True)
        kb = np.clip(k - 1, 0, d_sel.size - 1)
        ka = np.clip(k, 0, d_sel.size - 1)
        better = np.isfinite(mag) & (~np.isfinite(best) | (mag > best))
        best = np.where(better, mag, best)
        before = np.where(better, d_sel[kb], before)
        after = np.where(better, d_sel[ka], after)
        if with_local:
            local = np.where(better, _local_step(db[sel], k), local)

    masked = _mask_nan(cube, best, lcz_classes)
    cleared = ~np.isfinite(masked)  # drop dates for cells the mask removed
    out = (masked, np.where(cleared, nat, before), np.where(cleared, nat, after))
    return out + (np.where(cleared, np.nan, local),) if with_local else out


def hotspot_layer(
    cube: xr.Dataset, *, pol: str = "vv", start=None, end=None, direction: str = "both",
    lcz_classes: Optional[Sequence[int]] = None,
) -> np.ndarray:
    """Per-cell max sustained drop (dB) over the range, max across matching orbits."""
    return _hotspot(cube, pol=pol, start=start, end=end, direction=direction,
                    lcz_classes=lcz_classes)


def hotspot_with_dates(
    cube: xr.Dataset, *, pol: str = "vv", start=None, end=None, direction: str = "both",
    lcz_classes: Optional[Sequence[int]] = None,
):
    """(magnitude, date_before, date_after) — the hotspot plus each cell's drop dates.

    The magnitude equals `hotspot_layer` exactly, so one call can feed both the map
    and the candidate list.
    """
    return _hotspot(cube, pol=pol, start=start, end=end, direction=direction,
                    lcz_classes=lcz_classes, with_timing=True)


def hotspot_with_local(
    cube: xr.Dataset, *, pol: str = "vv", start=None, end=None, direction: str = "both",
    lcz_classes: Optional[Sequence[int]] = None,
):
    """(magnitude, date_before, date_after, local) — `hotspot_with_dates` plus each
    cell's local drop, for the candidate list."""
    return _hotspot(cube, pol=pol, start=start, end=end, direction=direction,
                    lcz_classes=lcz_classes, with_local=True)


def _reported(mag: np.ndarray, local: Optional[np.ndarray]) -> np.ndarray:
    """The drop shown to users: the local drop where the whole-period one understates it."""
    return mag if local is None else np.fmax(mag, local)


def top_candidates(
    cube: xr.Dataset, mag: np.ndarray, before: np.ndarray, after: np.ndarray,
    local: Optional[np.ndarray] = None, *,
    n: int = 10, min_value: float = 0.0, spacing_m: float = 250.0,
) -> List[Dict]:
    """The n strongest hotspot cells, at most one per `spacing_m` neighbourhood.

    One demolition lights up a block of adjacent cells, so ranking raw cells would
    list the same site many times; greedy suppression keeps distinct sites instead.
    Ranking and filtering use the whole-period drop (`mag`, as on the map); with
    `local`, `drop_db` reports the larger of the two and `avg_drop_db` keeps `mag`.
    """
    shown = _reported(mag, local)
    from pyproj import Transformer

    yy, xx = np.nonzero(np.isfinite(mag) & (mag >= min_value))
    if yy.size == 0:
        return []
    order = np.argsort(mag[yy, xx], kind="stable")[::-1]
    r = int(np.ceil(spacing_m / float(cube.attrs.get("resolution", 100.0))))
    picked: List[Tuple[int, int]] = []
    for i in order:
        y, x = int(yy[i]), int(xx[i])
        if any(abs(y - py) <= r and abs(x - px) <= r for py, px in picked):
            continue
        picked.append((y, x))
        if len(picked) >= n:
            break

    tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
    xs = np.asarray(cube["x"].values, dtype="float64")
    ys = np.asarray(cube["y"].values, dtype="float64")
    out = []
    for y, x in picked:
        lon, lat = tx.transform(xs[x], ys[y])
        b, a = before[y, x], after[y, x]
        out.append({
            "row": y, "col": x, "lat": float(lat), "lon": float(lon),
            "drop_db": round(float(shown[y, x]), 2),
            "avg_drop_db": round(float(mag[y, x]), 2),
            "date_before": None if np.isnat(b) else str(np.datetime_as_string(b, unit="D")),
            "date_after": None if np.isnat(a) else str(np.datetime_as_string(a, unit="D")),
            "lcz": lcz_at(cube, y, x),
        })
    return out


# ---- change areas (spatial clusters) ----------------------------------------

_NEIGHBOURS = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)]


def passes_after(cube: xr.Dataset, day, *, end=None, direction: str = "both") -> int:
    """Passes from `day` to `end` on the best-covered matching track."""
    days = np.asarray(cube["time"].values).astype("datetime64[D]")
    ro = np.asarray(cube["relative_orbit"].values)
    state = np.asarray(cube["orbit_state"].values)
    sel = days >= np.datetime64(day, "D")
    if end is not None:
        sel &= days <= np.datetime64(end, "D")
    if direction in ("ascending", "descending"):
        sel &= state == direction
    return max((int(np.sum(sel & (ro == r))) for r in np.unique(ro)), default=0)


def flag_recent(cube: xr.Dataset, cands: List[Dict], *, end=None,
                direction: str = "both") -> List[Dict]:
    """Mark candidates whose drop has fewer than MIN_CONFIRM_PASSES passes after it."""
    for c in cands:
        n = passes_after(cube, c["date_after"], end=end, direction=direction) \
            if c["date_after"] else 0
        c["passes_after"], c["recent"] = n, n < MIN_CONFIRM_PASSES
    return cands


def find_clusters(
    cube: xr.Dataset, mag: np.ndarray, before: np.ndarray, after: np.ndarray,
    local: Optional[np.ndarray] = None, *,
    min_value: float = 0.0, min_area_ha: float = DEFAULT_MIN_AREA_HA,
    max_days: int = CLUSTER_DAYS, end=None, direction: str = "both",
) -> Tuple[List[Dict], np.ndarray]:
    """Change areas: touching hotspot cells whose drops happened at about the same time.

    An eviction clears a block of land, so it shows as many adjacent cells dropping
    together; a lone strong cell is more often speckle, a ship or one roof. Cells
    with a drop >= `min_value` are grown into areas from the strongest cell down
    (8-connected), admitting a neighbour only if its drop's "after" date is within
    `max_days` of the seed's. Comparing with the seed, not the neighbour, stops a
    slow wave of unrelated changes chaining across a district.

    Returns (areas, labels): areas of at least `min_area_ha`, largest first, then by
    median drop, with areas too recent to confirm (see `flag_recent`; `end` and
    `direction` say which passes count) after all the others. labels gives each
    cell its area's index in that list (-1 = none). Grouping and ranking use the
    whole-period drop (`mag`, as on the map); with `local`, an area's `drop_db` is
    the median of its cells' reported drops (see `top_candidates`) and
    `avg_drop_db` the median whole-period drop.
    """
    from pyproj import Transformer

    ny, nx = mag.shape
    flagged = np.isfinite(mag) & (mag >= min_value) & ~np.isnat(after)
    yy, xx = np.nonzero(flagged)
    order = np.argsort(mag[yy, xx], kind="stable")[::-1]
    grown = np.full(mag.shape, -1, dtype=int)
    after_d = after.astype("datetime64[D]").astype("int64")
    groups: List[List[Tuple[int, int]]] = []
    for i in order:
        y0, x0 = int(yy[i]), int(xx[i])
        if grown[y0, x0] >= 0:
            continue
        gid, seed_day = len(groups), after_d[y0, x0]
        grown[y0, x0] = gid
        cells, stack = [], [(y0, x0)]
        while stack:
            y, x = stack.pop()
            cells.append((y, x))
            for dy, dx in _NEIGHBOURS:
                v, u = y + dy, x + dx
                if (0 <= v < ny and 0 <= u < nx and flagged[v, u] and grown[v, u] < 0
                        and abs(after_d[v, u] - seed_day) <= max_days):
                    grown[v, u] = gid
                    stack.append((v, u))
        groups.append(cells)

    res = float(cube.attrs.get("resolution", 100.0))
    cell_ha = res * res / 10_000.0
    keep = [g for g in groups if len(g) * cell_ha >= min_area_ha - 1e-9]
    n_after = {id(g): passes_after(
        cube, _median_day(after[tuple(np.array(g).T)]), end=end, direction=direction)
        for g in keep}
    keep.sort(key=lambda g: (n_after[id(g)] < MIN_CONFIRM_PASSES, -len(g),
                             -float(np.median([mag[c] for c in g]))))

    shown = _reported(mag, local)
    tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
    xs = np.asarray(cube["x"].values, dtype="float64")
    ys = np.asarray(cube["y"].values, dtype="float64")
    labels = np.full(mag.shape, -1, dtype=int)
    areas = []
    for k, g in enumerate(keep):
        gy = np.array([c[0] for c in g]); gx = np.array([c[1] for c in g])
        labels[gy, gx] = k
        drops = shown[gy, gx]
        top = int(np.argmax(drops))
        # Place the area at the member cell nearest its centre (a ring-shaped area's
        # centroid can fall outside it).
        cy, cx = gy.mean(), gx.mean()
        mid = int(np.argmin((gy - cy) ** 2 + (gx - cx) ** 2))
        lon, lat = tx.transform(xs[gx[mid]], ys[gy[mid]])
        lons, lats = tx.transform(xs[gx], ys[gy])
        pad_lat = res / 2 / 111_320.0
        pad_lon = pad_lat / max(np.cos(np.radians(lat)), 1e-6)
        areas.append({
            "row": int(gy[mid]), "col": int(gx[mid]), "lat": float(lat), "lon": float(lon),
            "cells": [(int(y), int(x)) for y, x in zip(gy, gx)],
            "n_cells": len(g), "area_ha": round(len(g) * cell_ha, 2),
            "drop_db": round(float(np.median(drops)), 2),
            "avg_drop_db": round(float(np.median(mag[gy, gx])), 2),
            "max_drop_db": round(float(drops[top]), 2),
            "date_before": _median_day(before[gy, gx]),
            "date_after": _median_day(after[gy, gx]),
            "bounds": [[float(np.min(lats)) - pad_lat, float(np.min(lons)) - pad_lon],
                       [float(np.max(lats)) + pad_lat, float(np.max(lons)) + pad_lon]],
            "lcz": lcz_at(cube, int(gy[mid]), int(gx[mid])),
            "passes_after": n_after[id(g)],
            "recent": n_after[id(g)] < MIN_CONFIRM_PASSES,
        })
    return areas, labels


def _median_day(days: np.ndarray) -> str:
    return str(np.datetime_as_string(np.sort(days.astype("datetime64[D]"))[len(days) // 2],
                                     unit="D"))


def area_outline(cube: xr.Dataset, cells: Sequence[Tuple[int, int]]) -> Dict:
    """GeoJSON (lat/lon) outline of a change area: the union of its cell squares."""
    from pyproj import Transformer
    from shapely.geometry import box, mapping
    from shapely.ops import transform, unary_union

    res = float(cube.attrs.get("resolution", 100.0))
    xs = np.asarray(cube["x"].values, dtype="float64")
    ys = np.asarray(cube["y"].values, dtype="float64")
    h = res / 2.0
    shape = unary_union([box(xs[x] - h, ys[y] - h, xs[x] + h, ys[y] + h) for y, x in cells])
    tx = Transformer.from_crs(cube.attrs["crs"], "EPSG:4326", always_xy=True)
    return {"type": "Feature", "properties": {},
            "geometry": mapping(transform(tx.transform, shape))}
