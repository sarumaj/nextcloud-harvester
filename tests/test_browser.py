# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder and the site source in a real Chromium, against the fake archive.

The site is served by Playwright's request interception (no network). The tests are skipped
where Chromium is not installed (playwright install --only-shell chromium).
"""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import re

import pytest

from fake_site import SITE, Site
from harvester.browser import allowed_url
from harvester.browser.recorder import Recorder
from harvester.presets import make_job
from harvester.recipe import check_recipe, steps_from_events
from harvester.sources import ChallengeError, SourceError
from harvester.sources import site as site_module

# All browser slots: free before and after each test.
FREE = site_module.browser.free_browsers()


def chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

        from harvester.browser import launch_options  # pylint: disable=import-outside-toplevel

        with sync_playwright() as playwright:
            playwright.chromium.launch(**launch_options(9)).close()
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


needs_chromium = pytest.mark.skipif(not chromium_installed(), reason="Chromium is not installed")


def test_allowed_urls():
    assert allowed_url("https://www.heise.de/select")
    assert allowed_url("http://93.184.215.14/x")
    for url in (
        "file:///proc/self/environ",
        "chrome://settings",
        "http://localhost:23000/",
        "http://127.0.0.1/",
        "http://10.0.0.26/",
        "http://[::1]/",
        "http://169.254.169.254/latest",
        "javascript:alert(1)",
    ):
        assert not allowed_url(url), url


def test_steps_keep_no_password_and_turn_the_login_into_the_account():
    events = [
        {"action": "goto", "url": f"{SITE}/login"},
        {"action": "fill", "selector": "input[name=user]", "value": "m", "field": "text", "form": "form"},
        {"action": "fill", "selector": "input[name=user]", "value": "me", "field": "text", "form": "form"},
        {"action": "fill", "selector": "input[name=pass]", "value": "", "field": "password", "form": "form"},
        {"action": "click", "selector": "#go", "text": "Sign in"},
        {"action": "fill", "selector": "input[name=q]", "value": "pdf", "field": "search", "form": "search"},
        {"action": "press", "selector": "input[name=q]", "key": "Enter"},
    ]
    assert steps_from_events(events) == [
        {"action": "goto", "url": f"{SITE}/login"},
        {"action": "fill", "selector": "input[name=user]", "credential": "username"},
        {"action": "fill", "selector": "input[name=pass]", "credential": "password"},
        {"action": "click", "selector": "#go", "text": "Sign in"},
        {"action": "fill", "selector": "input[name=q]", "value": "pdf"},
        {"action": "press", "selector": "input[name=q]", "key": "Enter"},
    ]


def test_check_recipe():
    assert check_recipe({}) == ["no listing to start at", "no file links picked"]
    problems = check_recipe({"steps": [{"action": "goto", "url": "file:///etc/passwd"}, {"action": "eval"}]})
    assert problems == [
        "step 1: the browser may not open file:///etc/passwd",
        "step 2: unknown action eval",
        "no file links picked",
    ]


class Driver:
    """Drives the recorder as the page does: mouse and keys at the screen's coordinates."""

    def __init__(self, recorder: Recorder | None = None):
        self.sent: list[dict] = []
        self.recorder = recorder

    async def send(self, message: dict) -> None:
        # As over the WebSocket: a copy, not the recorder's own state.
        self.sent.append(json.loads(json.dumps(message)))

    async def click(self, selector: str) -> None:
        recorder = self.recorder
        assert recorder is not None and recorder._page is not None
        box = await recorder._page.locator(selector).first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        for event in ("move", "down", "up"):
            await recorder.handle({"type": "mouse", "event": event, "x": x, "y": y})

    async def type(self, text: str) -> None:
        assert self.recorder is not None
        for key in text:
            await self.recorder.handle({"type": "key", "key": key})

    async def mode(self, mode: str) -> None:
        assert self.recorder is not None
        await self.recorder.handle({"type": "mode", "mode": mode})

    def last(self) -> dict:
        return [m for m in self.sent if m["type"] == "recipe"][-1]

    async def until(self, condition) -> dict:
        """The first recipe message after now that meets the condition (a pick is handled
        after the click returns)."""
        start = len(self.sent)
        for _ in range(100):
            for message in self.sent[start:]:
                if message["type"] == "recipe" and condition(message):
                    return message
            await asyncio.sleep(0.05)
        raise AssertionError("the recorder did not answer")


