# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's tabs: a tab the site opens (a sign-in's popup) is shown, and while recording a
step "popup"; when it closes, the page that opened it comes back, a step "opener". And
recorder.js in each document of the recorder's own pages, which the context's script misses in a
popup at its opener's origin. The person's mouse on the tab shown, which a click may close.
"""

import asyncio
import weakref
from collections.abc import Awaitable, Callable

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from . import SCRIPT
from .engine import main_world


class Popups:  # pylint: disable=too-few-public-methods
    """The recorder's handling of its tabs (mixed into Recorder, whose state and methods these
    are)."""

    send: Callable[[dict], Awaitable[None]]
    mode: str
    _page: Page | None
    _context: BrowserContext | None
    _openers: weakref.WeakKeyDictionary
    _popups: weakref.WeakSet

    def _record(self, event: dict) -> None:
        """Records a step's event (the recorder's)."""

    async def _update(self) -> None:
        """Sends the recipe to the page (the recorder's)."""

    async def _show(self, page: Page) -> None:
        """Shows the page on the screen (the recorder's)."""

    async def _opened(self, page: Page) -> None:
        """A tab of the recorder's (its first, or one the site opened): shown. One the page shown
        opened (a popup) is a step "popup" while recording; when it closes, its opener is shown
        again (_closed)."""
        opener = await page.opener()
        if opener is not None:
            self._openers[page] = opener
            page.on("close", lambda _: asyncio.ensure_future(self._closed(page)))
            if self.mode == "record" and opener is self._page:
                self._popups.add(page)
                self._record({"action": "popup"})
                await self._update()
        await self._show(page)
        # Loaded already, maybe, before _show listened.
        await self._install(page)

    async def _closed(self, page: Page) -> None:
        """A popup closed (a sign-in's, once done): the page that opened it is shown again (else the
        newest open), and while recording that is a step "opener"."""
        opener = self._openers.pop(page, None)
        if page in self._popups and self.mode == "record":
            self._record({"action": "opener"})
            await self._update()
        if self._page is not page or self._context is None:
            return  # the recipe's test is shown, or another tab
        pages = [other for other in self._context.pages if not other.is_closed()]
        back = opener if opener is not None and not opener.is_closed() else (pages[-1] if pages else None)
        if back is not None:
            await self._show(back)
            await self.send({"type": "url", "url": back.url})

    async def _install(self, page: Page) -> None:
        """recorder.js in the recorder's own page, if a document of it lacks it. A popup opened at
        an address of its opener's origin gets the context's scripts in its first, empty document
        only (Chromium keeps that window for the page it loads); recorder.js checks itself that it
        runs once per document."""
        if page.context is not self._context:
            return  # the recipe's test's: nothing it does is recorded
        for frame in page.frames:
            try:
                await frame.evaluate(SCRIPT, **main_world(frame))
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # navigated meanwhile: its next document loads again

    @staticmethod
    async def _mouse(page: Page, message: dict) -> None:
        """The person's mouse on the page: a move, a button down or up."""
        try:
            await page.mouse.move(float(message["x"]), float(message["y"]))
            if message.get("event") in ("down", "up"):
                button = message.get("button", "left")
                await getattr(page.mouse, message["event"])(button=button if button in ("left", "right") else "left")
        except PlaywrightError:
            # A click that closed its page (a popup's last button): its opener is shown now.
            if not page.is_closed():
                raise
