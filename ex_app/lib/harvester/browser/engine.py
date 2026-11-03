# SPDX-License-Identifier: AGPL-3.0-or-later
"""What starts the browsers: Playwright, or Patchright for the jobs that disguise their browser
(the website source's option STEALTH, sources.base).

Patchright is a fork of Playwright with the same API and the same Chromium (its version shares
Playwright's browsers, PLAYWRIGHT_BROWSERS_PATH), patched where Playwright gives an automated
browser away to a page's bot check: the page finds none of Playwright's globals, its own scripts
run in isolated worlds, and Chromium starts without the automation's switches. Both start
Chromium with the same options (browser.launch_options, context_options) behind the same guard.
What Patchright changes for the app:

- page.evaluate runs in an isolated world by default: the calls of recorder.js's functions
  (window.__harvester) ask for the page's own (main_world); the others read the page's elements,
  which both worlds share.
- It applies no WebSocket route (context.route_web_socket), and its stand-in would show the page
  a global: a disguised browser has none, and its pages' WebSockets reach only the guard, which
  checks their destination as it checks every connection of the browser. Without the guard (the
  admin's switch, browser.GUARD) nothing would, so no disguised browser starts then (check).
- Both keep their wrapper of a route's handler on the handler itself, under the same name: one
  function given to both engines' routes in a process fails the second's requests (they hang),
  so each browser's routes get handlers of their own.
- A page restored from the back-forward cache tells no loads (the recorder's Back and Forward
  wait for its arrival alone).
- Its errors are Playwright's own classes (_patchright), so that the app's handlers of
  Playwright's errors catch them too.

Patchright never answers a challenge: a person still does, in the recorder ("Continue in the
browser"), and the disguise only keeps the browser from giving itself away.
"""

import functools
import importlib
import re
import sys
from collections.abc import Callable
from types import ModuleType

from playwright._impl import _errors  # type: ignore[import-not-found]

from ..sources.base import SourceError
from . import quiet_driver

REFUSED = (
    "the disguised browser needs the browsers' guard, which the admin switched off: a page's WebSockets "
    "would go unchecked (turn the guard on, or the job's option Disguise the browser off)"
)


@functools.cache
def _patchright() -> ModuleType:
    """Patchright's package, imported once: its errors made Playwright's (its module of errors is
    Playwright's, the same code, before any of its modules imports it), and its driver started
    without the app's secrets, as Playwright's (browser.quiet_driver)."""
    if "patchright._impl._errors" not in sys.modules:
        sys.modules["patchright._impl._errors"] = _errors
        importlib.import_module("patchright._impl")._errors = _errors  # pylint: disable=protected-access
    package = importlib.import_module("patchright")
    # pylint: disable-next=import-outside-toplevel
    from patchright._impl import _transport  # type: ignore[import-not-found]

    quiet_driver(_transport)
    return package


def api(stealth: bool, sync: bool = True) -> ModuleType:
    """Playwright's API module (sync_api or async_api), or Patchright's for a disguised browser."""
    name = "sync_api" if sync else "async_api"
    if stealth:
        _patchright()
        return importlib.import_module(f"patchright.{name}")
    return importlib.import_module(f"playwright.{name}")


def check(stealth: bool, guard: int) -> None:
    """Refuses a disguised browser without a guard (SourceError): its WebSockets are not routed,
    and the guard alone checks them."""
    if stealth and not guard:
        raise SourceError(REFUSED)


def start(stealth: bool, guard: int):
    """Playwright (sync API) started for a browser behind the guard's port (0: none), Patchright's
    for a disguised one; the caller launches Chromium with launch_options and stops it."""
    check(stealth, guard)
    return api(stealth).sync_playwright().start()


async def start_async(stealth: bool, guard: int):
    """start, with the async API (the recorder's)."""
    check(stealth, guard)
    return await api(stealth, sync=False).async_playwright().start()


def disguised(target: object) -> bool:
    """Whether a page, frame or element is of a disguised browser (Patchright's)."""
    return type(target).__module__.startswith("patchright.")


def main_world(target: object) -> dict:
    """The arguments of target.evaluate that run a script in the page's own world, where
    recorder.js's functions are: none for Playwright, which runs there anyway."""
    return {"isolated_context": False} if disguised(target) else {}


def guard_routes(context, guard: Callable, socket_guard: Callable) -> None:
    """A context's routes (sync API): every request through guard, every WebSocket through
    socket_guard, except a disguised browser's, which Patchright would not route (its guard checks
    them, check). The handlers are this context's own: each engine keeps its wrapper of a handler
    on the handler, under the same name, and one function given to both engines' routes in a
    process left the second's requests hanging."""
    context.route("**/*", functools.partial(guard))
    if not disguised(context):
        context.route_web_socket(re.compile(".*"), functools.partial(socket_guard))


async def guard_routes_async(context, guard: Callable, socket_guard: Callable) -> None:
    """guard_routes, with the async API (the recorder's)."""
    await context.route("**/*", functools.partial(guard))
    if not disguised(context):
        await context.route_web_socket(re.compile(".*"), functools.partial(socket_guard))