def async_site(site: Site):
    async def fulfill(route) -> None:
        status, headers, body = site.answer(route.request.url, route.request.method)
        await route.fulfill(status=status, headers=headers, body=body)

    return fulfill


async def outlined(recorder: Recorder) -> list[str]:
    """The outlines of the links on the page: solid (matched) or dashed (left out)."""
    assert recorder._page is not None
    return await recorder._page.eval_on_selector_all(
        "a", "els => els.filter(e => e.style.outline).map(e => e.style.outline.includes('solid') ? 'solid' : 'dashed')"
    )


@needs_chromium
def test_the_recorder_shows_its_first_page_once():
    # The context's "page" event fires for the first page too: shown twice, its screencast
    # was set up twice and the second stopped the first half-way (a broken screen in 0.7.1).
    async def scenario():
        driver = Driver()
        recorder = Recorder(driver.send)
        await recorder.start()
        try:
            await recorder._context.route(f"{SITE}/**", async_site(Site()))
            await recorder.handle({"type": "goto", "url": f"{SITE}/login"})
            # Until the page's first frame, however busy the machine: not a fixed pause.
            for _ in range(100):
                if any(m["type"] == "frame" for m in driver.sent):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.5)
        finally:
            await recorder.close()
        urls = [m["url"] for m in driver.sent if m["type"] == "url" and m["url"].endswith("/login")]
        assert urls == [f"{SITE}/login"]
        assert any(m["type"] == "frame" for m in driver.sent)

    asyncio.run(scenario())


@needs_chromium
def test_recorder_records_the_login_and_proposes_the_levels_patterns():
    async def scenario():
        driver = Driver()
        recorder = Recorder(driver.send)
        driver.recorder = recorder
        await recorder.start()
        try:
            await recorder._context.route(f"{SITE}/**", async_site(Site()))
            page = recorder._page
            await recorder.handle({"type": "goto", "url": f"{SITE}/login"})
            await driver.click("#login-user")
            await driver.type("me")
            await driver.click("#login-password")
            await driver.type("secret")
            await driver.click("button[name=rm_login]")
            await page.wait_for_url(re.compile(r"/select/mag/archiv$"))
            await page.wait_for_load_state()

            await driver.mode("follow")
            await driver.click("a.years__link")
            follow = await driver.until(lambda m: "follow0" in m["proposals"])
            assert await outlined(recorder) == ["solid"] * 3

            await driver.mode("browse")
            await driver.click("a.years__link >> nth=1")
            await page.wait_for_url(re.compile(r"/archiv/2025$"))
            await page.wait_for_load_state()
            await driver.mode("links")
            await driver.click("a.year__link")
            links = await driver.until(lambda m: "links" in m["proposals"])

            await recorder.handle(
                {"type": "pattern", "target": "links", "pattern": r"/archiv/(?P<year>\d{4})/(?P<issue>[12])$"}
            )
            edited = driver.last()
            return driver, follow, links, edited, await outlined(recorder)
        finally:
            await recorder.close()

    driver, follow, links, edited, outlines = asyncio.run(scenario())
    recipe = edited["recipe"]
    assert [step["action"] for step in recipe["steps"]] == ["goto", "fill", "fill", "click"]
    assert [step.get("credential") for step in recipe["steps"][1:3]] == ["username", "password"]
    assert "secret" not in json.dumps(driver.sent)
    assert recipe["start"] == f"{SITE}/select/mag/archiv"
    assert follow["recipe"]["follow"][0]["pattern"] == r"/select/mag/archiv/(?P<year>\d{4})$"
    proposal = links["proposals"]["links"]
    assert proposal["pattern"] == r"/select/mag/archiv/(?P<year>\d{4})/(?P<issue>\d+)$"
    assert proposal["level"] == 1 and proposal["matching"] == 3
    assert proposal["examples"][0]["values"] == {"year": 2025, "issue": 3}
    assert proposal["path"] == "{year}/{text}" and proposal["tags"] == ["{year}"]
    assert edited["proposals"]["links"]["matching"] == 2
    assert sorted(outlines) == ["dashed", "solid", "solid"]


