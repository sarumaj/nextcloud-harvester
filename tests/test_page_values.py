# SPDX-License-Identifier: AGPL-3.0-or-later
"""Values from the page around a link (dates in English, German and Polish forms, a row's or a
card's text, an attribute) and JSON listings (items, fields, pages, caps): the recipe's checks,
the walk over HTTP (httpx's MockTransport) and in Chromium (route fulfilment), and the recorder's
"Pick a value" and Network panel. No network."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_browser import FREE, needs_chromium
from test_recorder_features import recording, settled

from harvester import jsonlist, patterns
from harvester.recipe import check_recipe
from harvester.sources import SourceError
from harvester.sources import download
from harvester.sources import site as site_module

SHOP = "https://shop.example"
PDF = b"%PDF-1.7\n" + b"x" * 2000
# A table of invoices, the date beside each link in a form of its own (one row without one).
TABLE = """<html><body><table>
<tr class="row"><td class="date">02.11.2026</td><td><a class="file" href="/files/a.pdf">Rechnung A</a></td>
  <td><time datetime="2026-11-02T08:00">today</time></td></tr>
<tr class="row"><td class="date">Fällig: 3. Oktober 2026</td><td><a class="file" href="/files/b.pdf">Rechnung B</a></td>
  <td><time datetime="2026-10-03">a month ago</time></td></tr>
<tr class="row"><td class="date">12 września 2025</td><td><a class="file" href="/files/c.pdf">Faktura C</a></td>
  <td><time datetime="2025-09-12">last year</time></td></tr>
<tr class="row"><td class="date">—</td><td><a class="file" href="/files/d.pdf">Ohne Datum</a></td><td></td></tr>
</table></body></html>"""
# The recorder's page: alike dates (one form), for the proposal to recognise.
CARDS = """<html><body>
<div class="card"><h3>Rechnung 1</h3><span class="when">01.10.2026</span><a class="file" href="/files/1.pdf">PDF</a></div>
<div class="card"><h3>Rechnung 2</h3><span class="when">01.11.2026</span><a class="file" href="/files/2.pdf">PDF</a></div>
</body></html>"""
# A single-page app: its listing comes as JSON from /api/items, page by page.
APP = """<html><body><ul id="list"></ul><script>
fetch('/api/items?page=1&size=2').then(r => r.json()).then(answer => {
  for (const item of answer.data.items) {
    const li = document.createElement('li'); li.textContent = item.title; document.getElementById('list').append(li)
  }
})</script></body></html>"""
ITEMS = {
    1: [
        {"title": "Invoice 1", "pdf": {"href": "/files/1.pdf"}, "issued": "2026-10-01", "thumb": "/t/1.png"},
        {"title": "Invoice 2", "pdf": {"href": "/files/2.pdf"}, "issued": "2026-11-01", "thumb": "/t/2.png"},
    ],
    2: [{"title": "Invoice 3", "pdf": {"href": "https://cdn.shop.example/3.pdf"}, "issued": "2025-12-24"}],
}


class Pages:  # pylint: disable=too-few-public-methods
    """The shop: its pages, its JSON listing (pages of ITEMS, then empty ones) and its files; who
    asked for what (requests), with which cookies (cookies)."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.cookies: list[str] = []

    def answer(self, url: str, _method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        self.requests.append(url)
        self.cookies.append(cookie)
        parts = urlsplit(url)
        years = "".join(f'<a class="year" href="/years/{year}">{year}</a>' for year in (2026, 2025))
        html = {"/invoices": TABLE, "/cards": CARDS, "/app": APP, "/years": f"<html><body>{years}</body></html>"}
        if parts.path in html:
            return 200, {"content-type": "text/html; charset=utf-8"}, html[parts.path].encode()
        if parts.path.startswith("/api/"):
            page = int(parse_qs(parts.query).get("page", ["1"])[0])
            body = {"data": {"items": ITEMS.get(page, []), "total": 3}}
            return 200, {"content-type": "application/json"}, json.dumps(body).encode()
        if parts.path.startswith("/files/") or parts.hostname == "cdn.shop.example":
            return 200, {"content-type": "application/pdf"}, PDF
        return 404, {"content-type": "text/plain"}, b"not found"


def transport(pages: Pages) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        status, headers, body = pages.answer(str(request.url), request.method, request.headers.get("cookie", ""))
        return httpx.Response(status, headers=headers, content=body)

    return httpx.MockTransport(handle)


def http_site(pages: Pages, recipe: dict, **options) -> site_module.Site:
    client = httpx.Client(transport=transport(pages), follow_redirects=True)
    return site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0, **options}, {}, client)


