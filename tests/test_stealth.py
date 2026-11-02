"""The disguised browser (the website source's option "Disguise the browser", browser/engine.py):
offered for website jobs alone, started with Patchright instead of Playwright and otherwise alike
(the guard with its credentials, the launch and context options, the routes), refused without
the guard, for the job's runs, its recordings and "Continue in the browser". In Docker, a real
disguised Chromium behind its guard: the recorder's screen, recorder.js and its report, a
WebSocket checked by the guard alone.
"""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import re

import pytest

import main
from conftest import needs_loopback
from harvester import browser
from harvester.browser import engine
from harvester.browser.recorder import Recorder
from harvester.runner import COMMON_OPTIONS
from harvester.sources import SOURCES, SourceError
from harvester.sources import site as site_module
from harvester.store import Job, Store
from test_browser import needs_chromium
from test_security import OPEN_SOCKET, Nextcloud, Socket


def test_the_option_is_offered_for_website_jobs_alone():
    for name, source in SOURCES.items():
        assert ("stealth" in source.options) == (name == "site"), name
    assert "stealth" not in COMMON_OPTIONS
    option = SOURCES["site"].options["stealth"]
    assert (option.title, option.default, option.kind) == ("Disguise the browser", 0, "flag"), "off by default"
    assert option.description.startswith(
        "Disguises the browser as a person's, for sites whose bot check refuses it otherwise"
    )
    assert "answers no challenge" in option.description
    assert "stealth" not in SOURCES["site"].listing_options, "the same site either way"


def test_the_engine_follows_the_flag():
    assert engine.api(False).__name__ == "playwright.sync_api"
    assert engine.api(False, sync=False).__name__ == "playwright.async_api"
    assert engine.api(True).__name__ == "patchright.sync_api"
    assert engine.api(True, sync=False).__name__ == "patchright.async_api"


def test_patchrights_errors_are_playwrights():
    # The app catches Playwright's errors (a step's timeout, a selector Chromium refuses): a
    # disguised browser's must be caught alike.
    from playwright.sync_api import Error  # pylint: disable=import-outside-toplevel

    patchright = engine.api(True)
    assert patchright.Error is Error and issubclass(patchright.TimeoutError, Error)
    assert engine.api(True, sync=False).TimeoutError is patchright.TimeoutError


def test_patchrights_driver_starts_without_the_apps_secrets(monkeypatch):
    engine.api(True)
    from patchright._impl import _transport  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("APP_SECRET", "app secret")
    monkeypatch.setenv("HP_SHARED_KEY", "harp key")
    assert getattr(_transport.get_driver_env, "quiet", False), "browser.quiet_driver's"
    assert not {"APP_SECRET", "HP_SHARED_KEY"} & set(_transport.get_driver_env())


def test_no_disguised_browser_without_the_guard():
    engine.check(False, 0)
    engine.check(True, 4321)
    with pytest.raises(SourceError, match="needs the browsers' guard"):
        engine.check(True, 0)
    with pytest.raises(SourceError):
        engine.start(True, 0)
    with pytest.raises(SourceError):
        asyncio.run(engine.start_async(True, 0))


def test_main_world_only_for_patchrights_objects():
    class Page:  # pylint: disable=too-few-public-methods
        """A page of Playwright's (its module's name is what counts)."""

    Page.__module__ = "playwright.async_api._generated"
    assert not engine.main_world(Page())
    Page.__module__ = "patchright.async_api._generated"
    assert engine.main_world(Page()) == {"isolated_context": False}


class FakeGuard:
    """The browser's guard, without its process."""

    def __init__(self, upstream: str = ""):
        self.upstream = upstream
        self.credentials = ("user", "password")
        self.stopped = False

    def start(self) -> int:
        return 4321

    def stop(self) -> None:
        self.stopped = True

    def allow(self, origin: str) -> None:
        pass


