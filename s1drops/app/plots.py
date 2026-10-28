"""Plotly figure construction for the timeseries panel.

`simple_figure` (the default) is for readers new to radar: all tracks pooled on
one "% of usual level" scale, a trend line, a band for the normal ups and downs,
and each detected event shaded and labelled in words.

`figure` (Advanced) is the raw view: one dB trace per (pol x relative-orbit)
series; detected drops drawn as shaded [date_before, date_after] spans; PELT
segment levels drawn as horizontal step lines so the user sees the level shift
the detector found.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from ..analysis.detect import DetectionResult
from . import findings

_DROP_FILL = "rgba(214, 39, 40, 0.18)"
_SEG_LINE = {False: "rgba(0,0,0,0.55)", True: "rgba(255,255,255,0.6)"}
_RISE_FILL = "rgba(31, 119, 180, 0.18)"
_BAND_FILL = {False: "rgba(0,0,0,0.07)", True: "rgba(255,255,255,0.09)"}
_DOTS = "rgba(120,120,120,0.55)"
_TREND = "#1f77b4"
_TABLE_DROP = "#d62728"


def _theme(fig: go.Figure, dark: bool) -> None:
    fig.update_layout(template="plotly_dark" if dark else "plotly_white")
    if dark:
        # Let the dark page show through instead of Plotly's own near-black panel.
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")


def _label_rows(events, dates, width: float = 0.2) -> List[int]:
    """Row for each event's label so neighbouring labels don't overprint.

    A label is taken to span `width` of the time axis; each goes in the first row
    whose previous label has ended by the event's start.
    """
    if not events:
        return []
    t = [np.datetime64(d, "D").astype("int64") for d in dates] or [0]
    lo = min(min(t), *(np.datetime64(e.start, "D").astype("int64") for e in events))
    hi = max(max(t), *(np.datetime64(e.end, "D").astype("int64") for e in events))
    span = max(hi - lo, 1)
    ends: List[float] = []
    rows = []
    for e in events:
        x = (np.datetime64(e.start, "D").astype("int64") - lo) / span
        row = next((i for i, end in enumerate(ends) if end <= x), len(ends))
        if row == len(ends):
            ends.append(0.0)
        ends[row] = x + width
        rows.append(row)
    return rows


def simple_figure(
    sig: "findings.RelativeSignal",
    events: Sequence["findings.Event"],
    *,
    min_change: float,
    title: Optional[str] = None,
    dark: bool = False,
    table_drop: Optional[dict] = None,
) -> go.Figure:
    """The plain-language chart: % of usual level, trend, normal band, labelled events.

    `table_drop` ({"date", "drop_db", "avg_drop_db" or None}) marks the drop the
    Strongest changes table reports, as a level below the usual one from its date
    on, so readers can see where it sits against the trend; with `avg_drop_db`, the
    whole-period value it replaced is drawn too.
    """
    fig = go.Figure()
    x = [pd.Timestamp(d) for d in sig.dates]
    if sig.band:
        lo, hi = sig.band
        fig.add_hrect(y0=lo, y1=hi, fillcolor=_BAND_FILL[dark], line_width=0, layer="below",
                      annotation_text="normal ups and downs", annotation_position="top left",
                      annotation_font_size=11, annotation_opacity=0.7)
    fig.add_hline(y=100, line_dash="dot", line_width=1, opacity=0.6,
                  annotation_text="usual level", annotation_position="bottom right",
                  annotation_font_size=11)
    for e, row in zip(events, _label_rows(events, sig.dates)):
        fig.add_vrect(x0=pd.Timestamp(e.start), x1=pd.Timestamp(e.end),
                      fillcolor=_DROP_FILL if e.is_drop else _RISE_FILL,
                      line_width=0, layer="below")
        fig.add_annotation(
            x=pd.Timestamp(e.start), xanchor="left", yref="paper", y=1 - 0.13 * row,
            yanchor="top", showarrow=False, align="left", font_size=11,
            text=(f"{findings.strength(e.delta_db, min_change)} "
                  f"{'drop' if e.is_drop else 'rise'}<br>{findings.change_text(e.delta_db)}"),
        )
    # Plain Python lists: the Solara widget doesn't decode Plotly's binary arrays.
    fig.add_trace(go.Scatter(
        x=x, y=[float(v) for v in sig.pct], mode="markers", name="Each satellite pass",
        marker=dict(size=5, color=_DOTS), text=sig.hover, hovertemplate="%{text}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=[float(v) for v in sig.trend], mode="lines", name="Overall trend",
        line=dict(color=_TREND, width=2.5), hoverinfo="skip",
    ))
    if table_drop and len(x):
        t0, t1 = pd.Timestamp(table_drop["date"]), max(x)
        levels = [("Drop in table", table_drop["drop_db"], "dot", 1.0)]
        if table_drop.get("avg_drop_db") is not None:
            levels.append(("Whole-period average", table_drop["avg_drop_db"], "dashdot", 0.5))
        for name, db, dash, alpha in levels:
            pct = float(findings.to_pct(-db))
            fig.add_trace(go.Scatter(
                x=[t0, t1], y=[pct, pct], mode="lines", name=f"{name} (−{db:.1f} dB)",
                line=dict(color=_TABLE_DROP, width=2, dash=dash), opacity=alpha,
                hovertemplate=f"{name}: −{db:.1f} dB = {pct:.0f}% of usual<extra></extra>",
            ))
    fig.update_layout(
        title=title, yaxis_title="Radar reflection (% of usual)",
        legend=dict(orientation="h", y=-0.2), margin=dict(l=50, r=20, t=40, b=40),
        height=420, autosize=True, hovermode="closest",
    )
    fig.update_yaxes(ticksuffix="%", rangemode="tozero")
    fig.update_xaxes(type="date", tickformat="%b %Y")
    _theme(fig, dark)
    return fig


def figure(
    results: Sequence[DetectionResult],
    *,
    title: Optional[str] = None,
    show_segments: bool = True,
    dark: bool = False,
) -> go.Figure:
    fig = go.Figure()
    for res in results:
        s = res.series
        # Plain Python lists: avoid Plotly's binary (base64) array encoding, which
        # the Solara widget doesn't decode — that silently drops numpy-array data.
        x = [pd.Timestamp(d) for d in s.dates]
        y = [float(v) for v in s.values_db]
        fig.add_trace(go.Scatter(
            x=x, y=y, mode="lines+markers",
            name=s.label, connectgaps=False,
        ))
        # shaded drop spans
        for d in res.drops:
            fig.add_vrect(
                x0=pd.Timestamp(d.date_before), x1=pd.Timestamp(d.date_after),
                fillcolor=_DROP_FILL, line_width=0, layer="below",
            )
        # PELT segment levels
        if show_segments and res.seg_bounds and res.seg_means:
            b = res.seg_bounds
            for i, lvl in enumerate(res.seg_means):
                lo, hi = b[i], b[i + 1] - 1
                if hi < lo or hi >= len(s.dates):
                    continue
                fig.add_trace(go.Scatter(
                    x=[pd.Timestamp(s.dates[lo]), pd.Timestamp(s.dates[hi])], y=[lvl, lvl],
                    mode="lines", line=dict(color=_SEG_LINE[dark], dash="dot", width=1.5),
                    showlegend=False, hoverinfo="skip",
                ))
    fig.update_layout(
        title=title, xaxis_title="date", yaxis_title="backscatter (dB)",
        legend=dict(orientation="h", y=-0.2), margin=dict(l=50, r=20, t=40, b=40),
        height=420, autosize=True,
    )
    _theme(fig, dark)
    fig.update_xaxes(type="date", tickformat="%b %Y")
    return fig


def count_traces(fig: go.Figure) -> int:
    return len(fig.data)


def count_drop_spans(fig: go.Figure) -> int:
    return sum(1 for sh in (fig.layout.shapes or []))
