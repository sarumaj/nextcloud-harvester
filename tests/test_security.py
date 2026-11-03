# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fixes of a security review: what a job's options may be, where the browser's downloads and
WebSockets may go, ids never used again, tokens kept to their host, pages read within limits,
patterns that cannot hold the app, and the app's secrets out of its helpers' reach."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest
from fastapi import HTTPException

import main
from harvester import browser, rclone_wizard
from harvester.browser import datapicks
from harvester.browser.recorder import Recorder
from harvester.presets import PRESETS, make_job
from harvester.sources import SourceError, download
from harvester.sources import site as site_module
from harvester.sources.feed import Feed
from harvester.sources.gallery import Gallery
from harvester.sources.download import guarded_client
from harvester.sources.releases import Releases
from harvester.store import SCHEMA, Job, Store

PDF = b"%PDF-1.7 file"


def resolving(monkeypatch, names: dict[str, str]) -> None:
    """These names resolve to these addresses; others as before (conftest's test names)."""
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if str(host).lower() in names:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names[str(host).lower()], 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    browser._resolves_public.cache_clear()


# A job's options


def job_in(source: str = "feed", **options) -> main.JobIn:
    return main.JobIn(name="a", source=source, target="/x", options={"url": "https://news.example/feed"} | options)


@pytest.mark.parametrize(
    "options, message",
    [
        ({"home": "yes"}, "Through the proxy must be on or off (1 or 0)"),
        ({"max_items": ""}, "Files per run must be a number of at least 1 (or -1: no limit)"),
        ({"max_items": 0}, "Files per run must be a number of at least 1 (or -1: no limit)"),
        ({"max_items": "2.5"}, "Files per run must be a number of at least 1 (or -1: no limit)"),
        ({"max_size": 0}, "Largest file (MB) must be a number of at least 1"),
        ({"min_size": -1}, "Smallest file (bytes) must be a number of at least 0"),
        ({"take": "everything"}, "must be one of enclosures, links"),
        ({"file_type": "pdf,exe"}, "File types must be one or several of any, pdf"),
    ],
)
def test_options_of_the_wrong_kind_are_refused_by_their_titles(options, message):
    with pytest.raises(HTTPException) as refused:
        main.checked(job_in(**options))
    assert refused.value.status_code == 422 and message in refused.value.detail


def test_options_of_their_kind_pass():
    main.checked(job_in(home=1, max_items=-1, max_size="10", min_size=0, take="links", file_type="pdf,zip"))
    main.checked(job_in(home="0", max_items=3, file_type=""))
    with pytest.raises(HTTPException, match="Port must be a number from 1 to 65535"):
        main.checked(main.JobIn(name="m", source="mail", target="/x", options={"host": "imap.example", "port": 0}))


def test_every_templates_job_passes_the_checks():
    for preset_id, preset in PRESETS.items():
        values = {name: parameter.get("example", "x") for name, parameter in preset.get("parameters", {}).items()}
        try:
            job = make_job(preset_id, values)
        except ValueError:
            continue  # a parameter the example does not fill: the template's own tests cover it
        for name, value in job["options"].items():
            option = (main.SOURCES[job["source"]].options | main.COMMON_OPTIONS).get(name)
            assert option is None or not main.option_problem(option, value), (preset_id, name, value)


class FakeRecorder:
    """Stands in for the recorder's browser: its start fails, as a browser that does not start."""

    started: list[tuple[bool, bool]] = []

    def __init__(self, send):
        self.send = send

    async def start(self, *_args, http1=False, home=False, **_kwargs):
        FakeRecorder.started.append((http1, home))
        raise RuntimeError("no browser here")

    async def close(self):
        pass


class Socket:
    """The recorder's WebSocket, as Starlette gives it."""

    def __init__(self, job: int):
        self.query_params = {"job": str(job)}
        self.headers: dict = {}
        self.sent: list[dict] = []

    async def accept(self):
        pass

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        pass


