"""One plain answer to "how sure are we about this change?", with its reasons.

Four things decide it, each shown as its own line so the label is never a black
box:

- strength: how far the signal dropped, against the minimum change set
  (`findings.strength`: Strong / Moderate / Slight);
- tracks: how many of the satellite tracks covering the place saw the drop
  (each sees it from its own angle, so agreement rules out angle effects);
- time since: enough passes after the drop to show it lasted
  (`logic.MIN_CONFIRM_PASSES`), or too recent to tell;
- verdicts: what validators recorded (only for viewers allowed to see them).

A verdict settles it (Confirmed / Ruled out / Other change / Disputed). Without
one, a drop too recent to confirm is "Too early"; otherwise strength and track
agreement score 0-2 each and the total gives High (3-4), Medium (2) or Low.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from . import findings, logic, validation

GOOD, OK, WEAK, NONE = "good", "ok", "weak", "none"     # factor levels

LEVELS = {   # label -> colour (reads on light and dark backgrounds)
    "High": "#2e7d32", "Medium": "#f9a825", "Low": "#9e9e9e", "Too early": "#78909c",
    "Confirmed": validation.COLOURS["demolition"], "Ruled out": validation.COLOURS["none"],
    "Other change": validation.COLOURS["other"], "Disputed": validation.COLOURS["mixed"],
}
MARKS = {GOOD: "✓", OK: "~", WEAK: "✗", NONE: "–"}


@dataclass
class Factor:
    name: str
    level: str      # GOOD / OK / WEAK / NONE
    text: str


@dataclass
class Rating:
    label: str
    factors: List[Factor] = field(default_factory=list)

    @property
    def colour(self) -> str:
        return LEVELS[self.label]

    def explain(self) -> str:
        """The breakdown as plain lines, for hover text and reports."""
        return "\n".join(f"{MARKS[f.level]} {f.name}: {f.text}" for f in self.factors)


def track_agreement(results, date_after: Optional[str],
                    window_days: int = validation.MATCH_DAYS) -> Optional[Tuple[int, int]]:
    """(tracks that saw the drop, tracks covering the place) for the detected drop
    nearest `date_after`, or (0, total) if none is within `window_days`. None when
    there is no data."""
    evts = [e for e in findings.events(results) if e.is_drop]
    total = sum(1 for r in results if len(r.series))
    if not total:
        return None
    if not date_after or not evts:
        return (0, total)
    day = np.datetime64(str(date_after)[:10], "D")
    gap = lambda e: abs((e.end - day) / np.timedelta64(1, "D"))  # noqa: E731
    near = min(evts, key=gap)
    if gap(near) > window_days:
        return (0, total)
    return (near.tracks, near.total_tracks)


def rate(c: dict, min_change: float, tracks: Optional[Tuple[int, int]] = None,
         summary: Optional["validation.Summary"] = None, show_verdicts: bool = False
         ) -> Rating:
    """Rate a listed change `c` (an area or pixel from Strongest changes)."""
    factors: List[Factor] = []

    drop = abs(float(c.get("drop_db", 0.0)))
    word = findings.strength(-drop, min_change)
    s_score = {"Strong": 2, "Moderate": 1}.get(word, 0)
    factors.append(Factor("Strength", (GOOD, OK, WEAK)[2 - s_score],
                          f"{word.lower()} drop ({findings.change_text(-drop)})"))

    if tracks is None:
        t_score = 1
        factors.append(Factor("Satellite tracks", NONE, "not checked"))
    else:
        seen, total = tracks
        if total <= 1:
            t_score, level = (1, OK) if seen else (0, WEAK)
            text = "only one track covers this place" + ("" if seen else ", and it saw "
                                                          "no clear step")
        elif seen >= total:
            t_score, level, text = 2, GOOD, f"all {total} tracks saw it"
        elif seen:
            t_score, level, text = 1, OK, f"{seen} of {total} tracks saw it"
        else:
            t_score, level, text = 0, WEAK, (f"none of the {total} tracks shows a clear "
                                             "step on their own")
        factors.append(Factor("Satellite tracks", level, text))

    n = c.get("passes_after")
    recent = bool(c.get("recent"))
    if n is None:
        factors.append(Factor("Time since", NONE, "not checked"))
    elif recent:
        factors.append(Factor("Time since", WEAK,
                              f"only {n} image{'s' if n != 1 else ''} per track since; "
                              "too recent to tell if it lasts"))
    else:
        factors.append(Factor("Time since", GOOD, f"stayed low for {n}+ images per track"))

    verdict = summary.verdict if (show_verdicts and summary) else ""
    if show_verdicts:
        if not verdict:
            factors.append(Factor("Verdicts", NONE, "not checked by a validator yet"))
        else:
            who = f"{summary.count} validator{'s' if summary.count != 1 else ''}"
            text, level = {
                "demolition": (f"demolition, according to {who}", GOOD),
                "none": (f"no real change, according to {who}", WEAK),
                "other": (f"another kind of change, according to {who}", OK),
                "unsure": (f"{who} couldn't tell", NONE),
                "mixed": (f"{who} disagree", OK),
            }[verdict]
            factors.append(Factor("Verdicts", level, text))

    label = {"demolition": "Confirmed", "none": "Ruled out", "other": "Other change",
             "mixed": "Disputed"}.get(verdict)
    if label is None:
        if recent:
            label = "Too early"
        else:
            score = s_score + t_score
            label = "High" if score >= 3 else "Medium" if score == 2 else "Low"
    return Rating(label, factors)


def tracks_for(cube, c: dict, *, pol, orbit_dir, start, end, method, params):
    """Run the detector on a listed change's own series (as the chart does) and
    return its track agreement."""
    if "cells" in c:
        series = logic.select_area_series(cube, c["cells"], pol, orbit_dir)
    else:
        series = logic.select_series(cube, c["row"], c["col"], pol, orbit_dir)
    results = logic.run_detection(series, start=start, end=end, method=method,
                                  **dict(params, direction="drop"))
    return track_agreement(results, c.get("date_after"))
