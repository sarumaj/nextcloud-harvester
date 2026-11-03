# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser's side of the recorder's features against the fake site (routed, no network):
numbered pages that are buttons, the step "scroll to load more", what the browser saw at a
failed step, and in the recorder: Back, Forward and Reload, removing and undoing picks, picking
a level again, proposing numbered pages, and the recipe's test on the recorder's screen."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import re

import pytest
from test_browser import FREE, Driver, needs_chromium, site_with_cookies

from fake_site import SITE, Site
from harvester.browser.recorder import Recorder
from harvester.sources import SourceError
from harvester.sources import site as site_module


def browser_source(site: Site, monkeypatch, recipe: dict, **options) -> site_module.Site:
    monkeypatch.setattr(site_module, "guard", site_with_cookies(site))
    return site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0, **options}, {}, None)


def paths(source: site_module.Site) -> list[str]:
    try:
        return [item.path for item in source.items(lambda key: False)]
    finally:
        source.close()


@needs_chromium
def test_numbered_buttons_are_clicked_page_after_page(monkeypatch):
    recipe = {
        "start": f"{SITE}/buttons",
        "links": {"selector": "a.file", "pattern": r"\.pdf$"},
        "next": {"kind": "number", "selector": "div.pager", "level": 0, "buttons": True},
    }
    assert paths(browser_source(Site(), monkeypatch, recipe)) == [f"b{p}-{n}.pdf" for p in (1, 2, 3) for n in (1, 2)]
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_scroll_step_loads_what_the_listing_adds_as_it_is_scrolled(monkeypatch):
    links = {"selector": "a.post", "pattern": r"\.pdf$"}
    plain = {"steps": [{"action": "goto", "url": f"{SITE}/feed"}], "links": links}
    assert len(paths(browser_source(Site(), monkeypatch, plain))) == 10
    scrolled = {"steps": [{"action": "goto", "url": f"{SITE}/feed"}, {"action": "scroll"}], "links": links}
    assert len(paths(browser_source(Site(), monkeypatch, scrolled))) == 35
    # On a kept session the sign-in is skipped, the scroll at the steps' end is not.
    kept = browser_source(Site(), monkeypatch, scrolled | {"start": f"{SITE}/feed"})
    kept.restore({"cookies": [], "origins": []})
    assert len(paths(kept)) == 35


@needs_chromium
def test_a_failed_step_names_its_element_and_keeps_what_the_browser_showed(monkeypatch):
    monkeypatch.setattr(site_module, "STEP_TIMEOUT", 1000)
    recipe = {
        "steps": [
            {"action": "goto", "url": f"{SITE}/login"},
            {"action": "click", "selector": "#no-such-button", "text": ""},
        ],
        "links": {"selector": "a", "pattern": "x"},
    }
    source = browser_source(Site(), monkeypatch, recipe)
    with pytest.raises(SourceError, match=re.escape("step 2 (click #no-such-button) failed")):
        paths(source)
    shot = source.shot
    assert shot and shot["image"][:2] == b"\xff\xd8" and len(shot["image"]) <= site_module.SHOT_BYTES
    assert shot["url"] == f"{SITE}/login" and shot["what"] == "step 2 (click #no-such-button) failed"
    assert site_module.browser.free_browsers() == FREE


# The recorder.


def routed(site: Site):
    """The recorder's guard in the tests: the fake site for every context (the test's too)."""

    async def fulfill(route) -> None:
        status, headers, body = site.answer(route.request.url, route.request.method)
        await route.fulfill(status=status, headers=headers, body=body)

    return fulfill


async def recording(site: Site) -> tuple[Driver, Recorder]:
    driver = Driver()
    recorder = Recorder(driver.send)
    driver.recorder = recorder
    recorder._guard = routed(site)  # type: ignore[method-assign]
    await recorder.start()
    return driver, recorder


async def settled(recorder: Recorder, pattern: str) -> None:
    assert recorder._page is not None
    await recorder._page.wait_for_url(re.compile(pattern))
    await recorder._page.wait_for_load_state()