class FakeEngine:  # pylint: disable=too-few-public-methods
    """Playwright's or Patchright's sync API: what the job's browser was started with."""

    def __init__(self, name: str, calls: list):
        self.name, self.calls = name, calls

    def sync_playwright(self):
        outer = self

        class Starter:  # pylint: disable=too-few-public-methods
            """sync_playwright()."""

            def start(self):
                return Playwright(outer)

        return Starter()


class Playwright:  # pylint: disable=too-few-public-methods
    """A started engine with its Chromium."""

    def __init__(self, engine_: FakeEngine):
        self.chromium = self
        self._engine = engine_

    def launch(self, **options):
        self._engine.calls.append((self._engine.name, "launch", options))
        return Browser(self._engine)

    def stop(self):
        pass


class Browser:
    """A launched Chromium."""

    version = "156.0.8078.4"

    def __init__(self, engine_: FakeEngine):
        self._engine = engine_

    def new_context(self, **options):
        self._engine.calls.append((self._engine.name, "context", options))
        # Of the engine's module, as engine.disguised tells them apart.
        return type("Context", (Context,), {"__module__": f"{self._engine.name}.sync_api._generated"})(self._engine)

    def close(self):
        pass


class Context:
    """A browser context: its routes are noted."""

    def __init__(self, engine_: FakeEngine):
        self._engine = engine_

    def route(self, pattern, handler):
        self._engine.calls.append((self._engine.name, "route", (pattern, handler)))

    def route_web_socket(self, pattern, handler):
        self._engine.calls.append((self._engine.name, "route_web_socket", (pattern, handler)))

    def set_default_timeout(self, _timeout):
        pass

    def new_page(self):
        return None

    def close(self):
        pass


def started(monkeypatch, stealth: bool) -> list:
    calls: list = []
    monkeypatch.setattr(
        engine, "api", lambda disguised, sync=True: FakeEngine("patchright" if disguised else "playwright", calls)
    )
    monkeypatch.setattr(site_module, "Guard", FakeGuard)
    driver = site_module.BrowserDriver([], {}, stealth=stealth)
    driver.close()
    return calls


def test_the_jobs_browser_disguised_or_not_launches_alike(monkeypatch):
    monkeypatch.setattr(browser, "GUARD", True)
    free = browser.free_browsers()
    plain, disguised = started(monkeypatch, False), started(monkeypatch, True)
    assert {call[0] for call in plain} == {"playwright"} and {call[0] for call in disguised} == {"patchright"}
    # The same launch (the guard with this browser's credentials) and context; the same route,
    # and no WebSocket route for Patchright, which would not apply it (the guard checks them).
    assert [call[1] for call in plain] == ["launch", "context", "route", "route_web_socket"]
    assert [call[1] for call in disguised] == ["launch", "context", "route"]
    assert [call[2] for call in plain[:2]] == [call[2] for call in disguised[:2]]
    launch, context, route = (call[2] for call in disguised)
    assert launch["proxy"] == {
        "server": "http://127.0.0.1:4321",
        "bypass": "<-loopback>",
        "username": "user",
        "password": "password",
    }
    assert context["service_workers"] == "block"
    assert route[0] == "**/*"
    # Its handler is the guard's check (a handler of this browser's own: see BrowserDriver).
    assert route[1] is not dict((call[1], call[2]) for call in plain)["route"][1]
    aborted: list = []
    refused = type(
        "Route", (), {"request": type("Request", (), {"url": "http://10.0.0.1/"})(), "abort": aborted.append}
    )
    route[1](refused())
    assert aborted == ["blockedbyclient"]
    assert browser.free_browsers() == free


def test_the_jobs_disguised_browser_is_refused_without_the_guard(monkeypatch):
    monkeypatch.setattr(browser, "GUARD", False)
    free = browser.free_browsers()
    with pytest.raises(SourceError, match="needs the browsers' guard"):
        started(monkeypatch, True)
    assert browser.free_browsers() == free, "its slot is free again"
    assert {call[0] for call in started(monkeypatch, False)} == {"playwright"}, "undisguised, as before"