class Nextcloud:  # pylint: disable=too-few-public-methods
    """The user's Nextcloud: who they are, and their preferences (deleted ones listed)."""

    def __init__(self, user: str = "anna"):
        self._user = user
        self.deleted: list = []
        outer = self

        class Preferences:  # pylint: disable=too-few-public-methods
            """The user's preferences: those deleted are noted."""

            async def delete(self, keys):
                outer.deleted += keys

        self.preferences_ex = Preferences()

    @property
    async def user(self):
        return self._user


def test_a_saved_job_with_a_broken_flag_never_keeps_a_browsers_slot(tmp_path, monkeypatch):
    # Saved before the options were checked: the recording's start went wrong before its
    # try, and each try kept one of the app's few browsers until all were gone.
    monkeypatch.setitem(main.STATE, "store", Store(str(tmp_path / "h.db")))
    job = main.store().save(
        Job(user="anna", name="a", source="feed", target="/x", schedule="0 6 * * *", options={"home": "yes"})
    )
    monkeypatch.setattr(main, "Recorder", FakeRecorder)

    async def no_session(*_args):
        return None

    monkeypatch.setattr(main, "load_session", no_session)
    free = browser.SLOTS.free()
    for _ in range(browser.SLOTS.limit + 1):
        websocket = Socket(job.id or 0)
        asyncio.run(main.record(websocket, Nextcloud()))  # type: ignore[arg-type]
        assert websocket.sent[-1]["message"] == "The browser did not start: no browser here"
    assert browser.SLOTS.free() == free
    assert FakeRecorder.started[-1] == (False, False), "a flag that is no number is off"


# The browser's downloads


class Context:  # pylint: disable=too-few-public-methods
    """The browser's context as BrowserDriver.download reads it: its session's cookies, each
    request's asked for by its address (all here: cookies.session_cookies picks those it sends)."""

    def __init__(self, cookies: list[dict]):
        self.session = cookies
        self.asked: list[str] = []

    def cookies(self, url: str) -> list[dict]:
        self.asked.append(url)
        return self.session


def browser_driver(cookies: list[dict] | None = None, proxy: str = "") -> site_module.BrowserDriver:
    driver = object.__new__(site_module.BrowserDriver)
    driver.context = Context(cookies or [])  # type: ignore[assignment]
    driver.proxy = proxy
    driver.user_agent = "Mozilla/5.0 Chrome/140.0.0.0"
    return driver


def serving(monkeypatch, handler) -> list[dict]:
    """The guarded client's requests go to the handler; returns the clients' options."""
    made: list[dict] = []
    real = guarded_client

    def client(**options):
        made.append(options)
        # The proxy is only noted: the handler stands in for it and for the sites behind it.
        return real(transport=httpx.MockTransport(handler), **{k: v for k, v in options.items() if k != "proxy"})

    monkeypatch.setattr(download, "guarded_client", client)
    return made


