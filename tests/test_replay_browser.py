# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replays in the browser: the sign-in a kept session skips and the steps to the listing it does
not, a session that no longer signs in given up for a new sign-in, one that ends in the middle of
a run, dates typed into a field, file links that are clicked to download, and levels that load
more of their pages (Chromium against the fake site by route fulfilment, no network)."""

# pylint: disable=missing-function-docstring,protected-access

import datetime
import io
import json

import httpx
import pytest
from bs4 import BeautifulSoup
from test_browser import FREE, needs_chromium
from test_runner import FakeLibrary

from fake_site import LOGIN, PDF, SITE, Site
from harvester import runner
from harvester.presets import make_job
from harvester.sources import Refused
from harvester.sources.driver import HttpPage
from harvester.sources import site as site_module
from harvester.store import Job

SIGN_IN = [
    {"action": "goto", "url": f"{SITE}/sso/login"},
    {"action": "fill", "selector": "#login-user", "credential": "username"},
    {"action": "fill", "selector": "#login-password", "credential": "password"},
    {"action": "click", "selector": "button[name=rm_login]", "text": "Sign in"},
]


class FakePage:
    """A page whose actions are noted; it shows no sign-in."""

    def __init__(self):
        self.done: list[tuple] = []
        self.url = f"{SITE}/list"

    def goto(self, url: str, **_options) -> None:
        self.done.append(("goto", url))

    def click(self, selector: str, **_options) -> None:
        self.done.append(("click", selector))

    def fill(self, selector: str, value: str, **_options) -> None:
        self.done.append(("fill", selector, value))

    def evaluate(self, _script: str, *_args) -> bool:
        return False

    def wait_for_timeout(self, _ms: float) -> None:
        pass

    def wait_for_load_state(self, *_args, **_options) -> None:
        pass


def fake_driver() -> site_module.BrowserDriver:
    driver = object.__new__(site_module.BrowserDriver)
    driver.main = FakePage()
    driver.challenge = lambda page: None
    return driver


def test_a_kept_session_skips_the_sign_in_not_the_steps_to_the_listing():
    steps = [*SIGN_IN, {"action": "click", "selector": "#invoices"}, {"action": "click", "selector": "#all"}]
    driver = fake_driver()
    driver.sign_in(steps, {"username": "me", "password": "pw"}, {"cookies": []}, f"{SITE}/list")
    assert driver.main.done == [("goto", f"{SITE}/list"), ("click", "#invoices"), ("click", "#all")]
    without = fake_driver()
    without.sign_in(steps, {"username": "me", "password": "pw"}, None, f"{SITE}/list")
    assert [entry[0] for entry in without.main.done] == ["goto", "fill", "fill", "click", "click", "click"]


def test_steps_without_a_sign_in_are_all_replayed_with_a_kept_session():
    steps = [{"action": "goto", "url": f"{SITE}/list"}, {"action": "click", "selector": "#tab"}]
    driver = fake_driver()
    driver.sign_in(steps, {}, {"cookies": []}, f"{SITE}/list")
    assert driver.main.done == [("goto", f"{SITE}/list"), ("click", "#tab")]


def test_dates_in_a_fields_value_are_the_jobs_days():
    driver = fake_driver()
    driver.timezone = "Pacific/Kiritimati"
    steps = [{"action": "fill", "selector": "#from", "value": "{today-30d:%d.%m.%Y}"}]
    driver.sign_in(steps, {}, None, "")
    day = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=14))).date()
    assert driver.main.done == [("fill", "#from", (day - datetime.timedelta(days=30)).strftime("%d.%m.%Y"))]


# In Chromium.


class Sessions(Site):  # pylint: disable=too-few-public-methods
    """The fake archive whose sessions can end (generation), whose files need one too, and whose
    signed-out archive asks for the user name alone (a username-first sign-in)."""

    def __init__(self):
        super().__init__()
        self.generation = 1
        self.signed_out_pages = 0

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        path = url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        html = {"content-type": "text/html"}
        if path == "session" and method == "POST":
            self.requests.append(url)
            headers = html | {"set-cookie": f"sid=ok{self.generation}; Path=/"}
            return 200, headers, b"<script>location.href = '/select/mag/archiv'</script>"
        signed_in = f"sid=ok{self.generation}" in cookie
        if path.startswith(("select/", "files/")) and not signed_in:
            self.requests.append(url)
            self.signed_out_pages += 1
            # No password field and no sign-in address: the old check took it for the listing.
            return 200, html, b"<html><form><input name='who'><button>Next</button></form></html>"
        return super().answer(url, method, cookie)


def routed(site: Site, monkeypatch) -> None:
    def fulfill(route) -> None:
        status, headers, body = site.answer(
            route.request.url, route.request.method, route.request.all_headers().get("cookie", "")
        )
        route.fulfill(status=status, headers=headers, body=body)

    monkeypatch.setattr(site_module, "guard", fulfill)


def heise(site: Site, monkeypatch, **options) -> site_module.Site:
    routed(site, monkeypatch)
    job = make_job("heise", {"magazine": "mag", "name": "Mag"})
    given = dict(job["options"], recipe=job["options"]["recipe"].replace("https://www.heise.de", SITE))
    made = site_module.Site(given | {"min_size": 0} | options, {"username": "me", "password": "pw"}, None)
    made.sleep = lambda seconds: None
    made.page_pause = 0.0
    return made


def listed(made: site_module.Site) -> tuple[int, dict | None]:
    """How many files the source lists, and the session it would keep; closed then."""
    try:
        return len(list(made.items(lambda key: False))), made.session()
    finally:
        made.close()


@needs_chromium
def test_a_kept_session_that_ended_is_given_up_for_a_new_sign_in(monkeypatch):
    site = Sessions()
    count, kept = listed(heise(site, monkeypatch))
    assert count == 8 and kept
    site.generation = 2
    site.requests.clear()
    log: list[str] = []
    second = heise(site, monkeypatch)
    second.report = lambda level, text: log.append(text)
    second.restore(kept)
    count, renewed = listed(second)
    assert count == 8, "signed in anew: the whole archive"
    assert renewed and second.stale and "The kept session no longer reaches the listing: signing in anew" in log
    assert any(cookie["value"] == "ok2" for cookie in renewed["cookies"]), "the new session is the one to keep"
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_session_that_ends_in_the_middle_of_a_run_is_signed_in_anew(monkeypatch):
    site = Sessions()
    made = heise(site, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = site.answer(str(request.url), request.method, request.headers.get("cookie", ""))
        return httpx.Response(status, headers=headers, content=body)

    def client(**options) -> httpx.Client:
        # The downloads with the browser's cookies, to the fake site instead of the network.
        options.pop("proxy", None)
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, **options)

    monkeypatch.setattr(site_module.download, "guarded_client", client)
    library = FakeLibrary()
    put = library.put

    def put_and_end(path, content):
        found = put(path, content)
        if len(library.files) == 6:
            # The site ends the session with the last year's listing open: its next file asks
            # for a sign-in.
            site.generation = 2
        return found

    library.put = put_and_end
    log: list[str] = []
    job = Job(user="u", name="n", source="site", target="/In", schedule="* * * * *", options={"max_items": -1})
    try:
        message = runner.run(
            job, made, library, lambda key: False, lambda key: None, report=lambda level, text: log.append(text)
        )
    finally:
        made.close()
    assert message == "8 downloaded"
    assert any(line.endswith(": signed in anew") for line in log)
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_year_redirecting_to_a_sign_in_is_a_gap_in_the_browser(monkeypatch):
    site = Site()
    site.answers = {}
    original = site.answer

    def answer(url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        if url.endswith("/select/mag/archiv/2025"):
            return 200, {"content-type": "text/html"}, LOGIN.encode()
        return original(url, method, cookie)

    site.answer = answer  # type: ignore[method-assign]
    made = heise(site, monkeypatch)
    log: list[str] = []
    made.report = lambda level, text: log.append(text)
    try:
        assert len(list(made.items(lambda key: False))) == 5
    finally:
        made.close()
    assert made.gaps == 1 and any(line.endswith("gave nothing: a sign-in page") for line in log)


# File links that are clicked to download.

CLICKS = """<html><body>
<button class="dl" onclick="location.href='/dl/a'">Invoice A</button>
<form method="post" action="/dl/b"><button class="dl" type="submit">Invoice B</button></form>
<button class="dl" onclick="blob()">Invoice C</button>
<button class="dl" onclick="void 0">Help</button>
<script>
function blob() {
  const a = document.createElement('a')
  const file = new Blob(['%PDF-1.7\\n' + 'x'.repeat(2000)], {type: 'application/pdf'})
  a.href = URL.createObjectURL(file)
  a.download = 'blob.pdf'
  document.body.append(a)
  a.click()
}
</script></body></html>"""


class Clicks(Site):  # pylint: disable=too-few-public-methods
    """A page of buttons that download: by a script's address, a form, a blob."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        if url.endswith("/clicks"):
            return 200, {"content-type": "text/html"}, CLICKS.encode()
        if "/dl/" in url:
            name = url.rsplit("/", 1)[1]
            self.requests.append(url)
            headers = {"content-type": "application/pdf", "content-disposition": f'attachment; filename="{name}.pdf"'}
            return 200, headers, PDF
        return super().answer(url, method, cookie)


