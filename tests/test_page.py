# SPDX-License-Identifier: AGPL-3.0-or-later
"""The app's page (harvester-main.js) in Chromium against a stubbed API and WebSocket: the
recorder's navigation, the same controls for every pick (Pick again, the element, Remove), Undo
of a pick, numbered pages, scroll to load more and the recipe's test; a failed step's picture
from the progress log, with the job's browser opened at its page; the queue's status with why a
job waits; and on-demand jobs: Fetch a URL…, the waiting addresses, the form's On demand, and the
Send to Harvester bookmarklet with the page it opens (?fetch=); the recorder's steps in frames,
their switch "optional" with its hint for consent buttons, and "Acting in"; the cookies panel of both
browser views; the server's texts with values (templates) in the person's language, older plain
ones as they came."""

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
ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = (ROOT / "ex_app" / "js" / "harvester-main.js").read_text(encoding="utf-8")
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
# Nextcloud's translations, for a test in a language (@pytest.mark.language): OC.L10N.register
# (l10n/<language>.js), and t() and n() as Nextcloud's, which escape and sanitize unless told
# not to (the page tells them not to: it shows text).
L10N_STUBS = """
const bundles = {}
const plurals = {}
window.OC.L10N = { register: (app, bundle, form) => {
  bundles[app] = bundle
  plurals[app] = new Function('n', `${form.replace(/nplurals=\\d+;\\s*plural=/, 'return ').replace(/;\\s*$/, '')}`)
} }
const build = (text, vars, count, options) => {
  let out = text.replace(/%n/g, String(count))
  if (vars) out = out.replace(/{(\\w+)}/g, (m, k) => (k in vars ? String(vars[k]) : m))
  return options && options.sanitize === false ? out : out.replace(/</g, '&lt;')
}
window.t = (app, text, vars, count, options) => build(typeof (bundles[app] || {})[text] === 'string' ? bundles[app][text] : text, vars, count, options)
window.n = (app, one, many, count, vars, options) => {
  const forms = (bundles[app] || {})[`_${one}_::_${many}_`]
  return build(Array.isArray(forms) ? forms[Number(plurals[app](count))] : count === 1 ? one : many, vars, count, options)
}
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
# A running job whose sign-in asks its person for the code from their phone.
ASKING = JOB | {"running": True, "asking": {"kind": "code", "label": "the bank's SMS", "until": 4102444800}}
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


def briefly(job: dict) -> dict:
    """A job as GET /api/jobs?brief=1 lists it (main.listed): its own address, no options."""
    rest = {key: value for key, value in job.items() if key != "options"}
    return rest | {"address": main.SOURCES[job["source"]].address_of(job["options"])}


# The API's requests the page made: (method, path with its query, body).
CALLS: list[tuple[str, str, dict | None]] = []


@pytest.fixture(name="page")
def fixture_page(request):
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    # A test may give its jobs (parametrized indirectly), else the one of its own, and the
    # person's language (AppAPI adds its l10n/<language>.js, if the app has one, after the page).
    jobs = getattr(request, "param", [JOB])
    marker = request.node.get_closest_marker("language")
    language = ROOT / "l10n" / f"{marker.args[0]}.js" if marker else None
    answers = {
        "/sources": main.list_sources(),
        "/presets": main.list_presets(),
        "/jobs": jobs,
        # The log's polls agree with the list (else the page draws the list again each time).
        "/log": LOG
        | {
            "running": [job["id"] for job in jobs if job["running"]],
            "queued": [job["id"] for job in jobs if job["queued"] and not job["running"]],
            "asking": [job["id"] for job in jobs if job["running"] and job.get("asking")],
            "fetches": {
                str(job["id"]): [fetch["id"] for fetch in job["fetches"]] for job in jobs if job.get("fetches")
            },
        },
        "/jobs/1/shot": SHOT,
        # The schedule in words as the server says it (the job's form asks as it is typed).
        "/schedule": main.explain_schedule("0 6 * * *"),
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
        # As the app answers: the list without the jobs' options (brief), each job with them.
        if method == "GET" and path == "/jobs" and "brief=1" in full:
            answer = [briefly(job) for job in jobs]
        elif method == "GET" and (asked := re.fullmatch(r"/jobs/(\d+)", path)):
            answer = next(job for job in jobs if job["id"] == int(asked[1]))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))

    def document(route) -> None:
        stubs = STUBS + (L10N_STUBS if language else "")
        # Nextcloud names the person's language on the page (lists are joined in it).
        body = f'<html lang="{marker.args[0] if marker else "en"}"><head><script>{stubs}</script>'
        body += '<script src="/main.js"></script>'
        if language and language.exists():
            body += f"<script>{language.read_text(encoding='utf-8')}</script>"
        body += "</head>"
        body += '<body><main id="content"><div id="content"></div></main></body></html>'
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options(9))  # a port nobody listens on: all fulfilled
        page = browser.new_page()
        served(page, api, document)
        yield page
        browser.close()


def served(page, api, document) -> None:
    """Opens the jobs page with its routes: the app's API (api), the page's script, and
    Nextcloud's document around it (document)."""
    page.route("**/apps/app_api/proxy/harvester/api/**", api)
    page.route(
        "**/main.js",
        lambda route: route.fulfill(status=200, content_type="text/javascript; charset=utf-8", body=SCRIPT),
    )
    page.route(re.compile(r"^https://nc\.example/(\?.*)?$"), document)
    page.goto(PAGE)
    page.wait_for_selector("table.harvester-jobs")


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
    assert sent(page)[0] == {"type": "load", "recipe": RECIPE}, "the recorder starts with the form's recipe"
    assert sent(page)[1] == {"type": "mode", "mode": "browse"}, "with steps: in Browse"
    assert page.locator("input[value=browse]").is_checked()

    # Navigation: buttons, by mouse or by keyboard.
    page.get_by_role("button", name="Back", exact=True).click()
    page.get_by_role("button", name="Forward", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Reload", exact=True).focus()
    page.keyboard.press("Space")
    assert [m["type"] for m in sent(page)[2:]] == ["back", "forward", "reload"]

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
    receive(page, {"type": "recipe", "recipe": RECIPE, "proposals": proposals})
    assert "Scroll to load more" in page.locator(".harvester-steps ol").inner_text(), "the form's steps"
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
    scroll = page.get_by_role("button", name="Add step: scroll to load more")
    assert scroll.is_disabled()
    page.locator("input[value=record]").click()
    page.get_by_role("button", name="Append").click()
    scroll.click()
    assert sent(page)[-3:] == [
        {"type": "record-at", "after": 2, "replace": False},
        {"type": "mode", "mode": "record"},
        {"type": "scroll"},
    ]

    # Without a next page: read as it is scrolled, which then shows as the next page.
    receive(page, {"type": "recipe", "recipe": RECIPE | {"next": {}}, "proposals": {}})
    page.get_by_role("button", name="Read it as it is scrolled").click()
    assert sent(page)[-1] == {"type": "next-kind", "kind": "scroll"}
    receive(page, {"type": "recipe", "recipe": RECIPE | {"next": {"kind": "scroll", "level": 0}}, "proposals": {}})
    assert "a batch at a time" in level(page, "next").inner_text()
    level(page, "next").get_by_role("button", name="Remove").click()
    assert sent(page)[-1] == {"type": "remove", "target": "next", "level": 0}

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
    assert "1 file link on 1 page." in panel and "2026/A" in panel and "a.month matched nothing" in panel
    # The outcome first, in one line coloured by it.
    assert (
        page.locator(".harvester-test-summary.harvester-test-failed").inner_text() == "Failed: 1 problem, listed below."
    )

    # A recipe without a next page (removed) is kept without one, with the recorder's steps.
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": RECIPE["steps"][:1], "next": None}, "proposals": {}})
    page.get_by_role("button", name="Done").click()
    kept = json.loads(page.locator("textarea.harvester-recipe").first.input_value())
    assert "next" not in kept and kept["steps"] == RECIPE["steps"][:1]


@needs_chromium
def test_the_recorders_screen_stops_while_hidden_and_its_socket_closes_when_left(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    hide = """hidden => {
      Object.defineProperty(document, 'visibilityState', { value: hidden ? 'hidden' : 'visible', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }"""
    page.evaluate(hide, True)
    page.evaluate(hide, False)
    assert [m for m in sent(page) if m["type"] == "screen"] == [
        {"type": "screen", "visible": False},
        {"type": "screen", "visible": True},
    ]
    # Another view of the app, without the recorder's Cancel: its browser ends at once.
    page.evaluate("document.getElementById('harvester').replaceChildren()")
    page.wait_for_function("window.__sockets[0].readyState === 3")
    page.evaluate(hide, True)
    assert [m for m in sent(page) if m["type"] == "screen"][-1]["visible"], "nothing sent once closed"


@needs_chromium
def test_the_recorders_frames_come_as_binary_messages_each_shown_by_an_object_url_revoked_after(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    assert page.evaluate("window.__sockets[0].binaryType") == "blob"
    frame = """() => window.__sockets[0].emit('message', { data: new Blob([new Uint8Array([255, 216, 255])]) })"""
    page.evaluate(frame)
    first = page.locator("img.harvester-screen").get_attribute("src")
    page.evaluate(frame)
    second = page.locator("img.harvester-screen").get_attribute("src")
    assert first.startswith("blob:") and second.startswith("blob:") and first != second
    revoked = "url => fetch(url).then(() => false, () => true)"
    assert page.evaluate(revoked, first) and not page.evaluate(revoked, second), "the frame before let go"
    assert not page.locator(".harvester-screen-notice").is_visible()


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
    assert row.locator("button").all_inner_texts() == ["Run now", "Files", "Edit", "Delete"]
    assert row.get_by_role("button", name="Run now").is_disabled(), "queued already"


@needs_chromium
def test_the_list_comes_without_options_the_form_asks_for_them_and_the_log_goes_on_where_it_was(page):
    page.wait_for_selector(".harvester-log li time")
    assert ("GET", "/jobs?brief=1", None) in CALLS and not [call for call in CALLS if call[1] == "/jobs"]
    assert [path for _, path, _ in CALLS if path.startswith("/log")] == ["/log?after=0"]
    # The list drawn again (Run now's answer): the log's line at once, only newer ones asked for.
    CALLS.clear()
    page.get_by_role("button", name="Run now").click()
    page.wait_for_function("[...document.querySelectorAll('.harvester-log li time')].length === 1")
    page.wait_for_timeout(200)
    assert [path for _, path, _ in CALLS if path.startswith("/log")] == ["/log?after=1"]
    page.get_by_role("button", name="Edit").click()
    recipe = page.locator("textarea.harvester-recipe").first
    recipe.wait_for()
    assert ("GET", "/jobs/1", None) in CALLS
    assert json.loads(recipe.input_value()) == json.loads(JOB["options"]["recipe"])


@needs_chromium
@pytest.mark.parametrize("page", [[JOB, CLIPS]], indirect=True)
def test_the_log_is_not_polled_while_the_page_is_hidden(page):
    """A job runs: the log is asked every 2 seconds, but not while the page is hidden (another
    tab), and at once when it shows again."""

    def polls() -> int:
        return sum(1 for _, path, _ in CALLS if path.startswith("/log"))

    def hidden(value: bool) -> None:
        page.evaluate(
            """hidden => {
                Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
                document.dispatchEvent(new Event('visibilitychange'))
            }""",
            value,
        )

    page.wait_for_timeout(300)
    hidden(True)
    page.wait_for_timeout(300)
    before = polls()
    page.wait_for_timeout(4500)
    assert polls() == before, "two polls' time, none asked"
    hidden(False)
    page.wait_for_timeout(300)
    assert polls() == before + 1, "asked at once when shown"


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
        "Files",
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
    assert items.nth(1).get_by_role("checkbox").is_checked()
    assert items.nth(1).get_by_role("button", name="make optional?").count() == 0
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
    assert where.inner_text() == "Acting in a frame: cmp-cdn.example (level 2)"
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


@needs_chromium
def test_the_recorders_steps_are_edited_and_recorded_where_chosen(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    steps = [
        {"action": "goto", "url": "https://site.example/login"},
        {"action": "fill", "selector": "#user", "value": "me"},
        {"action": "fill", "selector": "#pw", "value": "hunter2"},
        {"action": "select", "selector": "#lang", "value": "de", "label": "Deutsch"},
        {
            "action": "click",
            "selector": "#go",
            "text": "Go",
            "wait": {"url": "^https://site\\.example/home", "selector": "#q"},
        },
        {"action": "popup"},
    ]
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": steps}, "proposals": {}})
    receive(page, {"type": "warning", "message": "In this recipe, step 3 fills a value of its own into #pw, …"})
    assert (
        "step 3 fills a value of its own into #pw" in page.locator(".harvester-steps .harvester-warning").inner_text()
    )
    items = page.locator(".harvester-steps ol > li")
    assert "Choose “Deutsch” in #lang" in items.nth(3).inner_text()
    assert "Go on in the popup it opened" in items.nth(5).inner_text()
    assert items.nth(0).get_by_role("button", name="Move up: step 1").is_disabled()
    assert items.nth(5).get_by_role("button", name="Move down: step 6").is_disabled()
    before = len(sent(page))
    items.nth(2).get_by_role("button", name="Move up: step 3").click()
    items.nth(1).get_by_role("button", name="Remove: step 2").click()
    items.nth(2).get_by_label("What step 3 fills in").select_option("password")
    items.nth(1).get_by_label("The value of step 2").fill("you")
    items.nth(1).get_by_label("The value of step 2").press("Tab")
    items.nth(3).get_by_label("The value of step 4").fill("en")
    items.nth(3).get_by_label("The value of step 4").press("Tab")
    assert "then waits for an address like" in items.nth(4).inner_text()
    items.nth(4).get_by_role("button", name="wait for the network instead").click()
    assert sent(page)[before:] == [
        {"type": "step-move", "by": -1, "step": 2},
        {"type": "step-delete", "step": 1},
        {"type": "step-edit", "step": 2, "credential": "password"},
        {"type": "step-edit", "step": 1, "value": "you"},
        {"type": "step-edit", "step": 3, "value": "en"},
        {"type": "step-edit", "step": 4, "wait": None},
    ]

    # Recording on a recipe with steps asks where the new ones go; Cancel keeps Browse.
    page.locator("input[value=record]").click()
    assert page.locator("input[value=browse]").is_checked()
    page.locator(".harvester-placing").get_by_role("button", name="Cancel").click()
    assert page.locator(".harvester-placing").inner_text() == ""
    page.locator("input[value=record]").click()
    page.get_by_label("After which step").select_option("2")
    page.get_by_role("button", name="Replace from there").click()
    assert sent(page)[-2:] == [{"type": "record-at", "after": 2, "replace": True}, {"type": "mode", "mode": "record"}]
    assert page.locator("input[value=record]").is_checked()
    page.locator("input[value=browse]").click()
    page.locator("input[value=record]").click()
    assert sent(page)[-1] == {"type": "mode", "mode": "record"}, "asked once"


@needs_chromium
def test_the_form_warns_of_a_password_in_the_recipe(page):
    page.get_by_role("button", name="Edit").click()
    recipe = page.locator("textarea.harvester-recipe").first
    warning = page.locator("#harvester-job-form .harvester-warning")
    assert warning.inner_text() == ""
    leaked = {**RECIPE, "steps": [{"action": "fill", "selector": "input[name=pass]", "value": "hunter2"}]}
    recipe.fill(json.dumps(leaked))
    assert "Step 1 fills a value of its own into input[name=pass]" in warning.inner_text()
    assert "hunter2" not in warning.inner_text()
    recipe.fill(
        json.dumps({**leaked, "steps": [{"action": "fill", "selector": "input[name=pass]", "credential": "password"}]})
    )
    assert warning.inner_text() == ""


COOKIE_LIST = {
    "type": "cookies",
    "host": "site.example",
    "all": False,
    "groups": [
        {
            "domain": "site.example",
            "cookies": [
                {
                    "name": "sid",
                    "domain": "site.example",
                    "path": "/",
                    "expires": None,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                    "size": 8,
                }
            ],
        }
    ],
}
SID = {"name": "sid", "domain": "site.example", "path": "/"}


@needs_chromium
def test_the_recorders_cookies_panel(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    panel = page.locator(".harvester-cookies-panel")
    assert panel.is_hidden()
    page.get_by_role("button", name="Cookies", exact=True).click()
    assert sent(page)[-1] == {"type": "cookies", "all": False}
    assert panel.get_by_role("button", name="Use for this job").count() == 0, "no job here"
    receive(page, COOKIE_LIST)
    row = panel.locator("tr[data-cookie=sid]")
    assert "•••• (8)" in row.inner_text() and "session" in row.inner_text() and "HttpOnly" in row.inner_text()
    row.get_by_role("button", name="Show").click()
    assert sent(page)[-1] == {"type": "cookie-show", **SID}
    receive(page, {"type": "cookie-value", **SID, "value": "secret-1"})
    assert row.locator(".harvester-cookie-value").inner_text() == "secret-1"
    row.get_by_role("button", name="Hide").click()
    assert "secret-1" not in row.inner_text()

    # Changed without showing its value: the value stays the browser's (null).
    row.get_by_role("button", name="Edit").click()
    panel.get_by_label("Path").fill("/account")
    panel.get_by_role("button", name="Save cookie").click()
    change = sent(page)[-1]
    assert change["type"] == "cookie-set" and change["original"] == SID
    assert change["cookie"]["value"] is None and change["cookie"]["path"] == "/account"
    panel.get_by_role("button", name="Add", exact=True).click()
    assert panel.get_by_label("Domain").input_value() == "site.example", "the page's host"
    panel.get_by_label("Name").fill("lang")
    panel.get_by_label("Value").fill("de")
    panel.get_by_role("button", name="Add cookie").click()
    added = sent(page)[-1]["cookie"]
    assert added["name"] == "lang" and added["value"] == "de" and added["path"] == "/" and added["expires"] is None
    receive(page, {"type": "cookies-error", "message": "x: bank.example is none of the sites"})
    assert "bank.example" in panel.locator(".harvester-error").inner_text()
    row.get_by_role("button", name="Delete").click()
    panel.get_by_role("button", name="Delete all").click()
    panel.locator("select").first.select_option("all")
    assert sent(page)[-3:] == [
        {"type": "cookie-delete", **SID, "all": False},
        {"type": "cookies-delete-site", "domain": "site.example", "all": False},
        {"type": "cookies", "all": True},
    ]

    # Into the account: the form's Cookies field, stored with the job's Save.
    panel.get_by_role("button", name="Into the account").click()
    assert sent(page)[-1] == {"type": "cookies-export", "all": True}
    text = "# Netscape HTTP Cookie File\nsite.example\tFALSE\t/\tTRUE\t0\tsid\tsecret-1\n"
    receive(page, {"type": "cookies-export", "text": text, "count": 1})
    assert "1 cookie in the account’s Cookies field" in panel.inner_text()
    page.get_by_role("button", name="Cancel").click()
    assert page.locator("textarea[data-credential=cookies]").input_value() == text
    assert not [call for call in CALLS if "credentials" in call[1] and call[0] == "PUT"], "nothing stored yet"


@needs_chromium
def test_the_job_browsers_cookies_panel_keeps_the_session_and_fills_the_account(page):
    page.get_by_role("button", name="Show what the browser saw").click()
    page.get_by_role("button", name="Open in the browser").click()
    page.wait_for_function("window.__sockets.length")
    page.get_by_role("button", name="Cookies", exact=True).click()
    receive(page, COOKIE_LIST)
    panel = page.locator(".harvester-cookies-panel")
    panel.get_by_role("button", name="Use for this job").click()
    assert sent(page)[-1] == {"type": "cookies-keep"}
    receive(page, {"type": "cookies-kept"})
    assert "Kept for the job" in panel.inner_text()
    assert page.locator("img.harvester-screen").count() == 1, "the browser stays open"

    panel.get_by_role("button", name="Into the account").click()
    text = "# Netscape HTTP Cookie File\nsite.example\tFALSE\t/\tTRUE\t0\tsid\tsecret-1\n"
    receive(page, {"type": "cookies-export", "text": text, "count": 1})
    page.locator("dialog").get_by_role("button", name="Cancel").click()
    page.wait_for_function("document.querySelector('.harvester-cookies-panel').innerText.includes('Not stored.')")
    receive(page, {"type": "cookies-export", "text": text, "count": 1})
    page.locator("dialog").get_by_role("button", name="Store").click()
    page.wait_for_function("document.querySelector('.harvester-cookies-panel').innerText.includes('stored in the job')")
    assert ("PUT", "/jobs/1/credentials", {"cookies": text}) in CALLS, "stored once the person confirmed"


@needs_chromium
@pytest.mark.language("de")
def test_the_page_in_the_persons_language(page):
    german = json.loads((ROOT / "l10n" / "de.json").read_text(encoding="utf-8"))["translations"]
    heads = page.locator("table.harvester-jobs th").all_inner_texts()
    assert heads[:5] == [german[text] for text in ("Job", "Folder", "Schedule", "Tags", "Last run")]
    # Placeholders and plurals: "2 jobs, 1 recording".
    load = page.locator(".harvester-load").inner_text()
    assert german["_%n job_::_%n jobs_"][1].replace("%n", "2") in load
    assert german["_%n recording_::_%n recordings_"][0].replace("%n", "1") in load
    # The server's texts too: a source's title and description on the New job page.
    page.get_by_role("button", name=german["New job"]).click()
    kinds = page.locator(".harvester-kinds").inner_text()
    site = main.SOURCES["site"]
    assert german[site.title] in kinds and german[site.description] in kinds
    assert site.title not in kinds


@needs_chromium
@pytest.mark.language("fr")
def test_a_language_without_translations_is_english(page):
    assert page.locator("table.harvester-jobs th").all_inner_texts()[:5] == [
        "Job",
        "Folder",
        "Schedule",
        "Tags",
        "Last run",
    ]
    assert page.get_by_role("button", name="New job").is_visible()
    assert page.locator(".harvester-load").inner_text() == "Runners 2/2 · Browsers 3/3 (2 jobs, 1 recording) · 3 queued"


# A queued job whose reason comes as a template, and a job whose last run's outcome does
# (harvester.l10n), as the API gives them.
WAITS = QUEUED | {
    "waiting_l10n": {
        "text": "{place}. in the queue: waits for a runner, all {runners} busy (%n job ahead)",
        "plural": "{place}. in the queue: waits for a runner, all {runners} busy (%n jobs ahead)",
        "count": 1,
        "params": {"place": 2, "runners": 2},
    }
}
DONE = JOB | {
    "id": 4,
    "name": "Done",
    "last_status": "ok",
    "last_message": "3 downloaded, 1 already there",
    "last_message_l10n": {
        "list": [
            {"text": "%n downloaded", "plural": "%n downloaded", "count": 3},
            {"text": "%n already there", "plural": "%n already there", "count": 1},
        ],
        "kind": "unit",
    },
}


@needs_chromium
@pytest.mark.language("de")
@pytest.mark.parametrize("page", [[JOB, WAITS, DONE]], indirect=True)
def test_the_servers_texts_in_the_persons_language(page):
    german = json.loads((ROOT / "l10n" / "de.json").read_text(encoding="utf-8"))["translations"]
    rows = page.locator("tbody tr")
    # Why a job waits: the template's German, its count's form and its values.
    key = "_{place}. in the queue: waits for a runner, all {runners} busy (%n job ahead)_::_{place}. in the queue: waits for a runner, all {runners} busy (%n jobs ahead)_"  # pylint: disable=line-too-long
    reason = german[key][0].replace("%n", "1").replace("{place}", "2").replace("{runners}", "2")
    assert reason in rows.nth(1).inner_text() and "in the queue" not in rows.nth(1).inner_text()
    # A run's outcome: each figure in German; an older run's message (no template) as it was.
    done = rows.nth(2).inner_text()
    assert german["_%n downloaded_::_%n downloaded_"][1].replace("%n", "3") in done
    assert german["_%n already there_::_%n already there_"][0].replace("%n", "1") in done
    assert "step 2 (click #go) failed" in rows.nth(0).inner_text()

    # The progress log: a line with its template in German, an older plain line as it is, and a
    # value that looks like markup shown as text.
    lines = [
        {"id": 2, "job": 1, "name": "Mag", "at": 2, "level": "info", "message": "Looking for new files", "shot": 0},
        {
            "id": 3,
            "job": 1,
            "name": "Mag",
            "at": 3,
            "level": "info",
            "message": "Saved /Mag/<b>a</b>.pdf (2.0 KB)",
            "message_l10n": {
                "text": "Saved {path} ({size})",
                "params": {"path": "/Mag/<b>a</b>.pdf", "size": "2.0 KB"},
            },
            "shot": 0,
        },
    ]
    answer = LOG | {"lines": lines, "running": [], "queued": [2]}
    page.route(
        "**/apps/app_api/proxy/harvester/api/log**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(answer)),
    )
    page.wait_for_function("document.querySelectorAll('.harvester-log-text').length >= 3", timeout=15000)
    texts = page.locator(".harvester-log-text").all_inner_texts()
    assert "Looking for new files" in texts[-2], "an older line, as it was"
    saved = german["Saved {path} ({size})"].replace("{path}", "/Mag/<b>a</b>.pdf").replace("{size}", "2.0 KB")
    assert saved in texts[-1]

    # The schedule in words, in the job's form.
    page.get_by_role("button", name=german["Edit"]).first.click()
    hint = page.locator(".harvester-schedule strong")
    hint.wait_for()
    assert hint.inner_text() == german["every day at {times}"].replace("{times}", "06:00")


@needs_chromium
@pytest.mark.parametrize("page", [[ASKING]], indirect=True)
def test_a_job_that_asks_for_a_code_takes_it_on_the_page(page):
    row = page.locator("tbody tr").first
    assert row.locator(".harvester-status").inner_text() == "Needs you"
    assert row.get_by_role("button", name="Stop").count() == 1, "a stop ends the wait"
    field = row.get_by_role("textbox", name="Enter the code from the bank's SMS")
    field.fill(" 482913 ")
    row.get_by_role("button", name="Send").click()
    page.wait_for_function("document.querySelector('.harvester-asking small').textContent.startsWith('Sent')")
    assert ("POST", "/jobs/1/code", {"code": "482913"}) in CALLS
    assert field.input_value() == "", "not kept on the page either"


@needs_chromium
def test_the_recorder_offers_a_code_from_the_phone_and_its_test_asks_for_it(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    steps = [
        {"action": "goto", "url": "https://bank.example/login"},
        {"action": "fill", "selector": "#otp", "credential": "totp"},
    ]
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": steps}, "proposals": {}})
    item = page.locator(".harvester-steps ol > li").nth(1)
    item.get_by_text("code from the phone?").click()
    item.get_by_role("textbox", name="Where the code comes from").fill("the bank's SMS")
    item.get_by_role("button", name="Ask me for the code").click()
    assert sent(page)[-1] == {"type": "ask-code", "step": 1, "label": "the bank's SMS"}
    item = page.locator(".harvester-steps ol > li").nth(1)
    assert "Ask you for the code from the bank's SMS and type it into #otp" in item.inner_text()
    item.get_by_role("button", name="Back to a one-time code").click()
    assert sent(page)[-1] == {"type": "ask-code", "step": 1, "off": True}

    receive(page, {"type": "test-started", "as_job": False})
    receive(page, {"type": "test-asking", "kind": "code", "label": "the bank's SMS", "seconds": 600})
    page.get_by_role("textbox", name="Enter the code from the bank's SMS").fill("135790")
    page.locator(".harvester-steps .harvester-asking").get_by_role("button", name="Send").click()
    assert sent(page)[-1] == {"type": "test-code", "code": "135790"}
    assert page.locator(".harvester-steps .harvester-asking").count() == 0


@needs_chromium
@pytest.mark.parametrize(
    "page",
    [[ASKING | {"asking": {"kind": "approve", "label": "Confirm in the bank's app", "until": 4102444800}}]],
    indirect=True,
)
def test_a_job_that_waits_for_an_approval_says_so(page):
    row = page.locator("tbody tr").first
    assert row.locator(".harvester-status").inner_text() == "Needs you"
    assert "Confirm in the bank's app: approve it on your phone or in the app." in row.inner_text()
    assert row.get_by_role("textbox").count() == 0, "nothing to type"


@needs_chromium
def test_the_recorder_adds_an_approval_and_a_handover_and_the_steps_list_edits_them(page):
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Record in the browser").click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    approve = page.get_by_role("button", name="Add step: wait for your approval")
    handover = page.get_by_role("button", name="Add step: hand over to you")
    assert approve.is_disabled() and handover.is_disabled(), "steps: only while recording"
    page.locator("input[value=record]").click()
    page.get_by_role("button", name="Append").click()
    approve.click()
    handover.click()
    assert sent(page)[-2:] == [
        {"type": "person", "action": "approve", "label": "Approve the sign-in in your app"},
        {"type": "person", "action": "handover", "label": "Sign in yourself (a passkey)"},
    ]

    steps = [
        {"action": "goto", "url": "https://bank.example/login"},
        {"action": "approve", "label": "Approve the sign-in in your app", "until": {"url": "^https://bank/x$"}},
        {"action": "handover", "label": "Sign in yourself (a passkey)"},
    ]
    receive(page, {"type": "recipe", "recipe": {**RECIPE, "steps": steps}, "proposals": {}})
    item = page.locator(".harvester-steps ol > li").nth(1)
    assert "Wait for your approval: Approve the sign-in in your app" in item.inner_text()
    assert item.get_by_role("checkbox").count() == 0, "never optional"
    assert item.get_by_role("textbox", name="The address it waits for (a pattern), step 2").input_value() == (
        "^https://bank/x$"
    )
    item.get_by_role("textbox", name="What you are asked, step 2").fill("Confirm in the bank's app")
    item.get_by_role("textbox", name="What you are asked, step 2").press("Enter")
    assert sent(page)[-1] == {"type": "person-edit", "step": 1, "label": "Confirm in the bank's app"}
    item.get_by_role("spinbutton", name="How long the job waits for you (minutes), step 2").fill("5")
    item.get_by_role("spinbutton", name="How long the job waits for you (minutes), step 2").press("Enter")
    assert sent(page)[-1] == {"type": "person-edit", "step": 1, "wait": 300}
    item.get_by_role("button", name="this page’s address").click()
    assert sent(page)[-1] == {"type": "person-edit", "step": 1, "here": True}
    item.get_by_role("button", name="Pick an element").click()
    assert sent(page)[-1] == {"type": "mode", "mode": "until", "step": 1}
    assert approve.is_disabled(), "picking: no steps added meanwhile"
    receive(page, {"type": "until-picked", "step": 1})
    assert sent(page)[-1] == {"type": "mode", "mode": "record"}, "back to recording"
    assert page.locator("input[value=record]").is_checked()

    item = page.locator(".harvester-steps ol > li").nth(2)
    assert "Hand the page over to you: Sign in yourself (a passkey)" in item.inner_text()
    assert item.get_by_role("spinbutton").count() == 0, "a handover does not wait"
    item.get_by_role("textbox", name="The element it waits for, step 3").fill("#accounts")
    item.get_by_role("textbox", name="The element it waits for, step 3").press("Enter")
    assert sent(page)[-1] == {"type": "person-edit", "step": 2, "selector": "#accounts"}