@needs_chromium
def test_back_forward_and_reload_are_steps_only_while_recording():
    async def scenario():
        driver, recorder = await recording(Site())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/list?p=1"})
            await driver.click("a.next")
            await settled(recorder, r"p=2$")
            await recorder.handle({"type": "back"})
            await settled(recorder, r"p=1$")
            await recorder.handle({"type": "reload"})
            recorded = [step["action"] for step in recorder.recipe["steps"]]
            await driver.mode("browse")
            await recorder.handle({"type": "forward"})
            await settled(recorder, r"p=2$")
            await recorder.handle({"type": "back"})
            await settled(recorder, r"p=1$")
            return recorded, recorder.recipe["steps"]
        finally:
            await recorder.close()

    recorded, steps = asyncio.run(scenario())
    assert recorded == ["goto", "click", "goto"], "Back while recording: the page arrived at; Reload: nothing"
    assert steps[-1] == {"action": "goto", "url": f"{SITE}/list?p=1"}
    assert len(steps) == 3, "browsing records nothing"


@needs_chromium
def test_levels_are_removed_picked_again_and_undone():
    recipe = {
        "start": f"{SITE}/select/mag/archiv",
        "follow": [{"selector": "a.years__link", "pattern": "a"}, {"selector": "a.year__link", "pattern": "b"}],
        "links": {"selector": "a.file", "pattern": "c"},
        "next": {"selector": "a.next", "level": 2},
    }

    async def scenario():
        driver, recorder = await recording(Site())
        try:
            await recorder.handle({"type": "load", "recipe": recipe})
            await recorder.handle({"type": "remove", "target": "follow", "level": 0})
            removed = json.loads(json.dumps(recorder.recipe))
            await recorder.handle({"type": "undo-pick"})
            undone = json.loads(json.dumps(recorder.recipe))
            await recorder.handle({"type": "goto", "url": f"{SITE}/select/mag/archiv"})
            await recorder.handle({"type": "mode", "mode": "follow", "level": 1})
            await driver.click("a.years__link")
            await driver.until(lambda m: m["recipe"]["follow"][1]["selector"] != "a.year__link")
            repicked = json.loads(json.dumps(recorder.recipe))
            await recorder.handle({"type": "selector", "target": "links", "selector": "a.pdf"})
            await recorder.handle({"type": "remove", "target": "next"})
            await recorder.handle({"type": "remove", "target": "links"})
            return removed, undone, repicked, recorder.recipe
        finally:
            await recorder.close()

    removed, undone, repicked, last = asyncio.run(scenario())
    assert removed["follow"] == [recipe["follow"][1]] and removed["next"]["level"] == 1, "the later levels move up"
    assert undone["follow"] == recipe["follow"] and undone["next"]["level"] == 2
    assert len(repicked["follow"]) == 2 and repicked["follow"][1]["selector"].endswith("a.years__link")
    assert repicked["follow"][0] == recipe["follow"][0], "picked again: that level alone changes"
    assert last["links"] == {} and "next" not in last


@needs_chromium
def test_a_number_picked_as_the_next_page_proposes_numbered_pages():
    async def scenario():
        driver, recorder = await recording(Site())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/pages?page=1"})
            await driver.mode("next")
            await driver.click("nav.pager a.page >> nth=1")
            picked = await driver.until(lambda m: "next" in m["proposals"])
            await recorder.handle({"type": "next-kind", "kind": "link"})
            return picked, recorder.recipe["next"]
        finally:
            await recorder.close()

    picked, link = asyncio.run(scenario())
    proposal = picked["proposals"]["next"]
    assert picked["recipe"]["next"]["kind"] == "number" and "buttons" not in picked["recipe"]["next"]
    assert proposal["numbers"] == [1, 2, 3, 4] and proposal["current"] == 1 and proposal["following"] == 2
    assert "kind" not in link and link["selector"], "the link alone, as before"


