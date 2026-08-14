import json

import numpy as np
import pytest
from shapely.geometry import box

from s1drops.cube.geobox import (
    aoi_to_geobox,
    geom_from_geojson,
    rasterize_mask,
    refine_geobox,
    utm_epsg,
)

_POLY = {"type": "Polygon",
         "coordinates": [[[3.35, 6.40], [3.50, 6.40], [3.50, 6.50],
                          [3.35, 6.50], [3.35, 6.40]]]}
_FEATURE = {"type": "Feature", "geometry": _POLY, "properties": {"name": "aoi"}}
_COLLECTION = {"type": "FeatureCollection", "features": [_FEATURE]}


def test_utm_epsg_zones():
    assert utm_epsg(3.425, 6.45) == "EPSG:32631"   # Lagos
    assert utm_epsg(121.0, 14.6) == "EPSG:32651"   # Metro Manila
    assert utm_epsg(-58.4, -34.6) == "EPSG:32721"  # southern hemisphere


def test_geobox_is_snapped_and_deterministic():
    bbox = (3.35, 6.40, 3.50, 6.50)
    g1, utm1 = aoi_to_geobox(bbox, 100.0)
    g2, utm2 = aoi_to_geobox(bbox, 100.0)
    assert utm1 == utm2 == "EPSG:32631"
    assert g1.shape == g2.shape
    # origin snapped to the 100 m grid
    t = g1.transform
    assert abs(t.a) == 100.0 and abs(t.e) == 100.0
    assert (t.c % 100 == 0) and (t.f % 100 == 0)
    # ~16 km wide / ~11 km tall at 100 m -> roughly 160 x 110 cells
    assert 150 <= g1.shape.x <= 175 and 100 <= g1.shape.y <= 125


def test_rasterize_mask_inside_polygon():
    bbox = (3.35, 6.40, 3.50, 6.50)
    gbox, _ = aoi_to_geobox(bbox, 100.0)
    # A polygon covering the left half of the bbox in lon.
    half = box(3.35, 6.40, 3.425, 6.50)
    mask = rasterize_mask(half, gbox)
    assert mask.dtype == bool
    assert mask.shape == (gbox.shape.y, gbox.shape.x)
    frac = mask.mean()
    # roughly half the cells should be inside
    assert 0.3 < frac < 0.7
    # full-bbox polygon should mark (almost) everything inside
    full = box(*bbox)
    assert rasterize_mask(full, gbox).mean() > 0.95


@pytest.mark.parametrize("obj", [_POLY, _FEATURE, _COLLECTION],
                         ids=["geometry", "feature", "collection"])
def test_geom_from_geojson_unwraps_every_aoi_shape(obj):
    """CLI (--aoi file) and app (upload/draw) share one parser, so all three
    GeoJSON shapes must yield the same geometry."""
    expected = box(3.35, 6.40, 3.50, 6.50)
    for source in (obj, json.dumps(obj), json.dumps(obj).encode()):
        geom = geom_from_geojson(source)
        assert geom.equals(expected)
        assert tuple(round(v, 6) for v in geom.bounds) == (3.35, 6.40, 3.50, 6.50)


def test_geom_from_geojson_collection_takes_first_feature():
    second = {"type": "Feature", "properties": {},
              "geometry": {"type": "Polygon",
                           "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}
    fc = {"type": "FeatureCollection", "features": [_FEATURE, second]}
    assert geom_from_geojson(fc).equals(box(3.35, 6.40, 3.50, 6.50))


def test_refine_geobox_is_exact_integer_refinement():
    gbox, _ = aoi_to_geobox((3.35, 6.40, 3.50, 6.50), 100.0)
    fine = refine_geobox(gbox, 10)
    assert fine.shape.x == gbox.shape.x * 10
    assert fine.shape.y == gbox.shape.y * 10
    # same extent origin, 10 m pixels
    assert abs(fine.transform.c - gbox.transform.c) < 1e-6
    assert abs(fine.transform.f - gbox.transform.f) < 1e-6
    assert abs(abs(fine.transform.a) - 10.0) < 1e-6
