# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipes as files: the export of a job's form (no account, session or folder), the import's
checks (size, JSON, format and version, the schema's fields, the source's options, the checks a
saved job passes) and the form it fills; a person's own templates. In Chromium, the page: Export
recipe, Import a recipe, Save as template and the templates' list, against a stubbed API."""

# pylint: disable=missing-function-docstring

import asyncio
import json

import pytest
from fastapi import HTTPException
from test_browser import needs_chromium
from test_page import JOB, RECIPE, STUBS, served
from test_runner import FakeNextcloud

import main
from harvester import recipe_file
from harvester.routes import recipes
from harvester.store import Store

SIGNED_IN = RECIPE | {
    "steps": [
        {"action": "goto", "url": "https://site.example/login"},
        {"action": "fill", "selector": "#user", "credential": "username", "value": "anna@example.org"},
        {"action": "fill", "selector": "#q", "value": "iX"},
    ]
}


def form(**changes) -> main.JobIn:
    """A website job as the form sends it."""
    fields = {
        "name": "Mag",
        "source": "site",
        "target": "/Private/Mag",
        "schedule": "0 6 * * 1",
        "timezone": "Europe/Berlin",
        "tags": ["Mag {year}"],
        "options": {"recipe": json.dumps(SIGNED_IN), "path": "{year}/{text}", "weekly_test": 1, "max_items": 3},
    }
    return main.JobIn(**(fields | changes))


def nextcloud(user: str = "anna") -> FakeNextcloud:
    nc = FakeNextcloud()
    nc._user = user  # pylint: disable=protected-access
    return nc


def exported(**changes) -> dict:
    return asyncio.run(recipes.export_recipe(form(**changes), nextcloud()))


def imported(file: dict | str) -> dict:
    text = file if isinstance(file, str) else json.dumps(file)
    return asyncio.run(recipes.import_recipe(recipes.RecipeText(text=text), nextcloud()))


def refusal(file: dict | str) -> str:
    with pytest.raises(HTTPException) as refused:
        imported(file)
    assert refused.value.status_code == 422
    return str(refused.value.detail)


def test_an_exported_recipe_has_no_account_session_or_folder():
    file = exported(options=form().options | {"password": "secret", "cookies": "sid=1", "session": "x"})
    assert file == {
        "format": "harvester-recipe",
        "version": 1,
        "name": "Mag",
        "source": "site",
        "schedule": "0 6 * * 1",
        "once": False,
        "tags": ["Mag {year}"],
        "options": {
            "recipe": SIGNED_IN
            | {
                "steps": [
                    {"action": "goto", "url": "https://site.example/login"},
                    {"action": "fill", "selector": "#user", "credential": "username"},
                    {"action": "fill", "selector": "#q", "value": "iX"},
                ]
            },
            "path": "{year}/{text}",
            "weekly_test": 1,
            "max_items": 3,
        },
    }
    text = json.dumps(file)
    for secret in ("secret", "sid=1", "anna@example.org", "/Private", "Europe/Berlin"):
        assert secret not in text, secret
    assert exported(schedule="")["schedule"] == "", "on demand"


def test_an_imported_recipe_fills_the_form_without_a_folder():
    job = imported(exported())
    assert job == {
        "name": "Mag",
        "source": "site",
        "schedule": "0 6 * * 1",
        "once": False,
        "tags": ["Mag {year}"],
        "options": {
            "recipe": json.dumps(json.loads(job["options"]["recipe"]), indent=2),
            "path": "{year}/{text}",
            "weekly_test": 1,
            "max_items": 3,
        },
        "target": "",
        "enabled": True,
    }
    assert json.loads(job["options"]["recipe"])["links"] == RECIPE["links"]
    assert imported(exported() | {"options": {"recipe": json.dumps(RECIPE)}})["options"]["recipe"] == json.dumps(RECIPE)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"version": 2}, "version 2 is not one this Harvester reads"),
        ({"version": "one"}, "version"),
        ({"format": "something-else"}, "no Harvester recipe"),
        ({"source": "shell"}, "unknown source shell"),
        ({"target": "/etc"}, "target: Extra inputs are not permitted"),
        ({"credentials": {"password": "x"}}, "credentials: Extra inputs are not permitted"),
        ({"name": ""}, "name: String should have at least 1 character"),
        ({"tags": ["x"] * 51}, "tags"),
        ({"options": {"password": "x"}}, "options Website (recipe) has not: password"),
        ({"options": {"nonsense": 1}}, "has not: nonsense"),
        ({"options": {"path": {"a": 1}}}, "options.path must be text or a number"),
        ({"options": {"max_items": "many"}}, "Files per run must be a number"),
        ({"options": {"recipe": {"start": "file:///etc/passwd"}}}, "file:///etc/passwd may not be opened"),
        ({"schedule": "every day"}, "not a cron expression"),
        ({"once": True}, "Only an on-demand job is deleted after its run"),
    ],
)
def test_an_imported_recipe_is_checked_as_a_saved_job(change, message):
    assert message in refusal(exported() | change)


