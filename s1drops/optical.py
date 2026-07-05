"""Sentinel-2 before/after optical imagery for validating a detected drop.

On demand (never at bake time), for a drop we fetch two true-colour S2 L2A scenes:
a *before* image (last clear scene on/before the drop's pre-date) and an *after*
image (first clear scene on/after the drop's post-date), each chosen by the cloud
fraction *over the AOI* (scene-level cloud is meaningless for a small site).

Design:
  - Optical is always loaded at 10 m over a small box, regardless of the SAR cube's
    resolution. In clicked-cell mode the box is a radius around the drop cell; in
    manual whole-AOI mode the box is the cube footprint, coarsened only if it would
    exceed a megapixel budget.
  - AOI cloud fraction is read from the SCL band at a coarse resolution (cheap), so
    probing many candidate scenes stays fast; only the two winners are loaded at the
    fine RGB resolution.
  - Selection walks candidates nearest-first (on the correct temporal side) and takes
    the first under the cloud threshold; if none qualify within a bounded number of
    probes, the least-cloudy probed scene is used.

Network I/O (`_load_scl`, `_load_rgb`, `search_s2`) is isolated so the selection and
cloud logic can be unit-tested with injected loaders.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence, Tuple

import numpy as np
from odc.geo.geobox import GeoBox

from .cube.stac import open_catalog

S2_COLLECTION = "sentinel-2-l2a"
RGB_BANDS = ("B04", "B03", "B02")           # true colour
CLOUD_SCL = (3, 8, 9, 10)                   # shadow, cloud med/high, thin cirrus
FINE_RES = 10.0                             # S2 true-colour native
CLOUD_RES = 60.0                            # SCL read resolution for cloud fraction

Bbox = Tuple[float, float, float, float]


# ---- geometry ---------------------------------------------------------------

def cell_center_utm(cube, row: int, col: int) -> Tuple[float, float, str]:
    """UTM centre coordinate of a cube cell, from its stored grid transform."""
    a, b, c, d, e, f = cube.attrs["grid_transform"]
    xc = a * (col + 0.5) + b * (row + 0.5) + c
    yc = d * (col + 0.5) + e * (row + 0.5) + f
    return xc, yc, str(cube.attrs["crs"])


def clicked_geoboxes(cube, row: int, col: int, radius_m: float,
                     fine_res: float = FINE_RES, coarse_res: float = CLOUD_RES):
    """(fine RGB geobox, coarse cloud geobox) for a box around a clicked drop cell."""
    xc, yc, utm = cell_center_utm(cube, row, col)
    bb = (xc - radius_m, yc - radius_m, xc + radius_m, yc + radius_m)
    fine = GeoBox.from_bbox(bb, utm, resolution=fine_res, tight=True)
    coarse = GeoBox.from_bbox(bb, utm, resolution=coarse_res, tight=True)
    return fine, coarse, fine_res


def gbox_lonlat_bbox(gbox: GeoBox) -> Bbox:
    """lon/lat bounding box of a UTM geobox (for the STAC search extent)."""
    from pyproj import Transformer
    bb = gbox.boundingbox
    t = Transformer.from_crs(gbox.crs, "EPSG:4326", always_xy=True)
    lons, lats = t.transform([bb.left, bb.right, bb.right, bb.left],
                             [bb.bottom, bb.bottom, bb.top, bb.top])
    return (min(lons), min(lats), max(lons), max(lats))


# ---- cloud + selection (pure; testable) -------------------------------------

def aoi_cloud_fraction(scl: np.ndarray, aoi_mask: Optional[np.ndarray] = None) -> float:
    """Fraction of valid AOI pixels flagged cloud/shadow/cirrus in an SCL array.

    SCL 0 is no-data and excluded; if a polygon `aoi_mask` is given, only cells
    inside it count. No valid pixels -> 1.0 (treat as unusable).
    """
    scl = np.asarray(scl)
    valid = scl != 0
    if aoi_mask is not None:
        valid = valid & aoi_mask
    n = int(valid.sum())
    if n == 0:
        return 1.0
    cloudy = np.isin(scl, CLOUD_SCL) & valid
    return float(cloudy.sum()) / n


def _item_date(item) -> np.datetime64:
    return np.datetime64(item.datetime.date())


class Pick(NamedTuple):
    item: object
    date: object
    cloud: object
    probed: int


def rank_scenes(
    items: Sequence, anchor, side: str, cloud_geobox, aoi_mask,
    window_days: float, cloud_thresh: float, cloud_bucket: float,
    max_probes: int, n_candidates: int, load_scl: Callable,
):
    """Rank up to `n_candidates` scenes on the correct side of `anchor`, clarity-first.

    side='before': scenes in [anchor-window, anchor]; 'after': [anchor, anchor+window].
    Every scene in the window (up to `max_probes`) is probed for AOI cloud; only scenes
    at/under `cloud_thresh` are eligible (hard gate — nothing cloudier is ever returned).
    Eligible scenes are ranked by cloud bucketed to `cloud_bucket` %, then by proximity,
    so a meaningfully clearer scene beats a nearer one across buckets while similarly-clear
    scenes prefer the nearer date. Returns (picks, probed); picks may be shorter than N (or
    empty) if fewer scenes qualify.
    """
    anchor = np.datetime64(anchor, "D")
    win = np.timedelta64(int(window_days), "D")
    cands = []
    for it in items:
        d = _item_date(it)
        if side == "before" and not (anchor - win <= d <= anchor):
            continue
        if side == "after" and not (anchor <= d <= anchor + win):
            continue
        cands.append((abs((d - anchor) / np.timedelta64(1, "D")), d, it))
    cands.sort(key=lambda c: c[0])                       # nearest-first (proximity tiebreak)

    bucket = max(1e-6, float(cloud_bucket))
    eligible = []       # (band, prox, d, cloud, it)
    probed = 0
    for prox, d, it in cands[: int(max_probes)]:
        cloud = aoi_cloud_fraction(load_scl(it, cloud_geobox), aoi_mask)
        probed += 1
        if cloud <= cloud_thresh:                        # hard gate
            band = round((cloud * 100.0) / bucket)
            eligible.append((band, prox, d, cloud, it))
    eligible.sort(key=lambda x: (x[0], x[1]))            # clearest bucket, then nearest
    chosen = eligible[: int(n_candidates)]
    return [Pick(it, d, cloud, probed) for (_b, _p, d, cloud, it) in chosen], probed


# ---- rendering --------------------------------------------------------------

def stretch_band(band: np.ndarray, lo_pct: float = 2.0, hi_pct: float = 98.0) -> np.ndarray:
    """Percentile contrast stretch of one reflectance band to uint8."""
    band = np.asarray(band, dtype="float32")
    finite = np.isfinite(band) & (band > 0)
    if not finite.any():
        return np.zeros(band.shape, dtype="uint8")
    lo, hi = np.percentile(band[finite], [lo_pct, hi_pct])
    if hi <= lo:
        hi = lo + 1.0
    return (np.clip((band - lo) / (hi - lo), 0, 1) * 255).astype("uint8")


def to_true_colour(r, g, b) -> np.ndarray:
    """Stack B04/B03/B02 into a stretched uint8 (H, W, 3) RGB image."""
    return np.dstack([stretch_band(r), stretch_band(g), stretch_band(b)])


# ---- network I/O (isolated) -------------------------------------------------

def search_s2(bbox_ll: Bbox, start: str, end: str, catalog=None) -> list:
    """S2 L2A items intersecting the AOI over [start, end], sorted by datetime."""
    cat = catalog or open_catalog()
    found = cat.search(collections=[S2_COLLECTION], bbox=list(bbox_ll),
                       datetime=f"{start}/{end}")
    items = list(found.items())
    items.sort(key=lambda it: it.datetime)
    return items


def _load(item, geobox, bands, resampling):
    import odc.stac
    import planetary_computer as pc
    ds = odc.stac.load([item], bands=list(bands), geobox=geobox,
                       resampling=resampling, patch_url=pc.sign, chunks={})
    return ds.isel(time=0)


def _load_scl(item, geobox) -> np.ndarray:
    return np.asarray(_load(item, geobox, ["SCL"], "nearest")["SCL"].values)


def _load_rgb(item, geobox) -> np.ndarray:
    ds = _load(item, geobox, RGB_BANDS, "bilinear")
    return to_true_colour(ds["B04"].values, ds["B03"].values, ds["B02"].values)


def write_rgb_tif(rgb: np.ndarray, geobox, path: Path) -> Path:
    """Write a 3-band uint8 true-colour GeoTIFF (tiled+deflate) on the geobox grid."""
    import rasterio
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w, _ = rgb.shape
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=3, dtype="uint8",
        crs=str(geobox.crs), transform=geobox.transform, tiled=True, compress="deflate",
    ) as dst:
        for i in range(3):
            dst.write(rgb[:, :, i], i + 1)
    return path


def read_tif_rgb(path: Path) -> np.ndarray:
    import rasterio
    with rasterio.open(path) as src:
        arr = src.read()  # (3, H, W)
    return np.transpose(arr, (1, 2, 0))


# ---- orchestration ----------------------------------------------------------

def _cache_paths(cache_dir, cube_key, scene_date, res, scope):
    # Keyed by the scene itself (date + geobox), not the anchor/threshold, so a scene
    # reused across drops or re-fetches loads its GeoTIFF instantly.
    payload = json.dumps([cube_key, scene_date, round(float(res), 2), scope], sort_keys=True)
    h = hashlib.sha1(payload.encode()).hexdigest()[:12]
    base = Path(cache_dir) / "optical" / str(cube_key)
    return base / f"{scene_date}_{h}.tif"


def _one_side(side, anchor, items, fine_gbox, cloud_gbox, aoi_mask, *, cube_key,
              window_days, cloud_thresh, cloud_bucket, max_probes, n_candidates, res, scope,
              cache_dir, load_scl, load_rgb, say):
    say(f"{side}: ranking scenes")
    picks, probed = rank_scenes(items, anchor, side, cloud_gbox, aoi_mask, window_days,
                                cloud_thresh, cloud_bucket, max_probes, n_candidates, load_scl)
    if not picks:
        return {"candidates": [], "probed": probed,
                "note": f"no S2 scene under {int(round(cloud_thresh * 100))}% "
                        f"in {int(window_days)} d"}

    candidates = []
    for pick in picks:
        date_str = np.datetime_as_string(pick.date, unit="D")
        tif = _cache_paths(cache_dir, cube_key, date_str, res, scope)
        if tif.exists():
            rgb = read_tif_rgb(tif)
        else:
            say(f"{side}: loading {date_str} ({pick.cloud * 100:.0f}% cloud)")
            rgb = load_rgb(pick.item, fine_gbox)
            write_rgb_tif(rgb, fine_gbox, tif)
        candidates.append({
            "date": date_str, "cloud": round(float(pick.cloud), 3),
            "scene_id": getattr(pick.item, "id", None),
            "rgb": rgb, "tif_path": str(tif),
        })
    return {"candidates": candidates, "probed": probed}


def fetch_before_after(
    cube, *, cube_key: str, cell, before_anchor, after_anchor,
    radius_m: float, window_days: float, cloud_thresh: float, cloud_bucket: float,
    max_probes: int, n_candidates: int,
    cache_dir, catalog=None, progress: Optional[Callable] = None,
    load_scl: Callable = _load_scl, load_rgb: Callable = _load_rgb,
) -> dict:
    """Fetch before/after S2 imagery for a drop over the clicked cell's box.

    Both the drop-anchored and manual-date modes use the same small box around `cell`
    (there is no whole-AOI path). Returns {'before', 'after', 'resolution', 'transform',
    'crs'}; each side has a `candidates` list (up to n_candidates, all under the cloud
    threshold), each with date, cloud, rgb, tif_path, scene_id; `probed`; or a `note`.
    """
    def say(m):
        if progress:
            progress(m)

    if cell is None:
        raise ValueError("optical fetch needs a clicked cell to place the box")
    fine, cloud_gbox, res = clicked_geoboxes(cube, cell[0], cell[1], radius_m)
    aoi_mask = None                                       # the box itself is the AOI
    scope = f"cell{cell[0]}-{cell[1]}-r{int(radius_m)}"

    win = np.timedelta64(int(window_days), "D")
    lo = min(np.datetime64(before_anchor, "D"), np.datetime64(after_anchor, "D")) - win
    hi = max(np.datetime64(before_anchor, "D"), np.datetime64(after_anchor, "D")) + win
    bbox_ll = gbox_lonlat_bbox(fine)
    say("searching Sentinel-2")
    items = search_s2(bbox_ll, str(lo), str(hi), catalog=catalog)
    if not items:
        say("no Sentinel-2 scenes in window")

    common = dict(cube_key=cube_key, window_days=window_days, cloud_thresh=cloud_thresh,
                  cloud_bucket=cloud_bucket, max_probes=max_probes, n_candidates=n_candidates,
                  res=res, scope=scope, cache_dir=cache_dir,
                  load_scl=load_scl, load_rgb=load_rgb, say=say)
    before = _one_side("before", before_anchor, items, fine, cloud_gbox, aoi_mask, **common)
    after = _one_side("after", after_anchor, items, fine, cloud_gbox, aoi_mask, **common)
    say("done")
    return {"before": before, "after": after, "resolution": res,
            "transform": tuple(fine.transform)[:6], "crs": str(fine.crs)}
