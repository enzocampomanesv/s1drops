"""Grid construction for the 100 m cube.

Everything here is deterministic and offline-testable: given a lon/lat AOI it
picks a local UTM zone, snaps the projected bounding box to the resolution grid,
builds a north-up GeoBox, and (optionally) rasterises an AOI polygon to a
boolean cell mask aligned to that GeoBox.
"""
from __future__ import annotations

import json
import math
from typing import Tuple, Union

import numpy as np
from odc.geo.geobox import GeoBox
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

Bbox = Tuple[float, float, float, float]
DEFAULT_RES = 100.0


def geom_from_geojson(source: Union[str, bytes, dict]):
    """Shapely geometry from a GeoJSON string, bytes, or already-parsed dict.

    Accepts the three shapes an AOI arrives in — a bare geometry, a Feature, or a
    FeatureCollection (first feature) — so the CLI's file input and the app's
    upload/draw inputs agree on what a valid AOI is.
    """
    gj = json.loads(source) if isinstance(source, (str, bytes)) else source
    if gj.get("type") == "FeatureCollection":
        return shape(gj["features"][0]["geometry"])
    if gj.get("type") == "Feature":
        return shape(gj["geometry"])
    return shape(gj)


def utm_epsg(lon: float, lat: float) -> str:
    """Local UTM EPSG code for a lon/lat point."""
    zone = int(math.floor((lon + 180.0) / 6.0) % 60) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def _to_utm(utm_crs: str) -> Transformer:
    return Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)


def _snap(minx: float, miny: float, maxx: float, maxy: float, res: float) -> Bbox:
    return (
        math.floor(minx / res) * res,
        math.floor(miny / res) * res,
        math.ceil(maxx / res) * res,
        math.ceil(maxy / res) * res,
    )


def aoi_to_geobox(bbox_ll: Bbox, resolution: float = DEFAULT_RES) -> Tuple[GeoBox, str]:
    """lon/lat bbox -> (snapped UTM GeoBox at `resolution` m, UTM EPSG string).

    All four corners are projected (not just two) so the UTM envelope is correct
    despite projection curvature, then snapped to the resolution grid so the same
    AOI always yields the same grid (stable cache keys and cell indices).
    """
    minlon, minlat, maxlon, maxlat = bbox_ll
    clon, clat = (minlon + maxlon) / 2.0, (minlat + maxlat) / 2.0
    utm = utm_epsg(clon, clat)
    t = _to_utm(utm)
    lons = [minlon, maxlon, maxlon, minlon]
    lats = [minlat, minlat, maxlat, maxlat]
    xs, ys = t.transform(lons, lats)
    sb = _snap(min(xs), min(ys), max(xs), max(ys), resolution)
    gbox = GeoBox.from_bbox(sb, utm, resolution=resolution, tight=True)
    return gbox, utm


def project_geom(geom_ll, utm_crs: str):
    """Reproject a shapely geometry from lon/lat to the given UTM CRS."""
    t = _to_utm(utm_crs)
    return shp_transform(lambda x, y, z=None: t.transform(x, y), geom_ll)


def rasterize_mask(geom_ll, gbox: GeoBox) -> np.ndarray:
    """Boolean (y, x) mask: True where a cell is inside the AOI polygon."""
    geom_utm = project_geom(geom_ll, str(gbox.crs))
    out_shape = (gbox.shape.y, gbox.shape.x)
    mask = rasterize(
        [(mapping(geom_utm), 1)],
        out_shape=out_shape,
        transform=gbox.transform,
        fill=0,
        all_touched=True,
        dtype="uint8",
    )
    return mask.astype(bool)


def refine_geobox(gbox: GeoBox, factor: int) -> GeoBox:
    """A GeoBox covering the same extent at 1/factor the pixel size.

    Used to load native-resolution pixels onto a grid that is an exact integer
    refinement of the target grid, so coarsening by `factor` lands back on it.
    """
    t = gbox.transform
    minx, maxy = t.c, t.f
    maxx = minx + gbox.shape.x * abs(t.a)
    miny = maxy - gbox.shape.y * abs(t.e)
    return GeoBox.from_bbox(
        (minx, miny, maxx, maxy), gbox.crs, resolution=abs(t.a) / factor, tight=True
    )
