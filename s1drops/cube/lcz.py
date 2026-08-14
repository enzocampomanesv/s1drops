"""Local Climate Zones (LCZ) support.

Source: the global 100 m LCZ map (Demuzere et al. 2022, nominal year 2018,
EPSG:4326), filtered version `lcz_filter_v1.tif`, downloaded once and referenced
locally via the S1DROPS_LCZ_PATH env var. It is a single static epoch — there is
no annual LCZ — so one layer is generated per cube grid and applies across the
whole date range. For eviction detection a static, pre-event urban mask is
actually preferable: a time-varying mask would drop a settlement once it is
demolished, hiding the very event we want to detect.

Classes 1-10 are built; 11-17 (A-G) are natural (17 = water).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

import numpy as np
import rasterio
from rasterio.enums import Resampling

ENV_VAR = "S1DROPS_LCZ_PATH"

# Default analysis filter: lower-rise / informal-relevant built classes.
DEFAULT_CLASSES = (3, 6, 7, 8)

LCZ_NAMES = {
    1: "Compact high-rise", 2: "Compact midrise", 3: "Compact low-rise",
    4: "Open high-rise", 5: "Open midrise", 6: "Open low-rise",
    7: "Lightweight low-rise", 8: "Large low-rise", 9: "Sparsely built",
    10: "Heavy industry",
    11: "Dense trees", 12: "Scattered trees", 13: "Bush, scrub",
    14: "Low plants", 15: "Bare rock/paved", 16: "Bare soil/sand", 17: "Water",
}

# Standard WUDAPT LCZ colours (hex) for the overlay.
LCZ_HEX = {
    1: "#8c0000", 2: "#d10000", 3: "#ff0000", 4: "#bf4d00", 5: "#ff6600",
    6: "#ff9955", 7: "#faee05", 8: "#bcbcbc", 9: "#ffccaa", 10: "#555555",
    11: "#006a00", 12: "#00aa00", 13: "#648525", 14: "#b9db79", 15: "#000000",
    16: "#fbf7ae", 17: "#6a6aff",
}


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


LCZ_RGB = {k: _hex_to_rgb(v) for k, v in LCZ_HEX.items()}


def lcz_path_from_env() -> Optional[str]:
    return os.environ.get(ENV_VAR) or None


def lcz_status() -> tuple:
    """('enabled'|'missing'|'unset', path) — what a bake will see right now."""
    p = lcz_path_from_env()
    if not p:
        return ("unset", None)
    if not os.path.exists(p):
        return ("missing", p)
    return ("enabled", p)


def resolve_lcz_path(explicit: Optional[str]) -> Optional[str]:
    """Use the explicit path if given, else the env var; None means 'no LCZ'."""
    return explicit if explicit is not None else lcz_path_from_env()


def lcz_for_geobox(gbox, dst_crs, lcz_path: str) -> np.ndarray:
    """Read + reproject the LCZ map onto a cube GeoBox (categorical, nearest).

    Window-reads only the AOI from the (global) source in its own CRS first, then
    reprojects that small array onto the cube grid. This avoids warping straight
    from the multi-GB global file, which on a non-tiled source pulls full-width
    strips and can fail with "chunk and warp failed" or exhaust memory. Returns an
    int16 (y, x) array of class codes aligned to the cube grid; 0 = no class.
    """
    if not os.path.exists(lcz_path):
        raise RuntimeError(
            f"LCZ file not found: {lcz_path!r}. Download the global LCZ GeoTIFF from "
            "https://zenodo.org/records/6364594 and set S1DROPS_LCZ_PATH to it."
        )
    from pyproj import Transformer
    from rasterio.transform import array_bounds
    from rasterio.warp import reproject
    from rasterio.windows import from_bounds as window_from_bounds

    h, w = int(gbox.shape.y), int(gbox.shape.x)
    left, bottom, right, top = array_bounds(h, w, gbox.transform)  # AOI bounds in cube CRS

    with rasterio.open(lcz_path) as src:
        # AOI bounds in the source CRS, padded so the window covers the whole AOI.
        tx = Transformer.from_crs(str(dst_crs), src.crs, always_xy=True)
        xs, ys = tx.transform([left, right, left, right], [bottom, bottom, top, top])
        pad = 0.02  # degrees (~2 km); source is EPSG:4326
        win = window_from_bounds(
            min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad,
            transform=src.transform,
        ).round_offsets().round_lengths()
        src_arr = src.read(1, window=win, boundless=True, fill_value=0)
        src_transform = src.window_transform(win)
        src_crs = src.crs

    dst = np.zeros((h, w), dtype=src_arr.dtype)
    reproject(
        src_arr, dst,
        src_transform=src_transform, src_crs=src_crs,
        dst_transform=gbox.transform, dst_crs=str(dst_crs),
        resampling=Resampling.nearest, src_nodata=0, dst_nodata=0,
    )
    return dst.astype("int16")


def allowed_mask(lcz: np.ndarray, classes: Iterable[int]) -> np.ndarray:
    """Boolean (y, x): cells whose LCZ code is in `classes`."""
    return np.isin(lcz, list(classes))
