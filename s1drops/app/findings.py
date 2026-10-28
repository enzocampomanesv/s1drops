"""Plain-language view of one location's detections, for readers new to radar.

The detector works per satellite track (each track sees the site from its own
angle, at its own absolute level). Readers care about events, not tracks, so:

- `events` merges per-track drops that happen at about the same time into one
  event and counts how many tracks agree ("seen by 2 of 2 tracks").
- `relative_signal` puts every track on one scale, "% of usual level", by
  dividing each by its own typical value before the first event. Tracks can then
  be pooled into one trend line, with a band for the normal ups and downs.
- `strength`, `change_text` and `when_text` turn dB values and date pairs into
  words ("Strong", "~90% weaker", "late Feb – early Mar 2023").

dB -> % uses the definition of the decibel: a change of d dB multiplies the
reflected power by 10^(d/10), so -3 dB is about half and -10 dB a tenth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..analysis.detect import DetectionResult

# Two tracks pass a site a few days apart, so their before/after windows for the
# same event can be offset by up to about two passes.
MERGE_DAYS = 24
MIN_REF_PASSES = 3     # passes needed to set a track's usual level
MIN_BAND_PASSES = 5    # pooled passes needed to draw the normal-range band
BAND_PCT = (10, 90)    # normal range = middle 80% of the passes before any change


@dataclass
class Event:
    start: np.datetime64     # earliest "before" date among the merged detections
    end: np.datetime64       # latest "after" date
    delta_db: float          # mean change across the tracks that saw it
    tracks: int              # tracks that flagged it
    total_tracks: int        # tracks with data at this location

    @property
    def is_drop(self) -> bool:
        return self.delta_db < 0


def _day(d) -> np.datetime64:
    return np.datetime64(d, "D")


def events(results: Sequence[DetectionResult], merge_days: int = MERGE_DAYS) -> List[Event]:
    """Per-track drops merged into events (drops and rises kept apart), by date."""
    total = sum(1 for r in results if len(r.series))
    found = [(d, r.series.label) for r in results for d in r.drops]
    gap = np.timedelta64(merge_days, "D")
    out: List[Event] = []
    for want_drop in (True, False):
        same = sorted((t for t in found if (t[0].delta_db < 0) == want_drop),
                      key=lambda t: _day(t[0].date_before))
        cluster, cluster_end = [], None
        for d, label in same:
            if cluster and _day(d.date_before) <= cluster_end + gap:
                cluster.append((d, label))
                cluster_end = max(cluster_end, _day(d.date_after))
            else:
                if cluster:
                    out.append(_event(cluster, total))
                cluster, cluster_end = [(d, label)], _day(d.date_after)
        if cluster:
            out.append(_event(cluster, total))
    out.sort(key=lambda e: e.start)
    return out


def _event(cluster, total) -> Event:
    per_track: Dict[str, float] = {}
    for d, label in cluster:   # a track may split one event into two steps: add them
        per_track[label] = per_track.get(label, 0.0) + d.delta_db
    return Event(
        start=min(_day(d.date_before) for d, _ in cluster),
        end=max(_day(d.date_after) for d, _ in cluster),
        delta_db=float(np.mean(list(per_track.values()))),
        tracks=len(per_track),
        total_tracks=max(total, len(per_track)),
    )


def to_pct(delta_db) -> np.ndarray:
    """dB difference -> % of the reference power (0 dB = 100%)."""
    return 100.0 * np.power(10.0, np.asarray(delta_db, dtype="float64") / 10.0)


@dataclass
class RelativeSignal:
    dates: np.ndarray       # every pass of every track, ascending
    pct: np.ndarray         # % of that track's usual level
    hover: List[str]
    trend: np.ndarray       # running median over neighbouring passes, % of usual
    band: Optional[tuple]   # (low %, high %) normal range before any change


def relative_signal(results: Sequence[DetectionResult], evts: Sequence[Event]) -> RelativeSignal:
    """All tracks on one "% of usual level" scale, pooled into one trend."""
    cutoff = evts[0].start if evts else None
    dates, rel, hover, pre = [], [], [], []
    for r in results:
        s = r.series
        ok = np.isfinite(s.values_db)
        d, v = s.dates[ok].astype("datetime64[D]"), s.values_db[ok]
        if not len(v):
            continue
        before = v[d < cutoff] if cutoff is not None else v
        if len(before) < MIN_REF_PASSES:     # change right at the start: use the first passes
            before = v[:MIN_REF_PASSES]
        x = v - float(np.median(before))
        pre.extend(x[d < cutoff] if cutoff is not None else x)
        when = "evening" if s.orbit_state == "ascending" else "morning"
        for di, xi in zip(d, x):
            hover.append(f"{pd.Timestamp(di):%d %b %Y}: {to_pct(xi):.0f}% of usual level "
                         f"({when} pass)")
        dates.extend(d)
        rel.extend(x)
    order = np.argsort(np.asarray(dates, dtype="datetime64[D]"), kind="stable")
    dates = np.asarray(dates, dtype="datetime64[D]")[order]
    rel = np.asarray(rel, dtype="float64")[order]
    hover = [hover[i] for i in order]
    trend = pd.Series(rel).rolling(3, center=True, min_periods=1).median().to_numpy()
    band = None
    if len(pre) >= MIN_BAND_PASSES:
        lo, hi = np.percentile(pre, BAND_PCT)
        band = (float(to_pct(lo)), float(to_pct(hi)))
    return RelativeSignal(dates, to_pct(rel), hover, to_pct(trend), band)


def strength(delta_db: float, min_change: float) -> str:
    """Strong / Moderate / Slight, relative to the minimum change the user set."""
    m, t = abs(delta_db), max(min_change, 0.1)
    return "Strong" if m >= 2 * t else "Moderate" if m >= 1.5 * t else "Slight"


def change_text(delta_db: float) -> str:
    """"~90% weaker", "~40% stronger", "~3× stronger" (rounded: these are estimates)."""
    f = 10.0 ** (delta_db / 10.0)
    if delta_db < 0:
        return f"~{_round5((1 - f) * 100)}% weaker"
    if f >= 2:
        return f"~{f:.0f}× stronger" if f >= 10 else f"~{f:.1f}× stronger"
    return f"~{_round5((f - 1) * 100)}% stronger"


def _round5(x: float) -> int:
    return int(5 * round(x / 5))


def _part(day: int) -> str:
    return "early" if day <= 10 else "mid" if day <= 20 else "late"


def when_text(start, end) -> str:
    """A readable window: "Mar 2023", "late Feb – early Mar 2023", "late Dec 2023 – early Jan 2024"."""
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%b %Y}"
    if a.year != b.year:
        return f"{_part(a.day)} {a:%b %Y} – {_part(b.day)} {b:%b %Y}"
    return f"{_part(a.day)} {a:%b} – {_part(b.day)} {b:%b %Y}"
