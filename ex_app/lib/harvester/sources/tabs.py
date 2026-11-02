"""Part of the site source's replay (harvester.sources.site): how long a step settles, what a
replayed step waits for (arrived), and the tabs the steps act in (Tabs): the first, and a
sign-in's popup a step went on in.
"""

import re
import time

from playwright.sync_api import Error as PlaywrightError

from .base import SourceError

# How long the network may take to go quiet after a step that waited for its address and element
# (ms): they are there already, the rest is what a script still loads.
SHORT_IDLE = 2_000
# How often a step looks again for the popup it goes on in, or for it to close (ms).
POLL = 250


class PopupMissing(SourceError):
    """The popup a step goes on in did not open, or the one it goes back from did not close."""


def settle(page, idle: float = 15_000) -> None:
    """Waits for what a step started: a new page, or the requests of a script (the network quiet,
    idle ms at most)."""
    page.wait_for_timeout(500)
    try:
        page.wait_for_load_state("networkidle", timeout=idle)
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # a page that never goes quiet (ads, polling) is used as it is


def arrived(page, step: dict, timeout: float) -> None:
    """Waits for where the step leads: the address and the element it recorded (recipe: "wait"),
    then a short settle; without them, for the network to go quiet (as recipes always did)."""
    wait = step.get("wait") if isinstance(step.get("wait"), dict) else {}
    try:
        if not wait.get("url") and not wait.get("selector"):
            settle(page)
            return
        if wait.get("url"):
            page.wait_for_url(re.compile(wait["url"]), wait_until="domcontentloaded", timeout=timeout)
        if wait.get("selector"):
            page.wait_for_selector(wait["selector"], state="visible", timeout=timeout)
        settle(page, SHORT_IDLE)
    except PlaywrightError:
        # A step that closed its own page (a popup's last click) has nothing to wait for there.
        if not page.is_closed():
            raise


class Tabs:
    """The tabs the steps act in: the first, and each popup a step went on in (the newest acts),
    back to the one that opened it after a step "opener"."""

    def __init__(self, page):
        self.pages = [page]
        self.known = 0

    @property
    def page(self):
        """The tab the steps act in now."""
        return self.pages[-1]

    def opening(self) -> None:
        """Before the step that opens a popup: the tabs open until then."""
        self.known = len(self.pages[-1].context.pages)

    def switch(self, step: dict, timeout: float) -> bool:
        """Switches the tab for a step "popup" or "opener", within timeout (ms); whether it was one
        (any other step is the replay's, in self.page)."""
        if step.get("action") not in ("popup", "opener"):
            return False
        page = self.pages[-1]
        deadline = time.monotonic() + timeout / 1000
        if step.get("action") == "popup":
            while len(opened := page.context.pages) <= self.known:
                if time.monotonic() >= deadline:
                    raise PopupMissing("no popup opened")
                page.wait_for_timeout(POLL)
            popup = opened[-1]
            popup.wait_for_load_state("domcontentloaded")
            self.pages.append(popup)
        elif step.get("action") == "opener":
            # The popup closes itself (a sign-in's, once done): the page that opened it goes on.
            while len(self.pages) > 1 and not page.is_closed():
                if time.monotonic() >= deadline:
                    raise PopupMissing("the popup did not close")
                self.pages[-2].wait_for_timeout(POLL)
            if len(self.pages) > 1:
                self.pages.pop()
            settle(self.pages[-1])
        return True