def walked(source: site_module.Site) -> list:
    try:
        return list(source.items(lambda key: False))
    finally:
        source.close()


DATED = {
    "start": f"{SHOP}/invoices",
    "links": {
        "selector": "a.file",
        "pattern": r"/files/(?P<id>\w+)\.pdf$",
        "values": [
            {"name": "date", "closest": "tr", "selector": "td.date", "type": "date"},
            {"name": "iso", "closest": "tr.row", "selector": "time", "attribute": "datetime", "type": "date"},
            {"name": "kind", "regex": r"^(?P<kind>Rechnung|Faktura)", "type": "text"},
        ],
    },
}


def test_dates_are_read_in_english_german_and_polish():
    cases = {
        "02.11.2026": (2026, 11, 2),
        "2/11/2026": (2026, 11, 2),
        "2026-11-02T10:00:00Z": (2026, 11, 2),
        "2. November 2026": (2026, 11, 2),
        "am 3. März 2025": (2025, 3, 3),
        "3 Mrz. 2025": (2025, 3, 3),
        "2 listopada 2026": (2026, 11, 2),
        "12 września 2025": (2025, 9, 12),
        "12 wrzesnia 2025": (2025, 9, 12),
        "November 2nd, 2026": (2026, 11, 2),
        "listopad 2026": (2026, 11, None),
        "Dezember 2025": (2025, 12, None),
    }
    for text, date in cases.items():
        found = patterns.parse_date(text)
        assert found and found[1] == date, text
    for text in ("31.02.2026", "Rechnung 123456", "2. Novembre", "11/2026"):
        assert patterns.parse_date(text) is None, text


def test_a_value_is_proposed_from_the_texts_picked():
    assert patterns.propose_value(["01.10.2026", "01.11.2026", "—"]) == {
        "name": "date",
        "regex": patterns.DATE_PATTERNS["dmy"],
        "type": "date",
    }
    assert (
        patterns.propose_value(["2. November 2026", "3. Oktober 2026"])["regex"] == patterns.DATE_PATTERNS["d month y"]
    )
    assert patterns.propose_value(["# 12", "1.204"])["type"] == "number"
    assert patterns.propose_value(["Rechnung A", "Rechnung B"]) == {"name": "title", "regex": "", "type": "text"}


def test_page_values_are_typed_named_and_never_replace_the_addresss_year():
    specs = [
        {"name": "date", "regex": patterns.DATE_PATTERNS["dmy"], "type": "date"},
        {"name": "number", "regex": r"Nr\. (\d+)", "type": "number"},
        {"name": "title", "type": "text"},
        {"name": "gone", "type": "text"},
    ]
    texts = ["fällig am 02.11.2026", "Nr. 0042", "Rechnung: Strom / Gas\n  Oktober", None]
    values = patterns.page_values(specs, texts, re.search, {"year": 2025})
    assert values == {"date": "2026-11-02", "month": 11, "day": 2, "number": 42, "title": "Rechnung Strom Gas Oktober"}
    assert "gone" not in values, "a value whose element is missing is missing"
    assert patterns.render_path("{year}/{date} {title}", {"year": 2025} | values) == (
        "2025/2026-11-02 Rechnung Strom Gas Oktober"
    ), "no folder from a slash in the page's text"
    assert patterns.render_tags(["{date}", "{gone}"], values) == ["2026-11-02"]


def test_the_recipe_checks_values_json_listings_and_the_window():
    assert not check_recipe(DATED)
    bad = {"start": f"{SHOP}/x", "links": {"selector": "a", "values": [{"name": "a b"}]}}
    assert check_recipe(bad) == ["file links: a value without a name of letters, digits and _"]
    bad["links"]["values"] = [{"name": "d", "type": "when"}, {"name": "e", "regex": "("}]
    problems = check_recipe(bad)
    assert problems[0] == "file links: the value d is of an unknown type when"
    assert problems[1].startswith("file links: the value e: the pattern is not a regular expression")
    bad["links"]["values"] = [{"name": "x", "attribute": "on click"}]
    assert check_recipe(bad) == ["file links: the value x's attribute is no attribute's name"]
    listing = {"kind": "json", "url": f"{SHOP}/api/{{year}}?page=1", "items": "data.items", "file": "pdf.href"}
    assert not check_recipe({"start": f"{SHOP}/app", "links": listing | {"page": {"param": "page", "first": 1}}})
    assert check_recipe({"start": f"{SHOP}/app", "links": listing | {"url": "http://10.0.0.1/api"}}) == [
        "file links: the JSON listing http://10.0.0.1/api may not be opened"
    ]
    assert check_recipe({"start": f"{SHOP}/app", "links": listing | {"file": ""}}) == [
        "file links: no field with the files' addresses in the JSON's items"
    ]
    assert check_recipe({"start": f"{SHOP}/app", "links": listing | {"page": {"param": "page", "step": 0}}})[
        0
    ].startswith("file links: the JSON's pages")
    assert check_recipe({"start": f"{SHOP}/app", "links": listing | {"values": [{"name": "d"}]}}) == [
        "file links: the value d has no field"
    ]
    assert not check_recipe(DATED | {"viewport": {"width": 1920, "height": 1080}})
    assert check_recipe(DATED | {"viewport": {"width": 300, "height": 1080}})[0].startswith("the window's size")


