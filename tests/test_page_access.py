# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page's use without a mouse, on a phone and with a screen reader (harvester-main.js in
Chromium, as tests/test_page.py drives it): the keyboard's way out of a browser's screen, the
live regions and errors, the job form's groups of options, the dialogs instead of the browser's
confirm()."""

# pylint: disable=missing-function-docstring

import main
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


def test_a_sources_essentials_are_shown_and_its_other_options_grouped():
    sources = {source["name"]: source for source in main.list_sources()}
    essentials = [key for key, option in sources["site"]["options"].items() if not option["group"]]
    assert essentials == ["recipe", "path", "file_type", "max_items"]
    groups = {option["group"] for source in sources.values() for option in source["options"].values()}
    assert groups == {"", "browser", "files", "runs", "links"}, "each group has its title on the page"


@needs_chromium
def test_the_job_forms_groups_open_where_an_option_differs_and_count_the_changes(page):
    page.get_by_role("button", name="Edit").click()
    form = page.locator("#harvester-job-form")
    form.wait_for()
    files = form.locator('details[data-group="files"]')
    assert not files.evaluate("box => box.open"), "all its options at their defaults"
    assert files.locator("summary").inner_text() == "Files and sizes"
    # Saved before Light browser and Dismiss cookie banners existed: off, as it ran.
    browser = form.locator('details[data-group="browser"]')
    assert browser.evaluate("box => box.open") and browser.locator("summary").inner_text().endswith("(2 changed)")
    files.locator("summary").click()
    form.locator('input[data-option="unpack"]').check()
    assert files.locator("summary").inner_text() == "Files and sizes (1 changed)"
    # Save stays in sight at the window's foot.
    assert page.locator(".harvester-form-actions").evaluate("node => getComputedStyle(node).position") == "sticky"
