"""Analysis layer: timeseries extraction + sharp-drop detection.

Pure, offline, and UI-agnostic — operates on a cached cube produced by the cube
layer. The PROJ bootstrap is applied on import because click->cell uses pyproj,
which is also vulnerable to a foreign PROJ database.
"""
from ..proj_env import fix_proj_env  # noqa: F401  (applies PROJ fix; keep first)

from .detect import (  # noqa: E402
    DIRECTIONS,
    DetectionResult,
    Drop,
    detect,
    detect_all,
    detect_pelt,
    detect_threshold,
)
from .series import (  # noqa: E402
    POLS,
    Series,
    cell_in_aoi,
    cell_index,
    extract_series,
    to_db,
)

__all__ = [
    "Series",
    "POLS",
    "to_db",
    "cell_index",
    "cell_in_aoi",
    "extract_series",
    "Drop",
    "DetectionResult",
    "DIRECTIONS",
    "detect",
    "detect_all",
    "detect_pelt",
    "detect_threshold",
]
