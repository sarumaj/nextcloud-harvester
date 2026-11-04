# SPDX-License-Identifier: AGPL-3.0-or-later
"""A mail job's form (harvester-main.js) in Chromium against the stubbed API of test_page: its
Fetch in words, the options of the part not chosen hidden (and a group of hidden options with
them) while the job keeps their values, a job saved before Fetch as it ran, the hint under Pages
to the on-demand job while the person has no on-demand website job, and Try the mailbox's folders
to choose from when the mailbox has not the job's."""

# pylint: disable=missing-function-docstring

import pytest
from test_browser import needs_chromium
from test_page import CALLS, JOB, fixture_page  # pylint: disable=unused-import
from test_page_try import answering

import main

# A mail job saved before Fetch, with its flag "links" off: the API gives its options as its
# source reads them (main.get_job).
MAIL = JOB | {
    "id": 5,
    "name": "Invoices",
    "source": "mail",
    "target": "/Invoices",
    "options": main.SOURCES["mail"].upgraded(
        {"host": "imap.example.org", "folder": "INBOX", "filename_pattern": r"\.pdf$", "links": 0}
    ),
    "last_status": "ok",
}
# An on-demand website job (a portal's recipe) to hand the pages to.
PORTAL = JOB | {"id": 6, "name": "Portal", "target": "/Portal", "schedule": ""}


def shown(page, key: str) -> bool:
    """Whether the option's field is shown (its box not hidden, nor its group)."""
    return page.locator(f"[data-option-box={key}]").evaluate(
        "box => !box.hidden && !(box.closest('details') || {}).hidden"
    )


def edit(page) -> None:
    page.locator("tr", has_text="Invoices").get_by_role("button", name="Edit").click()
    page.locator("#harvester-job-form").wait_for()


@needs_chromium
@pytest.mark.parametrize("page", [[MAIL]], indirect=True)
def test_fetch_shows_the_options_of_its_part_only_and_keeps_the_others(page):
    edit(page)
    fetch = page.locator('label.harvester-field:has(> span:text-is("Fetch")) select')
    assert fetch.input_value() == "attachments", "a job of before with its links off: attachments"
    assert [option.strip() for option in fetch.locator("option").all_inner_texts()] == [
        "Attachments",
        "Linked files",
        "Both",
    ]
    assert shown(page, "filename_pattern")
    assert not shown(page, "link_pattern") and not shown(page, "link_text_pattern") and not shown(page, "link_job")
    assert page.locator("details[data-group=links]").evaluate("box => box.hidden"), "its group with them"
    fetch.select_option("links")
    assert not shown(page, "filename_pattern"), "no word of attachment names"
    assert shown(page, "link_pattern") and shown(page, "link_text_pattern") and shown(page, "link_job")
    fetch.select_option("both")
    assert shown(page, "filename_pattern") and shown(page, "link_pattern")
    fetch.select_option("links")
    page.locator('label.harvester-field:has(> span:text-is("Link text")) input').fill("Download")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_function("() => !document.querySelector('#harvester-job-form')")
    saved = next(body for method, path, body in CALLS if (method, path) == ("PUT", "/jobs/5"))
    assert saved["options"]["fetch"] == "links" and saved["options"]["link_text_pattern"] == "Download"
    assert saved["options"]["filename_pattern"] == r"\.pdf$", "a hidden option keeps its value"


@needs_chromium
@pytest.mark.parametrize("page", [[MAIL]], indirect=True)
def test_without_an_on_demand_website_job_the_form_says_how_to_make_one(page):
    edit(page)
    page.locator('label.harvester-field:has(> span:text-is("Fetch")) select').select_option("both")
    hint = page.locator("[data-option-box=link_job] .harvester-no-job")
    page.wait_for_function("() => !document.querySelector('.harvester-no-job').hidden")
    assert hint.text_content() == (
        "No on-demand website job yet: record one for the portal (Website, On demand) to hand it the links that "
        "open a page"
    )


@needs_chromium
@pytest.mark.parametrize("page", [[MAIL, PORTAL]], indirect=True)
def test_an_on_demand_website_job_is_offered_without_the_hint(page):
    edit(page)
    select = page.locator("select[data-option=link_job]")
    page.wait_for_function("() => document.querySelectorAll('select[data-option=link_job] option').length === 2")
    assert select.locator("option").all_inner_texts() == ["None", "Portal (/Portal)"]
    assert page.locator(".harvester-no-job").evaluate("hint => hint.hidden")


@needs_chromium
@pytest.mark.parametrize("page", [[MAIL]], indirect=True)
def test_a_missing_folder_tried_offers_the_mailboxs_folders_to_take(page):
    error = "The mailbox has no folder “Rechnungen”. It has: INBOX/Rechnungen, Archive, INBOX"
    folders = ["INBOX/Rechnungen", "Archive", "INBOX"]
    answer = {"ok": False, "running": False, "seconds": 0.5, "found": 0, "items": [], "notes": []}
    bodies = answering(page, [answer | {"error": error, "hint": "", "folders": folders}])
    edit(page)
    field = page.locator('label.harvester-field:has(> span:text-is("Folder")) input').last
    field.fill("Rechnungen")
    page.get_by_role("button", name="Try the mailbox").click()
    choices = page.locator(".harvester-try-folders")
    choices.wait_for()
    assert bodies[0]["job"]["options"]["folder"] == "Rechnungen"
    assert f"The try failed: {error}" in page.locator(".harvester-try [role=alert]").inner_text()
    assert choices.get_by_role("button").all_inner_texts() == folders, "the closest first"
    choices.get_by_role("button", name="INBOX/Rechnungen").click()
    assert field.input_value() == "INBOX/Rechnungen", "taken into the field"