def test_the_browsers_download_goes_with_its_cookies_and_user_agent_through_the_guard(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    serving(monkeypatch, handler)
    cookies = [
        {"name": "session", "value": "s1", "domain": "files.example", "path": "/", "secure": True, "expires": -1},
        {"name": "wide", "value": "w1", "domain": ".example", "path": "/", "secure": False, "expires": -1},
        {"name": "other", "value": "o1", "domain": "elsewhere.example", "path": "/", "secure": False},
    ]
    driver = browser_driver(cookies)
    for url in ("https://files.example/a.pdf", "http://files.example/b.pdf", "https://cdn.files.example/c.pdf"):
        dest = io.BytesIO()
        download.fetch(driver.download, url, dest, {"file_type": "pdf"}, lambda seconds: None)
        assert dest.getvalue() == PDF
    sent = [request.headers.get("cookie", "") for request in seen]
    assert sent[0] in ("session=s1; wide=w1", "wide=w1; session=s1")
    assert sent[1] == "wide=w1", "a secure cookie only over https"
    assert sent[2] == "wide=w1", "a host's own cookie only to that host"
    assert seen[0].headers["user-agent"] == "Mozilla/5.0 Chrome/140.0.0.0"


@pytest.mark.parametrize(
    "location", ["http://127.0.0.1/admin", "http://169.254.169.254/latest", "https://inside.example/secret"]
)
def test_the_browsers_download_never_follows_a_redirect_to_a_local_address(monkeypatch, location):
    resolving(monkeypatch, {"inside.example": "10.0.0.26"})
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    serving(monkeypatch, handler)
    with pytest.raises(SourceError, match="may not be reached"):
        download.fetch(browser_driver().download, "https://files.example/a.pdf", io.BytesIO(), {}, lambda s: None)
    assert asked == ["https://files.example/a.pdf"]


def test_the_browsers_download_is_refused_by_its_size_before_it_is_read(monkeypatch):
    chunks: list[int] = []

    def endless():
        for number in range(10_000):
            chunks.append(number)
            yield b"%PDF" + b"x" * 65536

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless(), headers={"content-type": "application/pdf"})

    serving(monkeypatch, handler)
    with pytest.raises(SourceError, match="larger than the job's largest file"):
        download.fetch(
            browser_driver().download, "https://files.example/a.pdf", io.BytesIO(), {"max_size": 1}, lambda s: None
        )
    assert len(chunks) < 40, "read in chunks, ended past the limit"

    def announced(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless(), headers={"content-length": str(2 << 30)})

    serving(monkeypatch, announced)
    chunks.clear()
    with pytest.raises(SourceError, match="more than the job's largest file"):
        download.fetch(
            browser_driver().download, "https://files.example/a.pdf", io.BytesIO(), {"max_size": 1}, lambda s: None
        )
    assert not chunks


def test_the_browsers_download_takes_the_jobs_proxy(monkeypatch):
    made = serving(monkeypatch, lambda request: httpx.Response(200, content=PDF))
    download.fetch(
        browser_driver(proxy="http://proxy.example:3128").download, "https://f.example/a", io.BytesIO(), {}, print
    )
    download.fetch(browser_driver().download, "https://f.example/a", io.BytesIO(), {}, print)
    assert made[0]["proxy"] == "http://proxy.example:3128" and "proxy" not in made[1]


# Names that resolve to local addresses, WebSockets and service workers


def test_a_name_that_resolves_to_a_local_address_is_refused_everywhere(monkeypatch):
    resolving(monkeypatch, {"127.0.0.1.nip.io": "127.0.0.1", "metadata.example": "169.254.169.254"})
    assert not browser.allowed_host("127.0.0.1.nip.io") and not browser.allowed_host("metadata.example")
    assert not browser.allowed_url("https://127.0.0.1.nip.io/feed")
    assert browser.allowed_host("imap.example") and browser.allowed_url("https://news.example/feed")
    with pytest.raises(SourceError, match="may not be opened"):
        Feed({"url": "https://127.0.0.1.nip.io/feed"}, {}, None)
    with pytest.raises(SourceError, match="may not be opened"):
        Gallery({"url": "https://127.0.0.1.nip.io/album"}, {}, None)


def test_a_name_that_does_not_resolve_in_time_is_refused(monkeypatch):
    real = socket.getaddrinfo

    def slow(host, *args, **kwargs):
        if host == "slow.example":
            time.sleep(1)
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    monkeypatch.setattr(browser, "LOOKUP_TIMEOUT", 0.1)
    browser._resolves_public.cache_clear()
    started = time.monotonic()
    assert not browser.allowed_host("slow.example")
    assert time.monotonic() - started < 0.5


def test_websocket_addresses_are_checked_as_their_http_ones():
    assert browser.http_address("ws://127.0.0.1:9/x") == "http://127.0.0.1:9/x"
    assert browser.http_address("WSS://site.example/live") == "https://site.example/live"
    assert not browser.reachable(browser.http_address("ws://127.0.0.1:9/x"))
    assert browser.context_options("140.0")["service_workers"] == "block"


def chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

        with sync_playwright() as playwright:
            playwright.chromium.launch(**browser.launch_options(9)).close()
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