def test_values_from_the_page_name_the_files_over_http():
    pages = Pages()
    items = walked(http_site(pages, DATED, path="{date} {kind} {id}"))
    assert [item.path for item in items[:3]] == [
        "2026-11-02 Rechnung a",
        "2026-10-03 Rechnung b",
        "2025-09-12 Faktura c",
    ]
    assert items[0].values["iso"] == "2026-11-02" and items[0].values["year"] == 2026
    assert items[2].values["month"] == 9 and items[2].values["day"] == 12
    # The row without a date fails alone, naming what is missing.
    assert items[3].problem and "names date" in str(items[3].problem)


@needs_chromium
def test_values_from_the_page_are_the_same_in_the_browser(monkeypatch):
    pages = Pages()

    def fulfill(route) -> None:
        status, headers, body = pages.answer(route.request.url, route.request.method)
        route.fulfill(status=status, headers=headers, body=body)

    monkeypatch.setattr(site_module, "guard", fulfill)
    source = site_module.Site(
        {"recipe": json.dumps(DATED), "stop_at_seen": 0, "browser": 1, "path": "{date} {id}"}, {}, None
    )
    items = walked(source)
    assert [item.path for item in items[:3]] == ["2026-11-02 a", "2026-10-03 b", "2025-09-12 c"]
    assert items[1].values["kind"] == "Rechnung" and items[1].values["iso"] == "2026-10-03"
    assert items[3].problem
    assert site_module.browser.free_browsers() == FREE


def test_a_level_without_values_reads_its_links_as_before():
    class Counting(site_module.HttpDriver):
        """The HTTP driver, counting the reads of values."""

        valued = 0

        def valued_links(self, page, selector, specs):
            Counting.valued += 1
            return super().valued_links(page, selector, specs)

    pages = Pages()
    source = http_site(pages, {"start": f"{SHOP}/invoices", "links": {"selector": "a.file", "pattern": r"\.pdf$"}})
    source.driver = Counting(source.client)
    assert [item.path for item in walked(source)] == ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]
    assert Counting.valued == 0


# JSON listings.

LISTING = {
    "kind": "json",
    "url": f"{SHOP}/api/items?page=1&size=2",
    "items": "data.items",
    "file": "pdf.href",
    "text": "title",
    "page": {"param": "page", "first": 1, "step": 1},
    "values": [{"name": "date", "field": "issued", "type": "date"}],
}


def test_paths_select_items_and_fields():
    data = {"a": {"b": [{"c": 1, "d": {"e": "x"}}, {"c": 2}]}, "weird key": [True]}
    assert jsonlist.select(data, "$.a.b[*].c") == [1, 2]
    assert jsonlist.select(data, "a.b[1].c") == [2]
    assert jsonlist.select(data, 'a.b[0]["d"].e') == ["x"]
    assert jsonlist.select(data, "a.*") == [[{"c": 1, "d": {"e": "x"}}, {"c": 2}]]
    assert jsonlist.items(data, "a.b") == jsonlist.items(data, "a.b[*]")
    assert jsonlist.field(data, "weird key[0]") == "true" and jsonlist.field(data, "a.b") is None
    assert jsonlist.page_url(f"{SHOP}/api?page=1&q=x", {"param": "page", "first": 1}, 3) == f"{SHOP}/api?q=x&page=3"
    assert jsonlist.page_url(f"{SHOP}/api?offset=0", {"param": "offset", "first": 0, "step": 50}, 3).endswith(
        "offset=100"
    )
    assert jsonlist.detect_page(f"{SHOP}/api?skip=20&take=20", 20) == {"param": "skip", "first": 20, "step": 20}
    assert jsonlist.detect_page(f"{SHOP}/api?q=1", 20) is None


