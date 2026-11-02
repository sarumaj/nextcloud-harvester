"""The recipe's test in the recorder: the site source's own walk (Site.survey) in the recorder's
browser, on the person's screen, downloading nothing.

The walk is synchronous (a job runs it in a thread with Playwright's sync API); the recorder's
browser is driven with the async API on the app's event loop. The test runs the walk in a worker
thread, and Blocking hands each of its calls over to the loop and waits for the answer, so the
walk, its steps and its pages are the job's, not a second implementation. The test has a browser
context of its own (the recorder's session, its guard, no recorder.js: nothing it does is
recorded), which the recorder closes when the test ends or is stopped.
"""

import asyncio
import inspect
import threading
import time
from typing import Any

from ..sources import site
from ..sources.base import SourceError, Stopped

# How long a test may take in all (s), and one call of it at most (a step's own time limit, and
# some to spare for the loop).
TRIAL_SECONDS = 180
CALL_SECONDS = site.STEP_TIMEOUT / 1000 + 30


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
        # list, a screenshot's bytes) pass as they are.
        if hasattr(value, "_impl_obj"):
            return Blocking(value, self._loop, self._stop, self._deadline)
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
    recorder's, whose slot it holds), and no downloads."""

    def __init__(self, context: Blocking):  # pylint: disable=super-init-not-called
        self.context = context
        self.context.set_default_timeout(site.STEP_TIMEOUT)
        self.main = self.context.new_page()

    def download(self, url: str):
        raise SourceError(f"the recipe's test downloads nothing ({url})")

    def close(self) -> None:
        """Nothing: the recorder closes the test's context, and the browser is the recorder's."""


def survey(source: site.Site, context: Blocking, recipe: dict, session: dict | None) -> dict:
    """The test, in a worker thread: the steps (skipped while the session reaches the listing, as
    for a job with a kept session), then the walk (Site.survey)."""
    driver = TrialDriver(context)
    source.driver = driver
    return source.survey(
        begin=lambda: driver.sign_in(recipe.get("steps") or [], {}, session, recipe.get("start") or "")
    )
