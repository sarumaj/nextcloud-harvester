"""The app's page (harvester-main.js) in Chromium against a stubbed API and WebSocket: the
recorder's navigation, the same controls for every pick (Pick again, the element, Remove), Undo
of a pick, numbered pages, scroll to load more and the recipe's test; a failed step's picture
from the progress log, with the job's browser opened at its page; jobs shared with the person
(their chip and buttons, the form without the owner's folder and account), the share panel and
the queue's status."""

# pylint: disable=missing-function-docstring

import json
import pathlib

import pytest
from test_browser import needs_chromium

import main
from harvester.browser import launch_options

PAGE = "https://nc.example/"
SCRIPT = (pathlib.Path(__file__).parent.parent / "ex_app" / "js" / "harvester-main.js").read_text(encoding="utf-8")
# Nextcloud's globals, and a WebSocket that keeps what the page sends and takes what a test
# answers (receive).
STUBS = """
window.OC = { generateUrl: (path) => path, requestToken: 't', webroot: '' }
window.__sockets = []
class FakeSocket {
  static OPEN = 1
  constructor(url) {
    this.url = url; this.sent = []; this.readyState = 1; this.listeners = {}
    window.__sockets.push(this)
    setTimeout(() => this.emit('open', {}), 0)
  }
  send(data) { this.sent.push(JSON.parse(data)) }
  close() { this.readyState = 3; this.emit('close', {}) }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener) }
  emit(type, event) { (this.listeners[type] || []).forEach((listener) => listener(event)) }
  receive(data) { this.emit('message', { data: JSON.stringify(data) }) }
}
window.WebSocket = FakeSocket
"""
RECIPE = {
    "steps": [{"action": "goto", "url": "https://site.example/login"}, {"action": "scroll"}],
    "start": "https://site.example/archiv",
    "follow": [{"selector": "a.year", "pattern": "y"}, {"selector": "a.month", "pattern": "m"}],
    "links": {"selector": "a.file", "pattern": "pdf"},
    "next": {"kind": "number", "selector": "nav.pages", "level": 0},
}
JOB = {
    "id": 1,
    "name": "Mag",
    "source": "site",
    "target": "/Mag",
    "schedule": "0 6 * * *",
    "timezone": "UTC",
    "options": {"recipe": json.dumps(RECIPE), "path": "{year}/{text}"},
    "tags": [],
    "enabled": True,
    "last_run": 1,
    "last_status": "failed",
    "last_message": "step 2 (click #go) failed",
    "challenge_url": None,
    "running": False,
    "queued": False,
    "paused": False,
    "waiting": "",
    "owner": "anna",
    "owner_name": "",
    "mine": True,
    "permissions": ["view", "run", "edit", "delete", "reshare"],
    "shares": [],
}
# Ben's job, shared with Anna to view and run; queued behind others' jobs.
SHARED = JOB | {
    "id": 2,
    "name": "Bens feed",
    "source": "feed",
    "target": "/Ben/Feed",
    "options": {"url": "https://feed.example/rss"},
    "last_status": "challenge",
    "queued": True,
    "waiting": "2. in the queue: waits for a runner, all 2 busy (1 job ahead)",
    "owner": "ben",
    "owner_name": "Ben Example",
    "mine": False,
    "permissions": ["view", "run"],
}
SHARE = {"kind": "user", "sharee": "carol", "label": "Carol Example", "permissions": ["view"], "by": "anna", "at": 1}
STATUS = {"runners": {"busy": 2, "total": 2}, "browsers": {"jobs": 2, "recordings": 1, "total": 3}, "queued": 3}
LOG = {
    "lines": [
        {"id": 1, "job": 1, "name": "Mag", "at": 1, "level": "error", "message": "step 2 (click #go) failed", "shot": 1}
    ],
    "running": [],
    "queued": [],
}
SHOT = {
    "at": 1,
    "url": "https://site.example/login",
    "title": "Sign in",
    "what": "step 2 (click #go) failed",
    "image": "/9j/",
}


# The API's requests the page made: (method, path with its query, body).
CALLS: list[tuple[str, str, dict | None]] = []


