# SPDX-License-Identifier: AGPL-3.0-or-later
"""Steps inside frames and optional (best-effort) steps: the frame's path in the recipe, its
label, recording in a same-origin, a cross-origin and a nested frame (served by route
fulfilment, no network), where the person acts, picks refused inside a frame, replay in nested
frames and by the frame's address, optional steps skipped without a failure or a picture,
required ones failing as before, a stop never swallowed, and the recipe's test listing the
skipped ones apart."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import re

import pytest
from playwright.sync_api import Error as PlaywrightError
from test_browser import FREE, needs_chromium
from test_recorder_features import browser_source, paths, recording, settled, trial_result

from fake_site import SITE, Site
from harvester.recipe import check_recipe, frame_address, step_label, steps_from_events
from harvester.sources import SourceError, Stopped
from harvester.sources import site as site_module

CONSENT = [{"selector": 'iframe[title="Consent"]', "url": "https://consent.example/cmp"}]
DETAILS = CONSENT + [
    {"selector": 'iframe[name="details"]', "url": "https://cmp-cdn.example/details", "name": "details"}
]
LISTING = [{"action": "goto", "url": f"{SITE}/list?p=1"}]
LINKS = {"selector": "a.file", "pattern": r"\.pdf$"}


def test_a_frames_path_is_kept_labelled_and_checked():
    events = [
        {"action": "goto", "url": f"{SITE}/framed"},
        {"action": "click", "selector": "button.accept", "text": "Akzeptieren", "frame": CONSENT},
        {"action": "fill", "selector": "#who", "value": "a", "form": "", "frame": [{"selector": "#f", "url": ""}]},
        {"action": "fill", "selector": "#who", "value": "b", "form": ""},
        {"action": "click", "selector": "a", "text": "Go"},
    ]
    steps = steps_from_events(events)
    assert steps[1]["frame"] == CONSENT and "frame" not in steps[0] and "frame" not in steps[4]
    assert [step.get("value") for step in steps[2:4]] == ["a", "b"], "the same field in a frame and on the page"
    assert step_label(steps[1]) == "click “Akzeptieren” in frame consent.example"
    assert step_label(steps[2]) == "fill in a value (#who) in frame (no address)"
    assert step_label(steps[4]) == "click “Go”", "a step on the page as before"
    assert frame_address("https://consent.example/cmp?v=3#x") == "https://consent.example/cmp"
    assert frame_address("about:blank") == ""

    def problems(step: dict) -> list[str]:
        return check_recipe({"steps": LISTING + [step], "links": LINKS})

    assert not problems({"action": "click", "selector": "a", "frame": DETAILS, "optional": True})
    assert problems({"action": "click", "selector": "a", "optional": "yes"}) == ["step 2: optional is true or false"]
    for frame in ("iframe", [{}], [{"selector": 3}]):
        assert problems({"action": "click", "selector": "a", "frame": frame})[0].startswith("step 2: the frame is")
    assert problems({"action": "goto", "url": f"{SITE}/x", "frame": CONSENT})[0].startswith("step 2: the frame is")


class FakePage:
    """A page whose actions are noted; a selector in failing fails as Playwright would."""

    def __init__(self, failing: tuple[str, ...] = ()):
        self.done: list[tuple] = []
        self.failing = failing

    def click(self, selector: str, **options) -> None:
        if selector in self.failing:
            raise PlaywrightError("Page.click: Timeout 5000ms exceeded.\nCall log: ...")
        self.done.append(("click", selector, options))

    def wait_for_timeout(self, _ms: float) -> None:
        pass

    def wait_for_load_state(self, *_args, **_options) -> None:
        pass


def fake_driver(page: FakePage) -> tuple[site_module.BrowserDriver, list[str]]:
    driver = object.__new__(site_module.BrowserDriver)
    driver.main = page
    shots: list[str] = []
    driver.keep_shot = lambda page, what: shots.append(what)
    driver.challenge = lambda page: None
    return driver, shots


def test_an_old_step_acts_on_the_page_as_before():
    page = FakePage()
    site_module.replay(page, {"action": "click", "selector": "#go"}, {})
    assert page.done == [("click", "#go", {})], "no frame, no time limit of its own"
    site_module.replay(page, {"action": "click", "selector": "#go", "optional": True}, {})
    assert page.done[-1][2]["timeout"] <= site_module.OPTIONAL_TIMEOUT


def test_an_optional_step_that_fails_is_skipped_a_required_one_fails():
    driver, shots = fake_driver(FakePage(failing=("#banner",)))
    steps = [
        {"action": "click", "selector": "#banner", "text": "Accept", "optional": True},
        {"action": "click", "selector": "#go"},
    ]
    driver.sign_in(steps, {}, None, "")
    assert driver.skipped == [{"number": 1, "label": "click “Accept”", "why": "Timeout 5000ms exceeded."}]
    assert site_module.skipped_line(driver.skipped[0]) == (
        "step 1 (click “Accept”, optional) skipped: Timeout 5000ms exceeded."
    )
    assert driver.main.done == [("click", "#go", {})] and not shots, "the run goes on, no picture"

    required = [{"action": "click", "selector": "#banner", "text": "Accept"}]
    with pytest.raises(SourceError, match=re.escape("step 1 (click “Accept”) failed")):
        driver.sign_in(required, {}, None, "")
    assert shots == ["step 1 (click “Accept”) failed"]


def test_a_stop_during_an_optional_step_is_never_swallowed(monkeypatch):
    def replay(page, step, secrets, repaired=None):  # pylint: disable=unused-argument
        raise Stopped("stopped")

    monkeypatch.setattr(site_module, "replay", replay)
    driver, shots = fake_driver(FakePage())
    with pytest.raises(Stopped):
        driver.sign_in([{"action": "click", "selector": "#banner", "optional": True}], {}, None, "")
    assert not shots and not driver.skipped


# In the browser.


async def click_in(recorder, *frames: str, selector: str) -> None:
    """Clicks an element inside the frames (their elements' selectors, from the page down) with
    the recorder's mouse, as the person does on the screen."""
    assert recorder._page is not None
    scope = recorder._page
    for frame in frames:
        scope = scope.frame_locator(frame)
    box = await scope.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    for event in ("move", "down", "up"):
        await recorder.handle({"type": "mouse", "event": event, "x": x, "y": y})


async def frames_loaded(recorder) -> None:
    assert recorder._page is not None
    await settled(recorder, r"/framed")
    await recorder._page.frame_locator('iframe[title="Consent"]').frame_locator("iframe").locator("#more").wait_for()


@needs_chromium
def test_steps_in_frames_are_recorded_with_their_path():
    async def scenario():
        driver, recorder = await recording(Site())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/framed"})
            await frames_loaded(recorder)
            await click_in(recorder, "#login-frame", selector="#inner")
            await driver.until(lambda m: len(m["recipe"]["steps"]) == 2)
            await click_in(recorder, 'iframe[title="Consent"]', selector="button.accept")
            await driver.until(lambda m: len(m["recipe"]["steps"]) == 3)
            await click_in(recorder, 'iframe[title="Consent"]', "iframe", selector="#more")
            await driver.until(lambda m: len(m["recipe"]["steps"]) == 4)
            await recorder.handle({"type": "mouse", "event": "move", "x": 600, "y": 10})
            for _ in range(50):
                if [m for m in driver.sent if m["type"] == "where"][-1]["frame"] is None:
                    break
                await asyncio.sleep(0.05)
            await recorder.handle({"type": "optional", "step": 2, "optional": True})
            await recorder.handle({"type": "optional", "step": 9, "optional": True})
            optional = recorder.recipe["steps"]
            await recorder.handle({"type": "undo"})
            await recorder.handle({"type": "undo"})
            return optional, recorder.recipe["steps"], [m["frame"] for m in driver.sent if m["type"] == "where"]
        finally:
            await recorder.close()

    steps, undone, places = asyncio.run(scenario())
    assert "frame" not in steps[0]
    login = {"selector": "#login-frame", "url": f"{SITE}/frame/form", "name": "login-frame"}
    assert steps[1]["frame"] == [login], "a frame without a name of its own goes by its id"
    assert steps[2]["frame"] == CONSENT and steps[2]["text"] == "Akzeptieren", "a cross-origin frame reports too"
    assert steps[3]["frame"] == DETAILS, "a frame in a frame: the path from the page down"
    assert steps[2]["optional"] is True and not any(step.get("optional") for step in steps[:2] + steps[3:])
    assert len(undone) == 2 and not any(step.get("optional") for step in undone)
    assert {"host": "consent.example", "url": "https://consent.example/cmp", "level": 1} in places
    assert {"host": "cmp-cdn.example", "url": "https://cmp-cdn.example/details", "level": 2} in places
    assert places[-1] is None, "back on the page"


@needs_chromium
def test_picks_inside_a_frame_are_refused_with_a_message():
    async def scenario():
        driver, recorder = await recording(Site())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/framed"})
            await frames_loaded(recorder)
            await driver.mode("links")
            await click_in(recorder, "#login-frame", selector="a.file")
            for _ in range(100):
                errors = [m["message"] for m in driver.sent if m["type"] == "error"]
                if errors:
                    return errors, recorder.recipe
                await asyncio.sleep(0.05)
            raise AssertionError("no message")
        finally:
            await recorder.close()

    errors, recipe = asyncio.run(scenario())
    assert errors[0].startswith("Picks inside a frame (site.example) are not supported")
    assert errors[0].endswith(f"{SITE}/frame/form")
    assert recipe["links"] == {} and recipe["steps"] == [{"action": "goto", "url": f"{SITE}/framed"}]


@needs_chromium
def test_steps_replay_in_nested_frames_and_find_a_frame_by_its_address(monkeypatch):
    site = Site()
    steps = [
        {"action": "goto", "url": f"{SITE}/framed?v=2"},
        # The query of the frame's address changed since the recording (v=1): no matter.
        {"action": "click", "selector": "#inner", "frame": [{"selector": "#login-frame", "url": f"{SITE}/frame/form"}]},
        {"action": "click", "selector": "#more", "frame": DETAILS},
        # A selector that no longer finds the frame: its address does.
        {
            "action": "click",
            "selector": "button.accept",
            "frame": [{"selector": "iframe#gone", "url": CONSENT[0]["url"]}],
        },
        *LISTING,
    ]
    assert len(paths(browser_source(site, monkeypatch, {"steps": steps, "links": LINKS}))) == 2
    done = [url for url in site.requests if "/done/" in url]
    assert done == [f"{SITE}/done/inner", "https://cmp-cdn.example/done/details", "https://consent.example/done/accept"]
    assert site_module.browser.free_browsers() == FREE


def banner_recipe(optional: bool) -> dict:
    click = {"action": "click", "selector": "button.accept", "text": "Akzeptieren", "frame": CONSENT}
    steps = [{"action": "goto", "url": f"{SITE}/framed?bare=1"}, click | ({"optional": True} if optional else {})]
    return {"steps": steps + LISTING, "links": LINKS}


@needs_chromium
def test_an_optional_step_in_a_frame_not_there_is_skipped_and_logged(monkeypatch):
    monkeypatch.setattr(site_module, "STEP_TIMEOUT", 1500)
    monkeypatch.setattr(site_module, "OPTIONAL_TIMEOUT", 1500)
    source = browser_source(Site(), monkeypatch, banner_recipe(optional=True))
    log: list[tuple[str, str]] = []
    source.report = lambda level, text: log.append((level, text))
    assert len(paths(source)) == 2
    assert log == [
        (
            "info",
            "step 2 (click “Akzeptieren” in frame consent.example, optional) skipped: "
            "the frame https://consent.example/cmp (level 1) is not on the page",
        )
    ]
    assert source.shot is None, "no picture of a failure: there was none"

    required = browser_source(Site(), monkeypatch, banner_recipe(optional=False))
    with pytest.raises(SourceError, match=re.escape("step 2 (click “Akzeptieren” in frame consent.example) failed")):
        paths(required)
    assert required.shot and required.shot["what"].startswith("step 2")
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_the_recipes_test_lists_skipped_optional_steps_apart(monkeypatch):
    monkeypatch.setattr(site_module, "OPTIONAL_TIMEOUT", 1500)

    async def scenario():
        driver, recorder = await recording(Site())
        try:
            return await trial_result(driver, recorder, {"recipe": banner_recipe(optional=True)})
        finally:
            await recorder.close()

    result = asyncio.run(scenario())
    assert result["ended"] == "complete" and result["problems"] == [] and result["files"] == 2
    assert [(skipped["number"], skipped["label"]) for skipped in result["skipped"]] == [
        (2, "click “Akzeptieren” in frame consent.example")
    ]
    assert result.get("shot") is None
    assert json.dumps(result)