def sync_site(site: Site):
    def fulfill(route) -> None:
        status, headers, body = site.answer(route.request.url, route.request.method)
        route.fulfill(status=status, headers=headers, body=body)

    return fulfill


@needs_chromium
def test_heise_template_runs_in_the_browser_against_the_fake_archive(monkeypatch):
    site = Site()
    monkeypatch.setattr(site_module, "guard", sync_site(site))
    job = make_job("heise", {"magazine": "mag", "name": "Mag"})
    options = dict(job["options"], recipe=job["options"]["recipe"].replace("https://www.heise.de", SITE))
    source = site_module.Site(options, {"username": "me", "password": "pw"}, None)
    try:
        items = list(source.items(lambda key: False))
    finally:
        source.close()
    assert [item.path for item in items][:3] == ["2026/Mag 2026-02.pdf", "2026/Mag 2026-01.pdf", "2025/Mag 2025-03.pdf"]
    assert len(items) == 8 and items[0].url.endswith("/select/mag/archiv/2026/2/download")
    assert any(url.endswith("/session") for url in site.requests), "signed in"
    assert site_module.browser.free_browsers() == FREE


def site_with_cookies(site: Site):
    """The fake site through the browser, with the request's cookies."""

    def fulfill(route) -> None:
        status, headers, body = site.answer(
            route.request.url, route.request.method, route.request.all_headers().get("cookie", "")
        )
        route.fulfill(status=status, headers=headers, body=body)

    return fulfill


def heise_like(site: Site, monkeypatch, **credentials) -> site_module.Site:
    """The heise template against the fake archive, in the browser."""
    monkeypatch.setattr(site_module, "guard", site_with_cookies(site))
    job = make_job("heise", {"magazine": "mag", "name": "Mag"})
    options = dict(job["options"], recipe=job["options"]["recipe"].replace("https://www.heise.de", SITE))
    return site_module.Site(options, credentials or {"username": "me", "password": "pw"}, None)


def signed_in(site: Site) -> bool:
    return any(url.endswith("/session") for url in site.requests)


@needs_chromium
def test_a_kept_session_skips_the_sign_in_until_it_expires(monkeypatch):
    site = Site(require_login=True)
    first = heise_like(site, monkeypatch)
    try:
        assert len(list(first.items(lambda key: False))) == 8
        kept = first.session()
    finally:
        first.close()
    assert signed_in(site) and kept and any(cookie["name"] == "sid" for cookie in kept["cookies"])

    site.requests.clear()
    second = heise_like(site, monkeypatch)
    second.restore(kept)
    try:
        assert len(list(second.items(lambda key: False))) == 8
    finally:
        second.close()
    assert not signed_in(site), "the kept session reached the listing: no sign-in"

    site.requests.clear()
    expired = heise_like(site, monkeypatch)
    expired.restore({"cookies": [], "origins": []})
    try:
        assert len(list(expired.items(lambda key: False))) == 8
    finally:
        expired.close()
    assert signed_in(site), "the listing showed the login form: signed in again"


@needs_chromium
def test_a_page_asking_for_a_person_stops_the_run(monkeypatch):
    site = Site()
    site.challenge.add("/select/mag/archiv/2025")
    source = heise_like(site, monkeypatch)
    try:
        with pytest.raises(ChallengeError) as caught:
            list(source.items(lambda key: False))
    finally:
        source.close()
    assert caught.value.url.endswith("/select/mag/archiv/2025") and caught.value.kind == "reCAPTCHA"
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_failed_sign_in_releases_the_browser(monkeypatch):
    site = Site()
    monkeypatch.setattr(site_module, "guard", site_with_cookies(site))
    recipe = {
        "steps": [{"action": "goto", "url": f"{SITE}/login"}, {"action": "click", "selector": "#no-such-button"}],
        "links": {"selector": "a", "pattern": "x"},
    }
    monkeypatch.setattr(site_module, "STEP_TIMEOUT", 1000)
    source = site_module.Site({"recipe": json.dumps(recipe)}, {}, None)
    with pytest.raises(SourceError, match="step 2"):
        list(source.items(lambda key: False))
    source.close()
    assert site_module.browser.free_browsers() == FREE, "the job's browser is free for the next one"


