"""What the recorder records, as steps, and their replay: pages served by route fulfilment (no
network) to recorder.js in Chromium, and the steps it made replayed in a fresh page.

A sign-in submitted with Enter (one fill per field, no click of the browser's own), a "show
password" switch (the password never reported, by type or autocomplete), a list's choice, a
checkbox's label, a file to upload refused, one-time codes after a password (Microsoft's otc,
Nextcloud's challenge, a plain code, 6 to 8 digits), the keys of a combobox, text edited in
place, menus that open under the mouse (by a style sheet, by a script), open shadow roots (and a
closed one said); in the recorder: a sign-in's popup and back, the address and element a step
waits for, editing the steps, new steps appended or replacing, and a password found in an older
recipe. And the parts on their own: the steps from events, their checks and labels, the
waits' patterns, and public suffixes in the cookies panel.
"""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright
from test_browser import Driver, needs_chromium
from test_recorder_features import routed

from harvester.browser import SCRIPT, jar, launch_options
from harvester.browser.recorder import Recorder
from harvester.browser.steps import StepList
from harvester.recipe import (
    check_recipe,
    edit_step,
    leaked_passwords,
    link_waits,
    step_label,
    steps_from_events,
    url_pattern,
)
from harvester.sources import site as site_module
from harvester.sources import tabs as tabs_module
from harvester.sources.tabs import SHORT_IDLE

SITE = "https://rec.example"
LOGIN = """<html><body><form id="login" action="/listing" method="get">
<input name="user" id="user"><input name="pw" id="pw" type="password">
<button type="button" id="eye" onclick="pw.type = pw.type === 'password' ? 'text' : 'password'">show</button>
<input id="pin" name="pin" autocomplete="current-password">
<button type="submit">Sign in</button></form></body></html>"""
WIDGETS = """<html><body>
<select id="lang"><option value="en">English</option><option value="de">Deutsch</option></select>
<label for="cb">Remember me</label><input type="checkbox" id="cb">
<label><input type="checkbox" id="cb2"> <span id="keep">Keep me signed in</span></label>
<label for="up">Upload</label><input type="file" id="up">
<input id="city" role="combobox" aria-autocomplete="list"><input id="plain">
<div id="note" contenteditable="true"></div><button id="save">Save</button>
</body></html>"""
VERIFY = """<html><body><form action="/home" method="get">
<input name="otc" id="idTxtBx_SAOTCC_OTC" inputmode="numeric" maxlength="6"><button>Verify</button>
</form></body></html>"""
CODES = """<html><body>
<input name="challenge" id="challenge"><input name="code" id="code">
<input name="digits" id="digits" inputmode="numeric" maxlength="8"><input name="zip" id="zip">
<button id="out">Done</button></body></html>"""
MENUS = """<html><head><style>.menu .sub { display: none } .menu:hover .sub { display: block }</style></head>
<body><nav><div class="menu" id="m">Archive<div class="sub"><a id="item" href="#archive">2026</a></div></div>
<div id="js">More<div id="jsub" style="display: none"><a id="jitem" href="#more">Older</a></div></div>
<a id="plainlink" href="#plain">Plain</a></nav>
<script>
js.addEventListener('mouseenter', () => { jsub.style.display = 'block' })
js.addEventListener('mouseleave', () => { jsub.style.display = 'none' })
</script></body></html>"""
SHADOW = """<html><body><login-box id="box"></login-box><secret-box id="sb"></secret-box>
<script>
customElements.define('login-box', class extends HTMLElement {
  constructor() {
    super()
    const root = this.attachShadow({ mode: 'open' })
    root.innerHTML = '<form><input id="user"><button type="button" id="go">Go</button></form><p id="out"></p>'
    root.getElementById('go').onclick = () => { root.getElementById('out').textContent = 'hi ' + root.getElementById('user').value }
  }
})
customElements.define('secret-box', class extends HTMLElement {
  constructor() {
    super()
    this.attachShadow({ mode: 'closed' }).innerHTML = '<button>Hidden</button>'
  }
})
</script></body></html>"""
POPUP_START = """<html><body><button id="login" onclick="window.open('/oauth', 'auth', 'width=400,height=500')">
Sign in with Provider</button><p id="state"></p><a id="next" href="/home">Home</a></body></html>"""
OAUTH = """<html><body><button id="allow" onclick="window.opener.document.getElementById('state').textContent =
'signed in'; window.close()">Allow</button></body></html>"""
LIST = '<html><body><a id="open" href="/item/12345">Item</a></body></html>'
# The item's page: its button comes a little later, and a request that never ends keeps the
# network from going quiet (a page that polls).
ITEM = """<html><body><script>
setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<button id="dl">Download</button>'), 300)
fetch('/poll')
</script></body></html>"""
PAGES = {
    "/login": LOGIN,
    "/widgets": WIDGETS,
    "/verify": VERIFY,
    "/codes": CODES,
    "/menus": MENUS,
    "/shadow": SHADOW,
    "/start": POPUP_START,
    "/oauth": OAUTH,
    "/list": LIST,
    "/item/12345": ITEM,
}


