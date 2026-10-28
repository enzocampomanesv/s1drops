"""One-page PDF "case sheet" for a change: the facts, how sure we are and why,
where it is, the radar signal over time and (if fetched) before/after photos.

Made for sharing outside the app (partners, legal teams, press), so it holds no
validator names and never the admins' private reference data: verdicts appear
only as counts and evidence types, and only when the person making the report
may see them. Drawn with matplotlib (already a dependency), no extra packages.
"""
from __future__ import annotations

import io
import textwrap
from datetime import date
from typing import Dict, List, Optional, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from . import confidence, findings, validation  # noqa: E402

A4 = (8.27, 11.69)
INK, MUTED, DROP, TREND = "#222222", "#666666", "#d62728", "#1f77b4"
MARK_COLOUR = {confidence.GOOD: "#2e7d32", confidence.OK: "#f9a825",
               confidence.WEAK: "#c62828", confidence.NONE: "#9e9e9e"}

DISCLAIMER = (
    "How to read this: the Sentinel-1 radar satellites measure how strongly the ground "
    "reflects their signal. Buildings reflect strongly; when they are removed the signal "
    "drops and stays low. A lasting drop is a lead to check, not proof of an eviction: "
    "construction, flooding or vegetation can look similar. Confidence combines the "
    "drop's strength, agreement between satellite tracks, how long the drop has lasted "
    "and, where available, validators' verdicts.")


def build(*, place: str, area_name: str, c: Dict, rating: "confidence.Rating",
          events: Sequence["findings.Event"], signal: "findings.RelativeSignal",
          min_change: float, cube, mag: np.ndarray, resolution: float,
          land_use: str = "", link: str = "", photos: Optional[Dict] = None,
          verdicts: Sequence["validation.Record"] = (), fmt: str = "pdf") -> bytes:
    """The PDF (or, with fmt="png", a picture of the page) as bytes. `c` is the listed change (area or pixel), `mag` the drop
    layer behind the map, `photos` the before/after result from the photo panel."""
    fig = plt.figure(figsize=A4)
    fig.patch.set_facecolor("white")
    ev = _event_for(events, c.get("date_after"))

    # --- header -------------------------------------------------------------
    fig.text(0.06, 0.955, f"Detected change near {place}" if place else "Detected change",
             fontsize=15, weight="bold", color=INK, va="top")
    fig.text(0.06, 0.928, f"{area_name} · report made {date.today():%d %b %Y}",
             fontsize=9, color=MUTED, va="top")

    # --- facts (left) ---------------------------------------------------------
    lines: List[tuple] = []
    when = findings.when_text(ev.start, ev.end) if ev else _month(c.get("date_after"))
    lines.append(("When", when))
    drop = abs(float(c.get("drop_db", 0.0)))
    lines.append(("Change", f"{findings.strength(-drop, min_change)} drop · "
                            f"{findings.change_text(-drop)} (−{drop:.1f} dB)"))
    if "area_ha" in c:
        lines.append(("Area", f"{c['area_ha']:g} ha ({c['n_cells']} pixels of "
                              f"{resolution:g} m)"))
    if land_use:
        lines.append(("Land use", land_use))
    lines.append(("Location", f"{c['lat']:.5f}, {c['lon']:.5f}"))
    y = 0.885
    for k, v in lines:
        fig.text(0.06, y, k, fontsize=9, color=MUTED, va="top")
        fig.text(0.19, y, v, fontsize=9.5, color=INK, va="top")
        y -= 0.024

    y -= 0.008
    fig.text(0.06, y, "Confidence", fontsize=9, color=MUTED, va="top")
    fig.text(0.19, y, f" {rating.label} ", fontsize=10, weight="bold", color="white",
             va="top", bbox=dict(boxstyle="round,pad=0.25", fc=rating.colour, ec="none"))
    y -= 0.03
    for f in rating.factors:
        fig.text(0.19, y, confidence.MARKS[f.level], fontsize=10, weight="bold",
                 color=MARK_COLOUR[f.level], va="top")
        body = textwrap.fill(f"{f.name}: {f.text}", 52)
        fig.text(0.215, y, body, fontsize=8.5, color=INK, va="top")
        y -= 0.02 * (body.count("\n") + 1)

    if verdicts:
        y -= 0.008
        fig.text(0.06, y, "Verdicts", fontsize=9, color=MUTED, va="top")
        for line in _verdict_lines(verdicts):
            body = textwrap.fill(line, 54)
            fig.text(0.19, y, body, fontsize=8.5, color=INK, va="top")
            y -= 0.02 * (body.count("\n") + 1)

    # --- map (right) ------------------------------------------------------------
    ax_map = fig.add_axes([0.62, 0.66, 0.33, 0.25])
    _drop_map(ax_map, cube, mag, c, resolution)

    # --- signal over time ---------------------------------------------------------
    ax = fig.add_axes([0.09, 0.39, 0.86, 0.2])
    _signal_chart(ax, signal, events, c, min_change)

    # --- photos -------------------------------------------------------------------
    if photos:
        _photos(fig, photos)
    else:
        fig.text(0.06, 0.29, "Satellite photos were not fetched for this report "
                             "(use “Get photos” in the app first to include them).",
                 fontsize=8.5, color=MUTED, va="top", style="italic")

    # --- footer -------------------------------------------------------------------
    foot = textwrap.fill(DISCLAIMER, 125)
    fig.text(0.06, 0.085, foot, fontsize=7, color=MUTED, va="top")
    credits = (f"Contains modified Copernicus Sentinel data {date.today().year}. "
               "Place names © OpenStreetMap contributors.")
    if link:
        credits = f"Open this change in the app: {link}\n" + credits
    fig.text(0.06, 0.035, credits, fontsize=7, color=MUTED, va="top")

    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=110 if fmt == "png" else None)
    plt.close(fig)
    return buf.getvalue()


