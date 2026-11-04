# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page's Save to and Tag as (the form's section: the fields of each place, the vault's
warning and password, what a place switches off with why, no Nextcloud tags outside the files),
a job's Files with their labels, and the Library (locked until unlocked, its token in the files'
addresses, the grid, the list and the viewer), in Chromium against a stubbed API."""

# pylint: disable=missing-function-docstring,unused-import,redefined-outer-name

import json

from test_browser import needs_chromium
from test_page import CALLS, fixture_page  # noqa: F401  # the page's fixture

ENTRIES = [
    {"id": 2, "path": "/Mag/2024", "name": "2024", "dir": True, "size": 0, "type": "", "labels": [], "job": 1, "at": 1},
    {
        "id": 3,
        "path": "/Mag/cover.png",
        "name": "cover.png",
        "dir": False,
        "size": 10,
        "type": "image/png",
        "labels": ["2024"],
        "job": 1,
        "at": 1,
    },
    {
        "id": 4,
        "path": "/Mag/issue.pdf",
        "name": "issue.pdf",
        "dir": False,
        "size": 2000,
        "type": "application/pdf",
        "labels": ["2024"],
        "job": 1,
        "at": 1,
    },
]


# The library's token each request came with, by path.
TOKENS: list[tuple[str, str]] = []


def answering(page, answers: dict) -> None:
    """The library's and the job's routes, before the fixture's (each answer by method and path)."""

    def route(handled) -> None:
        method = handled.request.method
        full = handled.request.url.split("/api", 1)[1]
        path = full.split("?")[0]
        body = handled.request.post_data
        CALLS.append((method, full, json.loads(body) if body else None))
        TOKENS.append((path, handled.request.headers.get("x-harvester-library", "")))
        answer = answers.get(f"{method} {path}")
        if answer is None:
            handled.fallback()
            return
        status, data = answer if isinstance(answer, tuple) else (200, answer)
        handled.fulfill(status=status, content_type="application/json", body=json.dumps(data))

    page.route("**/apps/app_api/proxy/harvester/api/**", route)


@needs_chromium
def test_save_to_shows_each_places_fields_and_switches_off_what_it_cannot_do(page):
    answering(page, {"PUT /jobs/1": {"id": 1, "name": "Mag"}, "PUT /jobs/1/vault": {}})
    page.get_by_role("button", name="Edit").click()
    place = page.locator("select[data-option=save_to]")
    tag_as = page.locator("select[data-option=tag_as]")
    assert place.input_value() == "files" and tag_as.input_value() == "nextcloud", "a job of before as it was"
    assert not page.locator("input[data-option=vault]").is_visible()
    assert not page.locator(".harvester-vault-warning").is_visible()
    place.select_option("inbox")
    assert (
        page.locator("input[data-option=vault]").is_visible()
        and page.locator("input[data-option=vault_target]").is_visible()
    )
    assert tag_as.input_value() == "labels" and page.locator(
        "select[data-option=tag_as] option[value=nextcloud]"
    ).evaluate("o => o.disabled")
    again = page.locator("input[data-option=again_deleted]")
    assert again.is_disabled() and "Off for an inbox" in page.locator(".harvester-off-reason").first.inner_text()
    place.select_option("library")
    assert not again.is_disabled() and page.locator(".harvester-off-reason").count() == 0
    place.select_option("vault")
    warning = page.locator(".harvester-vault-warning")
    assert warning.is_visible() and warning.get_attribute("role") == "alert"
    assert "whoever takes over the server can read the whole vault" in warning.inner_text()
    page.locator("input[data-option=vault]").fill("/Vaults/Harvester")
    page.locator("input[data-field=vault-password]").fill("vault secret")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_function("() => !document.querySelector('#harvester-job-form')")
    saved = next(body for method, path, body in CALLS if (method, path) == ("PUT", "/jobs/1"))
    assert saved["options"]["save_to"] == "vault" and saved["options"]["vault"] == "/Vaults/Harvester"
    assert saved["options"]["tag_as"] == "labels"
    assert ("PUT", "/jobs/1/vault", {"password": "vault secret"}) in CALLS
    assert all("vault secret" not in json.dumps(body) for method, path, body in CALLS if path != "/jobs/1/vault")


@needs_chromium
def test_a_jobs_files_with_their_labels(page):
    answering(
        page,
        {
            "GET /jobs/1/files": {
                "files": [{"file": 3, "path": "/Mag/cover.png", "at": 1, "tags": ["2024"]}],
                "labels": ["2024"],
            }
        },
    )
    page.get_by_role("button", name="Files", exact=True).click()
    page.wait_for_selector(".harvester-file-list")
    assert "/Mag/cover.png" in page.locator(".harvester-file-list").inner_text()
    page.get_by_label("Tag or label").select_option("2024")
    page.wait_for_function(
        "() => document.querySelector('select') && document.querySelector('select').value === '2024'"
    )
    assert ("GET", "/jobs/1/files?label=2024", None) in CALLS


@needs_chromium
def test_the_library_unlocks_and_shows_its_files(page):
    state = {"password": True, "unlocked": False, "minutes": 15, "usage": 2010, "limit": 10_000_000_000}
    answers = {
        "GET /library/state": state,
        "POST /library/unlock": (403, {"detail": "Wrong password."}),
        "GET /library": {"folder": "/Mag", "entries": ENTRIES, "labels": ["2024"]},
    }
    answering(page, answers)
    page.get_by_role("button", name="Library", exact=True).click()
    password = page.get_by_label("Library password")
    password.fill("wrong")
    page.get_by_role("button", name="Unlock").click()
    page.wait_for_selector("text=Wrong password.")
    answers["POST /library/unlock"] = {"token": "t0k3n"}
    state["unlocked"] = True
    password.fill("right")
    page.get_by_role("button", name="Unlock").click()
    page.wait_for_selector(".harvester-gallery")
    assert page.locator(".harvester-gallery img").get_attribute("src").endswith("/library/files/3?unlock=t0k3n")
    listed = page.locator(".harvester-library > .harvester-file-list").inner_text()
    assert "2024/" in listed and "issue.pdf" in listed
    assert ("/library", "t0k3n") in TOKENS, "the listing asked with the browser's token"
    assert "2.0 KB of 10.0 GB used" in page.locator(".harvester-library").inner_text()
    page.get_by_role("button", name="issue.pdf").click()
    viewer = page.locator("dialog.harvester-viewer")
    assert viewer.locator("iframe").get_attribute("src").endswith("/library/files/4?unlock=t0k3n")
    assert viewer.get_by_role("link", name="Download").get_attribute("href").endswith("&download=1")
    viewer.get_by_role("button", name="Close").click()
    assert not viewer.count()
