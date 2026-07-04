"""End-to-end analysis demo against a cached cube.

Loads a cube, resolves a clicked lon/lat (or the cube centre) to a cell, builds
per-(pol x orbit) dB series, filters to a date window (with detection padding),
runs a detector, and prints the drops.

Examples:
    python demo_analysis.py --cube cache/lagos_2024-01-01_2025-01-01_nm.zarr
    python demo_analysis.py --cube cache/lagos_2024-01-01_2025-01-01_nm.zarr \
        --lon 3.38 --lat 6.46 --pol vv --start 2024-03-01 --end 2024-10-01 \
        --method pelt --sensitivity 1.0 --min-drop 2.5
"""
from __future__ import annotations

import argparse

import numpy as np

from s1drops.cube import read_cube
from s1drops.analysis import cell_index, detect, extract_series

PAD = 2  # passes beyond the visible window, per the agreed detection scope


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cube", required=True)
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--pol", default="vv", choices=("vv", "vh", "vv_vh"))
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--method", default="pelt", choices=("pelt", "threshold"))
    ap.add_argument("--direction", default="drop", choices=("drop", "rise", "both"))
    ap.add_argument("--sensitivity", type=float, default=1.0)
    ap.add_argument("--min-drop", type=float, default=2.5)
    ap.add_argument("--window", type=int, default=2)
    args = ap.parse_args()

    cube = read_cube(args.cube)
    print(f"cube: {dict(cube.sizes)}  crs={cube.attrs.get('crs')}  "
          f"reduction={cube.attrs.get('reduction')}")

    if args.lon is None or args.lat is None:
        yi, xi = cube.sizes["y"] // 2, cube.sizes["x"] // 2
        print(f"no lon/lat given -> using centre cell (yi={yi}, xi={xi})")
    else:
        idx = cell_index(cube, args.lon, args.lat)
        if idx is None:
            raise SystemExit("Click is outside the cube extent.")
        yi, xi = idx
        print(f"cell (yi={yi}, xi={xi}) for lon/lat ({args.lon}, {args.lat})")

    series = [s for s in extract_series(cube, yi, xi) if s.pol == args.pol]
    kw = dict(direction=args.direction)
    if args.method == "pelt":
        kw.update(sensitivity=args.sensitivity, min_drop_db=args.min_drop)
    else:
        kw.update(window=args.window, min_drop_db=args.min_drop)

    for s in series:
        view = s.filter(args.start, args.end, pad=PAD)
        res = detect(view, method=args.method, **kw)
        n = len(view.dropna())
        print(f"\n=== {s.label} | {n} obs in window | {len(res.drops)} drop(s) ===")
        for dpt in res.drops:
            print(f"  {np.datetime_as_string(dpt.date_before, unit='D')} -> "
                  f"{np.datetime_as_string(dpt.date_after, unit='D')}  "
                  f"({dpt.gap_days}d)  {dpt.pre_db:+.1f} -> {dpt.post_db:+.1f} dB  "
                  f"(Δ {dpt.delta_db:+.1f} dB)")


if __name__ == "__main__":
    main()
