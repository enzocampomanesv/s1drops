"""PROJ/GDAL data bootstrap.

A system-wide ``PROJ_LIB``/``PROJ_DATA`` (commonly set by a PostgreSQL/PostGIS
install) can point GDAL at a stale, pre-v6-layout ``proj.db`` that modern
rasterio refuses to read, which kills any CRS lookup (e.g. EPSG:32631).

Importing this module clears those pointers and aims PROJ/GDAL at rasterio's
own bundled data. It MUST be imported before rasterio / pyproj / odc initialise
their PROJ context, so ``cube/__init__.py`` imports it first.
"""
from __future__ import annotations

import importlib.util
import os


def fix_proj_env() -> None:
    for var in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
        os.environ.pop(var, None)
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


fix_proj_env()
