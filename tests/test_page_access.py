# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page's use without a mouse, on a phone and with a screen reader (harvester-main.js in
Chromium, as tests/test_page.py drives it): the keyboard's way out of a browser's screen, the
live regions and errors, the job form's groups of options, the dialogs instead of the browser's
confirm()."""

# pylint: disable=missing-function-docstring

import json

import main
from test_browser import needs_chromium
from test_page import CALLS, RECIPE, fixture_page, receive, sent  # pylint: disable=unused-import


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


@needs_chromium
def test_live_regions_and_errors_are_announced_once(page):
    log = page.locator(".harvester-log")
    assert log.get_attribute("role") == "log" and log.get_attribute("aria-live") == "off", "not each poll"
    assert page.locator(".harvester-load").get_attribute("role") is None
    # A refusal of Save naming a field: the field marked invalid and described by the error.
    page.route(
        "**/api/jobs/1",
        lambda route: (
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps({"detail": "Files per run must be a number of at least 1 (or -1: no limit)"}),
            )
            if route.request.method == "PUT"
            else route.fallback()
        ),
    )
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Save", exact=True).click()
    error = page.locator("#harvester-form-error")
    error.wait_for()
    assert error.get_attribute("role") == "alert"
    field = page.locator('[aria-invalid="true"]')
    assert field.count() == 1 and field.get_attribute("aria-describedby").startswith("harvester-form-error ")
    assert field.evaluate("node => node === document.activeElement")
    assert field.evaluate("node => node.closest('label').textContent.startsWith('Files per run')")


@needs_chromium
def test_a_jobs_delete_asks_in_the_page_and_escape_says_no(page):
    page.get_by_role("button", name="Delete").click()
    dialog = page.locator("dialog.harvester-dialog")
    assert dialog.get_attribute("aria-labelledby") and "Delete Mag?" in dialog.inner_text()
    assert dialog.get_by_role("button", name="Cancel").evaluate("node => node === document.activeElement")
    page.keyboard.press("Escape")
    assert dialog.count() == 0 and not [call for call in CALLS if call[0] == "DELETE"]
    page.get_by_role("button", name="Delete").click()
    dialog.get_by_role("button", name="Delete").click()
    page.wait_for_function("document.querySelectorAll('dialog').length === 0")
    page.wait_for_timeout(100)
    assert ("DELETE", "/jobs/1", None) in CALLS


@needs_chromium
def test_the_recorders_clear_picks_has_undo_and_cancel_asks_once_the_recipe_changed(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    page.get_by_role("button", name="Clear picks").click()
    assert sent(page)[-1] == {"type": "clear"}
    page.get_by_role("button", name="Undo", exact=True).click()
    assert sent(page)[-1] == {"type": "undo-pick"}
    # The recipe as it came: Cancel leaves at once; changed, it asks first.
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "next": None}, "proposals": {}})
    cancel = page.locator(".harvester-toolbar-end").get_by_role("button", name="Cancel")
    cancel.click()
    page.locator("dialog").get_by_role("button", name="Cancel").click()
    assert page.locator(".harvester-recorder").count() == 1, "stays"
    cancel.click()
    page.locator("dialog").get_by_role("button", name="Leave").click()
    page.locator("#harvester-job-form").wait_for()


@needs_chromium
def test_a_fields_help_describes_it_and_a_required_one_is_marked_and_save_says_saving(page):
    page.get_by_role("button", name="Edit").click()
    folder = page.get_by_label("Folder", exact=True)
    help_ = page.locator(f"#{folder.get_attribute('aria-describedby')}")
    assert help_.inner_text() == "In your files, e.g. /Magazines/iX."
    assert help_.evaluate("node => !node.closest('label')"), "not part of the field's name"
    marker = "node => getComputedStyle(node.closest('label').querySelector('span'), '::after').content"
    assert "*" in folder.evaluate(marker)
    assert "*" not in page.get_by_label("Tags", exact=True).evaluate(marker)
    # Save says so while the server answers (here it never does).
    page.route("**/api/jobs/1", lambda route: None if route.request.method == "PUT" else route.fallback())
    save = page.get_by_role("button", name="Save", exact=True)
    save.click()
    page.get_by_role("button", name="Saving…").wait_for()