def clicking(site: Site, monkeypatch, **options) -> site_module.Site:
    routed(site, monkeypatch)
    recipe = {
        "start": f"{SITE}/clicks",
        "links": {"selector": "button.dl", "pattern": r"^Invoice (?P<letter>\w)$", "click": True},
    }
    made = site_module.Site({"recipe": json.dumps(recipe), "path": "{disposition}"} | options, {}, None)
    made.sleep = lambda seconds: None
    return made


@needs_chromium
def test_file_links_that_are_clicked_download_by_script_form_and_blob(monkeypatch):
    site = Clicks()
    made = clicking(site, monkeypatch, file_type="pdf")
    library = FakeLibrary()
    job = Job(user="u", name="n", source="site", target="/In", schedule="* * * * *")
    seen: set[str] = set()
    try:
        message = runner.run(job, made, library, seen.__contains__, seen.add, lambda seconds: None)
    finally:
        made.close()
    assert message == "3 downloaded"
    assert sorted(library.files) == ["/In/a.pdf", "/In/b.pdf", "/In/blob.pdf"]
    assert all(content == PDF for content in library.files.values())
    assert f"{SITE}/clicks#Invoice A" in seen, "known by the listing and the element's text"
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_clicked_download_keeps_the_jobs_size_limit(monkeypatch):
    made = clicking(Clicks(), monkeypatch, min_size=0)
    try:
        items = made.items(lambda key: False)
        first = next(items)
        monkeypatch.setattr(site_module.download, "MB", 1)
        made.settings["max_size"] = 100
        with pytest.raises(Refused, match="more than the job's largest file"):
            made.fetch(first, io.BytesIO())
    finally:
        made.close()


