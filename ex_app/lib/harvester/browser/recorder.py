"""The recorder: a browser in the app that a person drives from the Harvester page.

The page shows the browser's screen (Chromium's screencast, JPEG frames) and sends the
person's mouse, keys and commands; recorder.js in the browser reports the steps and picks.
For each pick of links the recorder proposes a pattern with named groups
(harvester.patterns.propose), outlines in the page the links it matches (solid) and those it
leaves out (dashed), and shows examples with their values; the person may edit the pattern.

The messages are JSON over the page's WebSocket:

- to the page: {type: frame, data}, {type: url, url}, {type: error, message},
  {type: recipe, recipe, proposals};
- from the page: {type: goto, url}, {type: mouse, event (move, down, up), x, y, button},
  {type: wheel, dx, dy}, {type: key, key}, {type: text, text}, {type: mode, mode},
  {type: pattern, target (follow, links), level, pattern}, {type: undo}, {type: back},
  {type: clear}.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable

from playwright.async_api import Browser, BrowserContext, CDPSession, Page, Playwright, Route, async_playwright

from .. import patterns
from ..recipe import steps_from_events
from . import LAUNCH_ARGS, SCRIPT, VIEWPORT, allowed_url

# record: steps; browse: go somewhere unrecorded; follow, links, next: pick links.
MODES = ("record", "browse", "follow", "links", "next")
EXAMPLES = 8
# Seconds closing the browser may take.
CLOSE_TIMEOUT = 10
HIGHLIGHT = "([selector, pattern]) => window.__harvester ? window.__harvester.highlight(selector, pattern) : null"
MATCHES = "els => els.map(e => [e.href || '', (e.innerText || '').trim().slice(0, 80)])"


class Recorder:  # pylint: disable=too-many-instance-attributes
    """One recording: a browser, the page shown and the recipe so far."""

    def __init__(self, send: Callable[[dict], Awaitable[None]]):
        self.send = send
        self.events: list[dict] = []
        self.start_url = ""
        self.follow: list[dict] = []
        self.links: dict = {}
        self.next: dict = {}
        # Per picked level ("follow0", "links"): groups, examples, proposed file name and tags.
        self.proposals: dict[str, dict] = {}
        self.mode = "record"
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp: CDPSession | None = None

    @property
    def recipe(self) -> dict:
        """The recipe so far."""
        recipe: dict = {"steps": steps_from_events(self.events), "start": self.start_url, "follow": self.follow}
        recipe["links"] = self.links
        if self.next:
            recipe["next"] = self.next
        return recipe

    async def start(self, session: dict | None = None, url: str = "") -> None:
        """Starts the browser with an empty page, or with a job's session at a page of it (the one
        that asked for a person)."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(args=LAUNCH_ARGS)
        self._context = await self._browser.new_context(
            viewport=VIEWPORT, accept_downloads=False, storage_state=session or None  # type: ignore[arg-type]
        )
        await self._context.route("**/*", self._guard)
        await self._context.expose_binding("__harvesterReport", self._report)
        await self._context.add_init_script(SCRIPT)
        self._context.on("page", lambda page: asyncio.ensure_future(self._show(page)))
        await self._show(await self._context.new_page())
        if url and allowed_url(url) and self._page:
            await self._page.goto(url, wait_until="domcontentloaded")

    async def session(self) -> dict:
        """The browser's session now: cookies and local storage, for the job to continue with."""
        assert self._context is not None
        return await self._context.storage_state()  # type: ignore[return-value]

    async def close(self) -> None:
        """Closes the browser, within seconds even when a page or a send is stuck."""
        for closing in (self._browser and self._browser.close(), self._playwright and self._playwright.stop()):
            if closing:
                try:
                    await asyncio.wait_for(closing, CLOSE_TIMEOUT)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass  # stopping Playwright ends its browser in any case

    @staticmethod
    async def _guard(route: Route) -> None:
        if allowed_url(route.request.url):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def _show(self, page: Page) -> None:
        """Shows this page (the first, or a new tab or window the site opened)."""
        if self._cdp:
            try:
                await self._cdp.send("Page.stopScreencast")
                await self._cdp.detach()
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # the page is gone
        self._page = page
        page.on("domcontentloaded", lambda _: asyncio.ensure_future(self._apply_mode()))
        page.on("framenavigated", lambda frame: asyncio.ensure_future(self._navigated(frame)))
        self._cdp = await page.context.new_cdp_session(page)
        self._cdp.on("Page.screencastFrame", lambda frame: asyncio.ensure_future(self._frame(frame)))
        await self._cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 60, "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"]},
        )

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
            await self.send({"type": "url", "url": frame.url})

    async def _apply_mode(self) -> None:
        if self._page:
            try:
                await self._page.evaluate("mode => window.__harvester && (window.__harvester.mode = mode)", self.mode)
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # navigated meanwhile

    async def _update(self) -> None:
        await self.send({"type": "recipe", "recipe": self.recipe, "proposals": self.proposals})

    def depth(self, url: str) -> int:
        """How deep the page is: 0 for the listing, n for a page the nth level's links lead to."""
        for level in range(len(self.follow), 0, -1):
            pattern = self.follow[level - 1].get("pattern", "")
            try:
                if re.search(pattern, url):
                    return level
            except re.error:
                continue
        return 0

    async def _report(self, _source, event: dict) -> None:
        if event.get("action") != "pick":
            if self.mode == "record":
                self.events.append(event)
                await self._update()
            return
        assert self._page is not None
        url, target = self._page.url, event.get("target")
        level = self.depth(url)
        if target == "next":
            self.next = {"selector": event["selector"], "level": level}
            await self._update()
            return
        if level == 0:
            self.start_url = url
        proposal = patterns.propose([href for href, _ in event.get("matches", [])])
        spec = {"selector": event["selector"], "pattern": proposal["pattern"]}
        if target == "follow":
            self.follow = self.follow[:level] + [spec]
            self.proposals = {key: value for key, value in self.proposals.items() if key == "links"}
            key = f"follow{level}"
        else:
            self.links = spec
            key = "links"
        self.proposals[key] = {**proposal, "level": level}
        await self._describe(key)

    async def _describe(self, key: str) -> None:
        """Outlines what the level's pattern matches on the page and lists examples with their values."""
        spec = self.links if key == "links" else self.follow[int(key.removeprefix("follow"))]
        page = self._page
        assert page is not None
        proposal = self.proposals.setdefault(key, {})
        try:
            pattern = re.compile(spec.get("pattern", ""))
        except re.error as error:
            proposal.update(error=f"not a regular expression: {error}", examples=[], matching=0)
            await self._update()
            return
        found = await page.eval_on_selector_all(spec["selector"], MATCHES)
        examples = []
        for href, text in found:
            match = pattern.search(href) if href else None
            if match and len(examples) < EXAMPLES:
                examples.append({"url": href, "text": text, "values": patterns.group_values(match)})
        matching = sum(1 for href, _ in found if href and pattern.search(href))
        proposal.update(error="", examples=examples, matching=matching, found=len(found), pattern=pattern.pattern)
        proposal["groups"] = list(pattern.groupindex)
        try:
            await page.evaluate(HIGHLIGHT, [spec["selector"], spec.get("pattern", "")])
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # a pattern JavaScript reads otherwise only outlines nothing
        await self._update()

    async def handle(self, message: dict) -> None:  # pylint: disable=too-many-branches
        """Does what the page asks for."""
        page = self._page
        assert page is not None
        kind = message.get("type")
        if kind == "goto":
            url = str(message.get("url", "")).strip()
            if "://" not in url:
                url = "https://" + url
            if not allowed_url(url):
                await self.send({"type": "error", "message": f"The browser may not open {url}."})
                return
            if self.mode == "record":
                self.events.append({"action": "goto", "url": url})
            await page.goto(url, wait_until="domcontentloaded")
            await self._update()
        elif kind == "mouse":
            await page.mouse.move(float(message["x"]), float(message["y"]))
            if message.get("event") in ("down", "up"):
                button = message.get("button", "left")
                await getattr(page.mouse, message["event"])(button=button if button in ("left", "right") else "left")
        elif kind == "wheel":
            await page.mouse.wheel(float(message.get("dx", 0)), float(message.get("dy", 0)))
        elif kind == "key":
            await page.keyboard.press(str(message["key"]))
        elif kind == "text":
            await page.keyboard.insert_text(str(message["text"]))
        elif kind == "mode" and message.get("mode") in MODES:
            self.mode = message["mode"]
            await self._apply_mode()
        elif kind == "pattern":
            await self._edit_pattern(message)
        elif kind == "undo" and self.events:
            self.events.pop()
            await self._update()
        elif kind == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif kind == "clear":
            self.start_url, self.follow, self.links, self.next, self.proposals = "", [], {}, {}, {}
            await page.evaluate(HIGHLIGHT, ["", ""])
            await self._update()

    async def _edit_pattern(self, message: dict) -> None:
        """Takes the person's pattern for a level and shows what it matches."""
        pattern = str(message.get("pattern", ""))
        if message.get("target") == "links" and self.links:
            self.links["pattern"] = pattern
            await self._describe("links")
        elif message.get("target") == "follow":
            level = int(message.get("level", 0))
            if 0 <= level < len(self.follow):
                self.follow[level]["pattern"] = pattern
                await self._describe(f"follow{level}")
