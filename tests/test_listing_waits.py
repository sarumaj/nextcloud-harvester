# SPDX-License-Identifier: AGPL-3.0-or-later
"""How long the browser waits for a listing page (harvester.sources.tabs: listing_shown, grown),
in Chromium against pages served by the routes (no network) that never let the network go quiet
(a beacon every 200 ms, as pages with analytics or polling do): a page is read once its level's
links are there, a next page once they changed, a scroll's round once the page grew; a page
without them still gets the network's whole wait, and a listing a script fills late is waited
for."""

# pylint: disable=missing-function-docstring

import json
import time

import pytest
from test_browser import FREE, needs_chromium

from harvester.sources import site as site_module
from harvester.sources import tabs

SITE = "https://busy.example"
# Every page asks for something every 200 ms: Playwright's "networkidle" (500 ms without
# requests) never comes.
BEACON = "<script>setInterval(() => fetch('/beacon'), 200)</script>"


def listing(number: int, last: int = 3) -> str:
    following = f'<a class="next" href="/list?p={number + 1}">next</a>' if number < last else ""
    files = "".join(f'<a class="file" href="/files/{number}-{n}.pdf">{number}-{n}</a>' for n in (1, 2))
    return f"<html><body>{files}{following}{BEACON}</body></html>"


# A listing a script builds a second after the page loaded (a single-page app's), and a feed that
# adds five posts a round as it is scrolled, up to 15.
LATE = f"""<html><body><div id="app"></div>{BEACON}<script>
setTimeout(() => {{ app.innerHTML = '<a class="file" href="/files/late.pdf">late</a>' }}, 1000)
</script></body></html>"""
FEED = f"""<html><body style="margin: 0"><div id="feed"></div>{BEACON}<script>
let count = 0
const more = () => {{
  for (let i = 0; i < 5 && count < 15; i++, count++)
    feed.insertAdjacentHTML('beforeend',
      `<a class="post" href="/files/p${{count}}.pdf" style="display: block; height: 900px">p${{count}}</a>`)
}}
more()
addEventListener('scroll', () => {{
  if (innerHeight + scrollY >= document.body.scrollHeight - 10) setTimeout(more, 300)
}})
</script></body></html>"""


def fulfill(route) -> None:
    path = route.request.url.removeprefix(SITE)
    if path.startswith("/list"):
        number = int(path.partition("p=")[2] or 1)
        body = listing(number)
    elif path == "/late":
        body = LATE
    elif path == "/feed":
        body = FEED
    elif path == "/beacon":
        route.fulfill(status=204, body=b"")
        return
    else:
        route.fulfill(status=404, body=b"")
        return
    route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=body.encode())


def walked(monkeypatch, recipe: dict, pages: int = 10) -> tuple[list[str], float]:
    monkeypatch.setattr(site_module, "guard", fulfill)
    source = site_module.Site(
        {"recipe": json.dumps(recipe), "stop_at_seen": 0, "browser": 1, "max_pages": pages}, {}, None
    )
    source.sleep = lambda seconds: None
    source.page_pause = 0.0
    started = time.monotonic()
    try:
        return [item.path for item in source.items(lambda key: False)], time.monotonic() - started
    finally:
        source.close()


@needs_chromium
def test_listing_pages_that_never_go_quiet_are_read_once_their_links_are_there(monkeypatch):
    recipe = {
        "start": f"{SITE}/list",
        "links": {"selector": "a.file", "pattern": r"\.pdf$"},
        "next": {"selector": "a.next", "level": 0},
    }
    paths, took = walked(monkeypatch, recipe)
    assert paths == [f"{p}-{n}.pdf" for p in (1, 2, 3) for n in (1, 2)]
    # Three pages: the links at once, then SHORT_IDLE each for what a script still loads (before:
    # 0.5 s and the whole 15 s of the network's wait each, some 46 s).
    assert took < 3 * (tabs.SHORT_IDLE / 1000 + 1.5), took
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_listing_a_script_fills_late_is_waited_for(monkeypatch):
    recipe = {"start": f"{SITE}/late", "links": {"selector": "a.file", "pattern": r"\.pdf$"}}
    paths, took = walked(monkeypatch, recipe)
    assert paths == ["late.pdf"]
    assert took < tabs.LISTING_WAIT / 1000, took


@needs_chromium
def test_a_feed_scrolled_to_its_end_waits_for_each_round_to_grow(monkeypatch):
    recipe = {"start": f"{SITE}/feed", "links": {"selector": "a.post", "pattern": r"\.pdf$", "scroll": 10}}
    paths, took = walked(monkeypatch, recipe)
    assert paths == [f"p{n}.pdf" for n in range(15)]
    # Two rounds that grew, then two that did not (GROW_WAIT each), instead of the network's
    # whole wait every round (some 62 s).
    assert took < 2 * 1.5 + 2 * tabs.GROW_WAIT / 1000 + tabs.SHORT_IDLE / 1000 + 3, took


@needs_chromium
def test_a_page_without_the_levels_links_still_waits_for_the_network(monkeypatch):
    monkeypatch.setattr(tabs, "IDLE", 3_000)
    monkeypatch.setattr(tabs, "LISTING_WAIT", 1_000)
    recipe = {
        "start": f"{SITE}/list",
        "follow": [{"selector": "a.year", "pattern": "x"}],
        "links": {"selector": "a.file", "pattern": r"\.pdf$"},
    }
    started = time.monotonic()
    with pytest.raises(site_module.SourceError, match="the start page has none of the recipe's links"):
        walked(monkeypatch, recipe)
    # The level's links never show: LISTING_WAIT, then the network's wait up to IDLE in all.
    assert 2.5 < time.monotonic() - started < 6