def test_a_file_that_is_no_recipe_is_refused():
    assert "no JSON" in refusal("{not json")
    assert "no Harvester recipe" in refusal("[1, 2]")
    # Its bytes count (the page's limit too), not its characters.
    large = json.dumps(exported() | {"name": "é" * (recipe_file.MAX_BYTES // 2)}, ensure_ascii=False)
    assert len(large) < recipe_file.MAX_BYTES and "larger than a recipe" in refusal(large)
    with pytest.raises(ValueError):
        recipes.RecipeText(text="x" * (recipe_file.MAX_BYTES + 1))


def test_own_templates_are_each_persons_and_have_no_folder(tmp_path, monkeypatch):
    monkeypatch.setitem(main.STATE, "store", Store(str(tmp_path / "db")))
    saved = asyncio.run(recipes.save_template(form(), nextcloud()))
    assert saved["name"] == "Mag" and "target" not in saved["file"] and saved["file"] == exported()
    assert asyncio.run(recipes.list_templates(nextcloud("ben"))) == [], "another person's are not listed"
    [listed] = asyncio.run(recipes.list_templates(nextcloud()))
    assert imported(listed["file"])["options"]["path"] == "{year}/{text}"
    with pytest.raises(HTTPException):
        asyncio.run(recipes.delete_template(listed["id"], nextcloud("ben")))
    asyncio.run(recipes.delete_template(listed["id"], nextcloud()))
    assert asyncio.run(recipes.list_templates(nextcloud())) == []


# The page.

FILE = {
    "format": "harvester-recipe",
    "version": 1,
    "name": "Mag",
    "source": "site",
    "schedule": "0 6 * * 1",
    "once": False,
    "tags": ["Mag {year}"],
    "options": {"recipe": RECIPE, "path": "{year}/{text}"},
}
CHANGED = JOB | {
    "last_status": "changed",
    "last_message": "The site seems to have changed: level 1 found nothing (it found 8 before); 0 downloaded",
}
CALLS: list[tuple[str, str, dict | None]] = []


@pytest.fixture(name="page")
def fixture_page():
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    from harvester.browser import launch_options  # pylint: disable=import-outside-toplevel

    answers = {
        "/sources": main.list_sources(),
        "/presets": main.list_presets(),
        "/jobs": [CHANGED],
        # Its form asks for the job with its options (the list's are brief in the app).
        f"/jobs/{CHANGED['id']}": CHANGED,
        "/log": {"lines": [], "running": [], "queued": []},
        "/status": {},
        "/templates": [{"id": 4, "name": "My mag", "file": FILE}],
        "POST /recipes/export": FILE,
        # The server's answer to an import: the form's job (checked there).
        "POST /recipes/import": imported(FILE),
    }
    CALLS.clear()

    def api(route) -> None:
        method = route.request.method
        path = route.request.url.split("/api", 1)[1].split("?")[0]
        CALLS.append((method, path, json.loads(route.request.post_data) if route.request.post_data else None))
        if (method, path) == ("POST", "/recipes/import") and "Bad" in (route.request.post_data or ""):
            route.fulfill(status=422, content_type="application/json", body=json.dumps({"detail": "version 9 no"}))
            return
        answer = answers.get(f"{method} {path}", answers.get(path, {}))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))

    def document(route) -> None:
        body = f'<html><head><script>{STUBS}</script><script src="/main.js"></script></head>'
        body += '<body><main id="content"><div id="content"></div></main></body></html>'
        route.fulfill(status=200, content_type="text/html", body=body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options(9))
        page = browser.new_page(accept_downloads=True)
        served(page, api, document)
        yield page
        browser.close()


@needs_chromium
def test_a_changed_job_says_so_and_offers_its_test(page):
    row = page.locator("tbody tr").first
    assert row.locator(".harvester-status").inner_text() == "Changed"
    assert "level 1 found nothing (it found 8 before)" in row.inner_text()
    row.get_by_role("button", name="Test the recipe").click()
    page.wait_for_function("document.querySelector('table.harvester-jobs')")
    assert ("POST", "/jobs/1/test", None) in CALLS


@needs_chromium
def test_export_save_as_template_and_import(page, tmp_path):
    page.get_by_role("button", name="Edit").click()
    page.get_by_text("Test the recipe weekly").wait_for()  # the option, in the website job's form
    with page.expect_download() as download:
        page.get_by_role("button", name="Export recipe").click()
    assert download.value.suggested_filename == "Mag.harvester.json"
    path = tmp_path / "Mag.harvester.json"
    download.value.save_as(path)
    assert json.loads(path.read_text(encoding="utf-8")) == FILE
    method, _, body = [call for call in CALLS if call[1] == "/recipes/export"][-1]
    assert method == "POST" and body and body["target"] == "/Mag" and body["options"]["path"] == "{year}/{text}"
    page.get_by_role("button", name="Save as template").click()
    page.get_by_text("Saved as your template").wait_for()
    assert [call[0] for call in CALLS if call[1] == "/templates"][-1] == "POST"

    page.get_by_role("button", name="Back to the jobs").click()
    page.get_by_role("button", name="New job").click()
    page.locator(".harvester-own-template").wait_for()
    assert page.locator(".harvester-own-template h3").inner_text() == "My mag"

    # A file that is refused says why, and stays on the page.
    page.get_by_label("Recipe file").set_input_files(
        files=[{"name": "bad.json", "mimeType": "application/json", "buffer": b'{"name": "Bad"}'}]
    )
    page.get_by_text("version 9 no").wait_for()
    page.get_by_label("Recipe file").set_input_files(
        files=[{"name": "mag.json", "mimeType": "application/json", "buffer": json.dumps(FILE).encode()}]
    )
    form_ = page.locator("#harvester-job-form")
    form_.wait_for()
    assert [call for call in CALLS if call[1] == "/recipes/import"][-1][2] == {"text": json.dumps(FILE)}
    folder = page.locator('label.harvester-field:has(> span:text-is("Folder")) input')
    assert folder.input_value() == "" and folder.get_attribute("required") is not None, "the person chooses it"
    assert page.locator('label.harvester-field:has(> span:text-is("Name")) input').input_value() == "Mag"
    assert json.loads(page.locator("textarea.harvester-recipe").first.input_value())["links"] == RECIPE["links"]
    saved = len([call for call in CALLS if call[1] == "/jobs" and call[0] == "POST"])
    page.get_by_role("button", name="Save", exact=True).click()
    assert len([call for call in CALLS if call[1] == "/jobs" and call[0] == "POST"]) == saved, "no folder: not sent"

    # A template of the person's: used as an import, or removed.
    page.get_by_role("button", name="Back to the jobs").click()
    page.get_by_role("button", name="New job").click()
    page.locator(".harvester-own-template").get_by_role("button", name="Use this template").click()
    page.locator("#harvester-job-form").wait_for()
    sent = [call for call in CALLS if call[1] == "/recipes/import"][-1][2] or {}
    assert json.loads(sent["text"]) == FILE, "checked as an imported file is"
    page.get_by_role("button", name="Back to the jobs").click()
    page.get_by_role("button", name="New job").click()
    page.get_by_role("button", name="Remove the template My mag").click()
    page.locator("dialog").get_by_role("button", name="Remove").click()  # asked in the page
    for _ in range(100):
        if ("DELETE", "/templates/4", None) in CALLS:
            break
        page.wait_for_timeout(50)
    assert ("DELETE", "/templates/4", None) in CALLS
