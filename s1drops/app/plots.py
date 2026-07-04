"""Plotly figure construction for the timeseries panel.

One dB trace per (pol x relative-orbit) series; detected drops drawn as shaded
[date_before, date_after] spans; PELT segment levels drawn as horizontal step
lines so the user sees the level shift the detector found.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from ..analysis.detect import DetectionResult

_DROP_FILL = "rgba(214, 39, 40, 0.18)"
_SEG_LINE = "rgba(0,0,0,0.55)"


def figure(
    results: Sequence[DetectionResult],
    *,
    title: Optional[str] = None,
    show_segments: bool = True,
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
                    mode="lines", line=dict(color=_SEG_LINE, dash="dot", width=1.5),
                    showlegend=False, hoverinfo="skip",
                ))
    fig.update_layout(
        title=title, xaxis_title="date", yaxis_title="backscatter (dB)",
        legend=dict(orientation="h", y=-0.2), margin=dict(l=50, r=20, t=40, b=40),
        template="plotly_white", height=420,
    )
    fig.update_xaxes(type="date", tickformat="%b %Y")
    return fig


def count_traces(fig: go.Figure) -> int:
    return len(fig.data)


def count_drop_spans(fig: go.Figure) -> int:
    return sum(1 for sh in (fig.layout.shapes or []))
