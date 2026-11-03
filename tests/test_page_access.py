# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page's use without a mouse, on a phone and with a screen reader (harvester-main.js in
Chromium, as tests/test_page.py drives it): the keyboard's way out of a browser's screen, the
live regions and errors, the job form's groups of options, the dialogs instead of the browser's
confirm()."""

# pylint: disable=missing-function-docstring

from test_browser import needs_chromium
from test_page import fixture_page, sent  # pylint: disable=unused-import


@needs_chromium
def test_the_recorders_keys_go_to_the_site_until_escape_twice_leaves_the_screen(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    keys = page.locator(".harvester-keys")
    keys.focus()
    hint = page.locator(".harvester-keys-hint")
    assert hint.inner_text() == "Keys go to the site; press Esc twice to leave"
    assert keys.get_attribute("aria-describedby") == hint.get_attribute("id")
    page.keyboard.press("Tab")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.className") == "harvester-keys", "Tab and one Escape stay"
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.className") != "harvester-keys"
    assert page.evaluate("!document.activeElement.closest('.harvester-screen-box')")
    keyed = [m["key"] for m in sent(page) if m["type"] == "key"]
    assert keyed == ["Tab", "Escape"], "the second Escape is not the site's"
    assert page.locator("img.harvester-screen[tabindex]").count() == 0
