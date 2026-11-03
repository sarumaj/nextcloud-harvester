# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recipe's test in the recorder: the site source's own walk (Site.survey) in the recorder's
browser, on the person's screen, downloading nothing.

The walk is synchronous (a job runs it in a thread with Playwright's sync API); the recorder's
browser is driven with the async API on the app's event loop. The test runs the walk in a worker
thread, and Blocking hands each of its calls over to the loop and waits for the answer, so the
walk, its steps and its pages are the job's, not a second implementation. The test has a browser
context of its own (the recorder's session, its guard, no recorder.js: nothing it does is
recorded), which the recorder closes when the test ends or is stopped (Trials: the recorder's part
that starts, shows and stops it).

As the job runs (as_job), the context starts empty, at the recipe's window size, with the job's
account (resolved by the app from the saved job, never sent to the page): the sign-in steps are
replayed with its secrets, as a run replays them, and the first bytes of the first file are
fetched, to tell a file of the job's types from a sign-in page (probe); file links that are
clicked are listed, never clicked (a click may download), as are pages to save or to capture files
of (never opened). Levels to follow that are clicked are
clicked as a run clicks them (harvester.sources.clicks: polled, so through Blocking alike).

A step that needs the person (a code from the phone, harvester.needs) asks on the test's own desk:
the page is told ({type: test-asking, kind, label, seconds}) and answers ({type: test-code}), within
the test's time.
"""

import asyncio
import base64
import inspect
import json
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page

from .. import browser
from .. import cookies as cookie_jar
from .. import needs
from ..recipe import uses_browser
from ..sources import download, site
from ..sources.base import SourceError, Stopped
from . import VIEWPORT

# Seconds closing a browser or a context may take.
CLOSE_TIMEOUT = 10
# How long a test may take in all (s), and one call of it at most (a step's own time limit, and
# some to spare for the loop).
TRIAL_SECONDS = 180
CALL_SECONDS = site.STEP_TIMEOUT / 1000 + 30
# How long the test waits for the page to be sent a step's progress (s).
SEND_SECONDS = 5
# What the test as the job runs reads of the first file: enough to tell its type, or a web page.
PROBE_BYTES = 64 * 1024


class Blocking:  # pylint: disable=too-few-public-methods
    """An async Playwright object (a context, a page, an element) used from a worker thread as
    the sync API's: each method runs on the loop and is waited for, what it returns wrapped
    alike. Once stop is set or the deadline passed, the next call raises Stopped ("stopped",
    "time"), as does a call cut short by the stop (its page closed under it)."""

    def __init__(self, target: Any, loop: asyncio.AbstractEventLoop, stop: threading.Event, deadline: float):
        self._target = target
        self._loop = loop
        self._stop = stop
        self._deadline = deadline

    def _wrap(self, value: Any) -> Any:
        # Playwright's own objects carry their implementation; plain values (a URL, the links'
        # list, a screenshot's bytes) pass as they are. A list may hold either (a frame's child
        # frames, a page's elements).
        if hasattr(value, "_impl_obj"):
            return Blocking(value, self._loop, self._stop, self._deadline)
        if isinstance(value, list):
            return [self._wrap(item) for item in value]
        return value

    def _check(self) -> None:
        if self._stop.is_set():
            raise Stopped("stopped")
        if time.monotonic() > self._deadline:
            raise Stopped("time")

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The event loop the calls run on."""
        return self._loop

    def nap(self, seconds: float) -> None:
        """A wait of the test (a step's pause, a retry's delay), in the worker thread: ended at
        once by the stop, and at the deadline (Stopped, as the next call would raise)."""
        self._check()
        self._stop.wait(max(0.0, min(seconds, self._deadline - time.monotonic())))
        self._check()

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if not callable(value):
            return self._wrap(value)

        def call(*args: Any, **kwargs: Any) -> Any:
            self._check()

            async def run() -> Any:
                result = value(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result

            try:
                answer = asyncio.run_coroutine_threadsafe(run(), self._loop).result(CALL_SECONDS)
            except Exception:
                self._check()
                raise
            return self._wrap(answer)

        return call


class TrialDriver(site.BrowserDriver):
    """The site source's browser driver in the test's context: no browser of its own (the
    recorder's, whose slot it holds). Its requests over HTTP (a JSON listing, the probe) are the
    job's: the context's cookies, the browser's user agent, its proxy."""

    def __init__(  # pylint: disable=super-init-not-called,too-many-arguments,too-many-positional-arguments
        self,
        context: Blocking,
        user_agent: str = "",
        proxy: str = "",
        told: Callable[[dict], None] | None = None,
        consent: bool = False,
    ):
        self.context = context
        self.user_agent = user_agent
        self.proxy = proxy
        # The test's context dismisses cookie banners as the recorder's does (consent.py): a
        # recorded click on one that is gone is skipped as optional, as a run with them dismissed
        # skips it.
        self.consent = consent
        # The steps' pauses and retries end with the test (its stop, its deadline); each step's
        # progress goes to the recorder's page (told).
        self.nap = context.nap
        self.told = told
        self.context.set_default_timeout(site.STEP_TIMEOUT)
        self.main = self.context.new_page()

    def close(self) -> None:
        """Nothing: the recorder closes the test's context, and the browser is the recorder's."""


def teller(send: Callable[[dict], Awaitable[None]], loop: asyncio.AbstractEventLoop) -> Callable[[dict], None]:
    """Tells the recorder's page of each step the test plays (tabs.step_event, as "test-step"),
    from the test's worker thread (send on the loop): its Steps show which one plays, its pause
    and attempts, how it ended."""

    def tell(event: dict) -> None:
        # Sent before the step goes on, so the page hears of the steps in their order and before
        # the test's result.
        try:
            asyncio.run_coroutine_threadsafe(send({"type": "test-step", **event}), loop).result(SEND_SECONDS)
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # the page is gone: the test goes on (and is stopped with the recording)

    return tell


def asker(send: Callable[[dict], Awaitable[None]], loop: asyncio.AbstractEventLoop) -> Callable[[needs.Asked], None]:
    """Tells the recorder's page what a step of the test asks the person (needs.Asked: never an
    answer), from the test's worker thread."""

    def tell(asked: needs.Asked) -> None:
        message = {"type": "test-asking", "kind": asked.kind, "label": asked.label, "seconds": asked.seconds}
        try:
            asyncio.run_coroutine_threadsafe(send(message), loop).result(SEND_SECONDS)
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # the page is gone: the step waits until the test ends

    return tell


def probe(driver: site.Driver, url: str, settings: dict) -> dict:
    """The first PROBE_BYTES of the file at url, checked as a download checks it: the answer's
    status, type and address (a redirect to a sign-in shows there), and whether it is a file of
    the job's types rather than a web page; nothing is kept."""
    types = download.file_types(settings)
    found: dict = {"url": url, "status": 0, "type": "", "address": "", "bytes": 0, "ok": False, "problem": ""}
    try:
        with driver.download(url) as response:
            start = bytearray()
            for chunk in response.chunks:
                start += chunk
                if len(start) >= PROBE_BYTES:
                    break
            found |= {
                "status": response.status,
                "type": response.headers.get("content-type", ""),
                "address": response.url,
            }
    except Stopped:
        raise
    except Exception as error:  # pylint: disable=broad-exception-caught
        return found | {"problem": (str(error) or type(error).__name__).splitlines()[0]}
    start = start[:PROBE_BYTES]
    found["bytes"] = len(start)
    if found["status"] >= 400:
        found["problem"] = f"the site answered {found['status']}"
    elif found["type"].startswith("text/html") and "html" not in types:
        found["problem"] = "a web page instead of a file (signed out, or a page that asks for a sign-in?)"
    elif not download.accepts(types, found["type"], bytes(start)):
        found["problem"] = f"not of the job's file types ({', '.join(types)})"
    found["ok"] = not found["problem"]
    return found


def survey(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    source: site.Site,
    context: Blocking,
    recipe: dict,
    session: dict | None,
    as_job: bool = False,
    user_agent: str = "",
    proxy: str = "",
    send: Callable[[dict], Awaitable[None]] | None = None,
    desk: needs.Desk | None = None,
    consent: bool = False,
) -> dict:
    """The test, in a worker thread: the steps (skipped while the session reaches the listing, as
    for a job with a kept session), each told to send as it is played (teller), then the walk
    (Site.survey). As the job runs: the account's cookies in the context and its secrets for the
    steps, and the first file probed. A step that needs the person asks on desk, told to send.
    With consent the context dismisses cookie banners: a recorded click on one is optional."""
    driver = TrialDriver(context, user_agent, proxy, teller(send, context.loop) if send else None, consent)
    source.driver = driver
    secrets = source.secrets if as_job else {}
    if as_job:
        # As a run adds them: the account's cookies over the (here empty) session.
        found = cookie_jar.to_playwright(source.account_cookies(recipe.get("start") or ""))
        if found:
            context.add_cookies(found)
    if desk is not None and send is not None:
        needs.serve(desk, 0, asker(send, context.loop))
    try:
        result = source.survey(
            begin=lambda: driver.sign_in(recipe.get("steps") or [], secrets, session, recipe.get("start") or "")
        )
    finally:
        needs.serve(None)
    # A test cut short (stopped, out of time) has no time left for the probe either.
    if as_job and result["ended"] not in ("stopped", "time"):
        # A file link that is clicked has no address to ask for: the test clicks nothing; a page
        # to save or to capture files of is a web page, as it should be.
        files = [
            file
            for page in result["pages"]
            for file in page["files"]
            if not (file["problem"] or file["click"] or file["save"] or file["capture"])
        ]
        result["probe"] = probe(driver, files[0]["url"], source.settings) if files else None
    return result


# The job's settings the recipe's test takes from the form: the file names, the pages, and what a
# download accepts (the file's extension comes from its type).
TRIAL_SETTINGS = (
    "path",
    "download_url",
    "max_pages",
    "browser",
    "http1",
    "file_type",
    "min_size",
    "max_size",
    "wait_pattern",
)


def recipe_size(recipe: dict) -> tuple[object, object]:
    """The window a recipe was recorded in (its "viewport"), else the jobs' default."""
    size = recipe.get("viewport") if isinstance(recipe.get("viewport"), dict) else VIEWPORT
    return size.get("width"), size.get("height")


class Trials:  # pylint: disable=too-few-public-methods
    """The recorder's test of the recipe (mixed into Recorder, whose state and methods these
    are): started, shown on the screen, stopped, and answered when a step asks the person."""

    send: Callable[[dict], Awaitable[None]]
    _browser: Browser | None
    _context: BrowserContext | None
    _page: Page | None
    _home: bool
    _consent: bool
    _desk: needs.Desk
    _trial_stop: threading.Event | None
    _trial: asyncio.Task | None
    _trial_context: BrowserContext | None
    _trial_size: dict | None

    recipe: dict

    async def _new_context(self, session: dict | None, viewport: dict | None = None) -> BrowserContext:
        """A browser context of the recorder's (its guard, its options)."""

    async def _show(self, page: Page) -> None:
        """Shows the page on the screen (the recorder's)."""

    async def test(self, message: dict, account: dict[str, str] | None = None) -> None:
        """Starts the recipe's test (trial.py) unless one runs; its result goes to the page. With
        the job's account (given by the app, never by the page) the test runs as the job does:
        a fresh context at the recipe's window, the account's secrets and cookies, the first file
        probed."""
        if self._trial and not self._trial.done():
            return
        recipe = message.get("recipe") if isinstance(message.get("recipe"), dict) else self.recipe
        given = message.get("settings") if isinstance(message.get("settings"), dict) else {}
        settings = {key: given[key] for key in TRIAL_SETTINGS if given.get(key) not in (None, "")}
        self._trial = asyncio.ensure_future(self._run_test(recipe, settings, account))

    async def _run_test(self, recipe: dict, settings: dict, account: dict[str, str] | None = None) -> None:
        """The test in a context of its own, shown on the screen; afterwards its context closes
        and the screen shows the recorder's page again."""
        assert self._context is not None and self._browser is not None
        home, context = self._page, None
        stop = self._trial_stop = threading.Event()
        as_job = account is not None
        try:
            source = site.Site({**settings, "recipe": json.dumps(recipe)}, account or {}, None)
            # As the job runs: no session (the sign-in is replayed), the recipe's window.
            session = None if as_job else await self._context.storage_state()
            self._trial_size = browser.fitted_viewport(*recipe_size(recipe)) if as_job else None
            context = self._trial_context = await self._new_context(session, self._trial_size)  # type: ignore[arg-type]
            context.on("page", lambda page: asyncio.ensure_future(self._show_trial(page, context, home)))
            blocking = Blocking(context, asyncio.get_running_loop(), stop, time.monotonic() + TRIAL_SECONDS)
            await self.send({"type": "test-started", "as_job": as_job})
            # The test's requests over HTTP as the browser's (user agent, connection); its steps told.
            # The test's steps that ask the person, on the test's desk; the cookie banners
            # dismissed in its context as in the recorder's.
            via = (
                browser.user_agent(self._browser.version),
                browser.PROXY if self._home else "",
                self.send,
                self._desk,
            )
            result = await asyncio.to_thread(survey, source, blocking, recipe, session, as_job, *via, self._consent)  # type: ignore[arg-type]
            result["as_job"] = as_job
            # Stopped by the person: what the stop cut short is no failure of the recipe.
            if stop.is_set() and result.get("ended") == "failed":
                result["ended"] = "stopped"
                result["problems"] = result["problems"][:-1]
            if not (uses_browser(recipe) or int(source.settings["browser"] or 0)):
                result["http"] = True
        except SourceError as error:
            result = {"pages": [], "problems": [str(error)], "files": 0, "ended": "failed", "shot": None}
        except Exception as error:  # pylint: disable=broad-exception-caught
            result = {
                "pages": [],
                "problems": [] if stop.is_set() else [(str(error) or type(error).__name__).splitlines()[0]],
                "files": 0,
                "ended": "stopped" if stop.is_set() else "failed",
                "shot": None,
            }
        finally:
            stop.set()
            if context is not None:
                try:
                    await asyncio.wait_for(context.close(), CLOSE_TIMEOUT)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass  # the browser is closing as a whole
            self._trial_stop = self._trial_context = self._trial_size = None
        shot = result.pop("shot", None)
        if shot:
            result["shot"] = {**shot, "image": base64.b64encode(shot["image"]).decode()}
        try:
            if home is not None and not home.is_closed():
                await self._show(home)
                await self.send({"type": "url", "url": home.url})
            await self.send({"type": "test", "result": result})
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # the recording ended meanwhile: its browser and page are gone

    async def _show_trial(self, page: Page, context: BrowserContext, home: Page | None) -> None:
        """Shows a page of the test; once it closes, the test's newest page still open (or the
        recorder's own)."""

        async def closed(_page: Page) -> None:
            if self._page is page:
                pages = [other for other in context.pages if not other.is_closed()]
                shown = pages[-1] if pages else home
                if shown is not None and not shown.is_closed():
                    try:
                        await self._show(shown)
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass  # the test's context is closing: the recorder's page comes back after

        page.on("close", lambda _: asyncio.ensure_future(closed(page)))
        try:
            await self._show(page)
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # closed at once (a page the walk read quickly): the next one is shown

    async def _trial_message(self, kind: object, message: dict) -> None:
        """The test's stop, or the code one of its steps asks the person for."""
        if kind == "test-code":
            self._desk.answer(0, str(message.get("code") or "").strip()[: needs.CODE_MAX])
        elif self._trial_stop:
            # The call under way ends with its pages (it may wait for a step's whole time else).
            self._trial_stop.set()
            if self._trial_context:
                await self._trial_context.close()
