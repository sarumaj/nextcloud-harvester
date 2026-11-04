# SPDX-License-Identifier: AGPL-3.0-or-later
"""Values of the pages a walk opens (harvester.pageinfo): the built-in ones (title, site, date,
language from <meta>, JSON-LD, <time>), a level's page_values (single, and many: tag chips) and
values of many elements beside the links; the recipe's checks, the walk over HTTP (httpx's
MockTransport) and in Chromium (route fulfilment). No network."""

# pylint: disable=missing-function-docstring

import json

import httpx
from bs4 import BeautifulSoup
from test_browser import FREE, needs_chromium
from test_page_values import walked

from harvester import pageinfo, patterns
from harvester.recipe import check_recipe
from harvester.sources import site as site_module
from harvester.sources import survey
from harvester.sources.base import pattern_search

GALLERY = "https://pics.example"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 2000
# The listing: a card per post, its tag chips beside its link.
LISTING = """<html lang="en"><head><title>Latest - Pics</title></head><body>
<div class="card"><a class="post" href="/post/1">Sunset</a>
  <ul class="chips"><li><a>#sky</a></li><li><a>sea (12)</a></li></ul></div>
<div class="card"><a class="post" href="/post/2">Dune</a><ul class="chips"><li><a>sand 1.2k</a></li></ul></div>
</body></html>"""
# A post's page: its title with the site's name, its date in JSON-LD, its tags with counts.
POST = """<html lang="de-DE"><head><title>{title} | Pics</title>
<meta property="og:site_name" content="Pics">
<script type="application/ld+json">{{"@graph": [{{"@type": "WebPage"}},
  {{"@type": "ImageObject", "datePublished": "2026-0{number}-14T10:00:00Z"}}]}}</script></head><body>
<p class="by">Anna Example</p>
<ul class="tags"><li><a href="/t/sky">sky</a> <span>123</span></li><li><a href="/t/sea">#sea</a></li>
  <li><a href="/t/x">sky (4)</a></li><li><a href="/t/2026">2026</a></li></ul>
<img class="full" src="/img/{number}.jpg"></body></html>"""
RECIPE = {
    "start": f"{GALLERY}/latest",
    "follow": [
        {
            "selector": "a.post",
            "pattern": r"/post/(?P<post>\d+)$",
            "page_values": [
                {"name": "tags", "selector": "ul.tags li", "many": True},
                {"name": "author", "selector": "p.by"},
            ],
        }
    ],
    "links": {"selector": "img.full", "pattern": r"/img/\d+\.jpg$"},
}


def answer(url: str) -> tuple[int, dict, bytes]:
    path = httpx.URL(url).path
    if path == "/latest":
        return 200, {"content-type": "text/html; charset=utf-8"}, LISTING.encode()
    if path.startswith("/post/"):
        number = path.rsplit("/", 1)[1]
        title = {"1": "Sunset", "2": "Dune"}[number]
        return 200, {"content-type": "text/html; charset=utf-8"}, POST.format(title=title, number=number).encode()
    if path.startswith("/img/"):
        return 200, {"content-type": "image/jpeg"}, JPEG
    return 404, {"content-type": "text/plain"}, b"not found"


def http_site(recipe: dict, **options) -> site_module.Site:
    def handle(request: httpx.Request) -> httpx.Response:
        status, headers, body = answer(str(request.url))
        return httpx.Response(status, headers=headers, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    return site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0, **options}, {}, client)


def raw(html: str, specs: list | None = None) -> dict:
    return pageinfo.soup_info(BeautifulSoup(html, "html.parser"), specs or [])


def test_the_built_in_values_from_meta_json_ld_and_time():
    page = """<html lang="pl"><head><title>Ignored</title><meta property="og:title" content="Zachód słońca – Pics">
    <meta property="og:site_name" content="Pics"><meta property="article:published_time" content="2026-11-02T08:00">
    </head><body><time datetime="2020-01-01">old</time></body></html>"""
    values = pageinfo.built_in(raw(page), "https://pics.example/p/1")
    assert values == {
        "page_title": "Zachód słońca",
        "site_name": "Pics",
        "page_date": "2026-11-02",
        "page_year": 2026,
        "page_month": 11,
        "page_day": 2,
        "page_lang": "pl",
    }
    # application-name names the site; a title's end that is not the site's name stays.
    named = '<title>Report - 2026 - Shop</title><meta name="application-name" content="shop">'
    assert pageinfo.built_in(raw(named), "https://shop.example/")["page_title"] == "Report - 2026"
    # Else the host; a JSON-LD date in a graph; the first <time datetime> last.
    bare = '<title>Post | Other</title><script type="application/ld+json">[{"datePublished": "2025-03-04"}]</script>'
    values = pageinfo.built_in(raw(bare), "https://www.pics.example/post")
    assert values["site_name"] == "www.pics.example" and values["page_title"] == "Post Other"
    assert values["page_date"] == "2025-03-04" and "page_lang" not in values
    timed = pageinfo.built_in(
        raw('<script type="application/ld+json">{broken</script><time datetime="2024-05-06">'), ""
    )
    assert timed["page_date"] == "2024-05-06" and "page_title" not in timed and "site_name" not in timed
    assert not pageinfo.values({}, "https://x.example/", [], pattern_search, {})


