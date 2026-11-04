# SPDX-License-Identifier: AGPL-3.0-or-later
"""Robust selectors and better proposals: the recorder's candidate selectors (each must find its
element alone in Playwright), their replay with repair (steps, levels, the next page; in the
browser and over HTTP), the next page's link on a pager without classes, the listing where the
steps end, proposals without a visit's tokens and fragments, escapes in names, generic file
names, and Python's patterns outlined by JavaScript."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import re

import httpx
import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from test_browser import Driver, needs_chromium, outlined, site_with_cookies
from test_recorder_features import recording, settled

from fake_site import SITE
from fake_site import Site as FakeSite
from harvester import selectors
from harvester.browser import SCRIPT, launch_options
from harvester.patterns import js_regex, propose, token_parameters
from harvester.recipe import check_recipe, steps_from_events, token_warnings
from harvester.sources import site as site_module

# Proposals.


def test_a_visits_tokens_are_no_part_of_a_proposal():
    proposal = propose([f"https://ex.org/dl?id={n}&sid=abc123" for n in (1, 2, 3)])
    assert "abc123" not in proposal["pattern"] and "sid" not in proposal["pattern"]
    assert proposal["groups"] == ["id"] and proposal["matches"] == 3
    for later in ("https://ex.org/dl?id=4&sid=other", "https://ex.org/dl?sid=x&id=4", "https://ex.org/dl?id=4"):
        assert re.search(proposal["pattern"], later)[1] == "4", later
    tracked = propose(["https://ex.org/f/a.pdf?utm_source=x", "https://ex.org/f/b.pdf"])
    assert tracked["pattern"] == r"/f/(?P<name>[^/?]+)\.pdf(?:\?[^#]*)?$" and tracked["matches"] == 2
    signed = propose([f"https://s3.ex.org/b/{n}.pdf?X-Amz-Signature={n}f&X-Amz-Expires=60" for n in (1, 2)])
    assert "Amz" not in signed["pattern"] and signed["matches"] == 2
    session = propose(["https://ex.org/f/a.pdf;jsessionid=AB12", "https://ex.org/f/b.pdf"])
    assert "AB12" not in session["pattern"] and session["matches"] == 2
    assert token_parameters("https://ex.org/cb?code=1&state=2&page=3") == ["code", "state"]


def test_fragments_are_ignored():
    proposal = propose(["https://ex.org/a/1.pdf#page=1", "https://ex.org/a/2.pdf", "https://ex.org/a/3.pdf#x"])
    assert proposal["pattern"] == r"/a/(?P<number>\d+)\.pdf(?:#.*)?$" and proposal["matches"] == 3
    # Without fragments in the picked links, the pattern is as it always was.
    assert propose(["https://ex.org/a/1.pdf", "https://ex.org/a/2.pdf"])["pattern"] == r"/a/(?P<number>\d+)\.pdf$"


def test_an_escape_is_no_part_of_a_number():
    proposal = propose([f"https://ex.org/r/Report%20{year}.pdf" for year in (2023, 2024, 2025)])
    assert proposal["pattern"] == r"/r/Report%20(?P<year>\d{4})\.pdf$"
    assert proposal["path"] == "{year}/{filename}" and proposal["matches"] == 3


def test_a_generic_file_name_proposes_the_links_text_or_its_groups():
    urls = [f"https://ex.org/download.php?id={n}" for n in (1, 2, 3)]
    by_text = propose(urls, ["Report A", "Report B", "Report C"])
    assert by_text["path"] == "{text}" and by_text["generic"]
    by_group = propose(urls, ["Download", "Download", "Download"])
    assert by_group["path"] == "{id}" and by_group["generic"]
    assert "generic" not in propose(["https://ex.org/a/x.pdf", "https://ex.org/a/y.pdf"])


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"/(?P<year>\d{4})/(?P=year)\.pdf$", {"source": r"/(?<year>\d{4})/\k<year>\.pdf$", "flags": ""}),
        (r"(?i)\.PDF\Z", {"source": r"\.PDF$", "flags": "i"}),
        (r"(?s)a(?#comment).b", {"source": "a.b", "flags": "s"}),
        (r"[(?P<]x\(?P<", {"source": r"[(?P<]x\(?P<", "flags": ""}),
        (r"(?x) a b", None),
        (r"(?i:a)b", None),
        (r"(?>a)b", None),
        (r"a++", None),
        (r"[]a]", None),
        (r"(a)?(?(1)b|c)", None),
    ],
)
def test_python_patterns_in_javascript(pattern, expected):
    assert js_regex(pattern) == expected


# Steps and recipes.


def test_steps_keep_their_alternatives_and_recipes_check_them():
    events = [
        {"action": "goto", "url": "https://ex.org/login"},
        {"action": "click", "selector": "#go", "alternatives": ['internal:role=button[name="Go"s]'], "text": "Go"},
        {"action": "fill", "selector": "#q", "value": "x"},
    ]
    steps = steps_from_events(events)
    assert steps[1]["alternatives"] == ['internal:role=button[name="Go"s]'] and "alternatives" not in steps[2]
    recipe = {"steps": steps, "links": {"selector": "a", "pattern": "", "alternatives": ["a.file"]}}
    assert not check_recipe(recipe)
    broken = recipe | {"links": {"selector": "a", "alternatives": "a.file"}}
    assert check_recipe(broken) == ["file links: the alternatives are a list of selectors"]
    steps[1]["alternatives"] = [""]
    assert check_recipe(recipe) == ["step 2: the alternatives are a list of selectors"]


def test_addresses_with_a_visits_token_are_warned_of():
    recipe = {
        "steps": [{"action": "goto", "url": "https://ex.org/cb?ticket=ST-1&x=1"}],
        "start": "https://ex.org/l?sid=2",
    }
    warnings = token_warnings(recipe)
    assert len(warnings) == 2
    first, second = warnings  # pylint: disable=unbalanced-tuple-unpacking
    assert first.startswith("step 1 opens an address with ticket") and second.startswith(
        "the listing's address carries sid"
    )
    assert not token_warnings({"steps": [{"action": "goto", "url": "https://ex.org/?page=2"}], "start": ""})


# Over HTTP: the recorder's selectors, and repairs.


def test_role_and_text_selectors_over_http():
    soup = BeautifulSoup(
        """<nav id="pager"><a href="/1">‹</a><a href="/3"> › </a></nav><a href="/x" aria-label="Next">»</a>
        <div><span>Accept all</span></div><button type="submit">Go</button>""",
        "html.parser",
    )

    def hrefs(selector):
        return [tag.get("href") or tag.name for tag in selectors.soup_select(soup, selector)]

    assert hrefs('internal:role=link[name="›"s]') == ["/3"]
    assert hrefs('internal:role=link[name="Next"s]') == ["/x"], "an aria-label is the name"
    assert hrefs('#pager >> internal:role=link[name="‹"s]') == ["/1"]
    assert hrefs('span:text-is("Accept all")') == ["span"] and hrefs('div:text-is("Accept all")') == []
    assert hrefs('internal:role=button[name="Go"s]') == ["button"] and hrefs("nav a") == ["/1", "/3"]
    with pytest.raises(ValueError):
        selectors.soup_select(soup, 'internal:role=heading[name="x"s]')


def http_source(recipe: dict) -> tuple[site_module.Site, list[str]]:
    site = FakeSite()

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = site.answer(str(request.url), request.method)
        return httpx.Response(status, headers=headers, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    made = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0}, {}, client)
    log: list[str] = []
    made.report = lambda level, text: log.append(text)
    return made, log


def test_levels_and_the_next_page_are_repaired_over_http():
    recipe = {
        "start": f"{SITE}/list?p=1",
        "links": {"selector": "a.gone", "pattern": r"\.pdf$", "alternatives": ["li.item a"]},
        "next": {"selector": "a.gone", "level": 0, "alternatives": ['internal:role=link[name="Next"s]']},
    }
    made, log = http_source(recipe)
    assert [item.path for item in made.items(lambda key: False)] == [f"{p}-{n}.pdf" for p in (1, 2, 3) for n in (1, 2)]
    assert log == [
        "file links: selector repaired (a.gone → li.item a)",
        'next page: selector repaired (a.gone → internal:role=link[name="Next"s])',
    ], "each repair said once, the recipe unchanged"
    assert log[0].template["params"]["what"] == {"text": "file links"}, "translated, what was repaired too"
    assert recipe["links"]["selector"] == "a.gone"
    # A recipe without alternatives finds what its selector finds, as always.
    plain, log = http_source({"start": f"{SITE}/list?p=1", "links": {"selector": "a.gone", "pattern": ""}})
    with pytest.raises(site_module.SourceError, match="the start page has none"):
        list(plain.items(lambda key: False))
    assert not log


# In the browser.

# Elements to find, and the selector a recorder ranks best for each.
CANDIDATES = """<html><body>
<form id="login"><input name="user"><button data-testid="go">Go</button></form>
<form><input name="user"><label for="mail">E-mail</label><input id="mail" name="mail"></form>
<button id=":r1:" class="btn css-1x2y3z">Sign in</button><button>Sign in later</button>
<a href="/a" class="more">More</a><a href="/b" class="more">More</a>
<nav><a href="/p/1">‹</a><a href="/p/3">›</a></nav><nav><a href="/x" rel="next">2</a></nav>
<ul><li><span onclick="1">Accept all</span></li></ul>
<a href="/icon"><i class="icon"></i> Icon</a>
<style>.icon::before { content: "→" }</style>
</body></html>"""
BEST = {
    "#login input": '#login input[name="user"]',
    "#login button": '[data-testid="go"]',
    "#mail": "#mail",
    "button.btn": 'internal:role=button[name="Sign in"s]',
    "nav a:last-child": 'internal:role=link[name="›"s]',
    "a[rel]": 'a[rel~="next"]',
    "span[onclick]": 'span:text-is("Accept all")',
}


@needs_chromium
def test_every_candidate_finds_its_element_alone_in_playwright():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options(9))
        try:
            page = browser.new_page()
            page.add_init_script(SCRIPT)
            page.route(
                "**/*",
                lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=CANDIDATES),
            )
            page.goto(f"{SITE}/candidates")
            checked = {}
            for element in page.query_selector_all("input, button, a, span[onclick]"):
                found = element.evaluate("(el) => window.__harvester.candidates(el, el.closest('nav') !== null)")
                assert found, element.evaluate("el => el.outerHTML")
                for selector in found:
                    matches = page.query_selector_all(selector)
                    assert len(matches) == 1 and matches[0].evaluate("(el, other) => el === other", element), selector
                checked[element.evaluate("el => el.outerHTML")] = found
            best = {
                key: page.eval_on_selector(key, "(el) => window.__harvester.candidates(el, true)[0]") for key in BEST
            }
            icon = page.eval_on_selector("a[href='/icon']", "(el) => window.__harvester.candidates(el)")
            generated = page.eval_on_selector("button.btn", "(el) => window.__harvester.candidates(el)")
        finally:
            browser.close()
    assert best == BEST
    assert not any(selector.startswith("internal:role") for selector in icon), "a name CSS adds to is not certain"
    assert not any(":r1:" in selector or "css-" in selector for selector in generated)
    assert len(checked) >= 10


def browser_source(site: FakeSite, monkeypatch, recipe: dict) -> tuple[site_module.Site, list[str]]:
    monkeypatch.setattr(site_module, "guard", site_with_cookies(site))
    made = site_module.Site(
        {"recipe": json.dumps(recipe), "stop_at_seen": 0}, {"username": "me", "password": "pw"}, None
    )
    log: list[str] = []
    made.report = lambda level, text: log.append(text)
    return made, log


@needs_chromium
def test_steps_and_levels_are_repaired_in_the_browser(monkeypatch):
    monkeypatch.setattr(site_module, "STEP_TIMEOUT", 6000)
    monkeypatch.setattr(selectors, "GRACE", 300)
    site = FakeSite(require_login=True)
    recipe = {
        "steps": [
            {"action": "goto", "url": f"{SITE}/login"},
            {
                "action": "fill",
                "selector": "#user-old",
                "credential": "username",
                "alternatives": ['input[name="user"]'],
            },
            {"action": "fill", "selector": "#login-password", "credential": "password", "alternatives": ["#gone"]},
            {
                "action": "click",
                "selector": "#gone",
                "alternatives": ["#nothing", 'internal:role=button[name="Sign in"s]'],
            },
        ],
        "start": f"{SITE}/select/mag/archiv",
        "follow": [{"selector": "a.gone", "pattern": r"/archiv/(?P<year>\d{4})$", "alternatives": ["ul a"]}],
        "links": {"selector": "a.year__link", "pattern": r"/archiv/\d{4}/(?P<issue>\d+)$"},
    }
    source, log = browser_source(site, monkeypatch, recipe)
    try:
        assert len(list(source.items(lambda key: False))) == 8
    finally:
        source.close()
    assert log == [
        'step 2: selector repaired (#user-old → input[name="user"])',
        'step 4: selector repaired (#gone → internal:role=button[name="Sign in"s])',
        "level 1: selector repaired (a.gone → ul a)",
    ]
    assert any(url.endswith("/session") for url in site.requests), "signed in"


class Pager:  # pylint: disable=too-few-public-methods
    """Pages of a listing whose pager has no classes, ids or rel (‹ and › only), one with rel=next,
    and a sign-in whose listing's address carries a ticket."""

    def __init__(self):
        self.requests: list[str] = []

    def answer(self, url: str, method: str = "GET") -> tuple[int, dict, bytes]:  # pylint: disable=unused-argument
        self.requests.append(url)
        html = {"content-type": "text/html; charset=utf-8"}
        match = re.search(r"/(bare|rel)\?p=(\d)", url)
        if match:
            page = int(match[2])
            back = f'<a href="/{match[1]}?p={page - 1}">‹</a>' if page > 1 else ""
            rel = ' rel="next"' if match[1] == "rel" else ""
            onward = f'<a href="/{match[1]}?p={page + 1}"{rel}>›</a>' if page < 3 else ""
            files = "".join(f'<li><a href="/files/{page}-{n}.pdf">F{page}.{n}</a></li>' for n in (1, 2))
            return 200, html, f"<html><body><ul>{files}</ul><nav>{back}{onward}</nav></body></html>".encode()
        if "/signin" in url:
            go = "<button onclick=\"location.href='/listing?ticket=ST-4711&p=1'\">Sign in</button>"
            return 200, html, f"<html><body>{go}</body></html>".encode()
        if "/listing" in url:
            files = "".join(f'<li><a href="/files/l{n}.pdf">L{n}</a></li>' for n in (1, 2))
            return 200, html, f"<html><body><ul>{files}</ul></body></html>".encode()
        return 200, {"content-type": "application/pdf"}, b"%PDF-1.7"