class Pages:  # pylint: disable=too-few-public-methods
    """The pages above for the recorder's routes (fake_site's answer), by path; others a page
    that says it arrived."""

    def answer(self, url: str, _method: str = "GET", _cookie: str = "") -> tuple[int, dict, bytes]:
        path = url.removeprefix(SITE).split("?")[0].split("#")[0]
        body = PAGES.get(path, "<html><body><p id='arrived'>arrived</p></body></html>")
        return 200, {"content-type": "text/html"}, body.encode()


def fulfil(route) -> None:
    if route.request.url.endswith("/poll"):
        return  # never answered: the request stays open
    status, headers, body = Pages().answer(route.request.url)
    route.fulfill(status=status, headers=headers, body=body)


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options(9))  # a port nobody listens on: all fulfilled
        yield browser
        browser.close()


def record(chromium, path: str, scenario) -> list[dict]:
    """What recorder.js reports while the scenario acts on the page at path (cursors, places and
    modes left out)."""
    events: list[dict] = []
    context = chromium.new_context()
    try:
        context.route("**/*", fulfil)
        context.expose_binding("__harvesterReport", lambda _source, event: events.append(event))
        context.add_init_script(SCRIPT)
        page = context.new_page()
        page.goto(SITE + path)
        scenario(page)
        page.wait_for_timeout(300)
    finally:
        context.close()
    return [event for event in events if event.get("action") not in ("cursor", "where", "mode")]


def replayed(chromium, path: str, steps: list[dict], secrets: dict | None = None):
    """A fresh page at path with the steps replayed as a job's sign-in does them; the page, open
    until its context closes (the caller's)."""
    context = chromium.new_context()
    context.route("**/*", fulfil)
    context.set_default_timeout(5000)
    page = context.new_page()
    page.goto(SITE + path)
    driver = object.__new__(site_module.BrowserDriver)
    driver.main, driver.context = page, context
    driver.keep_shot = lambda page, what: None
    driver.challenge = lambda page: None
    driver.sign_in(steps, secrets or {}, None, "")
    return page


# What recorder.js records.


def plain(steps: list[dict]) -> list[dict]:
    """The steps without their alternative selectors (harvester.selectors' to test)."""
    return [{key: value for key, value in step.items() if key != "alternatives"} for step in steps]


@needs_chromium
def test_a_sign_in_submitted_with_enter_is_its_fields_and_the_key(chromium):
    def sign_in(page):
        page.click("#user")
        page.keyboard.type("me")
        page.click("#pw")
        page.keyboard.type("s3cret")
        page.keyboard.press("Enter")
        page.wait_for_url("**/listing?*")

    events = record(chromium, "/login", sign_in)
    assert "s3cret" not in json.dumps(events)
    steps = steps_from_events(events)
    assert plain(steps) == [
        {"action": "fill", "selector": "#user", "credential": "username"},
        {"action": "fill", "selector": "#pw", "credential": "password"},
        {"action": "press", "selector": "#pw", "key": "Enter"},
    ], "no second fill of the password, no click of the submit button Enter made"
    page = replayed(chromium, "/login", steps, {"username": "me", "password": "pw"})
    try:
        assert page.url.endswith("/listing?user=me&pw=pw&pin=")
    finally:
        page.context.close()


