# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recipe's steps from and to Chrome DevTools Recorder's recordings (chromerecorder.py): a
recording as DevTools exports it (a sign-in, a double click, a key chord, a wait, a popup, a
frame, steps a recipe has none for) made steps, its password never kept; selectors both ways; a
recipe exported and imported again unchanged, Harvester's own steps as custom steps, never a
credential; and the routes the job's form uses."""

# pylint: disable=missing-function-docstring

import asyncio
import json

import pytest
from fastapi import HTTPException

from harvester import chromerecorder as chrome
from harvester.recipe import check_recipe
from harvester.routes import recipes as routes

# As Chrome's Recorder exports a sign-in (DevTools, Recorder, Export as JSON).
RECORDING = {
    "title": "Sign in to the portal",
    "steps": [
        {
            "type": "setViewport",
            "width": 1366,
            "height": 768,
            "deviceScaleFactor": 1,
            "isMobile": False,
            "hasTouch": False,
            "isLandscape": False,
        },
        {
            "type": "navigate",
            "url": "https://portal.example/login",
            "assertedEvents": [{"type": "navigation", "url": "https://portal.example/login", "title": "Sign in"}],
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                ['aria/Accept all[role="button"]'],
                ["#consent > button.accept"],
                ['xpath///*[@id="consent"]/button'],
                ["pierce/#consent > button.accept"],
                ["text/Accept all"],
            ],
            "frame": [0],
            "offsetX": 41.5,
            "offsetY": 12,
        },
        {
            "type": "change",
            "value": "anna@example.org",
            "selectors": [["aria/E-mail"], ["#email"]],
            "target": "main",
        },
        {
            "type": "change",
            "value": "hunter2-secret",
            "selectors": [["#password"], ["pierce/#password"]],
            "target": "main",
        },
        {"type": "keyDown", "target": "main", "key": "Enter"},
        {
            "type": "keyUp",
            "key": "Enter",
            "target": "main",
            "assertedEvents": [{"type": "navigation", "url": "https://portal.example/home/42", "title": ""}],
        },
        {
            "type": "waitForElement",
            "selectors": [['aria/Invoices[role="link"]']],
            "visible": True,
            "timeout": 15000,
        },
        {"type": "doubleClick", "selectors": [["div.year-2024"]], "offsetX": 3, "offsetY": 3},
        {"type": "click", "selectors": [["tr.row"]], "button": "secondary", "offsetX": 1, "offsetY": 1},
        {"type": "keyDown", "key": "Control"},
        {"type": "keyDown", "key": "a"},
        {"type": "keyUp", "key": "a"},
        {"type": "keyUp", "key": "Control"},
        {"type": "scroll", "x": 0, "y": 640},
        {"type": "waitForExpression", "expression": "document.querySelectorAll('tr').length > 3"},
        {"type": "click", "selectors": [["login-box", "#sso"]], "offsetX": 2, "offsetY": 2},
        {
            "type": "click",
            "target": "https://sso.example/authorize",
            "selectors": [["#allow"]],
            "offsetX": 2,
            "offsetY": 2,
        },
        {"type": "click", "target": "main", "selectors": [["a.export"]], "offsetX": 2, "offsetY": 2},
        {"type": "waitForElement", "selectors": [["#spinner"]], "count": 0, "operator": "=="},
        {"type": "customStep", "name": "lighthouse", "parameters": {}},
        {"type": "close", "target": "main"},
    ],
}


def imported():
    return chrome.import_text(json.dumps(RECORDING))


