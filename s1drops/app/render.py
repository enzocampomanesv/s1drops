"""Render a cube-aligned 2-D array as a single web-mercator image overlay.

The grid is never drawn as per-cell features (that would choke the browser at
~300k cells). Instead a 2-D layer (basemap or hotspot) is reprojected from the
cube's UTM grid to EPSG:3857, colourised to RGBA (NaN -> transparent), and
returned as a PNG data URL plus lat/lon corner bounds for an ipyleaflet
ImageOverlay. Because the image is in 3857 and Leaflet renders in 3857, the
linear corner placement is geometrically exact.
"""
from __future__ import annotations

import base64
import io
from typing import List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors as mcolors  # noqa: E402
import matplotlib.image as mpimg  # noqa: E402
import numpy as np  # noqa: E402
import xarray as xr  # noqa: E402
from affine import Affine  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.transform import array_bounds  # noqa: E402
from rasterio.warp import Resampling, calculate_default_transform, reproject  # noqa: E402

LatLonBounds = List[List[float]]  # [[south, west], [north, east]]


def src_transform(cube: xr.Dataset) -> Affine:
    if "grid_transform" in cube.attrs:
        a, b, c, d, e, f = cube.attrs["grid_transform"]
        return Affine(a, b, c, d, e, f)
    xs = np.asarray(cube["x"].values, dtype="float64")
    ys = np.asarray(cube["y"].values, dtype="float64")
    res = float(cube.attrs.get("resolution", abs(xs[1] - xs[0]) if xs.size > 1 else 100.0))
    return Affine(res, 0.0, xs[0] - res / 2, 0.0, -res, ys[0] + res / 2)


def _dst_grid(src_transform_: Affine, src_crs, h0: int, w0: int):
    """Web-mercator grid covering a source raster. Returns (transform, width, height)."""
    left, bottom, right, top = array_bounds(h0, w0, src_transform_)
    return calculate_default_transform(
        str(src_crs), "EPSG:3857", w0, h0, left, bottom, right, top
    )


def _latlon_bounds(dst_transform: Affine, h: int, w: int) -> LatLonBounds:
    """[[south, west], [north, east]] of a web-mercator grid, for an ImageOverlay."""
    bl, bb, br, bt = array_bounds(h, w, dst_transform)
    tx = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    west, south = tx.transform(bl, bb)
    east, north = tx.transform(br, bt)
    return [[south, west], [north, east]]


def to_webmercator(arr: np.ndarray, cube: xr.Dataset) -> Tuple[np.ndarray, LatLonBounds]:
    """Reproject a (y, x) UTM array to EPSG:3857. Returns (arr3857, latlon bounds)."""
    src_crs = cube.attrs.get("crs", "EPSG:4326")
    st = src_transform(cube)
    h0, w0 = arr.shape
    dt, w, h = _dst_grid(st, src_crs, h0, w0)
    dst = np.full((h, w), np.nan, dtype="float64")
    reproject(
        arr.astype("float64"), dst,
        src_transform=st, src_crs=src_crs,
        dst_transform=dt, dst_crs="EPSG:3857",
        resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan,
    )
    return dst, _latlon_bounds(dt, h, w)


def colorize(
    arr: np.ndarray,
    *,
    cmap: str = "gray",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
) -> np.ndarray:
    """RGBA uint8 from a float array; NaN -> fully transparent."""
    finite = np.isfinite(arr)
    if not finite.any():
        return np.zeros((*arr.shape, 4), dtype="uint8")
    lo = float(np.nanpercentile(arr, 2)) if vmin is None else vmin
    hi = float(np.nanpercentile(arr, 98)) if vmax is None else vmax
    if hi <= lo:
        hi = lo + 1.0
    norm = mcolors.Normalize(lo, hi, clip=True)
    rgba = matplotlib.colormaps[cmap](norm(np.where(finite, arr, lo)))
    rgba[..., 3] = finite.astype("float64")
    return (rgba * 255).astype("uint8")


def png_data_url(rgba: np.ndarray) -> str:
    buf = io.BytesIO()
    mpimg.imsave(buf, rgba, format="png")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def overlay_from_array(
    arr: np.ndarray,
    cube: xr.Dataset,
    *,
    cmap: str = "gray",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
) -> Tuple[str, LatLonBounds]:
    """Convenience: UTM array -> (PNG data URL, lat/lon bounds) for ImageOverlay."""
    merc, bounds = to_webmercator(arr, cube)
    return png_data_url(colorize(merc, cmap=cmap, vmin=vmin, vmax=vmax)), bounds


def overlay_rgb(rgb: np.ndarray, transform, crs) -> Tuple[str, LatLonBounds]:
    """3-band uint8 RGB on a UTM grid -> (PNG data URL, lat/lon bounds) in EPSG:3857.

    Like `overlay_from_array` but for a true-colour image with an explicit geobox
    transform/CRS (not a cube) — used for the Sentinel-2 optical overlays. Areas
    outside the reprojected footprint are made transparent via a coverage mask.
    """
    st = transform if isinstance(transform, Affine) else Affine(*tuple(transform)[:6])
    h0, w0 = rgb.shape[0], rgb.shape[1]
    dt, w, h = _dst_grid(st, crs, h0, w0)
    out = np.zeros((h, w, 4), dtype="uint8")
    for i in range(3):
        band = np.zeros((h, w), dtype="uint8")
        reproject(rgb[:, :, i], band, src_transform=st, src_crs=str(crs),
                  dst_transform=dt, dst_crs="EPSG:3857", resampling=Resampling.bilinear)
        out[:, :, i] = band
    cover = np.zeros((h, w), dtype="uint8")
    reproject(np.ones((h0, w0), dtype="uint8"), cover, src_transform=st, src_crs=str(crs),
              dst_transform=dt, dst_crs="EPSG:3857", resampling=Resampling.nearest)
    out[:, :, 3] = np.where(cover > 0, 255, 0).astype("uint8")
    return png_data_url(out), _latlon_bounds(dt, h, w)


def colorize_lcz(arr: np.ndarray) -> np.ndarray:
    """RGBA uint8 from LCZ class codes via the WUDAPT palette; non-classes transparent."""
    from ..cube.lcz import LCZ_RGB
    codes = np.where(np.isfinite(arr), np.round(arr), 0).astype(int)
    rgba = np.zeros((*codes.shape, 4), dtype="uint8")
    for cls, (r, g, b) in LCZ_RGB.items():
        sel = codes == cls
        rgba[sel, 0], rgba[sel, 1], rgba[sel, 2], rgba[sel, 3] = r, g, b, 255
    return rgba


def overlay_lcz(arr: np.ndarray, cube: xr.Dataset) -> Tuple[str, LatLonBounds]:
    """LCZ class array -> (PNG data URL, lat/lon bounds), coloured by the LCZ palette."""
    merc, bounds = to_webmercator(arr, cube)
    return png_data_url(colorize_lcz(merc)), bounds