def test_a_listing_is_proposed_from_an_answer():
    answer = {"meta": {"x": [1, 2]}, "data": {"items": ITEMS[1] * 3}}
    spec = jsonlist.propose(f"{SHOP}/api/items?page=1&size=2", answer)
    assert spec == LISTING | {
        "values": [{"name": "date", "field": "issued", "type": "date", "regex": ""}],
        "pattern": "",
    }
    assert jsonlist.propose(f"{SHOP}/api", {"ok": True})["file"] == ""


def test_a_json_listing_is_walked_page_by_page_over_http():
    pages = Pages()
    recipe = {"start": f"{SHOP}/app", "links": LISTING}
    items = walked(http_site(pages, recipe, path="{date} {text}"))
    assert [item.path for item in items] == ["2026-10-01 Invoice 1", "2026-11-01 Invoice 2", "2025-12-24 Invoice 3"]
    assert items[2].url == "https://cdn.shop.example/3.pdf", "an absolute address stays as it is"
    asked = [url for url in pages.requests if "/api/" in url]
    assert asked == [f"{SHOP}/api/items?size=2&page={page}" for page in (1, 2, 3)], "ends at the empty page"
    assert f"{SHOP}/app" not in pages.requests, "the listing's own page is not needed"
    # At most the job's pages; without a page parameter, one.
    pages.requests.clear()
    walked(http_site(pages, recipe, max_pages=1))
    assert len([url for url in pages.requests if "/api/" in url]) == 1
    pages.requests.clear()
    single = {"start": f"{SHOP}/app", "links": {key: value for key, value in LISTING.items() if key != "page"}}
    assert len(walked(http_site(pages, single))) == 2 and len(pages.requests) == 1


def test_a_json_listings_address_takes_the_levels_values():
    pages = Pages()
    recipe = {
        "start": f"{SHOP}/years",
        "follow": [{"selector": "a.year", "pattern": r"/years/(?P<year>\d{4})$"}],
        "links": LISTING | {"url": f"{SHOP}/api/{{year}}/items?page=1"},
    }
    items = walked(http_site(pages, recipe, max_pages=1))
    assert len(items) == 2, "the second year's page lists the files the first already gave"
    assert [url for url in pages.requests if "/api/" in url] == [
        f"{SHOP}/api/{year}/items?page=1" for year in (2026, 2025)
    ]
    assert items[0].values["year"] == 2026


def test_json_answers_are_capped_and_checked(monkeypatch):
    recipe = {"start": f"{SHOP}/app", "links": LISTING}

    def serving(body: bytes, kind: str = "application/json") -> site_module.Site:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers={"content-type": kind}, content=body)
            )
        )
        return site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0}, {}, client)

    with pytest.raises(SourceError, match="is not JSON .*signed out"):
        walked(serving(b"<html>Sign in</html>", "text/html"))
    with pytest.raises(SourceError, match="nested deeper than"):
        walked(serving(b"[" * 100 + b"]" * 100))
    monkeypatch.setattr(download, "MAX_PAGE", 1000)
    with pytest.raises(SourceError, match="larger than"):
        walked(serving(json.dumps({"data": {"items": ITEMS[1] * 50}}).encode()))
    monkeypatch.setattr(download, "MAX_PAGE", 16 * 1024 * 1024)
    monkeypatch.setattr(jsonlist, "MAX_ITEMS", 2)
    assert len(walked(serving(json.dumps({"data": {"items": ITEMS[1] * 50}}).encode()))) == 2
    # A text with brackets in its strings is no nesting.
    assert jsonlist.parse(json.dumps({"t": "[[[[" * 100}).encode()) == {"t": "[[[[" * 100}


@needs_chromium
def test_a_json_listing_is_read_with_the_browsers_cookies(monkeypatch):
    pages = Pages()

    def fulfill(route) -> None:
        if route.request.url.endswith("/app"):
            # The page's sign-in: a cookie of the session, as a step's sign-in would leave it.
            route.fulfill(
                status=200, headers={"content-type": "text/html", "set-cookie": "sid=ok; Path=/"}, body=b"<p>"
            )
            return
        status, headers, body = pages.answer(route.request.url, route.request.method)
        route.fulfill(status=status, headers=headers, body=body)

    real = download.guarded_client
    monkeypatch.setattr(site_module, "guard", fulfill)
    monkeypatch.setattr(
        site_module.download, "guarded_client", lambda **options: real(transport=transport(pages), **options)
    )
    recipe = {"steps": [{"action": "goto", "url": f"{SHOP}/app"}], "links": LISTING}
    source = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0}, {}, None)
    assert len(walked(source)) == 3
    asked = [cookie for url, cookie in zip(pages.requests, pages.cookies) if "/api/" in url]
    assert asked and all(cookie == "sid=ok" for cookie in asked)
    assert site_module.browser.free_browsers() == FREE