def test_a_fill_repeated_since_the_last_click_goes():
    events = [
        {"action": "fill", "selector": "#user", "value": "me", "field": "text", "form": "f"},
        {"action": "fill", "selector": "#pw", "value": "", "field": "password", "form": "f"},
        {"action": "press", "selector": "#pw", "key": "Enter"},
        {"action": "fill", "selector": "#pw", "value": "", "field": "password", "form": "f"},
        {"action": "click", "selector": "#next", "text": "Next"},
        {"action": "fill", "selector": "#pw", "value": "", "field": "password", "form": "f"},
    ]
    assert [(step["action"], step.get("selector")) for step in steps_from_events(events)] == [
        ("fill", "#user"),
        ("fill", "#pw"),
        ("press", "#pw"),
        ("click", "#next"),
        ("fill", "#pw"),
    ], "after a click the same field is a new page's"


@needs_chromium
def test_a_password_shown_as_text_is_never_reported(chromium):
    def show_first(page):
        page.click("#eye")
        page.click("#user")
        page.keyboard.type("me")
        page.click("#pw")
        page.keyboard.type("s3cret")
        page.click("#pin")
        page.keyboard.type("4711")
        page.click("#user")

    events = record(chromium, "/login", show_first)
    assert "s3cret" not in json.dumps(events) and "4711" not in json.dumps(events)
    assert [step for step in plain(steps_from_events(events)) if step["action"] == "fill"] == [
        {"action": "fill", "selector": "#user", "credential": "username"},
        {"action": "fill", "selector": "#pw", "credential": "password"},
        {"action": "fill", "selector": "#pin", "credential": "password"},
    ], "a field once a password's (or an autocomplete for one) stays one; the username before it is the account's"


@needs_chromium
def test_a_lists_choice_is_a_select_step_replayed_by_value_or_text(chromium):
    events = record(chromium, "/widgets", lambda page: page.select_option("#lang", "de"))
    steps = steps_from_events(events)
    assert steps == [{"action": "select", "selector": "#lang", "value": "de", "label": "Deutsch"}]
    assert step_label(steps[0]) == "choose “Deutsch” in #lang"
    page = replayed(chromium, "/widgets", steps + [{"action": "select", "selector": "#lang", "label": "English"}])
    try:
        assert page.input_value("#lang") == "en"
        site_module.replay(page, {"action": "select", "selector": "#lang", "value": "gone", "label": "Deutsch"}, {})
        assert page.input_value("#lang") == "de", "a value the list no longer has: its text"
    finally:
        page.context.close()


@needs_chromium
def test_a_checkboxs_label_is_one_click_and_a_file_is_refused(chromium):
    choosers = []

    def chose(chooser) -> None:
        # A function of its own: Playwright cannot take a list's bound append as a listener.
        choosers.append(chooser)

    def clicks(page):
        page.on("filechooser", chose)
        page.click("label[for=cb]")
        page.click("#keep")
        page.click("#up")
        page.click("label[for=up]")

    events = record(chromium, "/widgets", clicks)
    assert [event["action"] for event in events] == ["click", "click", "refused", "refused"]
    assert [event.get("what") for event in events[2:]] == ["file", "file"] and not choosers
    steps = steps_from_events(events)
    assert [step["action"] for step in steps] == ["click", "click"], "the label's click alone"
    assert "#cb" not in [step["selector"] for step in steps]
    page = replayed(chromium, "/widgets", steps)
    try:
        assert page.is_checked("#cb") and page.is_checked("#cb2"), "each toggled once"
    finally:
        page.context.close()


@needs_chromium
def test_a_one_time_code_after_a_password_is_the_current_code(chromium):
    def sign_in(page):
        page.fill("#user", "me")
        page.fill("#pw", "s3cret")
        page.press("#pw", "Enter")
        page.goto(f"{SITE}/verify")
        page.click("input[name=otc]")
        page.keyboard.type("123456")
        page.keyboard.press("Enter")

    steps = plain(steps_from_events(record(chromium, "/login", sign_in)))
    assert steps[-2:] == [
        {"action": "fill", "selector": "#idTxtBx_SAOTCC_OTC", "credential": "totp"},
        {"action": "press", "selector": "#idTxtBx_SAOTCC_OTC", "key": "Enter"},
    ], "Microsoft's otc, numeric and 6 long"

    def codes(page):
        for field, value in (
            ("#challenge", "654321"),
            ("#code", "111222"),
            ("#digits", "12345678"),
            ("#zip", "123456"),
        ):
            page.click(field)
            page.keyboard.type(value)
        page.click("#out")

    events = record(chromium, "/codes", codes)
    assert [bool(event.get("code_like")) for event in events if event["action"] == "fill"] == [True, True, True, False]
    alone = steps_from_events(events)
    assert all("value" in step for step in alone if step["action"] == "fill"), "without a password: values"
    after = steps_from_events([{"action": "fill", "selector": "#pw", "field": "password", "form": ""}] + events)
    assert [step.get("credential") for step in after if step["action"] == "fill"] == [
        "password",
        "totp",
        "totp",
        "totp",
        None,
    ], "a field named zip is no code"