needs_chromium = pytest.mark.skipif(not chromium_installed(), reason="Chromium is not installed")
PAGE = "<html><body>a page</body></html>"
# What became of the page's WebSocket: "open" (the guard's stand-in: no connection is tried), its
# close code (1006: a connection was tried and failed) or "timeout" (one is being tried).
OPEN_SOCKET = """url => new Promise(done => {
  const socket = new WebSocket(url)
  socket.onopen = () => done('open')
  socket.onclose = event => done(event.code)
  setTimeout(() => done('timeout'), 3000)
})"""


@needs_chromium
def test_a_job_browsers_page_cannot_open_a_websocket_to_a_local_address():
    driver = site_module.BrowserDriver([], {})
    try:
        driver.context.route(
            "https://site.example/**", lambda route: route.fulfill(body=PAGE, content_type="text/html")
        )
        page = driver.context.new_page()
        page.goto("https://site.example/")
        assert page.evaluate(OPEN_SOCKET, "ws://127.0.0.1:9/x") == "open", "never connected"
        # A public address is connected to (the test's name is unknown to Chromium: it fails, or
        # takes its time).
        assert page.evaluate(OPEN_SOCKET, "ws://site.example/live") != "open"
    finally:
        driver.close()


@needs_chromium
def test_a_recordings_page_cannot_open_a_websocket_to_a_local_address():
    async def scenario():
        recorder = Recorder(lambda message: asyncio.sleep(0))
        await recorder.start()
        try:
            assert recorder._context is not None and recorder._page is not None

            async def fulfill(route):
                await route.fulfill(body=PAGE, content_type="text/html")

            await recorder._context.route("https://site.example/**", fulfill)
            await recorder._page.goto("https://site.example/")
            return await recorder._page.evaluate(OPEN_SOCKET, "ws://127.0.0.1:9/x")
        finally:
            await recorder.close()

    assert asyncio.run(scenario()) == "open", "never connected"


# Ids never used again


def test_a_deleted_jobs_id_is_never_used_again(tmp_path):
    store = Store(str(tmp_path / "h.db"))
    anna = store.save(Job(user="anna", name="a", source="site", target="/x", schedule="0 6 * * *"))
    store.delete(anna.id or 0)
    ben = store.save(Job(user="ben", name="b", source="site", target="/y", schedule="0 6 * * *"))
    assert ben.id != anna.id
    # Anna's run, still going, writes for her job: nothing reaches Ben's.
    store.log(anna.id or 0, "info", "Downloading /x/anna-private.pdf")
    store.mark_seen(anna.id or 0, "https://anna.example/secret")
    store.finish(anna.id or 0, "ok", "1 file")
    assert not store.log_lines("ben") and not store.seen_keys(ben.id or 0)
    assert (store.job(ben.id or 0) or ben).last_message is None


def test_an_earlier_database_keeps_its_ids_and_never_uses_them_again(tmp_path):
    path = str(tmp_path / "h.db")
    earlier = SCHEMA.replace("id INTEGER PRIMARY KEY AUTOINCREMENT,\n    user", "id INTEGER PRIMARY KEY,\n    user")
    with sqlite3.connect(path) as db:
        db.executescript(earlier.replace(",\n    challenge_url TEXT", ""))
        for job_id in (1, 4, 7):
            db.execute(
                "INSERT INTO jobs (id, user, name, source, options, target, tags, schedule, enabled, created)"
                " VALUES (?, 'anna', 'a', 'site', '{}', '/x', '[]', '0 6 * * *', 1, 0)",
                (job_id,),
            )
        db.execute("INSERT INTO seen VALUES (4, 'k', 0)")
        db.execute("INSERT INTO log (job, at, level, message) VALUES (7, 0, 'info', 'm')")
    store = Store(path)
    assert [job.id for job in store.jobs()] == [1, 4, 7]
    assert store.seen_keys(4) == {"k"} and store.log_lines("anna", 7)[0]["message"] == "m"
    store.delete(7)
    assert store.save(Job(user="ben", name="b", source="site", target="/y", schedule="0 6 * * *")).id == 8
    store.delete(4)
    assert not store.seen_keys(4), "the other tables' rows still go with their job"
    assert Store(path).jobs()[-1].id == 8, "made once: opened again, it stays"


