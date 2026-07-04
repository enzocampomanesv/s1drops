"""Presentation layer (UI-agnostic helpers).

render/logic/plots are pure and offline-testable; the Solara components are thin
wrappers over them. The PROJ bootstrap is applied on import because render uses
rasterio/pyproj.
"""
from ..proj_env import fix_proj_env  # noqa: F401  (applies PROJ fix; keep first)

from . import logic, plots, render  # noqa: E402,F401

__all__ = ["render", "logic", "plots"]
