# SPDX-License-Identifier: AGPL-3.0-or-later
"""The website job's Light browser (harvester.sources.site: LIGHT, light_guard) in Chromium against
pages served by the routes (no network): images, fonts and videos never loaded, the links of
pictures found all the same; and when the option is ignored (a disguised browser, pages kept)."""

# pylint: disable=missing-function-docstring,protected-access

import json

from test_browser import FREE, needs_chromium

from harvester.sources import site as site_module

SITE = "https://light.example"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
GALLERY = """<html><head><style>
@font-face { font-family: Body; src: url('/font.woff2') }
body { font-family: Body; background: url('/back.png') }
</style></head><body>
<a class="photo" href="/photos/1"><img src="/thumbs/1.png" srcset="/full/1.png 2000w" alt="One"></a>
<img class="pic" src="/thumbs/2.png" data-src="/full/2.png" alt="Two">
<video src="/clip.mp4" autoplay muted></video>
<script src="/app.js"></script>
</body></html>"""


def walked(monkeypatch, **options) -> tuple[list[str], list[str]]:
    """The file names a walk of the gallery finds, and the resource types its browser asked for."""
    asked: list[str] = []

    def fulfill(route) -> None:
        asked.append(route.request.resource_type)
        path = route.request.url.removeprefix(SITE)
        if path == "/":
            route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=GALLERY.encode())
        elif path == "/app.js":
            route.fulfill(status=200, headers={"Content-Type": "text/javascript"}, body=b"window.ran = 1")
        elif path.endswith(".png"):
            route.fulfill(status=200, headers={"Content-Type": "image/png"}, body=PNG)
        else:
            route.fulfill(status=404, body=b"")

    monkeypatch.setattr(site_module, "guard", fulfill)
    recipe = {"start": f"{SITE}/", "links": {"selector": "img.pic, a.photo img", "pattern": r"\.png$"}}
    source = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0, "browser": 1, **options}, {}, None)
    source.sleep = lambda seconds: None
    try:
        return sorted(item.path for item in source.items(lambda key: False)), asked
    finally:
        source.close()


@needs_chromium
def test_a_light_browser_loads_no_images_fonts_or_videos_and_finds_the_pictures_all_the_same(monkeypatch):
    full, loaded = walked(monkeypatch, light=0)
    light, skipped = walked(monkeypatch, light=1)
    assert full == light == ["1.png", "2.png"], "the pictures at their full size, by their attributes"
    assert {"image", "font"} <= set(loaded)
    assert not set(skipped) & site_module.LIGHT, skipped
    assert {"document", "script"} <= set(skipped)
    assert site_module.browser.free_browsers() == FREE


def test_light_is_on_for_new_jobs_off_for_those_before_and_ignored_where_it_would_change_the_result(monkeypatch):
    lights: list = []

    class Driver:  # pylint: disable=too-few-public-methods
        """BrowserDriver: its light flag is noted."""

        skipped: list = []
        repaired: list = []

        def __init__(self, *_args, light=False, **_kwargs):
            lights.append(light)

    monkeypatch.setattr(site_module, "BrowserDriver", Driver)
    recipe = {"start": "https://site.example/", "links": {"selector": "a", "pattern": "x"}}
    for options, more in (
        ({"light": 1}, {}),
        ({}, {}),
        ({"light": 1, "stealth": 1}, {}),
        ({"light": 1}, {"save": "pdf"}),
        ({"light": 1}, {"capture": ["application/pdf"]}),
    ):
        links = recipe["links"] | more
        made = site_module.Site({"recipe": json.dumps(recipe | {"links": links}), "browser": 1, **options}, {}, None)
        made._driver()
    assert lights == [True, False, False, False, False]
    assert site_module.Site.options["light"].default == 1 and site_module.Site.options["light"].before == 0