def test_page_values_single_and_many_are_cleaned_and_filtered():
    specs = [
        {"name": "tags", "selector": "ul.tags li", "many": True},
        {"name": "words", "selector": "ul.tags li", "many": True, "regex": r"^[a-z]+$"},
        {"name": "author", "selector": "p.by"},
        {"name": "link", "selector": "ul.tags a", "attribute": "href", "regex": r"/t/(\w+)"},
        {"name": "gone", "selector": "p.none"},
        {"name": "broken", "selector": "p[", "many": True},
    ]
    found = raw(POST.format(title="Sunset", number=1), specs)
    values = patterns.page_values(specs, found["picked"], pattern_search, {})
    # "sky 123", "#sea", "sky (4)" and "2026" (a number alone stays).
    assert values["tags"] == ["sky", "sea", "2026"]
    assert values["words"] == ["sky", "sea"]
    assert values["author"] == "Anna Example" and values["link"] == "sky"
    assert "gone" not in values and "broken" not in values
    assert patterns.clean_entry("  # blue   sky (1,234) ") == "blue sky"
    assert patterns.clean_entry("windows") == "windows" and patterns.clean_entry("mp3") == "mp3"


def test_the_recipe_checks_page_values_and_many():
    assert not check_recipe(RECIPE)
    level = RECIPE["follow"][0]

    def problems(**changes) -> list[str]:
        return check_recipe(RECIPE | {"follow": [level | changes]})

    assert problems(page_values=[{"name": "tags", "many": True}]) == [
        "level 1: the value tags has no selector of its elements"
    ]
    assert "every page gives" in problems(page_values=[{"name": "page_title", "selector": "h1"}])[0]
    assert "not dates" in problems(page_values=[{"name": "d", "selector": "p", "many": True, "type": "date"}])[0]
    assert "true or false" in problems(page_values=[{"name": "d", "selector": "p", "many": "yes"}])[0]
    assert "at most" in problems(page_values=[{"name": f"v{n}", "selector": "p"} for n in range(11)])[0]
    assert "no selector" in problems(values=[{"name": "tags", "closest": ".card", "many": True}])[0]
    files = RECIPE["links"] | {"page_values": [{"name": "x", "selector": "p"}]}
    assert "not on files" in check_recipe(RECIPE | {"links": files})[0]
    # A key may name a page value.
    assert not check_recipe(RECIPE | {"links": RECIPE["links"] | {"key": "{author}-{post}"}})


def test_a_posts_page_gives_its_values_and_tags_to_its_files_over_http():
    items = walked(http_site(RECIPE, path="{site_name}/{page_year}/{page_title} {post}{ext}"))
    assert [item.path for item in items] == ["Pics/2026/Sunset 1.jpg", "Pics/2026/Dune 2.jpg"]
    values = items[0].values
    assert values["tags"] == ["sky", "sea", "2026"] and values["author"] == "Anna Example"
    assert values["page_lang"] == "de-DE" and values["page_date"] == "2026-01-14"
    assert patterns.render_tags(["{tags}", "{site_name}"], values) == ["sky", "sea", "2026", "Pics"]


def test_tags_beside_each_link_are_a_list_of_the_cards_chips():
    recipe = {
        "start": f"{GALLERY}/latest",
        "links": {
            "selector": "a.post",
            "pattern": r"/post/(?P<post>\d+)$",
            "values": [{"name": "tags", "closest": "div.card", "selector": "ul.chips a", "many": True}],
        },
    }
    items = walked(http_site(recipe, path="{post}"))
    assert [item.values["tags"] for item in items] == [["sky", "sea"], ["sand"]]
    # The listing's own values: without a site's name, its host's (which its title's end is not).
    assert items[0].values["page_title"] == "Latest - Pics" and items[0].values["site_name"] == "pics.example"
    assert items[0].values["page_lang"] == "en"


@needs_chromium
def test_the_pages_values_are_the_same_in_the_browser(monkeypatch):
    def fulfill(route) -> None:
        status, headers, body = answer(route.request.url)
        route.fulfill(status=status, headers=headers, body=body)

    monkeypatch.setattr(site_module, "guard", fulfill)
    source = site_module.Site(
        {"recipe": json.dumps(RECIPE), "stop_at_seen": 0, "browser": 1, "path": "{page_title} {post}{ext}"}, {}, None
    )
    items = walked(source)
    assert [item.path for item in items] == ["Sunset 1.jpg", "Dune 2.jpg"]
    assert items[1].values["tags"] == ["sky", "sea", "2026"] and items[1].values["page_month"] == 2
    assert items[1].values["author"] == "Anna Example" and items[1].values["site_name"] == "Pics"
    assert site_module.browser.free_browsers() == FREE


def test_the_recipes_test_shows_each_file_links_values():
    source = http_site(RECIPE, path="{page_title}{ext}")
    try:
        result = survey.survey(source)
    finally:
        source.close()
    shown = result["pages"][-1]["files"][0]["values"]
    assert shown["tags"] == ["sky", "sea", "2026"] and shown["page_title"] == "Dune" and shown["post"] == 2
    assert not {"url", "text", "ext"} & set(shown)