def test_click_links_and_loading_more_need_the_browser_over_http():
    driver = site_module.HttpDriver(httpx.Client())
    page = HttpPage(f"{SITE}/x", BeautifulSoup("", "html.parser"))
    with pytest.raises(site_module.SourceError, match="only the browser can"):
        driver.targets(page, "button")
    with pytest.raises(site_module.SourceError, match="only the browser can"):
        driver.expand(page, {"load_more": "button"})
    driver.expand(page, {"selector": "a"})


# Levels that load more of their pages.

MORE = """<html><body><ul id="files"></ul><button class="more">Load more</button><script>
let shown = 0
const year = location.pathname.split('/').pop()
function more() {
  for (let i = 0; i < 2; i++) {
    shown++
    const li = document.createElement('li')
    li.innerHTML = `<a class="file" href="/files/${year}-${shown}.pdf">${shown}</a>`
    document.getElementById('files').append(li)
  }
  if (shown >= 6) document.querySelector('.more').remove()
}
more()
document.querySelector('.more').addEventListener('click', more)
</script></body></html>"""


class Loading(Site):  # pylint: disable=too-few-public-methods
    """Years whose pages show two files and a Load more button for two more, up to six."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        html = {"content-type": "text/html"}
        if url.endswith("/years"):
            return 200, html, b'<a class="y" href="/more/2025">2025</a><a class="y" href="/more/2024">2024</a>'
        if url.endswith("/feeds"):
            return 200, html, b'<a class="f" href="/feed">Feed</a>'
        if "/more/" in url:
            return 200, html, MORE.encode()
        return super().answer(url, method, cookie)


@needs_chromium
def test_a_deeper_level_loads_more_with_its_button_and_by_scrolling(monkeypatch):
    routed(Loading(), monkeypatch)
    recipe = {
        "start": f"{SITE}/years",
        "follow": [{"selector": "a.y", "pattern": r"/more/(?P<year>\d{4})$"}],
        "links": {"selector": "a.file", "pattern": r"\.pdf$", "load_more": "button.more"},
    }
    made = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0}, {}, None)
    made.page_pause = 0.0
    try:
        paths = [item.path for item in made.items(lambda key: False)]
    finally:
        made.close()
    assert len(paths) == 12 and "2024-6.pdf" in paths

    scrolled = {
        "start": f"{SITE}/feeds",
        "follow": [{"selector": "a.f", "pattern": r"/feed$"}],
        "links": {"selector": "a.post", "pattern": r"\.pdf$", "scroll": 10},
    }
    made = site_module.Site({"recipe": json.dumps(scrolled), "stop_at_seen": 0}, {}, None)
    made.page_pause = 0.0
    try:
        assert len(list(made.items(lambda key: False))) == 35, "the feed's every post, scrolled to its end"
    finally:
        made.close()
    assert site_module.browser.free_browsers() == FREE
