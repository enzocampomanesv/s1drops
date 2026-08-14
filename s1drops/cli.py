"""Headless cube management.

    python -m s1drops.cli build  --bbox 3.10 6.35 3.70 6.75 --name lagos \
        --start 2024-01-01 --end 2025-01-01
    python -m s1drops.cli build  --aoi lagos.geojson --name lagos \
        --start 2015-01-01 --end 2026-06-01       # bakes full archive (slow, once)
    python -m s1drops.cli list
    python -m s1drops.cli delete --key lagos_2024-01-01_2025-01-01_nm

`build` auto-detects time-extension: re-running the same AOI with a wider range
bakes only the missing dates and merges. `delete` is an admin operation.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Tuple

from .cube import bake_or_extend, delete_cube, list_cubes, rescan
from .cube.geobox import geom_from_geojson

Bbox = Tuple[float, float, float, float]


def _read_aoi(path: str):
    """(bbox, geometry) for a GeoJSON AOI file."""
    geom = geom_from_geojson(Path(path).read_text())
    return tuple(geom.bounds), geom


def cmd_build(args: argparse.Namespace) -> None:
    if args.aoi:
        bbox_ll, geom = _read_aoi(args.aoi)
    elif args.bbox:
        bbox_ll, geom = tuple(args.bbox), None
    else:
        raise SystemExit("Provide --aoi <geojson> or --bbox minx miny maxx maxy")

    print(f"AOI bbox: {bbox_ll}  range {args.start}..{args.end}  reduction={args.reduction}")
    entry, changed = bake_or_extend(
        bbox_ll, args.start, args.end,
        name=args.name, aoi_geom=geom, reduction=args.reduction,
        resolution=args.res, cache_dir=Path(args.cache_dir),
        progress=print,
    )
    print(f"\n{'Changed' if changed else 'No change'} -> {entry.path}")
    print(f"  span {entry.start}..{entry.end}  passes={entry.n_passes}  "
          f"grid={entry.grid_shape}  crs={entry.crs}")


def cmd_list(args: argparse.Namespace) -> None:
    cubes = list_cubes(Path(args.cache_dir))
    if not cubes:
        print("(no cubes)")
        return
    for e in cubes:
        print(f"{e.key:42s}  {e.name:10s}  {e.start}..{e.end}  "
              f"passes={e.n_passes:3d}  {e.reduction}  {e.crs}")


def cmd_delete(args: argparse.Namespace) -> None:
    if not args.yes:
        ok = input(f"Delete cube '{args.key}'? This is permanent. [y/N] ").strip().lower()
        if ok != "y":
            print("Aborted.")
            return
    removed = delete_cube(Path(args.cache_dir), args.key)
    print("Deleted." if removed else "Nothing to delete (key not found).")


def cmd_rescan(args: argparse.Namespace) -> None:
    reg = rescan(Path(args.cache_dir))
    print(f"Rescanned: {len(reg)} cube(s).")


def main(argv=None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cache-dir", default="./cache")

    p = argparse.ArgumentParser(prog="s1drops")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", parents=[common], help="Bake or time-extend a cube.")
    b.add_argument("--aoi", help="Path to a GeoJSON AOI (polygon).")
    b.add_argument("--bbox", nargs=4, type=float, metavar=("MINX", "MINY", "MAXX", "MAXY"))
    b.add_argument("--name", default=None)
    b.add_argument("--start", default="2024-01-01")
    b.add_argument("--end", default="2025-01-01")
    b.add_argument("--res", type=float, default=100.0)
    b.add_argument("--reduction", choices=("native_median", "overview_med"), default="native_median")
    b.set_defaults(func=cmd_build)

    ls = sub.add_parser("list", parents=[common], help="List registered cubes.")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("delete", parents=[common], help="Delete a cube (admin).")
    d.add_argument("--key", required=True, help="Cube key (filename stem); see `list`.")
    d.add_argument("--yes", action="store_true", help="Skip confirmation.")
    d.set_defaults(func=cmd_delete)

    rs = sub.add_parser("rescan", parents=[common], help="Rebuild registry.json from disk.")
    rs.set_defaults(func=cmd_rescan)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
