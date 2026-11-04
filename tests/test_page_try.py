# SPDX-License-Identifier: AGPL-3.0-or-later
"""The job form's Browse (harvester-main.js) in Chromium against the stubbed API and WebSocket of
test_page: it opens the browser alone at the field's address, and Use this address takes the
browser's page into the field. A website job has none (its recorder has the browser)."""

# pylint: disable=missing-function-docstring

import pytest
from test_browser import needs_chromium
from test_page import JOB, QUEUED, fixture_page, receive, sent  # pylint: disable=unused-import


@needs_chromium
@pytest.mark.parametrize("page", [[QUEUED]], indirect=True)
def test_browse_takes_the_browsers_address_into_the_field(page):
    page.get_by_role("button", name="Edit").click()
    field = page.locator('label.harvester-field:has(> span:text-is("Feed")) input')
    assert field.input_value() == "https://feed.example/rss"
    page.get_by_role("button", name="Browse", exact=True).click()
    page.wait_for_function("window.__sockets.length")
    assert page.evaluate("window.__sockets[0].url").endswith(
        "/exapps/harvester/ws/record?browse=1&url=https%3A%2F%2Ffeed.example%2Frss"
    ), "the browser alone, at the field's address"
    assert page.get_by_role("button", name="Record in the browser").count() == 0
    assert page.locator(".harvester-modes, .harvester-steps").count() == 0, "no recording, no picks"
    page.get_by_role("button", name="Back", exact=True).click()
    assert sent(page) == [{"type": "back"}]
    receive(page, {"type": "url", "url": "https://feed.example/podcast.xml"})
    page.get_by_role("button", name="Use this address").click()
    assert field.input_value() == "https://feed.example/podcast.xml"
    assert page.evaluate("window.__sockets[0].readyState") == 3, "the browser closes with the view"
    # Cancel goes back with the field as it was.
    page.get_by_role("button", name="Browse", exact=True).click()
    receive(page, {"type": "url", "url": "https://elsewhere.example/"}, 1)
    page.get_by_role("button", name="Cancel").click()
    assert field.input_value() == "https://feed.example/podcast.xml"


@needs_chromium
def test_a_website_job_has_no_browse(page):
    assert JOB["source"] == "site"
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").wait_for()
    assert page.get_by_role("button", name="Browse", exact=True).count() == 0
