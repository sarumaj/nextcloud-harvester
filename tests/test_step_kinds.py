# SPDX-License-Identifier: AGPL-3.0-or-later
"""The kinds of step other recorders have that a recipe has too: double and right clicks, the
answer to a dialog a click opens (OK with a prompt's text, or Cancel), a fill typed key by key, a
key pressed on the page, a step "wait" for an element to show (with a text) or go, and a fill of
a list (Chrome's change of a <select>). Their checks, labels and the Steps list's edits; what
recorder.js records of them (route fulfilment, no network); their replay in Chromium."""

# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from test_browser import needs_chromium

from harvester.browser import SCRIPT, launch_options
from harvester.browser.steps import StepList
from harvester.recipe import check_recipe, edit_step, link_waits, step_label, steps_from_events
from harvester.sources import site as site_module

SITE = "https://kinds.example"
LINKS = {"selector": "a.file"}
PAGE = """<html><body>
<button id="twice" ondblclick="out.textContent = 'double'">Twice</button>
<div id="menu" oncontextmenu="event.preventDefault(); out.textContent = 'menu'">Right here</div>
<button id="sure" onclick="out.textContent = confirm('Delete?') ? 'yes' : 'no'">Delete</button>
<button id="ask" onclick="out.textContent = prompt('Name?', 'x')">Ask</button>
<input id="code" oninput="typed.textContent = (typed.textContent || '') + event.data">
<select id="lang"><option value="en">English</option><option value="de">Deutsch</option></select>
<button id="later" onclick="setTimeout(() => { busy.remove(); result.hidden = false }, 300)">Search</button>
<p id="busy">Please wait</p><p id="result" hidden>3 results</p>
<p id="out"></p><p id="typed"></p>
<script>document.addEventListener('keydown', (e) => { if (e.key === 'Escape') out.textContent = 'escaped' })</script>
</body></html>"""


def checked(*steps: dict) -> list[str]:
    return check_recipe({"steps": [{"action": "goto", "url": f"{SITE}/"}, *steps], "links": LINKS})


def test_the_new_kinds_are_checked_and_labelled():
    assert not checked(
        {"action": "click", "selector": "#a", "count": 2},
        {"action": "click", "selector": "#a", "button": "right", "dialog": "accept", "prompt": "Ann"},
        {"action": "press", "key": "Escape"},
        {"action": "press", "selector": "#q", "key": "Enter", "dialog": "dismiss"},
        {"action": "fill", "selector": "#q", "value": "x", "typed": True},
        {"action": "wait", "selector": "#r", "text": "results", "timeout": 60},
        {"action": "wait", "selector": "#busy", "state": "hidden"},
    )
    assert checked({"action": "click", "selector": "#a", "count": 5}) == ["step 2: the count of clicks is 1 to 3"]
    assert checked({"action": "click", "selector": "#a", "button": "left-ish"})[0].startswith("step 2: the button")
    assert checked({"action": "fill", "selector": "#a", "value": "", "count": 2}) == [
        "step 2: only a click has a button and a count"
    ]
    assert checked({"action": "hover", "selector": "#a", "dialog": "accept"}) == [
        "step 2: only a click or a press answers a dialog"
    ]
    assert checked({"action": "click", "selector": "#a", "dialog": "yes"})[0].endswith("accept or dismiss")
    assert checked({"action": "wait", "selector": "#a", "timeout": 999})[0].startswith("step 2: the wait from 1")
    assert checked({"action": "wait", "selector": "#a", "state": "gone"})[0].endswith("visible or hidden")
    assert checked({"action": "wait"}) == ["step 2: no element"]
    assert checked({"action": "press", "key": "Escape", "frame": [{"selector": "iframe"}]})[0].startswith(
        "step 2: a key"
    )
    assert step_label({"action": "click", "selector": "#a", "count": 2, "text": "Go"}) == "double-click “Go”"
    assert step_label({"action": "click", "selector": "#a", "button": "right"}) == "right-click #a"
    assert step_label({"action": "press", "key": "Escape"}) == "press Escape"
    assert step_label({"action": "wait", "selector": "#r", "text": "3"}) == "wait for #r to show with “3”"
    assert step_label({"action": "wait", "selector": "#busy", "state": "hidden"}) == "wait for #busy to go"


def test_the_steps_list_sets_the_new_options():
    click = {"action": "click", "selector": "#a"}
    edit_step(click, {"click": "double"})
    assert click["count"] == 2 and "button" not in click
    edit_step(click, {"click": "right"})
    assert click["button"] == "right" and "count" not in click
    edit_step(click, {"dialog": "accept", "prompt": "Ann"})
    assert (click["dialog"], click["prompt"]) == ("accept", "Ann")
    edit_step(click, {"dialog": None})
    assert "dialog" not in click and "prompt" not in click
    fill = {"action": "fill", "selector": "#q", "value": "x"}
    edit_step(fill, {"typed": True, "dialog": "accept", "click": "double"})
    assert fill == {"action": "fill", "selector": "#q", "value": "x", "typed": True}, "what does not fit is left"
    wait = {"action": "wait", "selector": "#r"}
    edit_step(wait, {"state": "hidden", "text": "x", "timeout": 600})
    assert wait == {"action": "wait", "selector": "#r", "state": "hidden", "text": "x"}, "a time out of bounds is left"
    steps = StepList()
    steps.load([{"action": "goto", "url": f"{SITE}/"}, {"action": "click", "selector": "#later"}])
    assert steps.edit("wait-add", {"step": 1, "selector": "#result"})
    assert steps.all()[2] == {"action": "wait", "selector": "#result", "state": "visible"}
    assert not steps.edit("wait-add", {"step": 7, "selector": "#x"}) and not steps.edit("wait-add", {"step": 0})