def _event_for(events, date_after):
    drops = [e for e in events if e.is_drop]
    if not drops:
        return None
    if not date_after:
        return min(drops, key=lambda e: e.delta_db)
    day = np.datetime64(str(date_after)[:10], "D")
    return min(drops, key=lambda e: abs((e.end - day) / np.timedelta64(1, "D")))


def _month(d) -> str:
    return f"{pd.Timestamp(d):%b %Y}" if d else "–"


def _verdict_lines(verdicts) -> List[str]:
    """Counts per verdict and the evidence used; no names."""
    counting = [r for r in verdicts if r.source == "user" and r.counts]
    if not counting:
        return ["None recorded yet."]
    out = []
    for key, label in validation.VERDICTS.items():
        rs = [r for r in counting if r.verdict == key]
        if rs:
            ev = sorted({validation.EVIDENCE[e] for r in rs for e in r.evidence})
            out.append(f"{label}: {len(rs)}" + (f" (evidence: {', '.join(ev)})" if ev else ""))
    return out


def _drop_map(ax, cube, mag, c, resolution):
    """The drop layer around the change, with its cells outlined."""
    cells = c.get("cells") or [(c["row"], c["col"])]
    rows = [r for r, _ in cells]; cols = [k for _, k in cells]
    pad = max(6, (max(rows) - min(rows) + max(cols) - min(cols)) // 2 + 4)
    r0, r1 = max(min(rows) - pad, 0), min(max(rows) + pad + 1, mag.shape[0])
    c0, c1 = max(min(cols) - pad, 0), min(max(cols) + pad + 1, mag.shape[1])
    crop = mag[r0:r1, c0:c1]
    vmax = float(np.nanpercentile(mag, 98)) if np.isfinite(mag).any() else 1.0
    ax.imshow(crop, cmap="YlOrRd", vmin=0, vmax=vmax, interpolation="nearest")
    # Outline only the area's edge: a cell side is drawn where its neighbour on that
    # side isn't part of the area.
    inside = set(map(tuple, cells))
    for r, k in inside:
        x, y = k - c0 - 0.5, r - r0 - 0.5
        for (dr, dk), seg in (((-1, 0), ([x, x + 1], [y, y])),
                              ((1, 0), ([x, x + 1], [y + 1, y + 1])),
                              ((0, -1), ([x, x], [y, y + 1])),
                              ((0, 1), ([x + 1, x + 1], [y, y + 1]))):
            if (r + dr, k + dk) not in inside:   # bright line on a dark edge: shows on any red
                ax.plot(*seg, color="black", lw=3.2, solid_capstyle="round")
                ax.plot(*seg, color="#00e5ff", lw=1.6, solid_capstyle="round")
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_facecolor("#f2f2f2")
    ax.set_title("Drop in radar signal (outlined: this change)", fontsize=8, color=INK)
    width_m = (c1 - c0) * resolution
    ax.set_xlabel(f"about {width_m / 1000:.1f} km across · north up", fontsize=7, color=MUTED)


def _signal_chart(ax, sig, events, c, min_change):
    x = [pd.Timestamp(d) for d in sig.dates]
    if sig.band:
        ax.axhspan(*sig.band, color="#9e9e9e", alpha=0.18, lw=0)
        ax.text(0.005, 0.97, "normal ups and downs", transform=ax.transAxes, fontsize=7,
                color=MUTED, va="top")
    ax.axhline(100, ls=":", lw=0.8, color=MUTED)
    for e in events:
        if e.is_drop:
            ax.axvspan(pd.Timestamp(e.start), pd.Timestamp(e.end), color=DROP, alpha=0.15, lw=0)
    ax.plot(x, sig.pct, ".", ms=3, color="#888888", label="Each satellite pass")
    ax.plot(x, sig.trend, "-", lw=1.8, color=TREND, label="Overall trend")
    if c.get("date_after") and len(x):
        level = float(findings.to_pct(-abs(float(c["drop_db"]))))
        ax.plot([pd.Timestamp(c["date_after"]), max(x)], [level, level], ":", lw=1.6,
                color=DROP, label=f"Drop in table (−{abs(float(c['drop_db'])):.1f} dB)")
    # Leave out the odd extreme pass (a ship, a wet roof) so the trend stays readable.
    pct = np.asarray(sig.pct, dtype="float64")
    top = max(150.0, float(np.nanpercentile(pct, 98)) * 1.15) if len(pct) else 150.0
    ax.set_ylim(0, top)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_ylabel("Radar reflection\n(% of usual)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.set_title("Radar signal over time", fontsize=9, color=INK, loc="left")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b %Y"))
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.12), frameon=False,
              ncol=3)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def _photos(fig, photos):
    for i, side in enumerate(("before", "after")):
        cands = photos[side].get("candidates", [])
        ax = fig.add_axes([0.06 + i * 0.46, 0.12, 0.42, 0.22])
        ax.set_xticks([]); ax.set_yticks([])
        if not cands:
            ax.text(0.5, 0.5, f"No clear {side} photo found", ha="center", va="center",
                    fontsize=8, color=MUTED, transform=ax.transAxes)
            continue
        cand = cands[photos.get(f"{side}_idx", 0) if photos.get(f"{side}_idx", 0) < len(cands)
                     else 0]
        ax.imshow(np.asarray(cand["rgb"]))
        ax.set_title(f"{side.capitalize()}: Sentinel-2, {cand['date']} "
                     f"({cand['cloud'] * 100:.0f}% cloud)", fontsize=8, color=INK)