async def picked(driver: Driver, selector: str, condition) -> dict:
    """Clicks the element and waits for the first recipe after it that meets the condition (one
    may come before the click returns)."""
    start = len(driver.sent)
    await driver.click(selector)
    for _ in range(100):
        for message in driver.sent[start:]:
            if message["type"] == "recipe" and condition(message):
                return message
        await asyncio.sleep(0.05)
    raise AssertionError("the recorder did not answer")


@needs_chromium
def test_the_next_page_of_a_pager_without_classes_is_its_text_or_rel():
    async def pick(recorder, driver: Driver, path: str) -> dict:
        await recorder.handle({"type": "goto", "url": f"{SITE}{path}"})
        await driver.mode("next")
        message = await picked(driver, "nav a", lambda m: "next" in m["recipe"])
        await recorder.handle({"type": "remove", "target": "next"})
        return message["recipe"]["next"]

    async def scenario():
        driver, recorder = await recording(Pager())  # type: ignore[arg-type]
        try:
            bare = await pick(recorder, driver, "/bare?p=1")
            rel = await pick(recorder, driver, "/rel?p=1")
            assert recorder._page is not None
            await recorder._page.goto(f"{SITE}/bare?p=2")
            onward = await recorder._page.eval_on_selector_all(bare["selector"], "els => els.map(e => e.href)")
            return bare, rel, onward
        finally:
            await recorder.close()

    bare, rel, onward = asyncio.run(scenario())
    assert bare["selector"] == 'internal:role=link[name="›"s]' and onward == [f"{SITE}/bare?p=3"], "never ‹ on page 2"
    assert not any("nth-of-type" in selector for selector in bare.get("alternatives", []))
    assert rel["selector"] == 'a[rel~="next"]'


