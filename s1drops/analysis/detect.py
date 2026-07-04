"""Sharp-drop detection on dB backscatter series.

Two methods, each run per series (one look geometry) on the NaN-dropped
subsequence, with detected events mapped back to real dates:

- PELT (data-driven default): ruptures mean-shift changepoints with an
  l2 cost. The penalty is auto-scaled to each series' own noise so a single
  `sensitivity` knob works across cells (higher sensitivity -> more, smaller
  changepoints). A changepoint is reported only if the pre->post level falls by
  at least `min_drop_db`.
- Threshold (manual): sliding pre/post median windows of width `window`; flag
  where median(before) - median(after) >= min_drop_db. At window=1 this is the
  raw single-step difference; wider windows favour sustained drops and resist
  speckle. Consecutive flags are merged into one event.

Detection runs on sample index; the real day-gap of each event is reported
separately, since S1 sampling is irregular.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from .series import Series

DIRECTIONS = ("drop", "rise", "both")


@dataclass
class Drop:
    date_before: np.datetime64
    date_after: np.datetime64
    delta_db: float       # post - pre (negative for a drop)
    pre_db: float
    post_db: float
    gap_days: int
    method: str


@dataclass
class DetectionResult:
    series: Series
    drops: List[Drop]
    method: str
    seg_bounds: Optional[List[int]] = None   # PELT segment boundaries (series indices)
    seg_means: Optional[List[float]] = None
    sigma_db: Optional[float] = None


def _robust_sigma(x: np.ndarray) -> float:
    """Noise std from first differences via MAD (changepoints are outliers here)."""
    if x.size < 3:
        return 1.0
    dx = np.diff(x)
    mad = np.median(np.abs(dx - np.median(dx)))
    sigma = 1.4826 * mad / np.sqrt(2.0)  # diff inflates variance by 2
    return float(max(sigma, 1e-6))


def _passes(delta: float, thr: float, direction: str) -> bool:
    if direction == "drop":
        return delta <= -thr
    if direction == "rise":
        return delta >= thr
    return abs(delta) >= thr


def detect_pelt(
    series: Series,
    *,
    sensitivity: float = 1.0,
    min_drop_db: float = 2.5,
    direction: str = "drop",
    min_size: int = 2,
) -> DetectionResult:
    import ruptures as rpt

    s = series.dropna()
    x = s.values_db.astype("float64")
    n = x.size
    if n < 2 * min_size + 1:
        return DetectionResult(s, [], "pelt", seg_bounds=[0, n] if n else [], seg_means=[], sigma_db=None)

    sigma = _robust_sigma(x)
    penalty = (sigma ** 2) * np.log(n) / max(sensitivity, 1e-6)
    bkps = rpt.Pelt(model="l2", min_size=min_size).fit(x).predict(pen=penalty)

    bounds = [0] + list(bkps)                       # segment edges; bounds[-1] == n
    seg_means = [float(np.mean(x[bounds[i]:bounds[i + 1]])) for i in range(len(bounds) - 1)]
    cps = bounds[1:-1]                               # interior changepoints

    drops: List[Drop] = []
    for k, cp in enumerate(cps):
        pre, post = seg_means[k], seg_means[k + 1]
        delta = post - pre
        if not _passes(delta, min_drop_db, direction):
            continue
        db_before, db_after = s.dates[cp - 1], s.dates[cp]
        gap = int((db_after - db_before) / np.timedelta64(1, "D"))
        drops.append(Drop(db_before, db_after, delta, pre, post, gap, "pelt"))

    return DetectionResult(s, drops, "pelt", seg_bounds=bounds, seg_means=seg_means, sigma_db=sigma)


def detect_threshold(
    series: Series,
    *,
    window: int = 2,
    min_drop_db: float = 3.0,
    direction: str = "drop",
) -> DetectionResult:
    s = series.dropna()
    x = s.values_db.astype("float64")
    d = s.dates
    n = x.size
    if n < 2:
        return DetectionResult(s, [], "threshold")

    # Flag each transition i (between i-1 and i) using up to `window` samples each side.
    flagged = []  # (i, pre, post)
    for i in range(1, n):
        pre = float(np.median(x[max(0, i - window):i]))
        post = float(np.median(x[i:min(n, i + window)]))
        if _passes(post - pre, min_drop_db, direction):
            flagged.append((i, pre, post))

    # Merge runs of consecutive transition indices into single events.
    drops: List[Drop] = []
    j = 0
    while j < len(flagged):
        k = j
        while k + 1 < len(flagged) and flagged[k + 1][0] == flagged[k][0] + 1:
            k += 1
        i_first, pre0, _ = flagged[j]
        i_last, _, post1 = flagged[k]
        db_before, db_after = d[i_first - 1], d[i_last]
        gap = int((db_after - db_before) / np.timedelta64(1, "D"))
        drops.append(Drop(db_before, db_after, post1 - pre0, pre0, post1, gap, "threshold"))
        j = k + 1

    return DetectionResult(s, drops, "threshold")


def detect(series: Series, *, method: str = "pelt", **kw) -> DetectionResult:
    if method == "pelt":
        return detect_pelt(series, **kw)
    if method == "threshold":
        return detect_threshold(series, **kw)
    raise ValueError(f"method must be 'pelt' or 'threshold', got {method!r}")


def detect_all(series_list: Sequence[Series], *, method: str = "pelt", **kw) -> List[DetectionResult]:
    return [detect(s, method=method, **kw) for s in series_list]