def test_a_recording_becomes_steps_without_its_password():
    steps, viewport, warnings = imported()
    assert viewport == {"width": 1366, "height": 768}
    assert steps[0] == {"action": "goto", "url": "https://portal.example/login"}
    consent = steps[1]
    assert consent["action"] == "click" and consent["text"] == "Accept all"
    assert consent["selector"] == 'internal:role=button[name="Accept all"s]'
    assert consent["alternatives"] == [
        "#consent > button.accept",
        'xpath=//*[@id="consent"]/button',
        'text="Accept all"',
    ], "pierce/ is the CSS again: each once"
    assert consent["frame"] == [{"selector": ":is(iframe, frame) >> nth=0"}]
    user, password, enter = steps[2], steps[3], steps[4]
    assert user == {
        "action": "fill",
        "selector": '[aria-label="E-mail"]',
        "alternatives": ['text="E-mail"', "#email"],
        "credential": "username",
    }
    assert password == {"action": "fill", "selector": "#password", "credential": "password"}
    assert "hunter2" not in json.dumps(steps), "the password typed is never kept"
    assert enter["action"] == "press" and enter["key"] == "Enter" and enter["selector"] == "#password"
    assert enter["wait"]["url"].startswith("^https://portal\\.example/home/\\d+"), "the address it led to, numbers open"
    assert steps[5] == {
        "action": "wait",
        "selector": 'internal:role=link[name="Invoices"s]',
        "state": "visible",
        "timeout": 15,
    }
    assert steps[6] == {"action": "click", "selector": "div.year-2024", "text": "", "count": 2}
    assert steps[7] == {"action": "click", "selector": "tr.row", "text": "", "button": "right"}
    assert steps[8] == {"action": "press", "key": "Control+a", "selector": "tr.row"}, "the modifier held"
    assert steps[9]["selector"] == "login-box >> #sso", "through a shadow root"
    assert [step["action"] for step in steps[10:]] == ["popup", "click", "opener", "click", "wait"]
    assert steps[-1] == {"action": "wait", "selector": "#spinner", "state": "hidden"}
    said = " ".join(warnings)
    for kind in ("scroll", "waitForExpression", "custom step lighthouse", "close"):
        assert f"({kind})" in said
    assert "password typed in the recording is not kept" in said
    recipe = {
        "steps": steps,
        "start": "https://portal.example/invoices",
        "links": {"selector": "a.file"},
        "viewport": viewport,
    }
    assert check_recipe(recipe) == [], "a recipe the job can run"


@pytest.mark.parametrize(
    "chrome_selector, ours",
    [
        ("#id > .a", ["#id > .a"]),
        ("pierce/.deep", [".deep"]),
        ("xpath///a[1]", ["xpath=//a[1]"]),
        ("text/Sign in", ['text="Sign in"']),
        ('aria/Sign in[role="button"]', ['internal:role=button[name="Sign in"s]']),
        ('aria/Say "hi"', ['[aria-label="Say \\"hi\\""]', 'text="Say \\"hi\\""']),
        (["my-app", "login-box", "#go"], ["my-app >> login-box >> #go"]),
        ([], []),
        (7, []),
    ],
)
def test_chromes_selectors_are_playwrights(chrome_selector, ours):
    assert chrome.from_chrome_selector(chrome_selector) == ours


def test_playwrights_selectors_are_chromes_where_it_has_them():
    assert chrome.to_chrome_selector('internal:role=button[name="Go \\"now\\""s]') == 'aria/Go "now"[role="button"]'
    assert chrome.to_chrome_selector('a:text-is("Next")') == "text/Next"
    assert chrome.to_chrome_selector("xpath=//a") == "xpath///a"
    assert chrome.to_chrome_selector('text="Hi"') == "text/Hi"
    assert chrome.to_chrome_selector("#a.b") == "#a.b"
    assert chrome.to_chrome_selector("ul >> nth=2") is None
    assert chrome.to_chrome_selector("internal:label=x") is None


RECIPE = {
    "viewport": {"width": 1280, "height": 900},
    "steps": [
        {"action": "goto", "url": "https://portal.example/login"},
        {
            "action": "click",
            "selector": "#ok",
            "alternatives": ['internal:role=button[name="OK"s]'],
            "text": "OK",
            "optional": True,
        },
        {"action": "fill", "selector": "#user", "credential": "username"},
        {"action": "fill", "selector": "#pass", "credential": "password"},
        {"action": "press", "selector": "#pass", "key": "Enter", "wait": {"url": "^https://portal\\.example/home$"}},
        {"action": "ask-code", "selector": "#sms", "label": "SMS code"},
        {"action": "fill", "selector": "#search", "value": "invoices {year}", "typed": True},
        {"action": "press", "selector": "#search", "key": "Control+Shift+KeyA", "retry": {"times": 2, "delay": 1}},
        {"action": "click", "selector": "a.menu", "button": "right", "dialog": "accept", "prompt": "x"},
        {"action": "wait", "selector": "#busy", "state": "hidden", "timeout": 20},
        {
            "action": "click",
            "selector": "#sso",
            "frame": [{"selector": "iframe#login", "url": "https://sso.example/f"}],
        },
        {"action": "popup"},
        {"action": "opener"},
        {"action": "each", "selector": "select#account", "values": "options"},
        {"action": "scroll", "rounds": 5},
        {"action": "select", "selector": "#lang", "value": "de"},
        {"action": "click", "selector": "ul >> nth=1"},
    ],
    "start": "https://portal.example/invoices",
    "links": {"selector": "a.file"},
}


