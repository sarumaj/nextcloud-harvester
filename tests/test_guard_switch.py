# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browsers' network guard switched on and off (the admin's setting, HARVESTER_GUARD): its
words, the admin's form and its warning (the executor takes it: test_controls), and the browsers
launched with their guard or without (the home connection given to Chromium then, rclone's
sign-in exception then the routes' alone).
"""

# pylint: disable=missing-function-docstring,protected-access

import asyncio

import pytest

from conftest import needs_loopback
from harvester import browser, settings
from harvester.browser.recorder import Recorder
from test_guard import http_server
from test_guard_process import serving


def test_the_setting_says_on_or_off():
    for word in ("On", "on", "1", "true", "yes"):
        assert settings.switch(word, False) is True, word
    for word in ("Off", "OFF", "0", "false", "no", " off "):
        assert settings.switch(word, True) is False, word
    assert settings.switch(None, True) is True and settings.switch(None, False) is False, "unset: the default"
    assert settings.switch("maybe", True) is True, "neither: the default"
    assert settings.DEFAULT_GUARD is True, "on unless the deployment says off"


def test_the_admins_form_offers_the_guard_and_says_when_to_switch_it_off():
    fields = {field["id"]: field for field in settings.form(2, 3).to_dict()["fields"]}
    field = fields[settings.GUARD]
    assert field["title"] == "Browser network guard"
    assert field["default"] == "On" and field["options"] == ["On", "Off"], "plain strings"
    assert "Switch it off only when the External Apps' network is already firewalled" in field["description"]
    assert "metadata service" in field["description"]
    off = {field["id"]: field for field in settings.form(2, 3, guard=False).to_dict()["fields"]}
    assert off[settings.GUARD]["default"] == "Off"


def test_the_launch_with_and_without_the_guard(monkeypatch):
    monkeypatch.setattr(browser, "PROXY", "http://anna%40home:p%3Ass@proxy.example:3128")
    guarded = browser.launch_options(4321, home=True)
    assert guarded["proxy"] == {"server": "http://127.0.0.1:4321", "bypass": "<-loopback>"}, "the guard has it"
    assert "anna" not in str(guarded)
    alone = browser.launch_options(0)
    assert "proxy" not in alone, "directly, as before the guard"
    home = browser.launch_options(0, home=True)
    assert home["proxy"] == {"server": "http://proxy.example:3128", "username": "anna@home", "password": "p:ss"}
    monkeypatch.setattr(browser, "PROXY", "socks5h://proxy.example:1080")
    assert browser.launch_options(0, home=True)["proxy"] == {"server": "socks5://proxy.example:1080"}
    for options in (guarded, alone, home):
        assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in options["args"], "WebRTC: no UDP of its own"


def test_the_jobs_browser_without_a_guard(monkeypatch):
    from test_browser import needs_chromium  # pylint: disable=import-outside-toplevel

    if needs_chromium.args[0]:
        pytest.skip("Chromium is not installed")
    from harvester.sources import site  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(browser, "GUARD", False)
    monkeypatch.setattr(browser, "PROXY", "http://anna:secret@proxy.example:3128")
    launched: list[dict] = []
    real = browser.launch_options

    def recorded(*args, **kwargs):
        launched.append(real(*args, **kwargs))
        return launched[-1]

    monkeypatch.setattr(browser, "launch_options", recorded)
    free = browser.free_browsers()
    driver = site.BrowserDriver([], {}, home=True)
    try:
        assert driver._guard is None
    finally:
        driver.close()
    assert browser.free_browsers() == free
    assert launched[0]["proxy"]["server"] == "http://proxy.example:3128", "the home connection, to Chromium"


@needs_loopback
@pytest.mark.parametrize("guarded", [True, False])
def test_the_sign_in_exception_with_and_without_the_guard(monkeypatch, guarded):
    """rclone's listener alone of the local addresses, while allowed: through the guard's
    exception and the routes', or without a guard through the routes' alone."""
    from test_browser import needs_chromium  # pylint: disable=import-outside-toplevel

    if needs_chromium.args[0]:
        pytest.skip("Chromium is not installed")
    monkeypatch.setattr(browser, "GUARD", guarded)
    heads: list[bytes] = []
    with serving(lambda: http_server(heads)) as port, serving(lambda: http_server(heads)) as other:

        async def run():
            recorder = Recorder(lambda message: asyncio.sleep(0))
            recorder.loopback = f"http://127.0.0.1:{port}"
            try:
                await recorder.start()
                assert (recorder._gate is not None) is guarded
                page = recorder._page
                assert page is not None
                await page.goto(f"http://127.0.0.1:{port}/listener")
                assert await page.inner_text("body") == "ok"
                with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
                    await page.goto(f"http://127.0.0.1:{other}/elsewhere")
            finally:
                await recorder.close()

        asyncio.run(run())
    assert [head.split(b" ")[1] for head in heads if not head.startswith(b"GET /favicon")] == [b"/listener"]
