"""The app's page (harvester-main.js) in Chromium against a stubbed API and WebSocket: the
recorder's navigation, the same controls for every pick (Pick again, the element, Remove), Undo
of a pick, numbered pages, scroll to load more and the recipe's test; a failed step's picture
from the progress log, with the job's browser opened at its page; the queue's status with why a
job waits; and on-demand jobs: Fetch a URL…, the waiting addresses, the form's On demand, and the
Send to Harvester bookmarklet with the page it opens (?fetch=); the recorder's steps in frames,
their switch "optional" with its hint for consent buttons, and "Acting in"."""

# pylint: disable=missing-function-docstring

import json
import pathlib
import re
from urllib.parse import quote

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
}
# A second job of the person's, queued behind others' jobs.
QUEUED = JOB | {
    "id": 2,
    "name": "Feed",
    "source": "feed",
    "target": "/Feed",
    "options": {"url": "https://feed.example/rss"},
    "last_status": "ok",
    "queued": True,
    "waiting": "2. in the queue: waits for a runner, all 2 busy (1 job ahead)",
}
# An on-demand job without an address of its own, one address being fetched and one waiting.
CLIPS = JOB | {
    "id": 3,
    "name": "Clips",
    "source": "media",
    "target": "/Clips",
    "schedule": "",
    "once": True,
    "options": {"url": ""},
    "last_status": None,
    "queued": True,
    "running": True,
    "fetches": [
        {"id": 6, "url": "https://v.example/watch?v=1", "at": 1, "running": True},
        {"id": 7, "url": "https://v.example/watch?v=2", "at": 2, "running": False},
    ],
}
SENT = "https://v.example/watch?v=3"
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
        "/log": LOG
        | {
            "running": [job["id"] for job in jobs if job["running"]],
            "queued": [job["id"] for job in jobs if job["queued"] and not job["running"]],
            "fetches": {
                str(job["id"]): [fetch["id"] for fetch in job["fetches"]] for job in jobs if job.get("fetches")
            },
        },
        "/jobs/1/shot": SHOT,
        "/status": STATUS,
        "POST /jobs/3/fetch": {"id": 8, "url": SENT, "at": 3, "new": True},
        "POST /jobs": CLIPS | {"id": 9, "name": "v.example", "once": False, "fetches": []},
        "POST /jobs/9/fetch": {"id": 10, "url": SENT, "at": 3, "new": True},
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
        page.route(re.compile(r"^https://nc\.example/(\?.*)?$"), document)
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
@pytest.mark.parametrize("page", [[JOB, QUEUED]], indirect=True)
def test_the_queues_status_and_why_a_job_waits(page):
    assert page.locator(".harvester-load").inner_text() == "Runners 2/2 · Browsers 3/3 (2 jobs, 1 recording) · 3 queued"
    row = page.locator("tbody tr").nth(1)
    assert "2. in the queue: waits for a runner, all 2 busy (1 job ahead)" in row.inner_text()
    assert row.locator("button").all_inner_texts() == ["Run now", "Edit", "Delete"]
    assert row.get_by_role("button", name="Run now").is_disabled(), "queued already"


@needs_chromium
@pytest.mark.parametrize("page", [[JOB, CLIPS]], indirect=True)
def test_an_on_demand_job_fetches_an_address(page):
    row = page.locator("tbody tr").nth(1)
    assert row.locator("td").nth(2).locator(".harvester-chip").all_inner_texts() == ["On demand", "once"]
    fetches = row.locator(".harvester-fetches li")
    assert fetches.all_inner_texts() == ["https://v.example/watch?v=1 fetching", "https://v.example/watch?v=2Cancel"]
    # Running: Pause and Stop; no Run now without an address of its own.
    assert row.locator("td.harvester-actions button").all_inner_texts() == [
        "Pause",
        "Stop",
        "Fetch a URL…",
        "Edit",
        "Delete",
    ]
    row.get_by_role("button", name="Cancel the fetch of https://v.example/watch?v=2").click()
    page.wait_for_function("document.querySelector('tbody tr:nth-child(2) .harvester-fetches')")
    assert ("DELETE", "/jobs/3/fetch/7", None) in CALLS
    page.locator("tbody tr").nth(1).get_by_role("button", name="Fetch a URL…").click()
    page.get_by_role("textbox", name="The address Clips fetches").fill(SENT)
    page.get_by_role("button", name="Fetch", exact=True).click()
    page.wait_for_function("!document.querySelector('.harvester-fetch-row')")
    assert ("POST", "/jobs/3/fetch", {"url": SENT}) in CALLS


@needs_chromium
@pytest.mark.parametrize("page", [[JOB, CLIPS]], indirect=True)
def test_the_form_of_an_on_demand_job(page):
    page.locator("tbody tr").nth(1).get_by_role("button", name="Edit").click()
    form = page.locator("#harvester-job-form")
    on_demand, once = form.locator("[data-field=on-demand]"), form.locator("[data-field=once]")
    assert on_demand.is_checked() and once.is_checked() and once.is_visible()
    schedule = page.locator('label.harvester-field:has(> span:text-is("Schedule")) input')
    assert schedule.is_hidden()
    on_demand.uncheck()
    assert schedule.is_visible() and schedule.input_value() == "0 6 * * *" and once.is_hidden()
    on_demand.check()
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector("table.harvester-jobs")
    method, path, body = [call for call in CALLS if call[0] == "PUT"][-1]
    assert (method, path) == ("PUT", "/jobs/3") and body and body["schedule"] == "" and body["once"] is True
    # A source without an address has no On demand.
    page.get_by_role("button", name="New job").click()
    page.locator(".harvester-kind", has_text="Email attachments").get_by_role("button", name="Start").click()
    assert page.locator("#harvester-job-form [data-field=on-demand]").count() == 0


