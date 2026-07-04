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
