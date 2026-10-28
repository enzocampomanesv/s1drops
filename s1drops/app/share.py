"""Links that open the app on a given area, period and change.

A link carries only what's needed to rebuild the view, never verdicts or names:

    /?area=<cube key>&at=<lat>,<lon>[&from=YYYY-MM-DD&to=YYYY-MM-DD]

`at` is the change's location: on opening, the app selects whatever is there with
the reader's own settings, so a link keeps working when the list is re-ranked.
"""
from __future__ import annotations

from typing import Dict, Optional
from urllib.parse import parse_qs, urlencode

import ipyvuetify as vy
import numpy as np
import traitlets


def query(area: str, c: Dict, start: Optional[str], end: Optional[str], dates) -> str:
    """The query string for change (or spot) `c` in `area`; the period is only
    included when it isn't the whole record."""
    q = {"area": area, "at": f"{c['lat']:.5f},{c['lon']:.5f}"}
    if len(dates) and (start, end) != (str(dates[0]), str(dates[-1])):
        q.update({"from": start, "to": end})
    return urlencode(q)


def parse(search: Optional[str]) -> Optional[Dict]:
    """{"area", "lat", "lon", "from", "to"} from a query string, or None."""
    q = {k: v[0] for k, v in parse_qs(search or "").items()}
    if "area" not in q:
        return None
    out: Dict = {"area": q["area"], "from": q.get("from"), "to": q.get("to")}
    try:
        lat, lon = (float(v) for v in q.get("at", "").split(","))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            out.update(lat=lat, lon=lon)
    except ValueError:
        pass
    return out


def date_range(dates, start: Optional[str], end: Optional[str]):
    """Indices into `dates` covering [start, end] (nearest passes), or None."""
    if not len(dates) or not (start or end):
        return None
    d = np.asarray(dates).astype("datetime64[D]")
    try:
        lo = int(np.searchsorted(d, np.datetime64(start, "D"))) if start else 0
        hi = int(np.searchsorted(d, np.datetime64(end, "D"), side="right")) - 1 if end \
            else len(d) - 1
    except ValueError:
        return None
    lo, hi = max(0, min(lo, len(d) - 1)), max(0, min(hi, len(d) - 1))
    return (lo, hi) if lo <= hi else None


_COPY_JS = r"""
<template>
  <v-btn small text style="text-transform:none;letter-spacing:0" @click="copy">
    <v-icon small left>{{ done ? 'mdi-check' : 'mdi-link-variant' }}</v-icon>
    {{ done ? 'Link copied' : 'Copy link' }}
  </v-btn>
</template>
<script>
module.exports = {
  data() { return { done: false }; },
  methods: {
    copy() {
      const url = location.origin + location.pathname + '?' + this.query;
      const ok = () => { this.done = true; setTimeout(() => { this.done = false; }, 2000); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(url).then(ok, () => window.prompt('Copy this link:', url));
      } else {
        window.prompt('Copy this link:', url);
      }
    },
  },
};
</script>
"""


class CopyLink(vy.VuetifyTemplate):
    """A "Copy link" button: copies this page's address with `query` attached."""
    template = traitlets.Unicode(_COPY_JS).tag(sync=True)
    query = traitlets.Unicode("").tag(sync=True)