@pytest.fixture(name="page")
def fixture_page(request):
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    # A test may give its jobs (parametrized indirectly), else the one of its own.
    jobs = getattr(request, "param", [JOB])
    answers = {
        "/sources": main.list_sources(),
        "/presets": main.list_presets(),
        "/jobs": jobs,
        # The log's polls agree with the list (else the page draws the list again each time).
        "/log": LOG | {"queued": [job["id"] for job in jobs if job["queued"] and not job["running"]]},
        "/jobs/1/shot": SHOT,
        "/status": STATUS,
        "/jobs/1/shares": [SHARE],
        "/sharees": [{"kind": "group", "sharee": "family", "label": "Family"}],
        "PUT /jobs/1/shares": [SHARE, {**SHARE, "kind": "group", "sharee": "family", "label": "Family"}],
        "DELETE /jobs/1/shares/user/carol": [],
    }
    CALLS.clear()

    def api(route) -> None:
        method = route.request.method
        full = route.request.url.split("/api", 1)[1]
        path = full.split("?")[0]
        CALLS.append((method, full, json.loads(route.request.post_data) if route.request.post_data else None))
        answer = answers.get(f"{method} {path}", answers.get(path, {}))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))

    def document(route) -> None:
        body = f'<html><head><script>{STUBS}</script><script src="/main.js"></script></head>'
        body += '<body><main id="content"><div id="content"></div></main></body></html>'
        route.fulfill(status=200, content_type="text/html", body=body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options(9))  # a port nobody listens on: all fulfilled
        page = browser.new_page()
        page.route("**/apps/app_api/proxy/harvester/api/**", api)
        page.route(
            "**/main.js",
            lambda route: route.fulfill(status=200, content_type="text/javascript; charset=utf-8", body=SCRIPT),
        )
        page.route(PAGE, document)
        page.goto(PAGE)
        page.wait_for_selector("table.harvester-jobs")
        yield page
        browser.close()


def sent(page, socket: int = 0) -> list[dict]:
    return page.evaluate("n => window.__sockets[n].sent", socket)


def receive(page, data: dict, socket: int = 0) -> None:
    page.evaluate("([n, data]) => window.__sockets[n].receive(data)", [socket, data])


def level(page, target: str, number: int = 0):
    return page.locator(f".harvester-level[data-target={target}][data-level='{number}']")


