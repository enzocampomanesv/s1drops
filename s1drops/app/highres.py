"""High-resolution before/after imagery from Esri World Imagery Wayback.

Esri keeps every monthly-ish release of its World Imagery basemap since 2014
("Wayback"), each as its own tile layer, plus a metadata service per release
that tells when and by whom the imagery at a point was captured. Releases are
not captures: a place's imagery is only replaced every year or few, so many
releases show the same photo. For a change between `date_before` and
`date_after` we want the newest capture before it and the oldest capture after.

Capture dates only grow from release to release (a release keeps or replaces
the imagery, never reverts it), so both are found by searching the release list:
each round asks SPLIT releases at once and keeps the stretch where the answer
lies (~3 rounds for ~200 releases, a few seconds; cached on disk per place and
change).

Tiles and metadata are public and free to view with attribution; the imagery
stays on Esri's servers (shown as map tiles, never copied into reports).
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

CONFIG_URL = "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json"
WAYBACK_APP = "https://livingatlas.arcgis.com/wayback/"
USER_AGENT = "s1drops/0.1 (+https://github.com/enzocampomanesv/s1drops)"
CACHE_NAME = "highres.json"
CONFIG_TTL_S = 24 * 3600
# Metadata layers by resolution (0 = 1.9 cm ... 13 = 150 m). Layer 5 (60 cm) is
# what the map shows around zoom 17-18; finer layers are empty where the best
# imagery is ~30-50 cm.
META_LAYER = 5
SPLIT = 7          # releases asked in parallel per search round

_lock = threading.Lock()
_config: Dict = {"at": 0.0, "releases": []}
_caches: Dict[str, Dict] = {}


def releases() -> List[Dict]:
    """[{"num", "date", "tiles", "meta"}], oldest first (fetched once a day)."""
    with _lock:
        if _config["releases"] and time.time() - _config["at"] < CONFIG_TTL_S:
            return _config["releases"]
    req = urllib.request.Request(CONFIG_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        cfg = json.load(r)
    out = []
    for num, v in cfg.items():
        m = re.search(r"(\d{4}-\d{2}-\d{2})", v.get("itemTitle", ""))
        if m and v.get("itemURL") and v.get("metadataLayerUrl"):
            out.append({"num": int(num), "date": m.group(1),
                        "tiles": v["itemURL"].replace("{level}", "{z}")
                                             .replace("{row}", "{y}").replace("{col}", "{x}"),
                        "meta": v["metadataLayerUrl"]})
    out.sort(key=lambda r: r["date"])
    with _lock:
        _config.update(at=time.time(), releases=out)
    return out


def capture(release: Dict, lat: float, lon: float) -> Optional[Dict]:
    """{"date", "res_m", "source"} of the imagery a release shows at a point."""
    q = urllib.parse.urlencode({
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false", "f": "json",
        "outFields": "SRC_DATE2,SRC_RES,NICE_DESC"})
    req = urllib.request.Request(f"{release['meta']}/{META_LAYER}/query?{q}",
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        feats = json.load(r).get("features", [])
    a = feats[0]["attributes"] if feats else None
    if not a or not a.get("SRC_DATE2"):
        return None
    when = datetime.fromtimestamp(a["SRC_DATE2"] / 1000, tz=timezone.utc).date()
    return {"date": when.isoformat(), "res_m": a.get("SRC_RES"),
            "source": a.get("NICE_DESC") or ""}


def find_pair(cache_dir, lat: float, lon: float, date_before: str, date_after: str,
              progress=None) -> Dict:
    """The newest capture before `date_before` and the oldest after `date_after`.

    Returns {"before": side or None, "after": side or None}, each side
    {"num", "release", "date", "res_m", "source", "tiles"}, plus "link" (the two
    in the Wayback app's swipe view). Cached per place (~100 m) and dates.
    """
    key = f"{lat:.3f},{lon:.3f}|{date_before[:10]}|{date_after[:10]}"
    cache = _cache(cache_dir)
    if key in cache:
        return cache[key]
    rels = releases()
    memo: Dict[int, Optional[Dict]] = {}

    def fetch(i):
        try:
            return capture(rels[i], lat, lon)
        except Exception:  # noqa: BLE001 — one release failing shouldn't stop the search
            return None

    def cap_many(idx):
        todo = [i for i in dict.fromkeys(idx) if i not in memo]
        if todo:
            with ThreadPoolExecutor(max_workers=SPLIT) as pool:
                for i, c in zip(todo, pool.map(fetch, todo)):
                    memo[i] = c
        return [memo[i] for i in idx]

    def first_index(pred, lo=0, hi=None):
        """Smallest i in [lo, hi) with pred(capture(i)) true (captures only grow),
        or hi if none: each round checks up to SPLIT evenly spaced releases."""
        hi = len(rels) if hi is None else hi
        while lo < hi:
            n = hi - lo
            probe = sorted({lo + (n * (j + 1)) // (SPLIT + 1) for j in range(SPLIT)}
                           if n > SPLIT else set(range(lo, hi)))
            if progress:
                progress(f"Checking imagery released {rels[probe[0]]['date'][:4]}"
                         f"–{rels[probe[-1]]['date'][:4]}")
            hits = [i for i, c in zip(probe, cap_many(probe)) if c is not None and pred(c)]
            if not hits:
                lo = probe[-1] + 1
            else:
                first = hits[0]
                below = [i for i in probe if i < first]
                lo, hi = (below[-1] + 1 if below else lo), first
                if n <= SPLIT:
                    return first
        return lo

    after_i = first_index(lambda c: c["date"] > date_after[:10])
    # Newest capture before the change: the last release whose capture predates it.
    before_end = first_index(lambda c: c["date"] >= date_before[:10], 0, after_i)
    before_i = before_end - 1

    def side(i):
        c = cap_many([i])[0] if 0 <= i < len(rels) else None
        if c is None:
            return None
        return {"num": rels[i]["num"], "release": rels[i]["date"], "tiles": rels[i]["tiles"],
                **c}

    out = {"before": side(before_i), "after": side(after_i)}
    out["link"] = wayback_link(lat, lon, out["before"], out["after"])
    with _lock:
        cache[key] = out
        text = json.dumps(cache, indent=0, sort_keys=True)
    try:
        path = Path(cache_dir) / CACHE_NAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return out


def wayback_link(lat: float, lon: float, before: Optional[Dict], after: Optional[Dict],
                 zoom: int = 17) -> str:
    """The Wayback app at this place, swiping between the two releases."""
    params = {"mapCenter": f"{lon:.5f},{lat:.5f},{zoom}"}
    nums = [s["num"] for s in (before, after) if s]
    if nums:
        params["active"] = str(nums[-1])
    if len(nums) == 2:
        params["swipeWidget"] = f"{nums[0]},{nums[1]}"
    return WAYBACK_APP + "#" + urllib.parse.urlencode(params, safe=",")


def describe(s: Dict) -> str:
    """ "23 May 2021 · 31 cm · Vantor" """
    d = date.fromisoformat(s["date"]).strftime("%d %b %Y")
    res = f"{s['res_m'] * 100:.0f} cm" if s.get("res_m") else ""
    return " · ".join(p for p in (d, res, s.get("source", "")) if p)


def _cache(cache_dir) -> Dict:
    d = str(cache_dir)
    with _lock:
        if d not in _caches:
            try:
                _caches[d] = json.loads((Path(d) / CACHE_NAME).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _caches[d] = {}
        return _caches[d]