@needs_chromium
def test_the_keys_of_a_combobox_and_text_edited_in_place(chromium):
    def keys(page):
        page.click("#city")
        page.keyboard.type("Ber")
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape", "Tab"):
            page.keyboard.press(key)
        page.click("#plain")
        page.keyboard.type("x")
        for key in ("Escape", "ArrowDown", "a", "Enter"):
            page.keyboard.press(key)
        page.click("#note")
        page.keyboard.type("Hello there")
        page.click("#save")

    steps = steps_from_events(record(chromium, "/widgets", keys))
    assert [(step["action"], step["selector"], step.get("key") or step.get("value")) for step in steps] == [
        ("fill", "#city", "Ber"),
        ("press", "#city", "ArrowDown"),
        ("press", "#city", "ArrowUp"),
        ("press", "#city", "Enter"),
        ("press", "#city", "Escape"),
        ("press", "#city", "Tab"),
        ("fill", "#plain", "xa"),
        ("press", "#plain", "Enter"),
        ("fill", "#note", "Hello there"),
        ("click", "#save", None),
    ], "a plain field's Escape and arrows are not steps"
    page = replayed(chromium, "/widgets", steps[-2:-1])
    try:
        assert page.inner_text("#note") == "Hello there"
    finally:
        page.context.close()


@needs_chromium
def test_a_menu_that_opens_under_the_mouse_is_hovered_before_its_click(chromium):
    def menus(page):
        page.hover("#m")
        page.click("#item")
        page.hover("#js")
        page.click("#jitem")
        page.click("#plainlink")

    steps = steps_from_events(record(chromium, "/menus", menus))
    assert [(step["action"], step["selector"]) for step in steps] == [
        ("hover", "#m"),
        ("click", "#item"),
        ("hover", "#js"),
        ("click", "#jitem"),
        ("click", "#plainlink"),
    ], "by a style sheet's :hover, by a script's mouseenter; a visible link alone"
    page = replayed(chromium, "/menus", steps[:2])
    try:
        assert page.url.endswith("#archive")
        site_module.replay(page, steps[2], {})
        site_module.replay(page, steps[3], {})
        assert page.url.endswith("#more")
    finally:
        page.context.close()


@needs_chromium
def test_open_shadow_roots_are_entered_and_a_closed_one_is_said(chromium):
    def shadow(page):
        page.click("login-box #user")
        page.keyboard.type("ann")
        page.click("login-box #go")
        page.click("#sb")

    events = record(chromium, "/shadow", shadow)
    assert {"action": "refused", "what": "closed-shadow"} in events
    steps = steps_from_events(events)
    assert [(step["action"], step["selector"]) for step in steps] == [
        ("fill", "#box #user"),
        ("click", "#box #go"),
        ("click", "#sb"),
    ]
    page = replayed(chromium, "/shadow", steps[:2])
    try:
        assert page.inner_text("login-box #out") == "hi ann"
    finally:
        page.context.close()


# In the recorder.


def run(scenario):
    """The recorder's scenario on an event loop of its own, in a thread: this module's Chromium
    (Playwright's sync API) holds one in the tests' thread."""
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["result"] = asyncio.run(scenario())
        except BaseException as error:  # pylint: disable=broad-exception-caught
            outcome["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


async def recorder_at(path: str) -> tuple[Driver, Recorder]:
    driver = Driver()
    recorder = Recorder(driver.send)
    driver.recorder = recorder
    recorder._guard = routed(Pages())  # type: ignore[method-assign,arg-type]
    await recorder.start()
    await recorder.handle({"type": "goto", "url": SITE + path})
    return driver, recorder


async def shown(recorder: Recorder, ending: str) -> None:
    for _ in range(100):
        if recorder._page is not None and recorder._page.url.endswith(ending):
            await recorder._page.wait_for_load_state()
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"the recorder does not show {ending}")