@needs_chromium
def test_scroll_to_load_more_is_a_step_and_loads_the_page():
    async def scenario():
        _, recorder = await recording(Site())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/feed"})
            await recorder.handle({"type": "wheel", "dx": 0, "dy": 600})
            await recorder.handle({"type": "scroll"})
            assert recorder._page is not None
            return recorder.recipe["steps"], await recorder._page.eval_on_selector_all("a.post", "els => els.length")
        finally:
            await recorder.close()

    steps, posts = asyncio.run(scenario())
    assert steps == [{"action": "goto", "url": f"{SITE}/feed"}, {"action": "scroll"}], "the wheel is no step"
    assert posts == 35


async def trial_result(driver: Driver, recorder: Recorder, message: dict, stop: bool = False) -> dict:
    await recorder.handle({"type": "test", **message})
    for _ in range(600):
        if stop and any(m["type"] == "test-started" for m in driver.sent):
            stop = False
            await recorder.handle({"type": "test-stop"})
        done = [m for m in driver.sent if m["type"] == "test"]
        if done:
            return done[-1]["result"]
        await asyncio.sleep(0.1)
    raise AssertionError("the test did not end")


@needs_chromium
def test_the_recipes_test_walks_as_a_job_without_downloading():
    site = Site()
    recipe = {
        "start": f"{SITE}/select/mag/archiv",
        "follow": [{"selector": "a.years__link", "pattern": r"/archiv/(?P<year>\d{4})$"}],
        "links": {"selector": "a.year__link", "pattern": r"/archiv/(?P<year>\d{4})/(?P<issue>\d+)$"},
    }

    async def scenario():
        driver, recorder = await recording(site)
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/list?p=1"})
            home = recorder._page
            result = await trial_result(driver, recorder, {"recipe": recipe, "settings": {"path": "{year}/{text}"}})
            assert recorder._browser is not None
            contexts = len(recorder._browser.contexts)
            return result, recorder._page is home, contexts
        finally:
            await recorder.close()

    result, home, contexts = asyncio.run(scenario())
    assert home and contexts == 1, "the test's context is gone, the screen shows the recorder's page"
    assert result["ended"] == "complete" and result["files"] == 8 and result["problems"] == []
    assert [(page["level"], page["kind"]) for page in result["pages"]][:2] == [(0, "follow"), (1, "links")]
    assert [file["path"] for file in result["pages"][1]["files"]] == ["2026/Mag 2 2026", "2026/Mag 1 2026"]
    assert result["http"], "no steps: the job reads the pages over HTTP"
    assert not any("/download" in url for url in site.requests), "nothing downloaded"


@needs_chromium
def test_the_recipes_test_tells_its_problems_and_stops(monkeypatch):
    monkeypatch.setattr(site_module, "STEP_TIMEOUT", 1000)
    failing = {
        "steps": [{"action": "goto", "url": f"{SITE}/login"}, {"action": "click", "selector": "#gone"}],
        "links": {"selector": "a.file", "pattern": "pdf"},
    }
    nothing = {"start": f"{SITE}/list?p=1", "links": {"selector": "a.none", "pattern": "pdf"}}
    slow = {"steps": [{"action": "goto", "url": f"{SITE}/feed"}, {"action": "scroll"}], "links": {"selector": "a.post"}}

    async def scenario():
        driver, recorder = await recording(Site())
        try:
            failed = await trial_result(driver, recorder, {"recipe": failing})
            driver.sent.clear()
            empty = await trial_result(driver, recorder, {"recipe": nothing})
            driver.sent.clear()
            stopped = await trial_result(driver, recorder, {"recipe": slow}, stop=True)
            assert recorder._browser is not None
            return failed, empty, stopped, len(recorder._browser.contexts)
        finally:
            await recorder.close()

    failed, empty, stopped, contexts = asyncio.run(scenario())
    assert failed["ended"] == "failed" and failed["problems"][0].startswith("step 2 (click #gone) failed")
    assert failed["shot"]["url"] == f"{SITE}/login" and failed["shot"]["image"]
    assert empty["problems"][0].startswith("the listing (page 1") and "a.none matched nothing" in empty["problems"][0]
    assert stopped["ended"] == "stopped" and contexts == 1