@needs_chromium
@pytest.mark.parametrize("page", [[JOB, CLIPS]], indirect=True)
def test_the_bookmarklet_sends_the_page_to_an_on_demand_job(page):
    page.get_by_text("Send to Harvester from any page").click()
    link = page.get_by_role("link", name="Send to Harvester")
    code = link.get_attribute("href") or ""
    assert (
        code.startswith("javascript:") and "https://nc.example/apps/app_api/embedded/harvester/harvester?fetch=" in code
    )
    page.goto(f"{PAGE}?fetch={quote(SENT, safe='')}#top")
    page.wait_for_selector(".harvester-sent")
    assert page.locator(".harvester-sent").inner_text() == SENT
    assert page.evaluate("location.search + location.hash") == "#top", "a reload shows the jobs"
    choices = page.get_by_role("combobox", name="On-demand job").locator("option").all_inner_texts()
    assert choices == ["Clips (/Clips)"], "on-demand jobs only"
    page.get_by_role("button", name="Fetch", exact=True).click()
    page.wait_for_selector("table.harvester-jobs")
    assert ("POST", "/jobs/3/fetch", {"url": SENT}) in CALLS


@needs_chromium
def test_a_new_job_from_a_sent_address(page):
    page.goto(f"{PAGE}?fetch={quote(SENT, safe='')}")
    page.wait_for_selector(".harvester-sent")
    assert "No on-demand job yet" in page.locator("#harvester").inner_text()
    kinds = page.locator(".harvester-send-kinds button").all_inner_texts()
    assert kinds == ["Website (recipe)", "Feed or podcast", "Videos and audio (yt-dlp)", "Image galleries (gallery-dl)"]
    page.get_by_role("button", name="Videos and audio (yt-dlp)").click()
    form = page.locator("#harvester-job-form")
    assert form.locator("[data-field=on-demand]").is_checked()
    assert page.locator('label.harvester-field:has(> span:text-is("Page")) input').input_value() == SENT
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector("table.harvester-jobs")
    created = [call for call in CALLS if call[:2] == ("POST", "/jobs")][-1][2]
    assert created and created["schedule"] == "" and created["options"]["url"] == SENT
    assert ("POST", "/jobs/9/fetch", {"url": SENT}) in CALLS, "the new job fetches the address sent"


@needs_chromium
def test_the_recorders_frames_and_optional_steps(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    frame = [{"selector": 'iframe[title="Consent"]', "url": "https://consent.example/cmp"}]
    steps = [
        {"action": "goto", "url": "https://site.example/login"},
        {"action": "click", "selector": "button.accept", "text": "Alle akzeptieren", "frame": frame},
        {"action": "click", "selector": "a.go", "text": "Archive"},
    ]
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": steps}, "proposals": {}})
    items = page.locator(".harvester-steps ol > li")
    assert "Click “Alle akzeptieren” ⧉ in frame consent.example" in items.nth(1).inner_text()
    assert items.nth(1).get_by_role("button", name="make optional?").count() == 1, "a consent button: the hint"
    assert items.nth(2).get_by_role("button", name="make optional?").count() == 0, "no hint for other clicks"
    assert "⧉" not in items.nth(2).inner_text()

    items.nth(1).get_by_role("button", name="make optional?").click()
    assert sent(page)[-1] == {"type": "optional", "step": 1, "optional": True}
    assert items.nth(1).get_by_role("checkbox").is_checked() and items.nth(1).get_by_role("button").count() == 0
    items.nth(2).get_by_role("checkbox").check()
    items.nth(2).get_by_role("checkbox").uncheck()
    assert sent(page)[-2:] == [
        {"type": "optional", "step": 2, "optional": True},
        {"type": "optional", "step": 2, "optional": False},
    ]

    where = page.locator(".harvester-where")
    assert where.inner_text() == "Acting in: the page"
    receive(
        page, {"type": "where", "frame": {"host": "cmp-cdn.example", "url": "https://cmp-cdn.example/d", "level": 2}}
    )
    assert where.inner_text() == "Acting in a frame: cmp-cdn.example (2nd level)"
    receive(page, {"type": "where", "frame": None})
    assert where.inner_text() == "Acting in: the page"

    result = {
        "pages": [],
        "problems": [],
        "files": 0,
        "ended": "complete",
        "skipped": [{"number": 2, "label": "click “Alle akzeptieren” in frame consent.example", "why": "Timeout"}],
    }
    receive(page, {"type": "test", "result": result})
    assert "Optional steps skipped:" in page.locator(".harvester-test").inner_text()
    assert (
        "step 2 (click “Alle akzeptieren” in frame consent.example): Timeout"
        in page.locator(".harvester-skipped").inner_text()
    )
    assert page.locator(".harvester-test .harvester-error").count() == 0, "not a problem"

    page.get_by_role("button", name="Done").click()
    kept = json.loads(page.locator("textarea.harvester-recipe").first.input_value())
    assert kept["steps"][1]["optional"] is True and kept["steps"][1]["frame"] == frame
    assert "optional" not in kept["steps"][2]
