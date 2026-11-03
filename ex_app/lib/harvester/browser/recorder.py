# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder: a browser in the app that a person drives from the Harvester page.

The page shows the browser's screen (Chromium's screencast, JPEG frames) and sends the
person's mouse, keys and commands; recorder.js in the browser reports the steps and picks.
For each pick of links the recorder proposes a pattern with named groups
(harvester.patterns.propose), outlines in the page the links it matches (solid) and those it
leaves out (dashed), and shows examples with their values; the person may edit the pattern.

The messages are JSON over the page's WebSocket:

- to the page: {type: frame, data}, {type: url, url}, {type: error, message}, {type: recipe, recipe,
  proposals}, {type: test, result} (the recipe's test, trial.py), {type: test-step, ...} (its steps),
  {type: where, frame (null: the page; else {host, url, level})} (where the person acts);
- from the page: {type: load, recipe} (the picks of the recipe the form holds), {type: goto,
  url}, {type: back}, {type: forward}, {type: reload}, {type: mouse, event (move, down, up), x,
  y, button}, {type: wheel, dx, dy}, {type: key, key}, {type: text, text}, {type: mode, mode,
  level (a level's links to pick again)}, {type: pattern, target (follow, links), level,
  pattern}, {type: selector, target (follow, links, next), level, selector}, {type: click, target,
  level, click} (a pick clicked or not, linkpicks.py), {type: save, save} (the file links' pages
  saved as files, linkpicks.py), {type: remove,
  target, level}, {type: next-kind, kind (link, number)}, {type: undo} (the last step), {type:
  undo-pick} (the last change of the picks), {type: scroll} (a step: scroll to load more),
  the Steps list's edits (steps.EDITS: optional, step-delete, step-move, step-edit, record-at,
  mail-code, ask-code, each), {type: test, recipe, settings, as_job, job} (as_job: the app adds the
  saved job's account, main.py), {type: test-stop}, {type: test-code, code} (what a step of the
  test asks, {type: test-asking}, trial.py), {type: clear}, and the cookies panel's (jar.py);
- values from the page: {type: mode, mode: value, target, level} (pick a value near the level's
  links), {type: value, target, level, index, changes (name, regex, type, attribute, closest,
  selector, field)} (one changed), {type: value-add, target, level, field} (a JSON item's field as one),
  {type: value-remove, target, level, index};
- JSON listings: to the page {type: requests, items: [{id, url, size, status, method, summary}]}
  (the page's XHR and fetch answers in JSON to GET and POST, the Network panel); from it {type:
  json-preview, id} (its body to read: to the page {type: request-body, id, text, truncated, kept}),
  {type: json-pick, id} (one as the file links), {type: json-edit, changes} (its address, items,
  fields, pages, cursor, body, pattern);

The steps are the recipe's: the form's (load), edited and recorded (steps.py); a popup is shown
and recorded (popups.py). A click or press the page navigated after keeps the address it led to.

Navigation (Back, Forward, Reload, an address) is never a step outside the mode record. In it,
an address typed, Back and Forward are a step "goto" to the page arrived at (a job's browser has
no history to go back in); Reload is none (the same page). Scrolling is no step: positions
depend on the window's size; a listing that loads more as it is scrolled gets the step "scroll"
(scroll to its end until no more comes), added with its button.

recorder.js finds each step's element and each pick by several selectors (harvester.selectors):
the step or the pick keeps the best and the others as alternatives. The listing (the recipe's
start) is the page picked on, or, when that page is where the recorded steps lead and its
address carries a token of the visit (?code=, ?ticket=, …), where the steps end: the recipe
has no start then. The recorder warns of such tokens in the steps' addresses and the listing's
({type: recipe, warnings}).

recorder.js runs in every frame of the page. A step done inside a frame keeps the frame's path
from the page down (recipe: "frame"), made here from the frame the report came from: each
frame's element as its parent's recorder.js names it, and its address. Picks inside a frame are
refused: the walk reads the page's own links (a listing inside a frame is rare, and its address
can be opened and picked on directly).
"""

import asyncio
import base64
import contextlib
import copy
import json
import logging
import re
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

import regex

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    Route,
    WebSocketRoute,
)

from .. import browser, images, needs, pagination, patterns
from ..recipe import SCROLL_ROUNDS, frame_address, in_this_recipe, leaked_passwords, token_warnings, uses_browser
from ..sources import site
from ..sources.base import SourceError
from . import jar, trial
from .datapicks import DATA_MESSAGES
from .linkpicks import HIGHLIGHT, LinkPicks, link_proposal
from .engine import disguised, guard_routes_async, main_world, start_async
from .popups import Popups
from . import (
    SCRIPT,
    TRACE_URL,
    VIEWPORT,
    allowed_url,
    context_options,
    fitted_viewport,
    http_address,
    launch_options,
    reachable,
    seen_address,
)
from .guard import Guard
from .steps import EDITS, StepList

# record: steps; browse: go somewhere unrecorded; follow, links, next: pick links; value: pick a
# value near a level's links.
MODES = ("record", "browse", "follow", "links", "next", "value")
# Text up to this long is typed key by key; longer (a paste) is inserted at once.
TYPED = 200
# Seconds closing the browser may take.
CLOSE_TIMEOUT = 10
# Tries to start a page's screencast while it is being attached, and the pause between them.
SCREENCAST_TRIES = 10
SCREENCAST_PAUSE = 0.2
DEPTH_TIMEOUT = 0.2
# The changes of the picks Undo can take back.
PICKS_KEPT = 50
# What the page sends to the browser's page itself: ignored while the recipe's test runs there.
INPUT = frozenset({"goto", "back", "forward", "reload", "mouse", "wheel", "key", "text", "scroll"})
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
FRAME_SELECTOR = (
    "el => window.__harvester && window.__harvester.frameSelector ? window.__harvester.frameSelector(el) : ''"
)
SET_MODE = "mode => window.__harvester && (window.__harvester.mode = mode)"
# What recorder.js refuses to record, and why.
REFUSED = {
    "file": "Files to upload cannot be recorded: a job has none of yours to upload. Leave the upload "
    "out of the recipe.",
    "closed-shadow": "This element is inside a closed shadow root, which neither the recorder nor a job "
    "can look into: the click is recorded on the element around it, which a job clicks in its middle.",
}
LOGGER = logging.getLogger("harvester")
# The links a value is picked near (mode value): recorder.js finds the one the picked text belongs to.
SET_VALUE_LINKS = "s => window.__harvester && (window.__harvester.valueLinks = s)"


# The cursors the page's screen may take (CSS keywords), as the page under the mouse shows them.
CURSORS = frozenset(
    "default pointer text crosshair move grab grabbing not-allowed wait progress help cell copy "
    "zoom-in zoom-out col-resize row-resize ew-resize ns-resize nesw-resize nwse-resize vertical-text".split()
)


def recipe_size(recipe: dict) -> tuple[object, object]:
    """The window a recipe was recorded in (its "viewport"), else the jobs' default."""
    size = recipe.get("viewport") if isinstance(recipe.get("viewport"), dict) else VIEWPORT
    return size.get("width"), size.get("height")


async def frame_path(frame) -> list[dict]:
    """The frame's path from the page down, as a step keeps it (recipe: "frame"): for each frame
    its element's selector in its parent, its address without the query and its name (unless it
    looks generated). A frame detached meanwhile has its address alone."""
    path: list[dict] = []
    while frame.parent_frame is not None:
        selector = ""
        try:
            owner = await frame.frame_element()
            selector = await owner.evaluate(FRAME_SELECTOR, **main_world(owner)) or ""
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # gone or navigating: its address finds it
        entry = {"selector": selector, "url": frame_address(frame.url)}
        if frame.name and not re.search(r"\d{3,}", frame.name):
            entry["name"] = frame.name
        path.insert(0, entry)
        frame = frame.parent_frame
    return path


def frame_place(frame) -> dict | None:
    """Where a frame is, for the page's "Acting in": None for the page itself, else the frame's
    host, address and level (1: a frame of the page, 2: a frame in that one, …)."""
    level, parent = 0, frame.parent_frame
    while parent is not None:
        level, parent = level + 1, parent.parent_frame
    if not level:
        return None
    return {"host": urlparse(frame.url).hostname or "", "url": frame_address(frame.url), "level": level}


class Recorder(LinkPicks, Popups):  # pylint: disable=too-many-instance-attributes
    """One recording: a browser, the page shown and the recipe so far."""

    def __init__(self, send: Callable[[dict], Awaitable[None]]):
        self.send = send
        # The steps: the form's, those edited, those recorded.
        self.steps = StepList()
        self.start_url = ""
        self.follow: list[dict] = []
        self.links: dict = {}
        self.next: dict = {}
        # Per picked level ("follow0", "links"): groups, examples, proposed file name and tags.
        self.proposals: dict[str, dict] = {}
        self.mode = "record"
        # Where the person acts now.
        self._where: dict | None = None
        # The page that opened each popup, and the popups recorded as a step "popup" (popups.py).
        self._openers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._popups: weakref.WeakSet = weakref.WeakSet()
        # Whether the page shown is where the recorded steps lead (no browsing since the last).
        self._at_steps_end = False
        # The window's size: the page's, as the person makes the view larger or smaller.
        self.viewport = dict(VIEWPORT)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        # Whether the page's screen comes as frames (its screencast started).
        self._casting = False
        # The pages whose events the recorder listens to (each once, however often shown).
        self._watched: weakref.WeakSet = weakref.WeakSet()
        # The picks before each change (Undo), and the level of links to follow being picked again.
        self._picks: list[tuple] = []
        self.repick: int | None = None
        # The address each level to follow was picked on (one clicked has no pattern on addresses).
        self._picked_at: dict[int, str] = {}
        # The level a value is picked for (mode value): ("links", 0) or ("follow", level).
        self.value_for: tuple[str, int] | None = None
        # The page's answers in JSON (Network), and the one chosen as the listing: (url, data).
        self.requests: list[dict] = []
        self._json: tuple[str, object] | None = None
        # The running test's stop, and the test itself.
        self._trial_stop: threading.Event | None = None
        self._trial: asyncio.Task | None = None
        self._trial_context: BrowserContext | None = None
        # Where the test's steps ask the person (a code from the phone) and get the answer.
        self._desk = needs.Desk()
        # The test's window (as the job runs: the recipe's), and whether the browser goes through
        # the home connection (the test's requests over HTTP take it too).
        self._trial_size: dict | None = None
        self._home = False
        # The browser's connections all go through its guard (started with the browser).
        self._gate: Guard | None = None
        self._proxy: dict | None = None
        self._loopback = ""

    @property
    def loopback(self) -> str:
        """The one local address this browser may open besides the public ones (scheme, host and
        port, e.g. rclone's sign-in listener while it runs); "" for none. Its guard learns it at
        once, for this browser alone."""
        return self._loopback

    @loopback.setter
    def loopback(self, origin: str) -> None:
        self._loopback = origin
        if self._gate:
            self._gate.allow(origin)

    def opens_loopback(self, url: str) -> bool:
        """Whether the address is at self.loopback (any path and query)."""
        if not self.loopback:
            return False
        try:
            parsed, allowed = urlparse(url), urlparse(self.loopback)
            return (parsed.scheme, parsed.hostname, parsed.port) == (allowed.scheme, allowed.hostname, allowed.port)
        except ValueError:
            return False

    @property
    def events(self) -> list[dict]:
        """What was recorded since the last edit of the steps."""
        return self.steps.events

    @property
    def recipe(self) -> dict:
        """The recipe so far."""
        recipe: dict = {"steps": self.steps.all(), "start": self.start_url, "follow": self.follow}
        recipe["links"] = self.links
        if self.next:
            recipe["next"] = self.next
        # The window the steps were recorded and the links picked in: the job's browser takes it.
        if self.events or self._picks:
            recipe["viewport"] = dict(self.viewport)
        return recipe

    async def start(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, session: dict | None = None, url: str = "", http1: bool = False, home: bool = False, stealth: bool = False
    ) -> None:
        """Starts the browser with an empty page, or with a job's session at a page of it (the one
        that asked for a person); http1 for the sites that cut HTTP/2 connections, home through
        the home connection, stealth disguised (engine.py: as the job's runs, so that a challenge
        answered here holds for them)."""
        self._home = home
        # The guard (unless the admin switched it off) with the home connection, if chosen, and
        # this browser's local exception; without one, the routes alone let that address through.
        port = 0
        if browser.GUARD:
            self._gate = Guard(browser.PROXY if home else "")
            self._gate.allow(self.loopback)
            port = await asyncio.to_thread(self._gate.start)
        self._playwright = await start_async(stealth, port)
        credentials = self._gate.credentials if self._gate else None
        options = launch_options(port, http1, home, credentials)
        # The browser's way out (its guard, the home connection, or none), for asking which address
        # sites see without the browser's cookies (_tell_address).
        self._proxy = options.get("proxy")
        self._browser = await self._playwright.chromium.launch(**options)
        self._context = await self._new_context(session)
        await self._context.expose_binding("__harvesterReport", self._report)
        await self._context.add_init_script(SCRIPT)
        self._context.on("page", lambda page: asyncio.ensure_future(self._opened(page)))
        await self._show(await self._context.new_page())
        asyncio.ensure_future(self._tell_address(home and bool(browser.PROXY)))
        if url and (self.opens_loopback(url) or await asyncio.to_thread(allowed_url, url)) and self._page:
            await self._page.goto(url, wait_until="domcontentloaded")

    async def _new_context(self, session: dict | None, viewport: dict | None = None) -> BrowserContext:
        """A browser context with the session (and a window of the size, the recorder's without),
        guarded as every one of the recorder's (its own, and the recipe's test's): the routes
        here, and the browser's guard (its proxy) for all of them alike."""
        assert self._browser is not None
        context = await self._browser.new_context(
            **context_options(self._browser.version, viewport or self.viewport),
            accept_downloads=False,
            storage_state=session or None,  # type: ignore[arg-type]
        )
        await guard_routes_async(context, self._guard, self._socket_guard)
        return context

    async def _tell_address(self, proxied: bool) -> None:
        """Tells the page which address sites see this browser come from, asked through the
        browser's own connection (its proxy, if any), and whether that is the proxy."""
        via = browser.PROXY_NAME if proxied else "directly"
        # A request context of its own on the browser's way out: the context's own (context.request)
        # shares the browser's cookies, and Cloudflare answers with one (__cf_bm), which would
        # land in the person's recording and the job's kept session.
        requests = None
        try:
            assert self._playwright is not None
            requests = await self._playwright.request.new_context(proxy=self._proxy)  # type: ignore[arg-type]
            answer = await requests.get(TRACE_URL, timeout=15000)
            seen = seen_address(await answer.text())
        except Exception as error:  # pylint: disable=broad-exception-caught
            text = (str(error) or type(error).__name__).splitlines()[0]
            await self.send({"type": "network", "via": via, "proxied": proxied, "error": text})
            return
        finally:
            if requests is not None:
                await requests.dispose()
        await self.send({"type": "network", "via": via, "proxied": proxied, **seen})

    async def session(self) -> dict:
        """The browser's session now: cookies and local storage, for the job to continue with."""
        assert self._context is not None
        return await self._context.storage_state()  # type: ignore[return-value]

    async def close(self) -> None:
        """Closes the browser, within seconds even when a page or a send is stuck, and then its
        guard (its port and connections)."""
        if self._trial_stop:
            self._trial_stop.set()
        for closing in (self._browser and self._browser.close(), self._playwright and self._playwright.stop()):
            if closing:
                try:
                    await asyncio.wait_for(closing, CLOSE_TIMEOUT)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass  # stopping Playwright ends its browser in any case
        if self._gate:
            await asyncio.to_thread(self._gate.stop)

    async def _guard(self, route: Route) -> None:
        # The name's lookup off the event loop (the screen and the input go on meanwhile).
        if self.opens_loopback(route.request.url) or await asyncio.to_thread(reachable, route.request.url):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def _socket_guard(self, route: WebSocketRoute) -> None:
        # A page's WebSockets pass by the routes: the same check, on their http(s) address; one
        # refused is never connected (sources.site.socket_guard).
        url = http_address(route.url)
        if self.opens_loopback(url) or await asyncio.to_thread(reachable, url):
            route.connect_to_server()

    def _record(self, event: dict) -> None:
        """A step's event, after the steps so far: the page is where the steps lead now; a click or
        press may navigate it (its address to wait for)."""
        self.steps.record(event)
        self._at_steps_end = True

    async def _show(self, page: Page) -> None:
        """Shows this page (the first, or a new tab or window the site opened). Once per page:
        the context's "page" event also fires for the first one, which start() shows itself, and
        a second screencast set up meanwhile would stop the first's half-way (0.7.1's broken
        screen)."""
        if page is self._page:
            return
        if self._cdp:
            try:
                await self._cdp.send("Page.stopScreencast")
                await self._cdp.detach()
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # the page is gone
        self._page = page
        # A page shown again (the recorder's own after the recipe's test) has its listeners.
        if page not in self._watched:
            self._watched.add(page)
            page.on("domcontentloaded", lambda loaded: asyncio.ensure_future(self._loaded(loaded)))
            page.on("framenavigated", lambda frame: asyncio.ensure_future(self._navigated(frame)))
            page.on("response", lambda response: asyncio.ensure_future(self._response(page, response)))
        # A page of the test as the job runs keeps the job's window.
        size = self._size(page)
        if page.viewport_size != size:
            await page.set_viewport_size(size)  # type: ignore[arg-type]
        self._cdp = await page.context.new_cdp_session(page)
        self._cdp.on("Page.screencastFrame", lambda frame: asyncio.ensure_future(self._frame(frame)))
        await self._screencast()

    def _size(self, page: Page | None) -> dict:
        """The window of the page: the test's (as the job runs) for its pages, else the recorder's."""
        testing = self._trial_context
        if page is not None and testing is not None and self._trial_size and page.context == testing:
            return self._trial_size
        return self.viewport

    async def _screencast(self) -> None:
        """The page's screen as frames, at the window's size. A page still being attached (just
        opened, or between two documents) refuses it ("Not attached to an active page"): tried
        again a few times, else at the page's next load (_loaded), never left without a screen."""
        assert self._cdp is not None
        self._casting = False
        for attempt in range(SCREENCAST_TRIES):
            try:
                await self._cdp.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 60,
                        "maxWidth": self._size(self._page)["width"],
                        "maxHeight": self._size(self._page)["height"],
                    },
                )
            except Exception:  # pylint: disable=broad-exception-caught
                if attempt + 1 < SCREENCAST_TRIES:
                    await asyncio.sleep(SCREENCAST_PAUSE)
                continue
            self._casting = True
            return

    async def _loaded(self, page: Page | None = None) -> None:
        """A document of the page loaded: recorder.js in it, its mode, and its screen if it did not
        start before."""
        if page is not None:
            await self._install(page)
        if not self._casting and self._cdp:
            await self._screencast()
        await self._apply_mode()

    async def resize(self, width: object, height: object) -> None:
        """The window at the size the person's view has (within the limits): the page lays out
        anew for it, the frames come at it, and the page learns it (for the mouse's positions)."""
        self.viewport = fitted_viewport(width, height)
        if self._context:
            for page in self._context.pages:
                await page.set_viewport_size(self.viewport)  # type: ignore[arg-type]
        if self._cdp:
            await self._cdp.send("Page.stopScreencast")
            await self._screencast()
        await self.send({"type": "viewport", **self.viewport})

    async def _frame(self, frame: dict) -> None:
        # Acknowledged once sent: Chromium sends the next frame only then, so frames never
        # pile up for a page that reads slowly (or not at all).
        try:
            await self.send({"type": "frame", "data": frame["data"]})
        finally:
            if self._cdp:
                try:
                    await self._cdp.send("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})
                except Exception:  # pylint: disable=broad-exception-caught
                    pass  # the page is gone

    async def _navigated(self, frame) -> None:
        if self._page and frame == self._page.main_frame:
            if self.mode != "record":
                self._at_steps_end = False
            # A click or press just before: the address it led to (the last, after redirects).
            step = self.steps.navigation()
            if step is not None and await asyncio.to_thread(allowed_url, frame.url):
                step["navigated"] = frame.url
                await self._update()
            await self.send({"type": "url", "url": frame.url})

    async def _apply_mode(self) -> None:
        # In every frame: a click into one picks or records as in the page (a frame loaded
        # later asks recorder.js's "mode" itself).
        for frame in self._page.frames if self._page else []:
            try:
                await frame.evaluate(SET_MODE, self.mode, **main_world(frame))
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # navigated meanwhile
        if self._page and self.mode == "value":
            # The page's own links only: picks in frames are refused.
            with contextlib.suppress(Exception):
                await self._page.evaluate(
                    SET_VALUE_LINKS,
                    self._spec(*self.value_for).get("selector", "") if self.value_for else "",
                    **main_world(self._page),
                )

    async def _update(self) -> None:
        recipe = self.recipe
        await self.send(
            {"type": "recipe", "recipe": recipe, "proposals": self.proposals, "warnings": token_warnings(recipe)}
        )

    def depth(self, url: str) -> int:
        """How deep the page is: 0 for the listing, n for a page the nth level's links lead to (a
        pattern that is none, or takes too long, matches nothing); below a level that is clicked,
        any address but the one it was picked on (a dialog on that address is not below it)."""
        for level in range(len(self.follow), 0, -1):
            if self.follow[level - 1].get("click"):
                picked = self._picked_at.get(level - 1, self.start_url if level == 1 else "")
                if picked and url != picked:
                    return level
                continue
            pattern = self.follow[level - 1].get("pattern", "")
            try:
                if regex.search(pattern, url, timeout=DEPTH_TIMEOUT):
                    return level
            except (regex.error, TimeoutError):
                continue
        return 0

    async def _report(self, source: dict, event: dict) -> str | None:
        """What recorder.js reports from a frame (source["frame"]: the page's or one inside it):
        the cursor, where the person acts, a step, a pick; the mode to a frame that asks."""
        action, frame = event.get("action"), source.get("frame")
        inside = frame is not None and frame.parent_frame is not None
        if action == "mode":
            return self.mode
        if action == "cursor":
            # Shown on the page's screen, never a step; only cursors CSS knows by name.
            cursor = str(event.get("cursor", ""))
            await self.send({"type": "cursor", "cursor": cursor if cursor in CURSORS else "default"})
        elif action == "where":
            place = frame_place(frame) if frame is not None else None
            if place != self._where:
                self._where = place
                await self.send({"type": "where", "frame": place})
        elif action == "pick" and inside:
            host = urlparse(frame.url).hostname or "a frame"
            await self.send(
                {
                    "type": "error",
                    "message": f"Picks inside a frame ({host}) are not supported: the job "
                    f"reads the page's own links. Open the frame's address to pick there: {frame.url}",
                }
            )
        elif action == "pick" and event.get("target") == "value":
            await self._pick_value(event)
        elif action == "pick":
            await self._pick(event)
        elif action == "refused":
            await self.send({"type": "error", "message": REFUSED.get(str(event.get("what")), "Not recorded.")})
        elif self.mode == "record":
            if inside:
                event["frame"] = await frame_path(frame)
            self._record(event)
            await self._update()
        return None

    async def _pick(self, event: dict) -> None:
        """A pick of links to follow, file links or the next page, on the page itself."""
        assert self._page is not None
        url, target = self._page.url, event.get("target")
        level = self.depth(url)
        self._remember()
        if target == "next":
            await self._pick_next(event, level)
            return
        if level == 0:
            self.start_url = "" if self._starts_at_steps_end(url) else url
        # Elements without an address (buttons, rows a script opens): the job clicks them.
        proposal, clicked = link_proposal(images.resolved(event.get("matches", [])), str(target))
        spec = {"selector": event["selector"], "pattern": proposal["pattern"]} | self._alternatives(event)
        spec |= {"click": True} if clicked else {}
        if target == "follow":
            self._picked_at[self.repick if self.repick is not None else level] = url
        if target == "follow" and self.repick is not None and self.repick < len(self.follow):
            # Picked again: this level's links change, the levels after it stay.
            level, self.repick = self.repick, None
            self.follow[level] = spec
            key = f"follow{level}"
        elif target == "follow":
            self.follow = self.follow[:level] + [spec]
            self.proposals = {key: value for key, value in self.proposals.items() if key in ("links", "next")}
            key = f"follow{level}"
        else:
            self.links = spec
            key = "links"
        self.proposals[key] = {**proposal, "level": level}
        await self._describe(key)

    def _starts_at_steps_end(self, url: str) -> bool:
        """Whether the listing picked on is better the page the steps end on than its address: the
        steps lead there (they open a page, nothing browsed since) and the address carries a token
        of this visit (a sign-in's ?code=, say), spent when the job runs."""
        opens = any(step.get("action") == "goto" for step in self.steps.all())
        return self._at_steps_end and opens and bool(patterns.token_parameters(url))

    @staticmethod
    def _alternatives(event: dict) -> dict:
        """The pick's alternative selectors recorder.js found, if any."""
        alternatives = [str(value) for value in event.get("alternatives") or [] if value]
        return {"alternatives": alternatives} if alternatives else {}

    async def _pick_next(self, event: dict, level: int) -> None:
        """The next page's link picked: the link itself, or, when it is a number among numbers
        (recorder.js found their container), numbered pages (harvester.pagination), proposed
        with what this page shows: its number and the next one's."""
        link = {"selector": event["selector"], "level": level} | self._alternatives(event)
        container = str((event.get("numbered") or {}).get("selector") or "")
        if not container:
            self.next = link
            self.proposals["next"] = {"kind": "link", "link": link}
            await self._update()
            return
        assert self._page is not None
        html = await self._page.evaluate(site.OUTER_HTML, container)
        found = pagination.numbers(html)
        current = pagination.current_page(found, self._page.url, 1)
        following = pagination.following(html, self._page.url, 1)
        self.next = {"kind": "number", "selector": container, "level": level}
        # Numbers that are buttons have no address: only the browser clicks them.
        if any(item.clickable and not item.href for item in found):
            self.next["buttons"] = True
        self.proposals["next"] = {
            "kind": "number",
            "link": link,
            "number": dict(self.next),
            "numbers": sorted({item.number for item in found}),
            "current": current,
            "following": following[0] if following else None,
        }
        await self._page.evaluate(HIGHLIGHT, [container, None, None], **main_world(self._page))
        await self._update()

    def _remember(self) -> None:
        """Keeps the picks as they are, for Undo to bring them back."""
        picks = (self.start_url, self.follow, self.links, self.next, self.proposals)
        self._picks = (self._picks + [copy.deepcopy(picks)])[-PICKS_KEPT:]

    async def _undo_pick(self) -> None:
        if self._picks:
            self.start_url, self.follow, self.links, self.next, self.proposals = self._picks.pop()
            await self._update()

    async def _remove(self, message: dict) -> None:
        """Removes the file links, the next page's link or a level of links to follow; the levels
        after a removed one move up, and what was picked on their pages with them."""
        target = message.get("target")
        level = int(message.get("level", 0) or 0)
        if target == "follow" and not 0 <= level < len(self.follow):
            return
        self._remember()
        if target == "links":
            self.links = {}
            self.proposals.pop("links", None)
        elif target == "next":
            self.next = {}
            self.proposals.pop("next", None)
        elif target == "follow":
            del self.follow[level]
            proposals = {}
            for key, proposal in self.proposals.items():
                if key.startswith("follow"):
                    number = int(key.removeprefix("follow"))
                    if number == level:
                        continue
                    key = f"follow{number - 1 if number > level else number}"
                if int(proposal.get("level", 0) or 0) > level:
                    proposal = proposal | {"level": proposal["level"] - 1}
                proposals[key] = proposal
            self.proposals = proposals
            if self.next and int(self.next.get("level", 0)) > level:
                self.next["level"] = int(self.next["level"]) - 1
        await self._update()

    async def _edit_selector(self, message: dict) -> None:
        """Takes the person's selector for the file links, a level's links or the next page."""
        selector = str(message.get("selector", "")).strip()
        target = message.get("target")
        level = int(message.get("level", 0) or 0)
        if not selector:
            return
        # The alternatives found the picked links: another selector is the person's own.
        if target == "next" and self.next:
            self._remember()
            self.next["selector"] = selector
            self.next.pop("alternatives", None)
            await self._update()
        elif target == "links" and self.links:
            self._remember()
            self.links["selector"] = selector
            self.links.pop("alternatives", None)
            await self._describe("links")
        elif target == "follow" and 0 <= level < len(self.follow):
            self._remember()
            self.follow[level]["selector"] = selector
            self.follow[level].pop("alternatives", None)
            await self._describe(f"follow{level}")

    async def _next_kind(self, kind: object) -> None:
        """The next page as picked: its link alone, or the numbered pages proposed with it."""
        proposal = self.proposals.get("next") or {}
        chosen = proposal.get("number") if kind == "number" else proposal.get("link")
        if chosen:
            self._remember()
            self.next = dict(chosen)
            await self._update()

    def load(self, recipe: object) -> None:
        """Takes the recipe the form holds (an earlier recording, a template): its steps, to edit
        or record more after, and its picks, to change, remove or keep."""
        if not isinstance(recipe, dict):
            return
        self.steps.load(recipe.get("steps"))
        follow = recipe.get("follow")
        if isinstance(follow, list) and all(isinstance(spec, dict) and spec.get("selector") for spec in follow):
            self.follow = copy.deepcopy(follow)
        for name in ("links", "next"):
            spec = recipe.get(name)
            # A JSON listing has no element: its address and fields (the answer is chosen anew).
            if isinstance(spec, dict) and (spec.get("selector") or (name == "links" and spec.get("kind") == "json")):
                setattr(self, name, copy.deepcopy(spec))
        if isinstance(recipe.get("start"), str):
            self.start_url = recipe["start"]

    async def _navigate(self, kind: str) -> None:
        """Back, Forward or Reload; in the mode record, the page arrived at is a step "goto"."""
        page = self._page
        assert page is not None
        before = page.url
        self.steps.navigating = None
        if kind == "reload":
            await page.reload(wait_until="domcontentloaded")
            return
        # A disguised page (Patchright's) never tells the loads of a page restored from the
        # back-forward cache: there, the page arriving is enough.
        arrived = "commit" if disguised(page) else "domcontentloaded"
        if kind == "back":
            await page.go_back(wait_until=arrived)
        else:
            await page.go_forward(wait_until=arrived)
        if self.mode == "record" and page.url != before and allowed_url(page.url):
            self._record({"action": "goto", "url": page.url})
            await self._update()

    async def _scroll(self) -> None:
        """Scrolls to the page's end until no more comes, as the step does (site.scroll_to_load,
        in a thread with the page handed over); in the mode record, it is the step."""
        assert self._page is not None
        if self.mode == "record":
            self._record({"action": "scroll"})
        page = trial.Blocking(self._page, asyncio.get_running_loop(), threading.Event(), time.monotonic() + 90)
        await asyncio.to_thread(site.scroll_to_load, page, SCROLL_ROUNDS)
        await self._update()

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
            blocking = trial.Blocking(context, asyncio.get_running_loop(), stop, time.monotonic() + trial.TRIAL_SECONDS)
            await self.send({"type": "test-started", "as_job": as_job})
            # The test's requests over HTTP as the browser's (user agent, connection); its steps told.
            # The test's steps that ask the person, on the test's desk.
            via = (browser.user_agent(self._browser.version), browser.PROXY if self._home else "", self.send)
            result = await asyncio.to_thread(trial.survey, source, blocking, recipe, session, as_job, *via, self._desk)  # type: ignore[arg-type]
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

    async def handle(self, message: dict) -> None:  # pylint: disable=too-many-branches
        """Does what the page asks for."""
        page = self._page
        assert page is not None
        kind = message.get("type")
        if kind in INPUT and self._trial and not self._trial.done():
            return  # the test's page: the person watches it
        if kind in INPUT and page.is_closed():
            return  # a popup that closed itself: its opener comes back (_closed)
        if kind == "goto":
            url = str(message.get("url", "")).strip()
            if "://" not in url:
                url = "https://" + url
            # The name's lookup off the event loop, as in _guard.
            if not await asyncio.to_thread(allowed_url, url):
                await self.send({"type": "error", "message": f"The browser may not open {url}."})
                return
            if self.mode == "record":
                self._record({"action": "goto", "url": url})
            await page.goto(url, wait_until="domcontentloaded")
            await self._update()
        elif kind == "mouse":
            await self._mouse(page, message)
        elif kind == "wheel":
            await page.mouse.wheel(float(message.get("dx", 0)), float(message.get("dy", 0)))
        elif kind == "key":
            await page.keyboard.press(str(message["key"]))
        elif kind == "text":
            # Typed text with key events, as sites that check fields while typing expect; a long
            # paste in one go. Characters outside a US keyboard arrive as input either way.
            text = str(message["text"])
            if len(text) <= TYPED:
                await page.keyboard.type(text)
            else:
                await page.keyboard.insert_text(text)
        elif kind == "mode" and message.get("mode") in MODES:
            self.mode = message["mode"]
            level = message.get("level")
            self.repick = int(level) if self.mode == "follow" and isinstance(level, int) else None
            self.value_for = self._value_target(message) if self.mode == "value" else None
            await self._apply_mode()
        elif kind == "pattern":
            await self._edit_pattern(message)
        elif kind in ("click", "save"):
            # A pick clicked or not, the file links' pages saved or not (linkpicks.py).
            await (self._set_click if kind == "click" else self._set_save)(message)
        elif kind in EDITS:
            if self.steps.edit(kind, message):
                await self._update()
        elif kind in ("back", "forward", "reload"):
            await self._navigate(kind)
        elif kind in DATA_MESSAGES:
            await self.handle_data(kind, message)
        elif kind in jar.COMMANDS and self._context is not None:
            # The cookies panel: the recorder's own context (never the recipe's test's).
            for answer in await jar.handle(self._context, page.url, message):
                await self.send(answer)
        else:
            await self._handle_picks(kind, message)

    async def _handle_picks(self, kind: object, message: dict) -> None:
        """The page's messages about the picks and the recipe."""
        page = self._page
        assert page is not None
        if kind == "undo-pick":
            await self._undo_pick()
        elif kind == "remove":
            await self._remove(message)
        elif kind == "selector":
            await self._edit_selector(message)
        elif kind == "next-kind":
            await self._next_kind(message.get("kind"))
        elif kind == "load":
            self.load(message.get("recipe"))
            # A password a recipe of before the recorder knew it holds: said, never shown.
            for warning in leaked_passwords(message.get("recipe")):
                LOGGER.warning("a recipe opened in the recorder: %s", warning)
                await self.send({"type": "warning", "message": in_this_recipe(warning)})
            await self._update()
        elif kind == "scroll":
            await self._scroll()
        elif kind == "test":
            # The page's own test: the recorder's session (as the job runs only with the account
            # the app adds, test()).
            await self.test(message)
        elif kind in ("test-stop", "test-code"):
            await self._trial_message(kind, message)
        elif kind == "viewport":
            await self.resize(message.get("width"), message.get("height"))
        elif kind == "clear":
            self._remember()
            self.start_url, self.follow, self.links, self.next, self.proposals = "", [], {}, {}, {}
            await page.evaluate(HIGHLIGHT, ["", None, None], **main_world(page))
            await self._update()

    async def _trial_message(self, kind: object, message: dict) -> None:
        """The test's stop, or the code one of its steps asks the person for."""
        if kind == "test-code":
            self._desk.answer(0, str(message.get("code") or "").strip()[: needs.CODE_MAX])
        elif self._trial_stop:
            # The call under way ends with its pages (it may wait for a step's whole time else).
            self._trial_stop.set()
            if self._trial_context:
                await self._trial_context.close()

    async def _edit_pattern(self, message: dict) -> None:
        """Takes the person's pattern for a level and shows what it matches."""
        pattern = str(message.get("pattern", ""))
        self._remember()
        if message.get("target") == "links" and self.links:
            self.links["pattern"] = pattern
            await self._describe("links")
        elif message.get("target") == "follow":
            level = int(message.get("level", 0))
            if 0 <= level < len(self.follow):
                self.follow[level]["pattern"] = pattern
                await self._describe(f"follow{level}")