def test_the_job_starts_its_browser_with_its_flag(monkeypatch):
    flags: list = []

    class Driver:  # pylint: disable=too-few-public-methods
        """BrowserDriver: its arguments are noted."""

        skipped: list = []
        repaired: list = []

        def __init__(self, *_args, stealth=False, **_kwargs):
            flags.append(stealth)

    monkeypatch.setattr(site_module, "BrowserDriver", Driver)
    recipe = '{"start": "https://site.example/", "links": {"selector": "a"}}'
    for value in (0, 1, "1"):
        source = site_module.Site({"recipe": recipe, "browser": 1, "stealth": value}, {}, None)
        source._driver()
    assert flags == [False, True, True]
    site_module.Site({"recipe": recipe, "browser": 1}, {}, None)._driver()
    assert flags[-1] is False, "off by default"


class StartedRecorder:
    """The recorder's browser: how it was started (its start fails, as a browser that does not)."""

    started: list[bool] = []

    def __init__(self, send):
        self.send = send

    async def start(self, *_args, stealth=False, **_kwargs):
        StartedRecorder.started.append(stealth)
        raise RuntimeError("no browser here")

    async def close(self):
        pass


def test_recordings_take_the_jobs_flag_or_the_forms(tmp_path, monkeypatch):
    # Continue in the browser: the job's own flag, so that a challenge is answered under the
    # fingerprint its runs have (whatever the page's address says); a new recording: the form's.
    monkeypatch.setitem(main.STATE, "store", Store(str(tmp_path / "h.db")))
    monkeypatch.setattr(main, "Recorder", StartedRecorder)

    async def no_session(*_args):
        return None

    monkeypatch.setattr(main, "load_session", no_session)
    for options, query, expected in (
        ({"stealth": 1}, {}, True),
        ({"stealth": 0}, {"stealth": "1"}, False),
        ({}, {}, False),
    ):
        job = main.store().save(
            Job(user="anna", name="a", source="site", target="/x", schedule="0 6 * * *", options=options)
        )
        websocket = Socket(job.id or 0)
        websocket.query_params |= query
        asyncio.run(main.record(websocket, Nextcloud()))  # type: ignore[arg-type]
        assert StartedRecorder.started[-1] is expected, (options, query)
    for query, expected in (({"stealth": "1"}, True), ({}, False)):
        websocket = Socket(0)
        websocket.query_params = query
        asyncio.run(main.record(websocket, Nextcloud()))  # type: ignore[arg-type]
        assert StartedRecorder.started[-1] is expected, query


def test_the_recorder_starts_its_engine_with_the_flag_and_its_guard(monkeypatch):
    asked: list = []

    async def refused(stealth, guard):
        asked.append((stealth, guard))
        raise SourceError("stop here")

    monkeypatch.setattr(browser, "GUARD", True)
    monkeypatch.setattr("harvester.browser.recorder.Guard", FakeGuard)
    monkeypatch.setattr("harvester.browser.recorder.start_async", refused)
    for stealth in (False, True):
        with pytest.raises(SourceError):
            asyncio.run(Recorder(lambda message: None).start(stealth=stealth))
    assert asked == [(False, 4321), (True, 4321)]


