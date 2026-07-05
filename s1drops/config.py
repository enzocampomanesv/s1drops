"""Environment-driven operational limits (v2).

All are read live from the environment so they can be tuned without code changes:
  S1DROPS_NATIVE10_MAX_KM2    max AOI area for a 10 m native bake   (default 10)
  S1DROPS_MAX_CUBE_BYTES      hard ceiling on estimated cube RAM    (default 2 GB)
  S1DROPS_EST_PASSES_PER_YEAR passes/year for the live size estimate (default 40)
"""
from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def native10_max_km2() -> float:
    """Max AOI area (km²) allowed for a 10 m native bake."""
    return _env_float("S1DROPS_NATIVE10_MAX_KM2", 10.0)


def max_cube_bytes() -> float:
    """Ceiling on estimated in-memory cube size (bytes) before a bake is refused.

    write_cube() materialises the whole cube into RAM, so this bounds peak memory.
    """
    return _env_float("S1DROPS_MAX_CUBE_BYTES", 2_000_000_000.0)


def est_passes_per_year() -> float:
    """Rough passes/year used ONLY for the live size estimate in the bake form."""
    return _env_float("S1DROPS_EST_PASSES_PER_YEAR", 40.0)


# ---- Stage 2: Sentinel-2 optical before/after --------------------------------

def s2_window_days() -> float:
    """Half-window (days) each side of a drop to search for a clear S2 scene."""
    return _env_float("S1DROPS_S2_WINDOW_DAYS", 45.0)


def s2_cloud_thresh() -> float:
    """Max AOI cloud fraction (0-1) for a scene to count as 'clear enough'."""
    return _env_float("S1DROPS_S2_CLOUD_THRESH", 0.10)


def s2_max_probes() -> float:
    """Hard ceiling on scenes per side whose AOI cloud is evaluated. Default covers
    a normal +/-45 d window so a clear scene anywhere in it is found; raise for
    wider windows, lower to bound cost."""
    return _env_float("S1DROPS_S2_MAX_PROBES", 24.0)


def s2_candidates() -> float:
    """How many before/after scenes to offer per side (all under the cloud threshold,
    clearest first), cycled with arrows in the UI."""
    return _env_float("S1DROPS_S2_CANDIDATES", 3.0)


def s2_cloud_bucket() -> float:
    """Cloud-cover band width (%) for ranking: scenes are sorted by cloud rounded to
    this bucket, then proximity — so meaningfully clearer scenes win across buckets
    while similarly-clear scenes prefer the nearer date."""
    return _env_float("S1DROPS_S2_CLOUD_BUCKET", 5.0)


def optical_radius_m() -> float:
    """Half-size (m) of the box fetched around a clicked drop (default 1 km -> 2 km box)."""
    return _env_float("S1DROPS_OPTICAL_RADIUS_M", 1000.0)
