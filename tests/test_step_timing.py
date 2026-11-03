# SPDX-License-Identifier: AGPL-3.0-or-later
"""A step's pause before it and its retries: the recipe's checks and the Steps list's edits,
the replay's tries (again until one passes, all spent, an optional step skipped after them, a
stop never tried again), pauses and delays that a stop ends at once (a run's and the recorder's
test's), the step events the replay tells and the run's log lines of them, recipe files that
keep both; in Chromium, the recorder's test telling the page each step it plays, and the page
marking them on its Steps (the step playing, its attempts and pause, passed, skipped, failed)
with the failed one opening its editor."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import threading
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from test_browser import needs_chromium
from test_frames_optional import FakePage, fake_driver
from test_page import RECIPE, fixture_page, receive, sent  # pylint: disable=unused-import
from test_recipe_files import exported, form, imported
from test_recorder_features import recording, trial_result

from fake_site import SITE, Site
from harvester import recipe as recipes
from harvester.browser import trial
from harvester.recipe import check_recipe, edit_step
from harvester.sources import Stopped
from harvester.sources import base
from harvester.sources import site as site_module

LISTING = [{"action": "goto", "url": f"{SITE}/list?p=1"}]
LINKS = {"selector": "a.file", "pattern": r"\.pdf$"}


def problems(step: dict) -> list[str]:
    return check_recipe({"steps": LISTING + [step], "links": LINKS})


def test_a_steps_pause_and_retry_are_checked():
    click = {"action": "click", "selector": "#go"}
    assert not problems(click | {"pause": 0.5, "retry": {"times": 3, "delay": 0}})
    assert not problems(click | {"pause": 60, "retry": {"times": 10}}), "the delay has its default"
    for pause in (0, -1, 61, "2", True, None, float("nan")):
        assert problems(click | {"pause": pause}) == ["step 2: the pause is a number of seconds above 0, at most 60"]
    for retry in (3, {}, {"times": 0}, {"times": 11}, {"times": 2.5}, {"times": True}, {"times": 2, "delay": 61}):
        assert problems(click | {"retry": retry})[0].startswith("step 2: retry is {"), retry
    assert problems(click | {"retry": {"times": 2, "delay": -1}})
    assert problems(click | {"retry": {"times": 2, "wait": 1}}), "no other keys"
    assert recipes.step_pause(click | {"pause": 2}) == 2.0 and recipes.step_pause(click) == 0
    assert recipes.step_retry(click | {"retry": {"times": 3}}) == (3, recipes.RETRY_DELAY)
    assert recipes.step_retry(click | {"retry": {"times": 99}}) == (0, 0.0), "out of bounds: none"


def test_the_steps_list_sets_and_removes_a_pause_and_retries():
    step = {"action": "click", "selector": "#go"}
    edit_step(step, {"pause": 2.5, "retry": {"times": 2, "delay": 1}})
    assert step == {"action": "click", "selector": "#go", "pause": 2.5, "retry": {"times": 2, "delay": 1}}
    edit_step(step, {"pause": 99, "retry": {"times": 0}})
    assert step["pause"] == 2.5 and step["retry"] == {"times": 2, "delay": 1}, "out of bounds: unchanged"
    edit_step(step, {"pause": None, "retry": None})
    assert step == {"action": "click", "selector": "#go"}


class Flaky(FakePage):
    """A page whose element shows only after a number of failed tries."""

    def __init__(self, fails: int):
        super().__init__()
        self.fails = fails
        self.tries = 0

    def click(self, selector: str, **options) -> None:
        self.tries += 1
        if self.tries <= self.fails:
            raise PlaywrightError(f"Page.click: Timeout 30000ms exceeded (try {self.tries}).\nCall log: ...")
        super().click(selector, **options)


def timed_driver(page: FakePage):
    driver, shots = fake_driver(page)
    naps: list[float] = []
    events: list[dict] = []
    driver.nap = naps.append
    driver.told = events.append
    return driver, shots, naps, events


def test_a_step_is_tried_again_until_it_passes():
    driver, shots, naps, events = timed_driver(Flaky(1))
    driver.sign_in(
        [{"action": "click", "selector": "#go", "pause": 1.5, "retry": {"times": 2, "delay": 3}}], {}, None, ""
    )
    assert driver.main.tries == 2 and not shots and not driver.skipped
    assert naps == [1.5, 3.0], "the pause, then one delay"
    assert [(event["state"], event.get("attempt")) for event in events] == [
        ("pausing", None),
        ("playing", 1),
        ("retrying", 2),
        ("playing", 2),
        ("passed", None),
    ]
    assert events[0] == {"number": 1, "label": "click #go", "state": "pausing", "seconds": 1.5}
    assert events[2] | {"why": ""} == {
        "number": 1,
        "label": "click #go",
        "state": "retrying",
        "attempt": 2,
        "of": 3,
        "seconds": 3.0,
        "why": "",
    }
    assert events[2]["why"].startswith("Timeout 30000ms exceeded (try 1)")
    assert site_module.step_line(events[0]) == "step 1 (click #go): pausing 1.5 s before it"
    assert site_module.step_line(events[2]) == (
        "step 1 (click #go) failed, trying again in 3 s (attempt 2 of 3): Timeout 30000ms exceeded (try 1)."
    )
    assert site_module.step_line(events[-1]) == "", "no line for a step that simply passes"


def test_a_step_fails_once_its_tries_are_spent_an_optional_one_is_skipped():
    driver, shots, naps, events = timed_driver(Flaky(9))
    with pytest.raises(site_module.StepFailed, match="step 1 \\(click #go\\) failed: Page.click: Timeout"):
        driver.sign_in([{"action": "click", "selector": "#go", "retry": {"times": 2}}], {}, None, "")
    assert driver.main.tries == 3 and naps == [2.0, 2.0] and shots == ["step 1 (click #go) failed"]
    assert events[-1]["state"] == "failed" and events[-1]["why"].startswith("Timeout")

    driver, shots, naps, events = timed_driver(Flaky(9))
    steps = [{"action": "click", "selector": "#banner", "optional": True, "retry": {"times": 1, "delay": 0}}]
    driver.sign_in(steps, {}, None, "")
    assert driver.main.tries == 2 and naps == [0.0] and not shots
    assert driver.skipped[0]["number"] == 1 and events[-1]["state"] == "skipped"


def test_a_stop_is_never_tried_again(monkeypatch):
    tries = []

    def replay(page, step, secrets, repaired=None):  # pylint: disable=unused-argument
        tries.append(step)
        raise Stopped("stopped")

    monkeypatch.setattr(site_module, "replay", replay)
    driver, shots, naps, events = timed_driver(FakePage())
    with pytest.raises(Stopped):
        driver.sign_in([{"action": "click", "selector": "#go", "retry": {"times": 5}}], {}, None, "")
    assert len(tries) == 1 and not naps and not shots
    assert [event["state"] for event in events] == ["playing"], "neither failed nor skipped"


def test_a_stop_during_a_retrys_delay_ends_it():
    driver, shots, _, events = timed_driver(Flaky(9))

    def stopped(_seconds: float) -> None:
        raise Stopped("stopped")

    driver.nap = stopped
    with pytest.raises(Stopped):
        driver.sign_in([{"action": "click", "selector": "#go", "retry": {"times": 5}}], {}, None, "")
    assert driver.main.tries == 1 and not shots and events[-1]["state"] == "retrying"


def stop_soon(event: threading.Event) -> None:
    threading.Timer(0.2, event.set).start()


def test_a_pause_ends_at_once_on_the_runs_stop():
    driver, _, _, _ = timed_driver(FakePage())
    driver.nap = site_module.BrowserDriver.nap
    stop = threading.Event()
    base.watch_stop(stop)
    try:
        stop_soon(stop)
        began = time.monotonic()
        with pytest.raises(Stopped):
            driver.sign_in([{"action": "click", "selector": "#go", "pause": 60}], {}, None, "")
        assert time.monotonic() - began < 5 and driver.main.done == []
    finally:
        base.watch_stop(None)
    base.nap(0.01)  # without a stop to watch: a plain wait


def test_a_pause_in_the_recorders_test_ends_with_its_stop_and_its_time():
    stop = threading.Event()
    blocking = trial.Blocking(object(), asyncio.new_event_loop(), stop, time.monotonic() + 60)
    stop_soon(stop)
    began = time.monotonic()
    with pytest.raises(Stopped, match="stopped"):
        blocking.nap(60)
    assert time.monotonic() - began < 5
    late = trial.Blocking(object(), asyncio.new_event_loop(), threading.Event(), time.monotonic() + 0.2)
    with pytest.raises(Stopped, match="time"):
        late.nap(60)


def test_a_runs_log_tells_the_pauses_and_retries():
    source = site_module.Site({"recipe": json.dumps({"steps": LISTING, "links": LINKS})}, {}, None)
    lines: list[tuple[str, str]] = []
    source.report = lambda level, text: lines.append((level, text))
    source._step_told(site_module.step_event(2, {"action": "scroll"}, "pausing", seconds=5))
    source._step_told(site_module.step_event(2, {"action": "scroll"}, "passed"))
    assert lines == [("info", "step 2 (scroll to load more): pausing 5 s before it")]


def test_recipe_files_keep_a_steps_pause_and_retry():
    timed = RECIPE | {
        "steps": [
            {"action": "goto", "url": "https://site.example/login"},
            {"action": "click", "selector": "#go", "pause": 2, "retry": {"times": 3, "delay": 1.5}},
        ]
    }
    file = exported(options={"recipe": json.dumps(timed), "path": "{year}/{text}"})
    assert file["options"]["recipe"]["steps"][1] == timed["steps"][1]
    job = imported(file)
    assert json.loads(job["options"]["recipe"])["steps"][1] == timed["steps"][1]
    assert form().options, "the form's other fields as before"
    wrong = json.loads(json.dumps(file))
    wrong["options"]["recipe"]["steps"][1]["retry"] = {"times": 50}
    with pytest.raises(Exception, match="retry is"):
        imported(wrong)


# In the browser.


@needs_chromium
def test_the_recorders_test_tells_the_page_each_step_it_plays(monkeypatch):
    monkeypatch.setattr(site_module, "STEP_TIMEOUT", 1000)
    monkeypatch.setattr(site_module, "OPTIONAL_TIMEOUT", 1000)
    recipe = {
        "steps": [
            {"action": "goto", "url": f"{SITE}/list?p=1", "pause": 0.2},
            {"action": "click", "selector": "#gone", "optional": True, "retry": {"times": 1, "delay": 0}},
            {"action": "click", "selector": "#missing", "retry": {"times": 2, "delay": 0.1}},
        ],
        "links": LINKS,
    }

    async def scenario():
        driver, recorder = await recording(Site())
        try:
            result = await trial_result(driver, recorder, {"recipe": recipe})
            return result, driver.sent
        finally:
            await recorder.close()

    result, messages = asyncio.run(scenario())
    steps = [(m["number"], m["state"], m.get("attempt")) for m in messages if m["type"] == "test-step"]
    assert steps == [
        (1, "pausing", None),
        (1, "playing", 1),
        (1, "passed", None),
        (2, "playing", 1),
        (2, "retrying", 2),
        (2, "playing", 2),
        (2, "skipped", None),
        (3, "playing", 1),
        (3, "retrying", 2),
        (3, "playing", 2),
        (3, "retrying", 3),
        (3, "playing", 3),
        (3, "failed", None),
    ]
    order = [m["type"] for m in messages if m["type"] in ("test-started", "test-step", "test")]
    assert order[0] == "test-started" and order[-1] == "test", "the steps come between start and result"
    assert result["ended"] == "failed" and result["problems"][0].startswith("step 3 (click #missing) failed")


@needs_chromium
def test_the_recorders_steps_show_the_test_playing_them(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    steps = [
        {"action": "goto", "url": "https://site.example/login"},
        {"action": "click", "selector": "#banner", "optional": True},
        {"action": "click", "selector": "#go", "retry": {"times": 2, "delay": 1}},
    ]
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": steps}, "proposals": {}})
    items = page.locator(".harvester-steps ol > li")
    assert "retry 2×" in items.nth(2).locator("summary").inner_text()

    def step(number: int, state: str, **more) -> None:
        receive(page, {"type": "test-step", "number": number, "label": "", "state": state, **more})

    receive(page, {"type": "test-started", "as_job": False})
    step(1, "pausing", seconds=30)
    assert "pausing 30 s" in items.nth(0).inner_text()
    assert "harvester-step-current" in str(items.nth(0).get_attribute("class"))
    step(1, "playing", attempt=1, of=1)
    step(1, "passed")
    step(2, "skipped", why="Timeout")
    step(3, "playing", attempt=1, of=3)
    step(3, "retrying", attempt=2, of=3, seconds=1, why="Timeout")
    assert "attempt 2 of 3 in" in items.nth(2).inner_text()
    step(3, "playing", attempt=3, of=3)
    assert "playing (attempt 3 of 3)" in items.nth(2).inner_text()
    assert "harvester-step-current" not in str(items.nth(0).get_attribute("class"))
    step(3, "failed", why="Timeout")
    result = {"pages": [], "problems": ["step 3 (click #go) failed: Timeout"], "files": 0, "ended": "failed"}
    receive(page, {"type": "test", "result": result})
    assert "✓ passed" in items.nth(0).inner_text() and "skipped (optional)" in items.nth(1).inner_text()
    assert "✗ failed" in items.nth(2).inner_text()
    assert "harvester-step-current" in str(items.nth(2).get_attribute("class")), "the failed step stays highlighted"

    items.nth(2).locator("span").nth(1).click()
    assert items.nth(2).locator("details.harvester-step-timing").get_attribute("open") is not None
    pause = items.nth(2).get_by_role("spinbutton", name="The pause before (seconds) of step 3")
    pause.fill("4")
    pause.dispatch_event("change")
    assert sent(page)[-1] == {"type": "step-edit", "step": 2, "pause": 4}
    tries = items.nth(2).get_by_role("spinbutton", name="Tries again of step 3")
    tries.fill("0")
    tries.dispatch_event("change")
    assert sent(page)[-1] == {"type": "step-edit", "step": 2, "retry": None}