def test_deleting_a_job_stops_its_run(tmp_path, monkeypatch):
    class Executor:  # pylint: disable=too-few-public-methods
        """The executor: the runs stopped are noted."""

        stopped: list[int] = []

        def stop(self, job_id: int) -> bool:
            self.stopped.append(job_id)
            return True

    monkeypatch.setitem(main.STATE, "store", Store(str(tmp_path / "h.db")))
    monkeypatch.setitem(main.STATE, "executor", Executor())
    job = main.store().save(Job(user="anna", name="a", source="site", target="/x", schedule="0 6 * * *"))
    asyncio.run(main.delete_job(job.id or 0, Nextcloud()))  # type: ignore[arg-type]
    assert Executor.stopped == [job.id] and main.store().job(job.id or 0) is None


# GitLab's token


def test_a_gitlab_token_never_follows_a_release_link_to_another_host():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/releases"):
            links = [{"id": 1, "name": "app.zip", "direct_asset_url": "https://gitlab.com/o/r/-/releases/v1/app.zip"}]
            return httpx.Response(200, json=[{"tag_name": "v1", "assets": {"links": links, "sources": []}}])
        if request.url.host == "gitlab.com":
            return httpx.Response(302, headers={"location": "https://downloads.example.net/app.zip"})
        return httpx.Response(200, content=b"PK\x03\x04zip", headers={"content-type": "application/zip"})

    made = Releases(
        {"repository": "https://gitlab.com/o/r"},
        {"token": "secret"},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    made.sleep = lambda seconds: None
    (item,) = list(made.items(lambda key: False))
    dest = io.BytesIO()
    made.fetch(item, dest)
    assert dest.getvalue() == b"PK\x03\x04zip"
    forge = [request for request in requests if request.url.host == "gitlab.com"]
    assert forge and all(request.headers["authorization"] == "Bearer secret" for request in forge)
    elsewhere = [request for request in requests if request.url.host != "gitlab.com"]
    assert elsewhere and not any("secret" in str(request.headers) for request in elsewhere)
    assert not any("private-token" in request.headers for request in requests)


# Pages within limits


def test_a_page_or_feed_larger_than_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(download, "MAX_PAGE", 1000)
    page = b"<html>" + b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/announced":
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        return httpx.Response(200, content=iter([page[:600], page[600:]]), headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = site_module.HttpDriver(client)
    for path in ("/announced", "/streamed"):
        with pytest.raises(SourceError, match="too large for a page"):
            driver.open(f"https://site.example{path}")
    feed = Feed({"url": "https://news.example/streamed"}, {}, client)
    with pytest.raises(SourceError, match="too large for a page"):
        feed.entries()
    small = site_module.HttpDriver(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<a>")))
    )
    assert small.open("https://site.example/")[0] == "https://site.example/"


def test_a_web_page_instead_of_a_file_is_read_only_up_to_the_limit(monkeypatch):
    monkeypatch.setattr(download, "MAX_PAGE", 1000)
    read: list[int] = []

    def endless():
        for number in range(100_000):
            read.append(number)
            yield b"<p>" + b"x" * 500

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless(), headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="got a web page instead of a file"):
        download.fetch(download.http(client), "https://site.example/f", io.BytesIO(), {}, lambda s: None)
    assert len(read) < 10


# The rclone wizards' limit


def test_a_refused_wizard_ends_none_of_the_users_others(monkeypatch):
    from test_rclone_wizard import FakeRclone  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("HARVESTER_RCLONE", "/usr/bin/rclone")
    wizards = rclone_wizard.Wizards(run=FakeRclone().run)
    try:
        views = [wizards.start("anna", "webdav") for _ in range(rclone_wizard.PER_USER)]
        wizards.start("ben", "webdav")
        # The limit lowered under the wizards open (as if more had been open before).
        monkeypatch.setattr(rclone_wizard, "TOTAL", 2)
        with pytest.raises(rclone_wizard.WizardError) as refused:
            wizards.start("anna", "webdav")
        assert refused.value.status == 429
        for view in views:
            session = wizards.sessions[view["session"]]
            assert os.path.exists(session.dir), "still listed, still ended at the end"
    finally:
        wizards.close()


# Patterns with a time limit


def test_a_pattern_that_backtracks_without_end_ends_in_time_and_leaves_the_app_free():
    links = [[f"https://site.example/{'a' * 40}!", "x"] for _ in range(50)]

    class Page:  # pylint: disable=too-few-public-methods
        """A page of links that all make the pattern backtrack."""

        async def eval_on_selector_all(self, _selector, _script):
            return links

        async def evaluate(self, *_args):
            return None

    async def scenario() -> tuple[dict, int]:
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        recorder = Recorder(send)
        recorder._page = Page()  # type: ignore[assignment]
        recorder.links = {"selector": "a", "pattern": r"^https://site\.example/(a|aa)+$"}
        ticks = 0

        async def ticking():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        ticker = asyncio.ensure_future(ticking())
        await recorder._describe("links")
        ticker.cancel()
        return recorder.proposals["links"], ticks

    started = time.monotonic()
    proposal, ticks = asyncio.run(scenario())
    assert "takes too long" in proposal["error"]
    assert time.monotonic() - started < datapicks.PATTERN_TIMEOUT + 2
    assert ticks >= 10, "the event loop went on meanwhile"


def test_the_depth_of_a_page_with_a_pattern_that_backtracks_is_found_in_time():
    recorder = Recorder(lambda message: asyncio.sleep(0))
    recorder.follow = [{"selector": "a", "pattern": r"^(a|aa)+$"}]
    started = time.monotonic()
    assert recorder.depth("a" * 60 + "!") == 0
    assert time.monotonic() - started < 1


# The gallery's proxy


def test_the_proxys_password_never_is_on_gallery_dls_command_line(monkeypatch):
    monkeypatch.setattr(browser, "PROXY", "http://anna:pr0xy-secret@proxy.example:3128")
    made = Gallery({"url": "https://pics.example/album/1"}, {}, None)
    made.home = True
    configs: list[dict] = []

    def run(command, **_kwargs):
        path = command[command.index("--config") + 1]
        with open(path, encoding="utf-8") as file:
            configs.append(json.load(file))
        assert os.stat(path).st_mode & 0o077 == 0
        return subprocess.CompletedProcess(command, 0, "[]", "")

    made.run = run
    made.listing()
    assert configs == [{"extractor": {"proxy": "http://anna:pr0xy-secret@proxy.example:3128"}}]
    made.home = False
    made.run = lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "[]", "")
    made.listing()


# The app's secrets


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's /proc")
def test_the_apps_environment_is_not_readable_by_its_own_user_once_undumpable(tmp_path):
    script = (
        "import sys, time\nfrom harvester import browser\nprint(browser.undumpable(), flush=True)\ntime.sleep(30)\n"
    )
    environment = os.environ | {"PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "ex_app", "lib")}
    with subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=environment, cwd=tmp_path
    ) as process:
        try:
            assert process.stdout is not None and process.stdout.readline().strip() == "True"
            with pytest.raises(PermissionError):
                with open(f"/proc/{process.pid}/environ", "rb"):
                    pass
            assert os.stat(f"/proc/{process.pid}/environ").st_uid == 0
        finally:
            process.kill()


def test_playwrights_driver_starts_without_the_apps_secrets(monkeypatch):
    from playwright._impl import _transport  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("APP_SECRET", "app secret")
    monkeypatch.setenv("HP_SHARED_KEY", "harp key")
    environment = _transport.get_driver_env()
    assert "APP_SECRET" not in environment and "HP_SHARED_KEY" not in environment
    assert environment["PW_LANG_NAME"] == "python" and environment.get("PATH") == os.environ.get("PATH")
