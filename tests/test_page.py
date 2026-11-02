"""The app's page (harvester-main.js) in Chromium against a stubbed API and WebSocket: the
recorder's navigation, the same controls for every pick (Pick again, the element, Remove), Undo
of a pick, numbered pages, scroll to load more and the recipe's test; and a failed step's
picture from the progress log, with the job's browser opened at its page."""

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
}
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


@pytest.fixture(name="page")
def fixture_page():
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    answers = {
        "/sources": main.list_sources(),
        "/presets": main.list_presets(),
        "/jobs": [JOB],
        "/log": LOG,
        "/jobs/1/shot": SHOT,
    }

    def api(route) -> None:
        path = route.request.url.split("/api", 1)[1].split("?")[0]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answers.get(path, {})))

    def document(route) -> None:
        body = f'<html><head><script>{STUBS}</script><script src="/main.js"></script></head>'
        body += '<body><main id="content"><div id="content"></div></main></body></html>'
        route.fulfill(status=200, content_type="text/html", body=body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options(9))  # a port nobody listens on: all fulfilled
        page = browser.new_page()
        page.route("**/apps/app_api/proxy/harvester/api/**", api)
        page.route("**/main.js", lambda route: route.fulfill(status=200, content_type="text/javascript", body=SCRIPT))
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
