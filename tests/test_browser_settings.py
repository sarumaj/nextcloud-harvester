# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser's settings: like a person's Chrome (for sites' bot protection), the recorder's
window sizes, and a clearer word for a refused connection."""

# pylint: disable=missing-function-docstring

import asyncio

from test_browser import needs_chromium

from harvester import browser
from harvester.browser.recorder import Recorder


def test_the_user_agent_is_chromes_without_headless():
    agent = browser.user_agent("156.0.8078.4")
    assert agent.endswith("Chrome/156.0.0.0 Safari/537.36") and "Headless" not in agent


def test_the_launch_hides_automation_and_can_speak_http1():
    plain, http1 = browser.launch_options(9), browser.launch_options(9, http1=True)
    assert "--disable-blink-features=AutomationControlled" in plain["args"]
    assert "--disable-http2" not in plain["args"] and "--disable-http2" in http1["args"]
    assert plain["channel"] == "chromium" and "APP_SECRET" not in plain["env"]
    # Everything through the guard, loopback too; WebRTC never past it.
    assert plain["proxy"] == {"server": "http://127.0.0.1:9", "bypass": "<-loopback>"}
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in plain["args"]


def test_window_sizes_stay_within_the_limits():
    assert browser.fitted_viewport(1920, 1080) == {"width": 1920, "height": 1080}
    assert browser.fitted_viewport(100, 99999) == {"width": 640, "height": 1600}
    assert browser.fitted_viewport("wide", None) == browser.VIEWPORT


def test_a_cut_connection_gets_a_clearer_word():
    assert "HTTP/1.1 only" in browser.connection_refused(Exception("Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at x"))
    assert browser.connection_refused(Exception("Timeout 30000ms exceeded")) == ""


@needs_chromium
def test_pages_see_a_persons_browser():
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(**browser.launch_options(9))
        try:
            page = chromium.new_context(**browser.context_options(chromium.version)).new_page()
            seen = page.evaluate(
                "() => [navigator.webdriver, navigator.userAgent, navigator.language,"
                " Intl.DateTimeFormat().resolvedOptions().timeZone]"
            )
        finally:
            chromium.close()
    webdriver, agent, language, zone = seen
    assert webdriver is False
    assert "Headless" not in agent
    assert language == browser.LOCALE and zone == browser.TIMEZONE


@needs_chromium
def test_the_recorder_resizes_its_window_and_tells_the_page():
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def scenario() -> dict | None:
        recorder = Recorder(send)
        await recorder.start()
        try:
            await recorder.handle({"type": "viewport", "width": 1600, "height": 900})
            assert recorder._page is not None  # pylint: disable=protected-access
            return recorder._page.viewport_size  # pylint: disable=protected-access
        finally:
            await recorder.close()

    assert asyncio.run(scenario()) == {"width": 1600, "height": 900}
    assert {"type": "viewport", "width": 1600, "height": 900} in sent