def test_double_clicks_and_dialogs_change_the_step_before():
    events = [
        {"action": "click", "selector": "#twice", "text": "Twice"},
        {"action": "click", "selector": "#twice", "text": "Twice"},
        {"action": "dblclick", "selector": "#twice"},
        {"action": "click", "selector": "#sure", "text": "Delete"},
        {"action": "dialog", "accept": True},
        {"action": "click", "selector": "#menu", "text": "", "button": "right"},
    ]
    steps = steps_from_events(events)
    assert steps == [
        {"action": "click", "selector": "#twice", "text": "Twice", "count": 2},
        {"action": "click", "selector": "#sure", "text": "Delete", "dialog": "accept"},
        {"action": "click", "selector": "#menu", "text": "", "button": "right"},
    ]
    assert steps_from_events([{"action": "dialog"}]) == [], "a dialog before any step changes nothing"
    navigating = StepList()
    navigating.record({"action": "click", "selector": "#sure"})
    navigating.record({"action": "dialog", "accept": True})
    assert navigating.navigation() is not None, "the click may still navigate after its dialog"


def test_a_wait_for_an_element_to_go_is_no_element_to_wait_for():
    steps = [
        {"action": "click", "selector": "#later", "wait": {"url": "^https://kinds\\.example/"}},
        {"action": "wait", "selector": "#busy", "state": "hidden"},
    ]
    link_waits(steps)
    assert "selector" not in steps[0]["wait"]


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options(9))
        yield browser
        browser.close()


def page_of(chromium):
    context = chromium.new_context()
    context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=PAGE))
    context.set_default_timeout(5000)
    page = context.new_page()
    page.goto(f"{SITE}/")
    return page


def out(page) -> str:
    return page.inner_text("#out")


@needs_chromium
def test_the_new_kinds_replay(chromium):
    page = page_of(chromium)
    try:
        site_module.replay(page, {"action": "click", "selector": "#twice", "count": 2}, {})
        assert out(page) == "double"
        site_module.replay(page, {"action": "click", "selector": "#menu", "button": "right"}, {})
        assert out(page) == "menu"
        site_module.replay(page, {"action": "click", "selector": "#sure", "dialog": "accept"}, {})
        assert out(page) == "yes"
        site_module.replay(page, {"action": "click", "selector": "#sure"}, {})
        assert out(page) == "no", "without an answer a dialog is dismissed, as always"
        site_module.replay(page, {"action": "click", "selector": "#ask", "dialog": "accept", "prompt": "Ann"}, {})
        assert out(page) == "Ann"
        site_module.replay(page, {"action": "fill", "selector": "#code", "value": "123", "typed": True}, {})
        assert page.inner_text("#typed") == "123", "key by key: an input event each"
        site_module.replay(page, {"action": "fill", "selector": "#lang", "value": "de"}, {})
        assert page.eval_on_selector("#lang", "(e) => e.value") == "de", "a list's change chooses its option"
        site_module.replay(page, {"action": "press", "key": "Escape"}, {})
        assert out(page) == "escaped"
        site_module.replay(page, {"action": "click", "selector": "#later"}, {})
        site_module.replay(page, {"action": "wait", "selector": "#busy", "state": "hidden", "timeout": 5}, {})
        site_module.replay(page, {"action": "wait", "selector": "p", "text": "3 results", "timeout": 5}, {})
        with pytest.raises(PlaywrightError):
            site_module.replay(page, {"action": "wait", "selector": "#result", "text": "none", "timeout": 1}, {})
    finally:
        page.context.close()


@needs_chromium
def test_double_and_right_clicks_and_dialogs_are_recorded(chromium):
    events: list[dict] = []
    context = chromium.new_context()
    try:
        context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=PAGE))
        context.expose_binding("__harvesterReport", lambda _source, event: events.append(event))
        context.add_init_script(SCRIPT)
        page = context.new_page()
        page.goto(f"{SITE}/")
        page.evaluate("window.__harvesterMode && window.__harvesterMode('record')")
        page.dblclick("#twice")
        page.click("#menu", button="right")
        page.wait_for_timeout(300)
    finally:
        context.close()
    steps = steps_from_events([event for event in events if event.get("action") not in ("cursor", "where", "mode")])
    kinds = [(step["action"], step["selector"], step.get("count"), step.get("button")) for step in steps]
    assert kinds == [("click", "#twice", 2, None), ("click", "#menu", None, "right")]
