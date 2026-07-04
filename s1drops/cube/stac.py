"""STAC search against the Planetary Computer sentinel-1-rtc collection.

Signing is done at *read* time (``patch_url=planetary_computer.sign`` in the
loader), not at search time. SAS tokens are short-lived (~1 h), so signing every
asset when the search returns them means (a) a long bake can outlive its tokens
and hit 403s, and (b) a full-archive search signs hundreds of items in one burst,
which can trip the anonymous SAS rate limit (also a 403). Read-time signing mints
a token only when an asset is actually read, and ``planetary_computer.sign``
caches the token per storage container, so the whole bake reuses one cached token
and makes very few SAS-API calls.

Note: personal PC subscription keys are no longer issued (the Hub and developer
portal were retired); the free anonymous access used here needs no key.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import pystac_client

CATALOG_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-1-rtc"
DEFAULT_BANDS: Tuple[str, ...] = ("vv", "vh")

Bbox = Tuple[float, float, float, float]


@dataclass
class ItemMeta:
    item: object
    datetime: Optional[str]
    orbit_state: str
    relative_orbit: int
    pols: Tuple[str, ...]


def open_catalog(url: str = CATALOG_URL) -> pystac_client.Client:
    return pystac_client.Client.open(url)


def search_items(
    bbox_ll: Bbox,
    start: str,
    end: str,
    *,
    collection: str = COLLECTION,
    bands: Sequence[str] = DEFAULT_BANDS,
    catalog: Optional[pystac_client.Client] = None,
) -> List[ItemMeta]:
    cat = catalog or open_catalog()
    search = cat.search(
        collections=[collection],
        bbox=list(bbox_ll),
        datetime=f"{start}/{end}",
    )
    metas: List[ItemMeta] = []
    for it in search.items():
        p = it.properties
        metas.append(
            ItemMeta(
                item=it,
                datetime=p.get("datetime"),
                orbit_state=p.get("sat:orbit_state", "unknown"),
                relative_orbit=int(p.get("sat:relative_orbit", -1)),
                pols=tuple(b for b in bands if b in it.assets),
            )
        )
    return metas