@needs_chromium
@needs_loopback
def test_a_disguised_recorder_shows_its_screen_and_records(monkeypatch):  # pylint: disable=too-many-locals
    # Patchright in a real Chromium, behind its guard: the screencast, recorder.js in the page's
    # own world reporting a pick (expose_binding), its outlines (main_world), and the page's
    # WebSocket to a private address refused by the guard though no route sees it.
    from fake_site import SITE, Site  # pylint: disable=import-outside-toplevel
    from test_browser import Driver, async_site  # pylint: disable=import-outside-toplevel
    from test_guard import http_server  # pylint: disable=import-outside-toplevel
    from test_guard_process import serving  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(browser, "GUARD", True)

    async def scenario():
        driver = Driver()
        recorder = driver.recorder = Recorder(driver.send)
        recorder.loopback = f"http://127.0.0.1:{port}"
        await recorder.start(stealth=True)
        try:
            assert engine.disguised(recorder._page)
            sockets: list = []
            await recorder._context.route_web_socket(re.compile(".*"), lambda route: sockets.append(route.url))
            await recorder._context.route(f"{SITE}/**", async_site(Site()))
            await recorder.handle({"type": "goto", "url": f"{SITE}/select/mag/archiv"})
            await recorder._page.wait_for_load_state()
            await driver.mode("follow")
            await driver.click("a.years__link")
            follow = await driver.until(lambda m: "follow0" in m["proposals"])
            outlines = await recorder._page.eval_on_selector_all(
                "a.years__link", "els => els.map(e => e.style.outline.includes('solid'))"
            )
            # A WebSocket from the one local page this browser may open to another local server:
            # no route sees it, the guard refuses it.
            await recorder._page.goto(f"http://127.0.0.1:{port}/page")
            opened = await recorder._page.evaluate(OPEN_SOCKET, f"ws://127.0.0.1:{other}/socket")
            refusals = list(recorder._gate.refused) if recorder._gate else []
            # Back and forward: a page restored from the back-forward cache.
            await driver.mode("browse")
            await recorder.handle({"type": "goto", "url": f"{SITE}/list?p=1"})
            await driver.click("a.next")
            await recorder._page.wait_for_url(re.compile(r"p=2$"))
            await recorder.handle({"type": "back"})
            back = recorder._page.url
            return driver, follow, outlines, (opened, sockets, refusals), back
        finally:
            await recorder.close()

    with serving(lambda: http_server([])) as port, serving(lambda: http_server([])) as other:
        driver, follow, outlines, (opened, sockets, refusals), back = asyncio.run(scenario())
    assert any(m["type"] == "frame" for m in driver.sent), "the screen"
    assert follow["recipe"]["follow"][0]["pattern"] == r"/select/mag/archiv/(?P<year>\d{4})$", "the pick"
    assert outlines == [True, True, True], "outlined by recorder.js"
    assert opened != "open" and sockets == [], "Patchright routes no WebSocket"
    assert f"127.0.0.1:{other}: not a public address" in refusals, "the guard refused it"
    assert back.endswith("p=1")


@needs_chromium
@needs_loopback
def test_a_disguised_jobs_browser_hides_the_automation(monkeypatch):
    monkeypatch.setattr(browser, "GUARD", True)
    page = "<html><body><script>document.title = String(navigator.webdriver)</script></body></html>"
    driver = site_module.BrowserDriver([], {}, stealth=True)
    try:
        assert engine.disguised(driver.main)
        driver.context.route(
            "https://site.example/**", lambda route: route.fulfill(status=200, content_type="text/html", body=page)
        )
        driver.main.goto("https://site.example/", wait_until="load")
        assert driver.main.title() == "false"
        assert "Headless" not in driver.main.evaluate("() => navigator.userAgent")
    finally:
        driver.close()


@needs_chromium
@needs_loopback
def test_a_disguised_browser_after_a_plain_one_in_the_same_thread(monkeypatch):
    # Playwright and Patchright keep their wrapper of a route's handler on the handler: with the
    # guard's own function for both, the second engine's routes got the first's wrapper and their
    # requests hung until their timeout. A request the route lets through ends at once here (the
    # guard does not find the test's name).
    from playwright.sync_api import Error  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(browser, "GUARD", True)
    for stealth in (False, True, False):
        driver = site_module.BrowserDriver([], {}, stealth=stealth)
        try:
            with pytest.raises(Error) as failed:
                driver.main.goto("https://site.example/", timeout=15000)
            assert "Timeout" not in str(failed.value), stealth
        finally:
            driver.close()
