"""Shared ipyleaflet map helpers."""
from __future__ import annotations

import ipyleaflet

# Forward-geocoding endpoint; the query runs from the user's browser, not the server.
NOMINATIM = "https://nominatim.openstreetmap.org/search?format=json&q={s}"


def add_search(m, *, zoom: int = 13):
    """Add an OSM place-search box to an ipyleaflet Map (recenters on result)."""
    search = ipyleaflet.SearchControl(
        position="topleft",
        url=NOMINATIM,
        zoom=zoom,
        property_name="display_name",
        marker=ipyleaflet.Marker(draggable=False),
    )
    m.add(search)
    return search