@needs_chromium
def test_a_sign_in_popup_is_recorded_and_replayed(chromium):
    async def scenario():
        driver, recorder = await recorder_at("/start")
        try:
            await driver.click("#login")
            await shown(recorder, "/oauth")
            await driver.click("#allow")
            await shown(recorder, "/start")
            assert await recorder._page.inner_text("#state") == "signed in"
            await driver.click("#next")
            await shown(recorder, "/home")
            return recorder.recipe["steps"]
        finally:
            await recorder.close()

    steps = run(scenario)
    assert [step["action"] for step in steps] == ["goto", "click", "popup", "click", "opener", "click"]
    assert step_label(steps[2]) == "go on in the popup" and not check_recipe(
        {"steps": steps, "links": {"selector": "a"}}
    )
    page = replayed(chromium, "/start", steps[1:5])
    try:
        assert page.inner_text("#state") == "signed in" and len(page.context.pages) == 1
    finally:
        page.context.close()


def test_a_popup_that_never_opens_fails_its_step_or_is_skipped():
    page = SimpleNamespace(context=SimpleNamespace(pages=[None]), wait_for_timeout=lambda ms: time.sleep(ms / 1000))
    tabs = site_module.Tabs(page)
    tabs.opening()
    started = time.monotonic()
    with pytest.raises(site_module.PopupMissing):
        tabs.switch({"action": "popup", "optional": True}, 500)
    assert time.monotonic() - started < 1.5


@needs_chromium
def test_a_navigating_click_waits_for_its_address_and_the_next_element(chromium):
    async def scenario():
        driver, recorder = await recorder_at("/list")
        try:
            await driver.click("#open")
            await shown(recorder, "/item/12345")
            await recorder._page.wait_for_selector("#dl")
            await driver.click("#dl")
            for _ in range(100):
                if len(recorder.recipe["steps"]) == 3:
                    break
                await asyncio.sleep(0.05)
            return recorder.recipe["steps"]
        finally:
            await recorder.close()

    steps = run(scenario)
    assert steps[1]["wait"] == {"url": r"^https://rec\.example/item/\d+(?:[?#].*)?$", "selector": "#dl"}
    assert "wait" not in steps[2], "a click that went nowhere"
    started = time.monotonic()
    page = replayed(chromium, "/list", steps[1:2])
    try:
        assert time.monotonic() - started < 10, "not the 15 s a page that never goes quiet costs"
        assert page.url.endswith("/item/12345") and page.is_visible("#dl")
    finally:
        page.context.close()


def test_a_step_without_a_wait_settles_as_before(monkeypatch):
    settled: list[float] = []
    monkeypatch.setattr(tabs_module, "settle", lambda page, idle=15_000: settled.append(idle))

    page = SimpleNamespace(
        click=lambda *_args, **_options: None,
        wait_for_url=lambda *_args, **_options: settled.append(-1),
        wait_for_selector=lambda *_args, **_options: settled.append(-2),
    )
    site_module.replay(page, {"action": "click", "selector": "#a"}, {})
    site_module.replay(page, {"action": "click", "selector": "#a", "wait": {"url": "^x"}}, {})
    site_module.replay(page, {"action": "click", "selector": "#a", "wait": {"url": "^x", "selector": "#b"}}, {})
    assert settled == [15_000, -1, SHORT_IDLE, -1, -2, SHORT_IDLE]


