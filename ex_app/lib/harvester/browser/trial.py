# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recipe's test in the recorder: the site source's own walk (Site.survey) in the recorder's
browser, on the person's screen, downloading nothing.

The walk is synchronous (a job runs it in a thread with Playwright's sync API); the recorder's
browser is driven with the async API on the app's event loop. The test runs the walk in a worker
thread, and Blocking hands each of its calls over to the loop and waits for the answer, so the
walk, its steps and its pages are the job's, not a second implementation. The test has a browser
context of its own (the recorder's session, its guard, no recorder.js: nothing it does is
recorded), which the recorder closes when the test ends or is stopped.

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
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .. import cookies as cookie_jar
from .. import needs
from ..sources import download, site
from ..sources.base import Stopped

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

    def __init__(
        self, context: Blocking, user_agent: str = "", proxy: str = "", told: Callable[[dict], None] | None = None
    ):  # pylint: disable=super-init-not-called
        self.context = context
        self.user_agent = user_agent
        self.proxy = proxy
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
) -> dict:
    """The test, in a worker thread: the steps (skipped while the session reaches the listing, as
    for a job with a kept session), each told to send as it is played (teller), then the walk
    (Site.survey). As the job runs: the account's cookies in the context and its secrets for the
    steps, and the first file probed. A step that needs the person asks on desk, told to send."""
    driver = TrialDriver(context, user_agent, proxy, teller(send, context.loop) if send else None)
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
