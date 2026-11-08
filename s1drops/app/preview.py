"""Link previews: what WhatsApp, LinkedIn, X and others show for a shared link.

Their crawlers read the page's HTML without running it, so the Open Graph tags
are written into the HTML on its way out (`PreviewTags`, ASGI middleware). A
shared change link (?area=…&at=…, see `share`) gets its own title naming the
place; everything else gets the site's. The image is a fixed 1200x630 card
(`static/preview.png`, made by `make_card`).
"""
from __future__ import annotations

import html
import os
from pathlib import Path

from starlette.responses import FileResponse
from starlette.routing import Route

from . import places, share

IMAGE = Path(__file__).parent / "static" / "preview.png"
TITLE = "Rapid change detection tool"
DESCRIPTION = ("Satellite radar flags places where buildings disappeared, so communities "
               "and partners can check, document and respond to evictions.")


def _tags(path: str, query: str, base: str) -> str:
    title, desc = TITLE, DESCRIPTION
    link = share.parse(query)
    if link and "lat" in link:
        cache_dir = os.environ.get("S1DROPS_CACHE_DIR", "./cache")
        place = places.cached(cache_dir, link["lat"], link["lon"])
        title = f"Detected change near {place}" if place else "Detected change"
        title = title.replace("near near ", "near ")
        desc = ("A lasting drop in the satellite radar signal, a possible sign that "
                "buildings were removed. Open it to see the signal over time, satellite "
                "photos and what validators found.")
    url = base + path + (f"?{query}" if query else "")
    meta = {"og:type": "website", "og:site_name": TITLE, "og:title": title,
            "og:description": desc, "og:url": url, "og:image": f"{base}/preview.png",
            "og:image:width": "1200", "og:image:height": "630"}
    out = [f'<meta property="{k}" content="{html.escape(v)}">' for k, v in meta.items()]
    out += [f'<meta name="description" content="{html.escape(desc)}">',
            '<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:title" content="{html.escape(title)}">',
            f'<meta name="twitter:description" content="{html.escape(desc)}">',
            f'<meta name="twitter:image" content="{base}/preview.png">']
    return "\n".join(out)


class PreviewTags:
    """Adds the preview tags to the app's page (GET /, HTML only)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET" or scope["path"] != "/":
            return await self.app(scope, receive, send)
        start, chunks = {}, []

        async def capture(msg):
            if msg["type"] == "http.response.start":
                start.update(msg)
                return
            if msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                body = b"".join(chunks)
                headers = list(start.get("headers", []))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if b"text/html" in ctype and b"</head>" in body:
                    hdrs = dict((k.decode().lower(), v.decode()) for k, v in scope["headers"])
                    base = os.environ.get("S1DROPS_PUBLIC_URL", "").rstrip("/") or (
                        f"{scope.get('scheme', 'https')}://{hdrs.get('host', '')}")
                    tags = _tags("/", scope.get("query_string", b"").decode(), base)
                    body = body.replace(b"</head>", tags.encode() + b"\n</head>", 1)
                    headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                    headers.append((b"content-length", str(len(body)).encode()))
                start["headers"] = headers
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return
            await send(msg)

        await self.app(scope, receive, capture)


async def _image(request):
    return FileResponse(IMAGE, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


routes = [Route("/preview.png", _image)]


def make_card(cube_path: str, out: Path = IMAGE) -> None:
    """Draw the preview card from a cube's drop map (run once; the PNG is committed)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    from ..cube import read_cube
    from . import logic

    cube = read_cube(cube_path)
    mag = logic.hotspot_layer(cube, pol="vv", direction="both")
    fig = plt.figure(figsize=(12, 6.3), dpi=100)
    fig.patch.set_facecolor("#121212")
    ax = fig.add_axes([0.52, 0.0, 0.48, 1.0])
    ax.set_facecolor("#1e1e1e")
    vmax = float(np.nanpercentile(mag, 98))
    ax.imshow(np.where(mag >= 2.5, mag, np.nan), cmap="YlOrRd", vmin=0, vmax=vmax,
              interpolation="nearest", aspect="auto")
    ax.set_xticks([]); ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    fig.text(0.05, 0.72, "Rapid change\ndetection tool", color="white", fontsize=40,
             weight="bold", va="top", linespacing=1.1)
    fig.text(0.05, 0.36, "Satellite radar flags places where\nbuildings disappeared, so "
                         "communities\nand partners can check and document\nevictions.",
             color="#cccccc", fontsize=17, va="top", linespacing=1.4)
    fig.text(0.05, 0.07, "Sentinel-1 radar · Sentinel-2 photos · community validation",
             color="#8ab4f8", fontsize=13)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
