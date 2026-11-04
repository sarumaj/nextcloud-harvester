# SPDX-License-Identifier: AGPL-3.0-or-later
"""The job form's Browse and Try the address (harvester-main.js) in Chromium against the stubbed
API and WebSocket of test_page: Browse opens the browser alone at the field's address and Use
this address takes the browser's page into the field; Try sends the form as Save would with the
account typed, says Trying… meanwhile, and shows the outcome, the files with their names, tags
and states, a mailbox's messages, and a failure with its hint as an alert. A website job has
neither (its recorder has Test the recipe)."""

# pylint: disable=missing-function-docstring

import json

import pytest
from test_browser import needs_chromium
from test_page import JOB, QUEUED, fixture_page, receive, sent  # pylint: disable=unused-import

TRIED = {
    "ok": True,
    "running": False,
    "seconds": 1.4,
    "found": 3,
    "counts": {"fetch": 1, "later": 0, "known": 1, "type": 1, "size": 0, "problem": 0, "pattern": 2},
    "items": [
        {"path": "/Feed/Ep 2.mp3", "url": "https://cdn.example/2.mp3", "tags": ["Show"], "state": "fetch"},
        {"path": "/Feed/Ep 1.mp3", "url": "https://cdn.example/1.mp3", "tags": [], "state": "known"},
        {"path": "/Feed/Notes.pdf", "url": "https://cdn.example/n.pdf", "tags": [], "state": "type", "pending": True},
    ],
    "more": 0,
    "cut": "",
    "notes": ["Message from billing@shop.example of 2025-10-14, “Invoice November”: 1 attachment"],
    "outcome": "found 3 files, 1 would be fetched this run",
}
FAILED = {
    "ok": False,
    "running": False,
    "seconds": 0.3,
    "found": 0,
    "items": [],
    "notes": [],
    "error": "the feed https://feed.example/rss answered 403",
    "hint": "The site may want the account: its user name and password, or a signed-in browser's cookies.",
}


def answering(page, answers: list[dict]) -> list[dict]:
    """POST /api/try answered with the answers in turn; returns the bodies the page sent."""
    bodies: list[dict] = []

    def handle(route):
        bodies.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answers[len(bodies) - 1]))

    page.route("**/apps/app_api/proxy/harvester/api/try", handle)
    return bodies


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
@pytest.mark.parametrize("page", [[QUEUED]], indirect=True)
def test_try_the_address_shows_what_a_run_would_fetch_and_a_failure_with_its_hint(page):
    bodies = answering(page, [TRIED, FAILED])
    page.get_by_role("button", name="Edit").click()
    page.locator('label.harvester-field:has(> span:text-is("User name")) input').fill("anna")
    page.locator('label.harvester-field:has(> span:text-is("Tags")) input').fill("{feed}")
    button = page.get_by_role("button", name="Try the address")
    button.click()
    result = page.locator(".harvester-try-result[role=status]")
    result.locator(".harvester-try-items").wait_for()
    sent_body = bodies[0]
    assert sent_body["id"] == 2 and sent_body["account"] == {"username": "anna"}
    assert sent_body["job"]["source"] == "feed" and sent_body["job"]["tags"] == ["{feed}"]
    assert sent_body["job"]["options"]["url"] == "https://feed.example/rss", "the form as Save sends it"
    text = result.inner_text()
    assert "Tried in 1.4 s: found 3 files, 1 would be fetched this run" in text
    assert "/Feed/Ep 2.mp3" in text and "fetched this run" in text and "Tags: Show" in text
    assert "already in the job’s history" in text and "skipped: not of the job’s file types" in text
    assert "named when downloaded" in text
    assert "“Invoice November”: 1 attachment" in text, "what the listing said"
    assert button.is_enabled() and button.inner_text() == "Try the address"
    button.click()
    alert = page.locator(".harvester-try [role=alert]")
    alert.locator("p").first.wait_for()
    assert "The try failed: the feed https://feed.example/rss answered 403" in alert.inner_text()
    assert "signed-in browser's cookies" in alert.inner_text()
    assert result.inner_text() == "", "the earlier outcome is gone"


@needs_chromium
@pytest.mark.parametrize("page", [[QUEUED]], indirect=True)
def test_trying_says_so_until_the_answer_comes(page):
    held = []

    def hold(route):
        held.append(route)  # answered below, once the page said it is trying

    page.route("**/apps/app_api/proxy/harvester/api/try", hold)
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Try the address").click()
    trying = page.get_by_role("button", name="Trying…")
    trying.wait_for()
    assert trying.is_disabled() and trying.get_attribute("aria-busy") == "true"
    assert "at most a minute" in page.locator(".harvester-try-result").inner_text()
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(TRIED))
    page.get_by_role("button", name="Try the address").wait_for()


@needs_chromium
def test_a_website_job_has_neither(page):
    assert JOB["source"] == "site"
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").wait_for()
    assert page.get_by_role("button", name="Browse", exact=True).count() == 0
    assert page.locator(".harvester-try").count() == 0


@needs_chromium
@pytest.mark.parametrize("page", [[QUEUED]], indirect=True)
def test_the_try_fits_a_phone(page):
    answering(page, [TRIED])
    page.set_viewport_size({"width": 360, "height": 740})
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Try the address").click()
    page.locator(".harvester-try-items").wait_for()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "no sideways scrolling"
