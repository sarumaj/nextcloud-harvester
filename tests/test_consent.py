# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cookie banners dismissed on their own (harvester.browser.consent, consent.js) in Chromium
against route-served pages (no network): the known consent managers' banners (also in a frame, in
a shadow root, shown late) and banners of exact texts in German, English and Polish, answered with
"Reject all", else "Only necessary"; nothing else clicked (a dialog that is no cookie banner, a
button outside a banner, a cookie page that does not float); the job's steps and pages, its
option (on for new jobs, off for those saved before) and the recorder's browser, which records no
step of it; a recorded click on a banner made optional."""

# pylint: disable=missing-function-docstring,protected-access

import json

import pytest
from test_browser import FREE, Driver, needs_chromium
from test_recorder_events import run

import main
from harvester.browser import consent
from harvester.browser.recorder import Recorder
from harvester.sources import site as site_module

SITE = "https://shop.example"
CLICKED = (
    "<script>window.clicked = []; function pressed(name, id) { window.clicked.push(name);"
    " if (id) document.getElementById(id).style.display = 'none' }</script>"
)
PAGES = {
    # OneTrust's banner, shown a moment after the page.
    "/onetrust": CLICKED
    + """<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend', `<div id="onetrust-banner-sdk"
  style="position: fixed; bottom: 0"><p>We use cookies.</p>
  <button id="onetrust-accept-btn-handler" onclick="pressed('accept', 'onetrust-banner-sdk')">Accept All</button>
  <button id="onetrust-reject-all-handler" onclick="pressed('reject', 'onetrust-banner-sdk')">Reject All</button>
  </div>`), 300)</script>""",
    # A dialog of its own in German, without "reject all": only the necessary ones.
    "/de": CLICKED
    + """<div role="dialog" id="box"><p>Wir verwenden Cookies, um …</p>
  <button onclick="pressed('all', 'box')">Alle akzeptieren</button>
  <button onclick="pressed('settings')">Einstellungen</button>
  <button onclick="pressed('necessary', 'box')">Nur notwendige Cookies</button></div>""",
    # A floating bar named cookie, in Polish.
    "/pl": CLICKED
    + """<div class="cookie-bar" id="bar" style="position: fixed; top: 0"><span>Ta strona używa plików cookie.</span>
  <a href="#" onclick="pressed('accept', 'bar')">Akceptuję</a> <a href="#" onclick="pressed('reject', 'bar')">Odrzuć
  wszystkie</a></div>""",
    # Usercentrics: its buttons in its shadow root.
    "/usercentrics": CLICKED
    + """<div id="usercentrics-root"></div><script>
  const root = document.getElementById('usercentrics-root').attachShadow({ mode: 'open' })
  root.innerHTML = '<div><button data-testid="uc-accept-all-button">Accept All</button>'
    + '<button data-testid="uc-deny-all-button">Deny</button></div>'
  root.querySelector('[data-testid=uc-accept-all-button]').onclick = () => pressed('accept')
  root.querySelector('[data-testid=uc-deny-all-button]').onclick = () => pressed('deny')
  </script>""",
    # Cookiebot in a frame of the page.
    "/framed": CLICKED + '<iframe src="/cookiebot"></iframe>',
    "/cookiebot": """<div id="CybotCookiebotDialog"><button id="CybotCookiebotDialogBodyButtonDecline"
  onclick="parent.pressed('decline')">Use necessary cookies only</button></div>""",
    # What is never clicked: a dialog that is no cookie banner, a button outside any banner, and
    # a cookie page's section that does not float.
    "/other": CLICKED
    + """<div role="dialog"><p>Delete the file?</p><button onclick="pressed('dialog')">Reject</button></div>
  <button onclick="pressed('page')">Reject all</button>
  <section class="cookie-policy"><p>About cookies</p>
  <button onclick="pressed('policy')">Reject all</button></section>""",
}


def serve(route) -> None:
    path = "/" + route.request.url.split("://", 1)[1].split("/", 1)[1]
    body = f"<html><body>{PAGES.get(path, 'nothing')}</body></html>"
    route.fulfill(status=200, headers={"content-type": "text/html; charset=utf-8"}, body=body)


def clicked(page, count: int) -> list[str]:
    """What the page's buttons noted, once count of them did (or after 2 s, for none)."""
    if count:
        page.wait_for_function(f"window.clicked && window.clicked.length >= {count}", timeout=5000)
    else:
        page.wait_for_timeout(2000)
    return page.evaluate("window.clicked")


@needs_chromium
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/onetrust", ["reject"]),
        ("/de", ["necessary"]),
        ("/pl", ["reject"]),
        ("/usercentrics", ["deny"]),
        ("/framed", ["decline"]),
        ("/other", []),
    ],
)
def test_banners_are_answered_and_nothing_else_is_clicked(monkeypatch, path, expected):
    monkeypatch.setattr(site_module, "guard", serve)
    driver = site_module.BrowserDriver([{"action": "goto", "url": SITE + path}], {}, consent=True)
    try:
        assert clicked(driver.main, len(expected)) == expected
        if expected:
            assert clicked(driver.main, 0) == expected, "answered once"
        # The walk's pages too.
        page = driver.open(SITE + path)
        assert clicked(page, len(expected)) == expected
    finally:
        driver.close()
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_without_the_option_banners_stay(monkeypatch):
    monkeypatch.setattr(site_module, "guard", serve)
    driver = site_module.BrowserDriver([{"action": "goto", "url": SITE + "/de"}], {})
    try:
        assert clicked(driver.main, 0) == []
        assert driver.main.is_visible("#box")
    finally:
        driver.close()


@needs_chromium
def test_a_recorded_click_on_a_banner_is_optional_once_banners_are_dismissed(monkeypatch):
    monkeypatch.setattr(site_module, "guard", serve)
    accept = {"action": "click", "selector": "#onetrust-accept-btn-handler", "text": "Accept All", "pause": 1}
    driver = site_module.BrowserDriver([{"action": "goto", "url": SITE + "/onetrust"}, accept], {}, consent=True)
    try:
        assert driver.main.evaluate("window.clicked") == ["reject"]
        assert [skipped["number"] for skipped in driver.skipped] == [2], "gone: skipped, not failed"
    finally:
        driver.close()


def test_which_recorded_clicks_are_made_optional():
    for step in (
        {"action": "click", "selector": "#onetrust-accept-btn-handler"},
        {"action": "click", "selector": "button.x", "alternatives": ["#CybotCookiebotDialogBodyButtonAccept"]},
        {"action": "click", "selector": "button", "frame": [{"url": "https://cmp.usercentrics.eu/x"}]},
        {"action": "click", "selector": "button.primary", "text": "Alle akzeptieren"},
    ):
        assert consent.relaxed(step) == step | {"optional": True}, step
    for step in (
        {"action": "click", "selector": "#go", "text": "Sign in"},
        {"action": "fill", "selector": "#cookie-name", "value": "x"},
    ):
        assert consent.relaxed(step) == step, step


def test_the_option_is_on_for_new_jobs_and_off_for_those_saved_before():
    option = site_module.Site.options["consent"]
    assert (option.default, option.before, option.kind) == (1, 0, "flag")
    recipe = json.dumps({"start": SITE + "/x", "links": {"selector": "a", "pattern": "pdf"}})
    assert site_module.Site({"recipe": recipe}, {}, None).settings["consent"] == 0, "saved before"
    assert site_module.Site({"recipe": recipe, "consent": 1}, {}, None).settings["consent"] == 1
    listed = next(source for source in main.list_sources() if source["name"] == "site")["options"]["consent"]
    assert (listed["default"], listed["before"]) == (1, 0), "the form knows both"


@needs_chromium
def test_the_recorders_browser_dismisses_banners_without_recording_them():
    async def fulfill(route) -> None:
        path = "/" + route.request.url.split("://", 1)[1].split("/", 1)[1]
        body = f"<html><body>{PAGES.get(path, 'nothing')}</body></html>"
        await route.fulfill(status=200, headers={"content-type": "text/html; charset=utf-8"}, body=body)

    async def scenario():
        driver = Driver()
        recorder = Recorder(driver.send)
        driver.recorder = recorder
        recorder._guard = fulfill  # type: ignore[method-assign]
        await recorder.start(consent=True)
        try:
            await recorder.handle({"type": "goto", "url": SITE + "/onetrust"})
            assert recorder._page is not None
            await recorder._page.wait_for_function("window.clicked && window.clicked.length >= 1", timeout=5000)
            return await recorder._page.evaluate("window.clicked"), recorder.recipe["steps"]
        finally:
            await recorder.close()

    pressed, steps = run(scenario)
    assert pressed == ["reject"]
    assert steps == [{"action": "goto", "url": SITE + "/onetrust"}], "no step of the banner's"
