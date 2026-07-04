"""Cube layer: ingestion of Sentinel-1 RTC into a cached 100 m data cube.

The PROJ/GDAL bootstrap is imported first so it runs before any rasterio/pyproj
context is created anywhere in the package.
"""
from ..proj_env import fix_proj_env  # noqa: F401  (applies PROJ fix on import; keep first)

from .build import (  # noqa: E402
    REDUCTIONS,
    assemble_tracks,
    build_cube,
    coarsen_median,
    tag_track,
)
from .cache import build_or_load, cache_key, cube_path, read_cube, write_cube  # noqa: E402
from .geobox import aoi_to_geobox, rasterize_mask, refine_geobox, utm_epsg  # noqa: E402
from .manage import bake_or_extend, merge_time, missing_ranges  # noqa: E402
from .registry import (  # noqa: E402
    CubeEntry,
    delete_cube,
    find_by_grid,
    grid_signature,
    list_cubes,
    load_registry,
    register_cube,
    rescan,
    scan_cubes,
)
from .stac import ItemMeta, search_items  # noqa: E402

__all__ = [
    "aoi_to_geobox",
    "rasterize_mask",
    "refine_geobox",
    "utm_epsg",
    "search_items",
    "ItemMeta",
    "build_cube",
    "assemble_tracks",
    "tag_track",
    "coarsen_median",
    "REDUCTIONS",
    "build_or_load",
    "cache_key",
    "cube_path",
    "read_cube",
    "write_cube",
    "bake_or_extend",
    "merge_time",
    "missing_ranges",
    "CubeEntry",
    "delete_cube",
    "find_by_grid",
    "grid_signature",
    "list_cubes",
    "load_registry",
    "register_cube",
    "rescan",
    "scan_cubes",
]
