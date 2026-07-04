"""Timeseries extraction from a cached cube.

A clicked cell is resolved to grid indices, the cube is sliced there, values are
converted to dB, and the result is split into one series per
(polarisation x relative orbit) so look geometries are never mixed. VV/VH is
derived on the fly as VV_dB - VH_dB. Date-window filtering supports detection
padding (a few passes beyond the visible range so edge drops keep a before/after).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
import xarray as xr
from pyproj import Transformer

POLS: Tuple[str, ...] = ("vv", "vh", "vv_vh")
PRETTY = {"vv": "VV", "vh": "VH", "vv_vh": "VV/VH"}


def to_db(linear) -> np.ndarray:
    """10*log10, with non-positive / non-finite values mapped to NaN."""
    x = np.asarray(linear, dtype="float64")
    out = np.full(x.shape, np.nan)
    pos = np.isfinite(x) & (x > 0)
    out[pos] = 10.0 * np.log10(x[pos])
    return out


@dataclass
class Series:
    pol: str
    relative_orbit: int
    orbit_state: str
    dates: np.ndarray       # datetime64[ns], ascending
    values_db: np.ndarray   # float64, may contain NaN
    yi: int
    xi: int

    def __len__(self) -> int:
        return int(self.dates.shape[0])

    @property
    def label(self) -> str:
        return f"{PRETTY.get(self.pol, self.pol)} {self.orbit_state} (orbit {self.relative_orbit})"

    def dropna(self) -> "Series":
        m = np.isfinite(self.values_db)
        return self._slice(m)

    def _slice(self, sel) -> "Series":
        return Series(
            self.pol, self.relative_orbit, self.orbit_state,
            self.dates[sel], self.values_db[sel], self.yi, self.xi,
        )

    def filter(self, start=None, end=None, pad: int = 0) -> "Series":
        """Restrict to [start, end] (inclusive, by calendar day), plus `pad`
        samples beyond each edge for detection context."""
        if len(self) == 0:
            return self
        days = self.dates.astype("datetime64[D]")
        lo = np.datetime64(start, "D") if start is not None else days.min()
        hi = np.datetime64(end, "D") if end is not None else days.max()
        idx = np.where((days >= lo) & (days <= hi))[0]
        if idx.size == 0:
            return self._slice(slice(0, 0))
        i0 = max(0, int(idx[0]) - pad)
        i1 = min(len(self) - 1, int(idx[-1]) + pad)
        return self._slice(slice(i0, i1 + 1))


def cell_index(cube: xr.Dataset, lon: float, lat: float) -> Optional[Tuple[int, int]]:
    """Nearest (yi, xi) for a lon/lat click, or None if outside the cube extent."""
    crs = cube.attrs.get("crs", "EPSG:4326")
    tx = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    X, Y = tx.transform(lon, lat)
    xs = np.asarray(cube["x"].values)
    ys = np.asarray(cube["y"].values)
    xi = int(np.abs(xs - X).argmin())
    yi = int(np.abs(ys - Y).argmin())
    res = float(cube.attrs.get("resolution", 100.0))
    if abs(xs[xi] - X) > res or abs(ys[yi] - Y) > res:
        return None
    return yi, xi


def cell_in_aoi(cube: xr.Dataset, yi: int, xi: int) -> bool:
    if "aoi_mask" not in cube:
        return True
    return bool(cube["aoi_mask"].values[yi, xi])


def extract_series(
    cube: xr.Dataset,
    yi: int,
    xi: int,
    pols: Sequence[str] = POLS,
) -> List[Series]:
    """One Series per (pol x relative orbit) at the given cell."""
    times = np.asarray(cube["time"].values)
    ro = np.asarray(cube["relative_orbit"].values)
    state = np.asarray(cube["orbit_state"].values)

    vv_db = to_db(cube["vv"].isel(y=yi, x=xi).values)
    vh_db = to_db(cube["vh"].isel(y=yi, x=xi).values)
    pol_db = {"vv": vv_db, "vh": vh_db, "vv_vh": vv_db - vh_db}

    out: List[Series] = []
    for ro_val in np.unique(ro):
        m = ro == ro_val
        order = np.argsort(times[m])
        d = times[m][order]
        st = str(state[m][0])
        for pol in pols:
            if pol not in pol_db:
                continue
            out.append(Series(pol, int(ro_val), st, d, pol_db[pol][m][order], yi, xi))
    return out