@needs_chromium
def test_challenges_are_recognised():
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    pages = {
        '<div class="g-recaptcha"></div>': "reCAPTCHA",
        '<iframe src="https://newassets.hcaptcha.com/captcha/v1/x"></iframe>': "hCaptcha",
        '<div class="cf-turnstile"></div>': "Cloudflare",
        "<title>Just a moment...</title><p>Checking your browser. Cloudflare Ray ID: 1</p>": "Cloudflare",
        "<p>Please verify you are human to continue.</p>": "a human check",
        '<a href="/a.pdf">A file</a>': None,
    }
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(channel="chromium")
        page = chromium.new_page()
        for html, kind in pages.items():
            page.set_content(html)
            assert page.evaluate(site_module.browser.CHALLENGE_SCRIPT) == kind, html
        chromium.close()


@needs_chromium
def test_the_recorders_keys_and_typed_text_reach_the_page():
    async def scenario():
        driver = Driver()
        recorder = Recorder(driver.send)
        driver.recorder = recorder
        await recorder.start()
        try:
            page = recorder._page
            await page.set_content('<input id="q" style="width:400px">')
            await driver.click("#q")
            for message in (
                {"type": "text", "text": "ac"},
                {"type": "key", "key": "ArrowLeft"},
                {"type": "text", "text": "b"},
                {"type": "key", "key": "End"},
                {"type": "text", "text": "é@ß"},
            ):
                await recorder.handle(message)
            typed = await page.input_value("#q")
            await recorder.handle({"type": "key", "key": "Control+a"})
            await recorder.handle({"type": "key", "key": "Backspace"})
            return typed, await page.input_value("#q")
        finally:
            await recorder.close()

    typed, cleared = asyncio.run(scenario())
    assert typed == "abcé@ß", "arrows move the caret; text in any layout arrives as typed"
    assert cleared == "", "Ctrl+A then Backspace select and delete"


@needs_chromium
def test_the_recorder_shows_the_pages_cursor_and_never_records_it():
    async def scenario():
        driver = Driver()
        recorder = Recorder(driver.send)
        driver.recorder = recorder
        await recorder.start()
        try:
            await recorder._context.route(f"{SITE}/**", async_site(Site()))
            for page, selector in ((f"{SITE}/login", "#login-user"), (f"{SITE}/select/mag/archiv", "a.years__link")):
                await recorder.handle({"type": "goto", "url": page})
                await recorder._page.wait_for_load_state()
                box = await recorder._page.locator(selector).first.bounding_box()
                await recorder.handle({"type": "mouse", "event": "move", "x": box["x"] + 5, "y": box["y"] + 5})
                await asyncio.sleep(0.3)
            await driver.mode("follow")
            box = await recorder._page.locator("a.years__link >> nth=1").bounding_box()
            await recorder.handle({"type": "mouse", "event": "move", "x": box["x"] + 8, "y": box["y"] + 8})
            await asyncio.sleep(0.3)
        finally:
            await recorder.close()
        cursors = [m["cursor"] for m in driver.sent if m["type"] == "cursor"]
        assert "text" in cursors and "pointer" in cursors and cursors[-1] == "crosshair"
        assert not any(event.get("action") == "cursor" for event in recorder.events)

    asyncio.run(scenario())


def test_a_screencast_the_page_refuses_while_attached_is_started_again():
    class Session:  # pylint: disable=too-few-public-methods
        """A CDP session whose page is attached only from the third try."""

        def __init__(self):
            self.tries = 0

        async def send(self, method, params=None):  # pylint: disable=unused-argument
            self.tries += 1
            if self.tries < 3:
                raise RuntimeError("Protocol error (Page.startScreencast): Not attached to an active page")

    async def scenario():
        recorder = Recorder(lambda message: asyncio.sleep(0))
        recorder._cdp = Session()  # type: ignore[assignment]
        await recorder._screencast()
        assert recorder._casting and recorder._cdp.tries == 3  # type: ignore[attr-defined]
        never = Session()
        never.tries = -100
        recorder._cdp = never  # type: ignore[assignment]
        await recorder._screencast()
        assert not recorder._casting, "refused every time: started at the next load"
        never.tries = 10
        await recorder._loaded()
        assert recorder._casting

    asyncio.run(scenario())