# The recorder.


@needs_chromium
def test_a_value_is_picked_beside_the_links_and_shown_in_the_examples():
    async def scenario():
        driver, recorder = await recording(Pages())
        try:
            await recorder.handle({"type": "goto", "url": f"{SHOP}/cards"})
            await settled(recorder, "/cards")
            await driver.mode("links")
            await driver.click("a.file")
            await driver.until(lambda m: m["recipe"]["links"].get("selector"))
            await recorder.handle({"type": "mode", "mode": "value", "target": "links", "level": 0})
            await driver.click(".card:nth-of-type(2) .when")
            picked = await driver.until(lambda m: m["recipe"]["links"].get("values"))
            await driver.click(".card:nth-of-type(1) h3")
            both = await driver.until(lambda m: len(m["recipe"]["links"].get("values", [])) == 2)
            await recorder.handle(
                {"type": "value", "target": "links", "level": 0, "index": 1, "changes": {"name": "title"}}
            )
            renamed = driver.last()
            await recorder.handle({"type": "value-remove", "target": "links", "level": 0, "index": 1})
            removed = driver.last()
            return picked, both, renamed, removed
        finally:
            await recorder.close()

    picked, both, renamed, removed = asyncio.run(scenario())
    value = picked["recipe"]["links"]["values"][0]
    assert value == {
        "name": "date",
        "regex": patterns.DATE_PATTERNS["dmy"],
        "type": "date",
        "closest": "div.card",
        "selector": "span.when",
        "attribute": "",
    }
    examples = picked["proposals"]["links"]["examples"]
    assert [example["values"].get("date") for example in examples] == ["2026-10-01", "2026-11-01"]
    assert examples[1]["values"]["month"] == 11
    assert both["recipe"]["links"]["values"][1]["selector"] == "h3"
    assert [example["values"]["title"] for example in renamed["proposals"]["links"]["examples"]] == [
        "Rechnung 1",
        "Rechnung 2",
    ]
    assert len(removed["recipe"]["links"]["values"]) == 1
    assert picked["recipe"]["viewport"] == {"width": 1280, "height": 800}, "the window it was picked in"


@needs_chromium
def test_the_network_panel_lists_json_answers_and_makes_one_the_listing():
    async def scenario():
        driver, recorder = await recording(Pages())
        try:
            await recorder.handle({"type": "goto", "url": f"{SHOP}/app"})
            for _ in range(100):
                if any(message["type"] == "requests" for message in driver.sent):
                    break
                await asyncio.sleep(0.05)
            listed = [message for message in driver.sent if message["type"] == "requests"][-1]["items"]
            await recorder.handle({"type": "json-pick", "id": listed[0]["id"]})
            chosen = driver.last()
            await recorder.handle({"type": "json-edit", "changes": {"pattern": r"2\.pdf$", "page": None}})
            edited = driver.last()
            await recorder.handle({"type": "value-add", "target": "links", "level": 0, "field": "pdf.href"})
            added = driver.last()
            return listed, chosen, edited, added
        finally:
            await recorder.close()

    listed, chosen, edited, added = asyncio.run(scenario())
    assert [item["url"] for item in listed] == [f"{SHOP}/api/items?page=1&size=2"] and listed[0]["size"] > 100
    assert "body" not in listed[0], "the page gets the list, not the answers"
    links = chosen["recipe"]["links"]
    assert links | {"values": None} == LISTING | {"values": None, "pattern": ""}
    assert chosen["recipe"]["start"] == f"{SHOP}/app"
    proposal = chosen["proposals"]["links"]
    assert proposal["items"] == 2 and proposal["matching"] == 2 and proposal["following"].endswith("page=2")
    assert [example["values"]["date"] for example in proposal["examples"]] == ["2026-10-01", "2026-11-01"]
    assert edited["proposals"]["links"]["matching"] == 1 and "page" not in edited["recipe"]["links"]
    assert added["recipe"]["links"]["values"][-1] == {"name": "href", "field": "pdf.href", "regex": "", "type": "text"}
    assert not check_recipe(added["recipe"])