@needs_chromium
def test_a_listing_reached_with_a_ticket_starts_where_the_steps_end(monkeypatch):
    async def scenario():
        driver, recorder = await recording(Pager())  # type: ignore[arg-type]
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/signin"})
            await driver.click("button")
            await settled(recorder, r"ticket=")
            await driver.mode("links")
            at_end = await picked(driver, "li a", lambda m: m["recipe"]["links"])
            # Browsed away and back: the address is all the job has.
            await driver.mode("browse")
            await recorder.handle({"type": "goto", "url": f"{SITE}/listing?ticket=ST-4711&p=1"})
            await settled(recorder, r"p=1")
            await driver.mode("links")
            browsed = await picked(driver, "li a", lambda m: m["recipe"]["start"])
            return at_end, browsed
        finally:
            await recorder.close()

    at_end, browsed = asyncio.run(scenario())
    assert at_end["recipe"]["start"] == "" and at_end["warnings"] == []
    assert browsed["recipe"]["start"].endswith("ticket=ST-4711&p=1")
    assert browsed["warnings"] and browsed["warnings"][0].startswith("the listing's address carries ticket")
    # The job: the steps, then the page they end on.
    pager = Pager()
    recipe = at_end["recipe"] | {"links": at_end["recipe"]["links"] | {"pattern": r"\.pdf$"}}

    def fulfill(route) -> None:
        status, headers, body = pager.answer(route.request.url)
        route.fulfill(status=status, headers=headers, body=body)

    monkeypatch.setattr(site_module, "guard", fulfill)
    source = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0}, {}, None)
    try:
        assert [item.path for item in source.items(lambda key: False)] == ["l1.pdf", "l2.pdf"]
    finally:
        source.close()


@needs_chromium
def test_python_only_patterns_are_outlined_as_python_matches_them():
    async def scenario():
        driver, recorder = await recording(FakeSite())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/list?p=1"})
            await driver.mode("links")
            await picked(driver, "a.file", lambda m: m["recipe"]["links"])
            shown = []
            for pattern in (r"(?i)/FILES/1-1", r"(?x) /files/1-  (?P<n>2)", r"/(?P<a>files)/1-(?P=a)?2"):
                await recorder.handle({"type": "pattern", "target": "links", "pattern": pattern})
                shown.append(await outlined(recorder))
            return shown, recorder.recipe["links"]
        finally:
            await recorder.close()

    shown, links = asyncio.run(scenario())
    assert shown == [["solid", "dashed"], ["dashed", "solid"], ["dashed", "solid"]]
    assert links["alternatives"] and all(alternative != links["selector"] for alternative in links["alternatives"])