def test_a_recipe_goes_to_chrome_and_comes_back_unchanged():
    flow, warnings = chrome.export_flow(RECIPE, "Portal")
    assert flow["title"] == "Portal"
    kinds = [step["type"] for step in flow["steps"]]
    assert kinds[:3] == ["setViewport", "navigate", "click"]
    assert flow["steps"][0]["width"] == 1280
    click = flow["steps"][2]
    assert click["selectors"] == [["#ok"], ['aria/OK[role="button"]']] and click["button"] == "primary"
    credentials = [step for step in flow["steps"] if step.get("name") == "harvester:fill"]
    assert [step["parameters"] for step in credentials] == [
        {"selector": "#user", "credential": "username"},
        {"selector": "#pass", "credential": "password"},
    ], "which credential, never one's value"
    chord = [step for step in flow["steps"] if step["type"] in ("keyDown", "keyUp")][2:]
    assert [(step["type"], step["key"]) for step in chord] == [
        ("keyDown", "Control"),
        ("keyDown", "Shift"),
        ("keyDown", "KeyA"),
        ("keyUp", "KeyA"),
        ("keyUp", "Shift"),
        ("keyUp", "Control"),
    ]
    wait = next(step for step in flow["steps"] if step["type"] == "waitForElement")
    assert (wait["count"], wait["operator"], wait["timeout"]) == (0, "==", 20000)
    assert any("acts in a frame" in warning for warning in warnings)
    assert any("Playwright's own" in warning for warning in warnings), "ul >> nth=1 has no selector of Chrome's"
    steps, viewport, _ = chrome.import_text(json.dumps(flow))
    assert steps == RECIPE["steps"] and viewport == {"width": 1280, "height": 900}
    # As Chrome's own parser keeps it: without Harvester's copies, its own steps still come back.
    plain = {"steps": [{k: v for k, v in step.items() if not k.startswith("harvester")} for step in flow["steps"]]}
    steps, _, _ = chrome.import_text(json.dumps(plain))
    assert steps[2] == {"action": "fill", "selector": "#user", "credential": "username"}
    assert steps[5] == {"action": "ask-code", "selector": "#sms", "label": "SMS code"}


@pytest.mark.parametrize("text", ["{", "[]", '{"steps": 3}', json.dumps({"steps": [{}] * 501})])
def test_what_is_no_recording_is_refused(text):
    with pytest.raises(chrome.RecordingError):
        chrome.import_text(text)


def test_the_forms_routes_import_into_the_recipe_and_export_it():
    form = json.dumps(
        {"start": "https://portal.example/invoices", "links": {"selector": "a.file"}, "steps": [{"action": "scroll"}]}
    )
    answer = asyncio.run(routes.import_chrome(routes.ChromeImport(text=json.dumps(RECORDING), recipe=form), None))
    assert answer["recipe"]["start"] == "https://portal.example/invoices", "the rest of the recipe stays"
    assert answer["recipe"]["steps"][0]["action"] == "goto" and answer["count"] == len(answer["recipe"]["steps"])
    assert answer["problems"] == [] and answer["warnings"]
    with pytest.raises(HTTPException) as refused:
        asyncio.run(routes.import_chrome(routes.ChromeImport(text="nope", recipe=form), None))
    assert refused.value.status_code == 422
    with pytest.raises(HTTPException):
        asyncio.run(routes.import_chrome(routes.ChromeImport(text=json.dumps(RECORDING), recipe="[1]"), None))
    exported = asyncio.run(routes.export_chrome(routes.ChromeExport(recipe=json.dumps(RECIPE), title=""), None))
    assert exported["recording"]["title"] == "https://portal.example/invoices"
