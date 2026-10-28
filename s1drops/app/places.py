"""Place names for candidate sites, via OpenStreetMap Nominatim reverse geocoding.

Nominatim's usage policy asks for at most one request per second, an identifying
User-Agent, and caching of results. So lookups are serialised behind one lock with
a minimum interval (a separate, short-held lock guards the cache, so reading a
cached name never waits behind a request in progress), and every name is cached on disk in `<cache_dir>/placenames.json`
keyed by coordinates rounded to ~100 m — a site is only ever looked up once, even
across restarts. Failures (e.g. offline) return None and are not cached, so they
are retried on a later visit.

Names are only as exact as OSM allows. Each lookup asks for the neighbourhood-level
feature at the site. If that feature is an area outline (way/relation) the site
lies in it, so the name is used as is. If it is only a point (a place node, as for
most Lagos neighbourhoods), the site is merely near it: within NEAR_M the name is
shown as "near X"; further away it is dropped, leaving only the enclosing
administrative area (city / LGA), which comes from a boundary and is exact.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

URL = ("https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=14"
       "&lat={lat:.5f}&lon={lon:.5f}")
USER_AGENT = "s1drops/0.1 (+https://github.com/enzocampomanesv/s1drops)"
MIN_INTERVAL_S = 1.1
CACHE_NAME = "placenames.json"
NEAR_M = 1500.0

# Feature types that name a neighbourhood-sized place, and the address fields for
# the enclosing administrative area (most specific boundary first).
_LOCAL = ("neighbourhood", "quarter", "suburb", "hamlet", "village", "residential",
          "city_block", "allotments", "isolated_dwelling")
_AREA = ("city", "county", "municipality", "town", "state_district", "state")

_lock = threading.Lock()            # the in-memory caches (held only briefly)
_request_lock = threading.Lock()    # one Nominatim request at a time
_last_request = 0.0
_caches: Dict[str, Dict[str, str]] = {}


def _key(lat: float, lon: float) -> str:
    return f"{lat:.3f},{lon:.3f}"


def _fetch(lat: float, lon: float) -> dict:
    req = urllib.request.Request(URL.format(lat=lat, lon=lon),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _distance_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def name_from_result(r: dict, lat: float, lon: float) -> str:
    """Short place name for a site from a Nominatim reverse result; "" if none.

    "Bagong Silangan, Quezon City" when the site lies inside the named area,
    "near Ijora, Apapa" when the name is only a nearby point, or just the
    administrative area when the nearest named point is too far to be useful.
    """
    addr = r.get("address", {})
    local = r.get("name") if r.get("addresstype") in _LOCAL else ""
    area = next((addr[k] for k in _AREA if addr.get(k) and addr[k] != local), "")
    if local and r.get("osm_type") == "node":
        try:
            far = _distance_m(lat, lon, float(r["lat"]), float(r["lon"])) > NEAR_M
        except (KeyError, TypeError, ValueError):
            far = True
        local = "" if far else f"near {local}"
    return ", ".join(p for p in (local, area) if p)


def _cache(cache_dir) -> Dict[str, str]:
    d = str(cache_dir)
    if d not in _caches:
        try:
            _caches[d] = json.loads((Path(d) / CACHE_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _caches[d] = {}
    return _caches[d]


def cached(cache_dir, lat: float, lon: float) -> Optional[str]:
    """The cached name for a site, or None if it hasn't been looked up yet."""
    with _lock:
        return _cache(cache_dir).get(_key(lat, lon))


def lookup(cache_dir, lat: float, lon: float) -> Optional[str]:
    """Name for a site: from the cache, else one rate-limited Nominatim request.

    Blocking (up to ~MIN_INTERVAL_S per uncached site), so call it off the UI
    thread. Returns None when the lookup fails.
    """
    global _last_request
    k = _key(lat, lon)
    with _request_lock:
        with _lock:
            if k in (cache := _cache(cache_dir)):     # found while we queued
                return cache[k]
        wait = _last_request + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            name = name_from_result(_fetch(lat, lon), lat, lon)
        except Exception:  # noqa: BLE001 — offline / rate-limited / bad reply
            return None
        with _lock:
            cache[k] = name
            text = json.dumps(cache, indent=0, sort_keys=True)
        path = Path(cache_dir) / CACHE_NAME
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # the in-memory cache still serves this session
        return name