@needs_chromium
def test_steps_are_edited_and_new_ones_appended_or_replacing():
    recipe = {
        "steps": [
            {"action": "goto", "url": f"{SITE}/login"},
            {"action": "fill", "selector": "#user", "value": "me"},
            {"action": "fill", "selector": "#pw", "value": "hunter2"},
            {"action": "click", "selector": "#go", "text": "Sign in", "wait": {"url": "^x", "selector": "#q"}},
            {"action": "fill", "selector": "#q", "value": "pdf"},
        ],
        "links": {"selector": "a"},
    }

    async def scenario():
        driver, recorder = await recorder_at("/login")
        recorder.events.clear()
        try:
            await recorder.handle({"type": "load", "recipe": recipe})
            warnings = [m["message"] for m in driver.sent if m["type"] == "warning"]
            states = [recorder.recipe["steps"]]
            await recorder.handle({"type": "step-edit", "step": 1, "credential": "username"})
            await recorder.handle({"type": "step-edit", "step": 2, "credential": "password"})
            await recorder.handle({"type": "step-edit", "step": 4, "value": "epub"})
            await recorder.handle({"type": "step-move", "step": 4, "by": 1})  # the last: stays
            await recorder.handle({"type": "step-move", "step": 1, "by": 1})
            await recorder.handle({"type": "step-delete", "step": 9})
            states.append(recorder.recipe["steps"])
            await recorder.handle({"type": "step-delete", "step": 4})
            states.append(recorder.recipe["steps"])
            await recorder.handle({"type": "record-at", "after": 1, "replace": False})
            await recorder.handle({"type": "goto", "url": f"{SITE}/widgets"})
            await recorder.handle({"type": "scroll"})
            states.append(recorder.recipe["steps"])
            await recorder.handle({"type": "undo"})
            await recorder.handle({"type": "record-at", "after": 2, "replace": True})
            states.append(recorder.recipe["steps"])
            await recorder.handle({"type": "record-at", "after": 0, "replace": True})
            await recorder.handle({"type": "goto", "url": f"{SITE}/list"})
            states.append(recorder.recipe["steps"])
            return warnings, states
        finally:
            await recorder.close()

    warnings, states = run(scenario)
    assert len(warnings) == 1 and "step 3" in warnings[0] and "#pw" in warnings[0] and "hunter2" not in warnings[0]
    assert states[0] == recipe["steps"], "the form's steps are the recorder's, unchanged"
    edited = states[1]
    assert [step.get("selector") for step in edited] == [None, "#pw", "#user", "#go", "#q"]
    assert edited[1] == {"action": "fill", "selector": "#pw", "credential": "password"}
    assert edited[2] == {"action": "fill", "selector": "#user", "credential": "username"}
    assert edited[4]["value"] == "epub"
    assert "selector" not in states[2][3]["wait"], "its next step removed: no element to wait for"
    assert [step["action"] for step in states[3]] == ["goto", "goto", "scroll", "fill", "fill", "click"]
    assert states[3][1]["url"] == f"{SITE}/widgets", "appended after step 1, the later steps after them"
    assert [step["action"] for step in states[4]] == ["goto", "goto"], "replaced from step 3 on"
    assert states[5] == [{"action": "goto", "url": f"{SITE}/list"}], "all replaced"


def test_a_steps_edits_on_their_own():
    fill = {"action": "fill", "selector": "#a", "credential": "password", "optional": True}
    edit_step(fill, {"credential": "", "optional": False})
    assert fill == {"action": "fill", "selector": "#a", "value": ""}
    edit_step(fill, {"value": "x", "credential": "nonsense"})
    assert fill["value"] == "x" and "credential" not in fill
    choice = {"action": "select", "selector": "#l", "value": "de", "label": "Deutsch", "wait": {"url": "^x"}}
    edit_step(choice, {"value": "en", "wait": None})
    assert choice == {"action": "select", "selector": "#l", "value": "en"}
    click = {"action": "click", "selector": "#b"}
    edit_step(click, {"value": "x", "credential": "password"})
    assert click == {"action": "click", "selector": "#b"}, "a click has no value"


def test_a_password_written_into_a_recipe_is_found_and_logged_without_it(caplog):
    import main  # pylint: disable=import-outside-toplevel

    steps = [
        {"action": "fill", "selector": "#login-user", "value": "me"},
        {"action": "fill", "selector": "input[name=pw]", "value": "hunter2"},
        {"action": "fill", "selector": 'input[type="password"]', "value": "hunter3"},
        {"action": "fill", "selector": "#passwd", "credential": "password"},
        {"action": "fill", "selector": "#pwd", "value": ""},
        {"action": "fill", "selector": "#q", "value": "pdf"},
    ]
    found = leaked_passwords({"steps": steps})
    assert [text.split(" fills")[0] for text in found] == ["step 2", "step 3"]
    assert not any("hunter" in text for text in found)
    assert not leaked_passwords({"steps": "x"}) and not leaked_passwords(None)
    job = main.Job(user="u", name="j", source="site", target="/", schedule="", id=7)
    job.options = {"recipe": json.dumps({"steps": steps})}
    with caplog.at_level(logging.WARNING, logger="harvester"):
        main.warn_of_leaks(job)
        main.warn_of_leaks(
            main.Job(user="u", name="j", source="site", target="/", schedule="", options={"recipe": "{"})
        )
    records = [record.getMessage() for record in caplog.records if record.name == "harvester"]
    assert len(records) == 2 and "job 7 saved: step 2" in records[0]
    assert "hunter" not in caplog.text


