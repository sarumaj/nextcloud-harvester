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
fetched, to tell a file of the job's types from a sign-in page (probe).
"""

import asyncio
import inspect
import threading
import time
from typing import Any

from .. import cookies as cookie_jar
from ..sources import download, site
from ..sources.base import Stopped

# How long a test may take in all (s), and one call of it at most (a step's own time limit, and
# some to spare for the loop).
TRIAL_SECONDS = 180
CALL_SECONDS = site.STEP_TIMEOUT / 1000 + 30
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
        self, context: Blocking, user_agent: str = "", proxy: str = ""
    ):  # pylint: disable=super-init-not-called
        self.context = context
        self.user_agent = user_agent
        self.proxy = proxy
        self.context.set_default_timeout(site.STEP_TIMEOUT)
        self.main = self.context.new_page()

    def close(self) -> None:
        """Nothing: the recorder closes the test's context, and the browser is the recorder's."""


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
) -> dict:
    """The test, in a worker thread: the steps (skipped while the session reaches the listing, as
    for a job with a kept session), then the walk (Site.survey). As the job runs: the account's
    cookies in the context and its secrets for the steps, and the first file probed."""
    driver = TrialDriver(context, user_agent, proxy)
    source.driver = driver
    secrets = source.secrets if as_job else {}
    if as_job:
        # As a run adds them: the account's cookies over the (here empty) session.
        found = cookie_jar.to_playwright(source.account_cookies(recipe.get("start") or ""))
        if found:
            context.add_cookies(found)
    result = source.survey(
        begin=lambda: driver.sign_in(recipe.get("steps") or [], secrets, session, recipe.get("start") or "")
    )
    # A test cut short (stopped, out of time) has no time left for the probe either.
    if as_job and result["ended"] not in ("stopped", "time"):
        files = [file for page in result["pages"] for file in page["files"] if not file["problem"]]
        result["probe"] = probe(driver, files[0]["url"], source.settings) if files else None
    return result