@needs_chromium
def test_the_recorders_controls(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    assert sent(page)[0] == {"type": "load", "recipe": RECIPE}, "the recorder starts with the form's picks"

    # Navigation: buttons, by mouse or by keyboard.
    page.get_by_role("button", name="Back", exact=True).click()
    page.get_by_role("button", name="Forward", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Reload", exact=True).focus()
    page.keyboard.press("Space")
    assert [m["type"] for m in sent(page)[1:]] == ["back", "forward", "reload"]

    proposals = {
        "next": {
            "kind": "number",
            "link": {"selector": "a.n", "level": 0},
            "number": RECIPE["next"],
            "numbers": [1, 2, 3],
            "current": 1,
            "following": 2,
        }
    }
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": []}, "proposals": proposals})
    assert "Scroll to load more" in page.locator(".harvester-steps ol").inner_text(), "the form's steps stay"
    page.wait_for_selector(".harvester-level[data-target=next]")
    before = len(sent(page))
    level(page, "follow", 1).get_by_role("button", name="Remove").click()
    level(page, "follow", 1).get_by_role("button", name="Pick again").click()
    level(page, "links").get_by_role("button", name="Pick again").click()
    level(page, "links").locator("input").first.fill("a.pdf")
    level(page, "links").locator("input").first.press("Tab")
    assert "this is page 1, the next is 2" in level(page, "next").inner_text()
    level(page, "next").get_by_role("button", name="Use the picked link alone").click()
    level(page, "next").get_by_role("button", name="Remove").click()
    page.get_by_role("button", name="Undo pick").click()
    assert sent(page)[before:] == [
        {"type": "remove", "target": "follow", "level": 1},
        {"type": "mode", "mode": "follow", "level": 1},
        {"type": "mode", "mode": "links"},
        {"type": "selector", "target": "links", "level": 0, "selector": "a.pdf"},
        {"type": "next-kind", "kind": "link"},
        {"type": "remove", "target": "next", "level": 0},
        {"type": "undo-pick"},
    ]
    assert page.locator("input[value=follow]").is_checked() is False and page.locator("input[value=links]").is_checked()

    # Scroll to load more: a step, so only while recording.
    scroll = page.get_by_role("button", name="Add: scroll to load more")
    assert scroll.is_disabled()
    page.locator("input[value=record]").check()
    scroll.click()
    assert sent(page)[-1] == {"type": "scroll"}

    page.get_by_role("button", name="Test the recipe").click()
    test = sent(page)[-1]
    assert test["type"] == "test" and test["settings"]["path"] == "{year}/{text}"
    receive(page, {"type": "test-started"})
    page.get_by_role("button", name="Stop the test").click()
    assert sent(page)[-1] == {"type": "test-stop"}
    result = {
        "pages": [
            {
                "level": 0,
                "page": 1,
                "url": "https://site.example/archiv",
                "kind": "links",
                "selector": "a.file",
                "found": 3,
                "matching": 2,
                "files": [{"url": "https://site.example/a.pdf", "path": "2026/A", "problem": ""}],
            }
        ],
        "problems": ["a level 1 page (page 1, https://site.example/2026): a.month matched nothing"],
        "files": 1,
        "ended": "complete",
        "http": True,
    }
    receive(page, {"type": "test", "result": result})
    panel = page.locator(".harvester-test").inner_text()
    assert "1 file links on 1 pages" in panel and "2026/A" in panel and "a.month matched nothing" in panel

    # A recipe without a next page (removed) is kept without one.
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": [], "next": None}, "proposals": {}})
    page.get_by_role("button", name="Done").click()
    kept = json.loads(page.locator("textarea.harvester-recipe").first.input_value())
    assert "next" not in kept and kept["steps"] == RECIPE["steps"]


@needs_chromium
def test_a_failed_steps_picture_and_the_browser_at_its_page(page):
    page.get_by_role("button", name="Show what the browser saw").click()
    image = page.locator("img.harvester-shot-image")
    image.wait_for()
    assert image.get_attribute("src") == "data:image/jpeg;base64,/9j/"
    assert "https://site.example/login" in page.locator(".harvester-shot").inner_text()
    page.get_by_role("button", name="Open in the browser").click()
    page.wait_for_function("window.__sockets.length")
    assert page.evaluate("window.__sockets[0].url").endswith("/exapps/harvester/ws/record?job=1&at=shot")
    page.locator("input.harvester-url").fill("https://site.example/other")
    page.get_by_role("button", name="Go", exact=True).click()
    page.get_by_role("button", name="Back", exact=True).click()
    assert sent(page) == [{"type": "goto", "url": "https://site.example/other"}, {"type": "back"}]


@needs_chromium
@pytest.mark.parametrize("page", [[JOB, SHARED]], indirect=True)
def test_shared_jobs_show_whose_they_are_and_only_what_may_be_done(page):
    assert page.locator(".harvester-load").inner_text() == "Runners 2/2 · Browsers 3/3 (2 jobs, 1 recording) · 3 queued"
    row = page.locator("tbody tr").nth(1)
    assert "shared by Ben Example" in row.inner_text()
    assert "2. in the queue: waits for a runner, all 2 busy (1 job ahead)" in row.inner_text()
    buttons = row.locator("button").all_inner_texts()
    # Run (queued: off), no Edit but View, no Delete, and no browser with Ben's session.
    assert buttons == ["Run now", "View"]
    assert page.locator("tbody tr").nth(0).locator("button").all_inner_texts() == ["Run now", "Edit", "Delete"]
    row.get_by_role("button", name="View").click()
    page.wait_for_selector("#harvester-job-form")
    form = page.locator("#harvester-job-form")
    assert form.locator("input:not([disabled])").count() == 0, "view only"
    assert page.get_by_role("button", name="Save").is_hidden()
    assert "In Ben Example’s files" in form.inner_text()
    assert "Ben Example’s account" in page.locator(".harvester-credentials").inner_text()
    assert page.locator(".harvester-share").count() == 0, "no reshare: no share panel"
    assert not [call for call in CALLS if "/credentials" in call[1]], "the owner's account is not asked for"


@needs_chromium
def test_the_owner_shares_a_job(page):
    page.get_by_role("button", name="Edit").click()
    panel = page.locator(".harvester-share")
    panel.locator("li[data-sharee='user:carol']").wait_for()
    boxes = panel.locator("li[data-sharee='user:carol'] input[type=checkbox]")
    assert [box.get_attribute("data-permission") for box in boxes.all()] == ["view", "run", "edit", "delete", "reshare"]
    assert boxes.nth(0).is_checked() and boxes.nth(0).is_disabled(), "View always"
    panel.get_by_role("searchbox", name="Share with").fill("fam")
    panel.get_by_role("button", name="Family (group)").click()
    panel.locator("li[data-sharee='group:family']").wait_for()
    assert CALLS[-1] == ("PUT", "/jobs/1/shares", {"kind": "group", "sharee": "family", "permissions": ["view"]})
    assert ("GET", "/sharees?search=fam&job=1", None) in CALLS
    panel.locator("li[data-sharee='user:carol'] input[data-permission=edit]").check()
    assert CALLS[-1] == ("PUT", "/jobs/1/shares", {"kind": "user", "sharee": "carol", "permissions": ["view", "edit"]})
    panel.get_by_role("button", name="Remove the share with Carol Example").click()
    panel.get_by_text("Not shared yet.").wait_for()
    assert CALLS[-1][:2] == ("DELETE", "/jobs/1/shares/user/carol")


@needs_chromium
@pytest.mark.parametrize(
    "page", [[SHARED | {"id": 1, "permissions": ["view", "edit", "reshare"], "queued": False}]], indirect=True
)
def test_an_editor_who_reshares_keeps_the_owners_folder_and_gives_no_more(page):
    page.get_by_role("button", name="Edit").click()
    page.wait_for_selector("#harvester-job-form")
    assert page.locator("#harvester-job-form input").first.is_enabled(), "the name is theirs to edit"
    folder = page.locator('label.harvester-field:has(> span:text-is("Folder")) input')
    assert folder.is_disabled() and folder.input_value() == "/Ben/Feed"
    page.locator(".harvester-share li[data-sharee='user:carol']").wait_for()
    allowed = {
        box.get_attribute("data-permission"): box.is_enabled()
        for box in page.locator(".harvester-share li[data-sharee='user:carol'] input").all()
    }
    assert allowed == {"view": False, "run": False, "edit": True, "delete": False, "reshare": True}