# The parts on their own.


def test_the_new_steps_are_checked_and_labelled():
    def problems(*steps: dict) -> list[str]:
        return check_recipe({"steps": [{"action": "goto", "url": f"{SITE}/x"}, *steps], "links": {"selector": "a"}})

    assert not problems(
        {"action": "select", "selector": "#l", "value": "de"},
        {"action": "hover", "selector": "#m", "frame": [{"url": "https://cmp.example/x"}]},
        {"action": "popup"},
        {"action": "opener", "optional": True},
        {"action": "click", "selector": "#a", "wait": {"url": "^https://x/", "selector": "#b"}},
    )
    assert problems({"action": "select"}) == ["step 2: no element"]
    assert problems({"action": "popup", "frame": [{"url": "https://x.example/"}]})[0].startswith("step 2: the frame")
    for wait in ("^x", {"url": "("}, {"selector": 3}):
        assert problems({"action": "click", "selector": "#a", "wait": wait})[0].startswith("step 2: wait is")
    assert step_label({"action": "hover", "selector": "#m"}) == "hover over #m"
    assert step_label({"action": "opener"}) == "go back to the page that opened the popup"


def test_an_address_pattern_leaves_out_what_changes_and_waits_follow_their_next_step():
    pattern = url_pattern("https://shop.example/orders/12345/ab12cd34ef56gh78ij?session=1#top")
    assert pattern == r"^https://shop\.example/orders/\d+/[^/]+(?:[?#].*)?$"
    assert url_pattern("https://shop.example") == r"^https://shop\.example(?:[?#].*)?$"
    steps = [
        {"action": "click", "selector": "#a", "wait": {"url": "^x"}},
        {"action": "click", "selector": "#b", "wait": {"url": "^y", "selector": "#old"}},
        {"action": "click", "selector": "#c", "frame": [{"url": "https://f.example/"}], "wait": {"url": "^z"}},
        {"action": "goto", "url": "https://x.example/"},
    ]
    link_waits(steps)
    assert [step["wait"] for step in steps[:3]] == [
        {"url": "^x", "selector": "#b"},
        {"url": "^y"},
        {"url": "^z"},
    ], "a frame's element is not the page's, an address no element"
    steps[1]["optional"] = True
    link_waits(steps)
    assert steps[0]["wait"] == {"url": "^x"}, "an optional step's element may never come"


def test_public_suffixes_are_never_a_cookies_domain():
    hosts = {"shop.example.co.uk", "me.github.io", "site.example.org"}
    for domain in ("co.uk", ".co.uk", "github.io", "uk", "org", "com"):
        assert not jar.domain_allowed(domain, hosts), domain
    for domain in ("example.co.uk", ".example.co.uk", "shop.example.co.uk", "me.github.io", "example.org"):
        assert jar.domain_allowed(domain, hosts), domain


def test_a_filled_code_comes_by_e_mail_and_goes_back():
    track = StepList()
    track.load([{"action": "fill", "selector": "#code", "alternatives": ["input[name=code]"], "credential": "totp"}])
    assert track.edit("mail-code", {"step": 0, "from": "no-reply@shop", "subject": "Code"})
    assert track.all() == [
        {
            "action": "mail-code",
            "selector": "#code",
            "alternatives": ["input[name=code]"],
            "from": "no-reply@shop",
            "subject": "Code",
        }
    ]
    assert not check_recipe({"steps": track.all(), "start": f"{SITE}/x", "links": {"selector": "a"}})
    track.edit("mail-code", {"step": 0, "off": True})
    assert track.all()[0] == {
        "action": "fill",
        "selector": "#code",
        "alternatives": ["input[name=code]"],
        "credential": "totp",
    }
    assert not track.edit("mail-code", {"step": 3})
