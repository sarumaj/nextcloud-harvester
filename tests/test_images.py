# SPDX-License-Identifier: AGPL-3.0-or-later
"""Images picked as file links (an <img>, a <picture>) at their full size (harvester.images): the
largest candidate of srcset and of a <picture>'s sources, an attribute naming the full size, the
lazy-loading ones over a placeholder src; read alike over HTTP and in the browser (Chromium
against the fake site, routed, no network), and so picked in the recorder."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import struct
import zlib

import pytest
from bs4 import BeautifulSoup
from test_browser import needs_chromium
from test_clicks import routed
from test_recorder_features import recording

from fake_site import SITE, Site
from harvester import images
from harvester.sources import site as site_module


def png(width: int) -> bytes:
    """A real PNG (the browser keeps only images it could decode), a width's apart from others."""
    raw = b"".join(b"\0" + b"\xff\x00\x00" * width for _ in range(2))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, 2, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# Images with placeholders, sizes and lazy loading.
PHOTOS = """<html><body>
<img class="photo" src="/img/blank.gif" data-src="/img/small-1.png" srcset="/img/small-1.png 320w, /img/large-1.png 1280w"
 alt="One">
<picture><source srcset="/img/large-2.png 2x, /img/small-2.png 1x"><img class="photo" src="/img/blank.gif" alt="Two">
</picture>
<img class="photo" src="/img/spacer.gif" data-original="/img/lazy-3.png" alt="Three">
</body></html>"""


class Photos(Site):  # pylint: disable=too-few-public-methods
    """The fake site with the photos' page, and a picture for every address under /img/."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        path = "/" + url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        self.requests.append(url)
        if path == "/photos":
            return 200, {"content-type": "text/html"}, PHOTOS.encode()
        if path.startswith("/img/"):
            # Each picture its own (a width by its name's length), so that none is another's.
            return 200, {"content-type": "image/png"}, png(len(path))
        return super().answer(url, method, cookie)


FULL_SIZE = [
    (f"{SITE}/img/large-1.png", "One"),
    (f"{SITE}/img/large-2.png", "Two"),
    (f"{SITE}/img/lazy-3.png", "Three"),
]


def test_a_srcset_is_read_as_the_standard_reads_it():
    assert images.parse_srcset("a.jpg 100w, https://cdn.example/w_200,h_100/b.jpg 200w,c.jpg") == [
        ("a.jpg", 100.0, "w"),
        ("https://cdn.example/w_200,h_100/b.jpg", 200.0, "w"),
        ("c.jpg", 1.0, "x"),
    ]
    assert images.parse_srcset(" s.jpg 1x , l.jpg 2.5x ") == [("s.jpg", 1.0, "x"), ("l.jpg", 2.5, "x")]
    assert not images.parse_srcset("  ,  ")


@pytest.mark.parametrize(
    "img, sources, expected",
    [
        # The largest width of srcset and of the <picture>'s sources.
        ({"src": "/s.jpg", "srcset": "/s.jpg 320w, /l.jpg 1280w"}, [{"srcset": "/xl.webp 1920w"}], "/xl.webp"),
        # Densities only: the highest.
        ({"src": "/s.jpg", "srcset": "/s.jpg 1x, /l.jpg 2x"}, [], "/l.jpg"),
        # A placeholder in src: the lazy-loading attribute.
        ({"src": "data:image/gif;base64,R0lGOD", "data-src": "/real.jpg"}, [], "/real.jpg"),
        ({"src": "/img/blank.gif", "data-original": "/real.jpg"}, [], "/real.jpg"),
        ({"src": "/1x1.png", "data-srcset": "/a.jpg 400w, /b.jpg 800w"}, [], "/b.jpg"),
        # An attribute naming the full size comes first.
        ({"src": "/s.jpg", "srcset": "/l.jpg 2x", "data-zoom-image": "/zoom.jpg"}, [], "/zoom.jpg"),
        # A plain image is its src; a placeholder alone is none; an image named lazy-3 is no placeholder.
        ({"src": "photo.jpg"}, [], "/p/photo.jpg"),
        ({"src": "/spacer.gif"}, [], ""),
        ({"src": "/product-placeholder.png"}, [], ""),
        ({"src": "/img/lazy-3.png"}, [], "/img/lazy-3.png"),
    ],
)
def test_the_largest_candidate_is_taken_over_a_placeholder(img, sources, expected):
    found = images.best_image({"img": img, "sources": sources}, f"{SITE}/p/")
    assert found == (f"{SITE}{expected}" if expected else "")


def test_images_over_http_are_read_as_in_the_browser():
    soup = BeautifulSoup(
        '<picture><source srcset="/w.webp 900w" type="image/webp"><img class="a b" src="/blank.gif" alt="Cat">'
        '</picture><p id="x">no image</p>',
        "html.parser",
    )
    info = images.soup_info(soup.picture)
    assert (
        info is not None
        and info["img"]["class"] == "a b"
        and info["sources"] == [{"srcset": "/w.webp 900w", "type": "image/webp"}]
    )
    assert images.soup_info(soup.img) == info, "an <img> in a <picture> has its sources"
    assert images.best_image(info, SITE) == f"{SITE}/w.webp" and images.alt(info) == "Cat"
    assert images.soup_info(soup.p) is None
    assert images.link("", "", info | {"base": SITE}) == (f"{SITE}/w.webp", "Cat")
    assert images.link(f"{SITE}/a", "A", None) == (f"{SITE}/a", "A")


@needs_chromium
@pytest.mark.parametrize("browser", [1, 0])
def test_images_picked_as_file_links_are_taken_at_their_full_size(monkeypatch, browser):
    recipe = {"start": f"{SITE}/photos", "links": {"selector": "img.photo", "pattern": r"\.png$"}}
    routed(Photos(), monkeypatch)
    client = site_module.download.guarded_client()
    made = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0, "browser": browser}, {}, client)
    try:
        items = list(made.items(lambda key: False))
        assert isinstance(made.driver, site_module.BrowserDriver if browser else site_module.HttpDriver)
    finally:
        made.close()
    assert [(item.url, item.values["text"]) for item in items] == FULL_SIZE


@needs_chromium
def test_images_are_picked_in_the_recorder_at_their_full_size():
    async def scenario():
        driver, recorder = await recording(Photos())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/photos"})
            await driver.mode("links")
            await driver.click("img.photo >> nth=0")
            return await driver.until(lambda m: m["recipe"]["links"].get("selector"))
        finally:
            await recorder.close()

    picked = asyncio.run(scenario())
    links, proposal = picked["recipe"]["links"], picked["proposals"]["links"]
    assert "click" not in links, "an image has an address: its own"
    assert [(example["url"], example["text"]) for example in proposal["examples"]] == FULL_SIZE
    assert proposal["matching"] == 3
