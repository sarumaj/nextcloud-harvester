# SPDX-License-Identifier: AGPL-3.0-or-later
"""Part of the site source's replay (harvester.sources.site): how long a step settles, what a
replayed step waits for (arrived) and a listing page for its elements (listing_shown), the tabs
the steps act in (Tabs): the first, and a sign-in's popup a step went on in; the frame a step
acts in (in_frame); a step's pause and its tries (with_tries), and the events the replay tells
of each step (step_event); a page that loads more as it is scrolled (scroll_to_load) or with a
button (load_more), each round until it grew (grown); and a page that prepares a file first,
polled (and reloaded) until its element shows (wait_until_shown).
"""

import re
import time
from collections.abc import Callable
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .. import recipe as recipes
from ..l10n import said
from .base import SourceError, check_stop

# How long the network may take to go quiet after a step that waited for its address and element
# (ms): they are there already, the rest is what a script still loads.
SHORT_IDLE = 2_000
# How often a step looks again for the popup it goes on in, or for it to close (ms).
POLL = 250
# How often a step looks again for the frame it acts in while the page builds it (ms).
FRAME_POLL = 250
# A scroll step ends once the page stopped growing for this many rounds in a row, or after
# SCROLL_SECONDS (a feed that never ends).
SCROLL_STILL = 2
SCROLL_SECONDS = 60
# The page's size as a scroll step compares it: its links, its elements and its height.
PAGE_SIZE = (
    "() => [document.links.length, document.getElementsByTagName('*').length,"
    " (document.scrollingElement || document.body).scrollHeight]"
)
# How long a scroll or a Load more button may take to add to the page before the round counts
# as one that loaded nothing (ms), and how often the page is looked at meanwhile (ms).
GROW_WAIT = 3_000
GROW_POLL = 100
# How often a page that prepares a file is looked at for its element (ms).
WAIT_POLL = 1_000
# How long a listing page may take to show its level's elements (or a click to change them)
# before the walk waits for the network to go quiet instead, as long as settle would (ms), and
# how often it looks for them meanwhile (ms).
LISTING_WAIT = 5_000
LISTING_POLL = 100
# What a level's elements are on the page: their addresses and texts, which the page's next
# page changes.
ELEMENTS = "els => els.map(e => (e.getAttribute('href') || '') + '\\t' + (e.textContent || '').trim()).join('\\n')"
# How long settle waits by default for the network to go quiet (ms), after a pause for what a
# click starts (ms).
IDLE = 15_000
CLICK_PAUSE = 500
SCROLL_DOWN = "() => window.scrollTo(0, (document.scrollingElement || document.body).scrollHeight)"


class PopupMissing(SourceError):
    """The popup a step goes on in did not open, or the one it goes back from did not close."""


class FrameMissing(SourceError):
    """The frame a step acts in is not on the page."""


# How a step fails that is worth trying again: its element, frame, popup or wait not there in time
# (never a stop, nor a page that asks for a person).
TRIED_AGAIN = (PlaywrightError, FrameMissing, PopupMissing)


def short_error(error: Exception) -> str:
    """An error's first line without Playwright's call ("Locator.click: "), for a log line."""
    text = (str(error) or type(error).__name__).splitlines()[0]
    return re.sub(r"^[A-Za-z]\w*\.\w+: ", "", text)


def step_event(number: int, step: dict, state: str, **more: Any) -> dict:
    """A step's event as the replay tells it: its number (from 1), its label and its state:
    "session" (skipped, the kept session reaches the listing), "pausing" (seconds), "playing"
    (attempt of of), "retrying" (the attempt next, of, after seconds, why the last failed),
    "passed", "skipped" (optional, why) or "failed" (why)."""
    return {"number": number, "label": recipes.step_label(step), "state": state, **more}


def step_line(event: dict) -> str:
    """The progress log's line of a step's pause or retry (step_event), a said() text, "" for
    its other events."""
    if event["state"] == "pausing":
        return said(
            "step {number} ({label}): pausing {seconds} s before it",
            number=event["number"],
            label=event["label"],
            seconds=f"{event['seconds']:g}",
        )
    if event["state"] == "retrying":
        return said(
            "step {number} ({label}) failed, trying again in {seconds} s (attempt {attempt} of {of}): {why}",
            number=event["number"],
            label=event["label"],
            seconds=f"{event['seconds']:g}",
            attempt=event["attempt"],
            of=event["of"],
            why=str(event["why"]),
        )
    return ""


def with_tries(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    play: Callable[[], None],
    number: int,
    step: dict,
    nap: Callable[[float], None],
    tell: Callable[[dict], None],
) -> None:
    """The step's pause (recipe: "pause"), then play, and again after its retry's delay while it
    fails as a step fails (TRIED_AGAIN) and tries are left; the last failure is raised. A stop
    (Stopped) is never tried again; nap's waits end at once on one."""
    pause = recipes.step_pause(step)
    if pause:
        tell(step_event(number, step, "pausing", seconds=pause))
        nap(pause)
    times, delay = recipes.step_retry(step)
    for attempt in range(1, times + 2):
        tell(step_event(number, step, "playing", attempt=attempt, of=times + 1))
        try:
            play()
            return
        except TRIED_AGAIN as error:
            if attempt > times:
                raise
            why = short_error(error)
            tell(step_event(number, step, "retrying", attempt=attempt + 1, of=times + 1, seconds=delay, why=why))
            nap(delay)


def _child_frame(parent, entry: dict):
    """The frame of the path's entry inside parent: by its element's selector, else by its address
    (without the query), else by its name; None while there is none."""
    if entry.get("selector"):
        try:
            owner = parent.query_selector(entry["selector"])
        except PlaywrightError:
            owner = None  # a selector the page cannot read: the address may still find it
        found = owner.content_frame() if owner else None
        if found is not None:
            return found
    children = parent.child_frames
    for key, value in (("url", entry.get("url")), ("name", entry.get("name"))):
        if not value:
            continue
        for child in children:
            if (recipes.frame_address(child.url) if key == "url" else child.name) == value:
                return child
    return None


def in_frame(page, frames: list[dict], timeout: float):
    """The frame the step's path leads to (recipe: "frame"), waiting up to timeout (ms) for the
    page to build it; FrameMissing when it does not."""
    deadline = time.monotonic() + timeout / 1000
    target = page.main_frame
    for depth, entry in enumerate(frames, 1):
        while (found := _child_frame(target, entry)) is None:
            if time.monotonic() >= deadline:
                where = entry.get("url") or entry.get("name") or entry.get("selector")
                raise FrameMissing(f"the frame {where} (level {depth}) is not on the page")
            page.wait_for_timeout(FRAME_POLL)
        target = found
    return target


def settle(page, idle: float = IDLE, pause: float = CLICK_PAUSE) -> None:
    """Waits for what a step started: a new page, or the requests of a script (the network quiet,
    idle ms at most), after pause ms for a navigation to begin (none after a goto, which waited
    for its document)."""
    if pause:
        page.wait_for_timeout(pause)
    try:
        page.wait_for_load_state("networkidle", timeout=idle)
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # a page that never goes quiet (ads, polling) is used as it is


def found_elements(page, selectors: list[str] | tuple[str, ...]) -> str | None:
    """What the selectors (a level's and its alternatives) find on the page, as one text ("" when
    none finds anything); None while the page cannot be read (between two documents)."""
    found = []
    for selector in selectors:
        try:
            found.append(page.eval_on_selector_all(selector, ELEMENTS))
        except PlaywrightError:
            return None
    return "\n\n".join(found) if any(found) else ""


def listing_shown(page, selectors: list[str] | tuple[str, ...], before: str | None = None) -> None:
    """Waits for a listing page (opened, or changed by a click) to show its level's elements:
    until one of the selectors finds any (after a click on the page itself: until what they find
    is no longer before), then for the network to go quiet SHORT_IDLE ms at most. Without
    selectors, or when nothing shows within LISTING_WAIT, for the network to go quiet as long as
    settle does in all (a page that builds its listing slowly, a sign-in page)."""
    started = time.monotonic()
    if selectors:
        while (time.monotonic() - started) * 1000 < LISTING_WAIT:
            found = found_elements(page, selectors)
            if found and found != before:
                settle(page, SHORT_IDLE, pause=0)
                return
            page.wait_for_timeout(LISTING_POLL)
    settle(page, max(SHORT_IDLE, IDLE - (time.monotonic() - started) * 1000), pause=0)


def grown(page, before: Any) -> bool:
    """Whether the page grew past its size before (PAGE_SIZE) within GROW_WAIT ms: once it did,
    until it stops growing for a moment (what a scroll loads comes in a batch). A page whose
    network went quiet once (Playwright's "networkidle", which stays reached) and that did not grow
    within CLICK_PAUSE ms grew not, as settle took it before; one that never goes quiet (analytics,
    polling) is given GROW_WAIT."""
    started = time.monotonic()
    size = before
    while (elapsed := (time.monotonic() - started) * 1000) < GROW_WAIT:
        page.wait_for_timeout(GROW_POLL)
        try:
            now = page.evaluate(PAGE_SIZE)
        except PlaywrightError:
            continue  # between two documents (a button that loads a new page)
        if now == size and size != before:
            return True
        size = now
        if size == before and elapsed >= CLICK_PAUSE and _quiet(page):
            return False
    return size != before


def _quiet(page) -> bool:
    """Whether the page's network went quiet once ("networkidle" reached)."""
    try:
        page.wait_for_load_state("networkidle", timeout=1)
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


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


def load_more(page, selector: str) -> None:
    """Clicks the page's Load more button until it is gone or hidden, or the page stops growing
    (as scroll_to_load), at most recipe.LOAD_MORE_MAX times or SCROLL_SECONDS."""
    deadline = time.monotonic() + SCROLL_SECONDS
    still = 0
    for _ in range(recipes.LOAD_MORE_MAX):
        button = page.query_selector(selector)
        if not button or not button.is_visible():
            return
        before = page.evaluate(PAGE_SIZE)
        try:
            button.click()
        except PlaywrightError:
            return  # it went away or was covered as the page grew: what loaded is read
        still = 0 if grown(page, before) else still + 1
        if still >= SCROLL_STILL or time.monotonic() > deadline:
            return


def scroll_for_more(page, selectors: tuple[str, ...]) -> bool:
    """Scrolls to the page's end until more of what the selectors find shows (a listing's next
    batch; without selectors, until the page grows), SCROLL_STILL rounds at most of GROW_WAIT
    each; whether more came."""
    before = found_elements(page, selectors) if selectors else None
    for _ in range(SCROLL_STILL):
        size = page.evaluate(PAGE_SIZE)
        page.evaluate(SCROLL_DOWN)
        if grown(page, size) and (not selectors or found_elements(page, selectors) not in (before, None)):
            return True
    return False


def scroll_to_load(page, rounds: int) -> None:
    """Scrolls to the page's end until it stops growing (no new links, elements or height for
    SCROLL_STILL rounds of GROW_WAIT each), at most rounds times or SCROLL_SECONDS."""
    deadline = time.monotonic() + SCROLL_SECONDS
    still = 0
    for _ in range(rounds):
        before = page.evaluate(PAGE_SIZE)
        page.evaluate(SCROLL_DOWN)
        still = 0 if grown(page, before) else still + 1
        if still >= SCROLL_STILL or time.monotonic() > deadline:
            return


def _shown(page, selector: str) -> bool:
    """Whether the selector's first element is on the page and visible."""
    try:
        found = page.query_selector(selector)
        return bool(found and found.is_visible())
    except PlaywrightError:
        return False  # between two documents (a reload): looked for again


def wait_until_shown(page, wait: dict) -> None:
    """Waits for an element the page shows once a file is ready (recipe: "wait_for": an export's
    link, a status that changed, a list's entry that turned ready): the page looked at every
    WAIT_POLL ms and, with "reload" (seconds), reloaded that often, until "timeout" seconds
    (recipe.WAIT_FOR_TIMEOUT without); SourceError when it never shows. A stop ends it at once."""
    selector = str(wait["selector"])
    timeout = float(wait.get("timeout") or recipes.WAIT_FOR_TIMEOUT)
    every = float(wait.get("reload") or 0)
    started = reloaded = time.monotonic()
    while not _shown(page, selector):
        check_stop()
        now = time.monotonic()
        if now - started >= timeout:
            raise SourceError(f"{selector} did not show within {timeout:g} s (the file not ready yet?)")
        if every and now - reloaded >= every:
            page.reload(wait_until="domcontentloaded")
            settle(page, SHORT_IDLE)
            reloaded = time.monotonic()
            continue
        page.wait_for_timeout(min(WAIT_POLL, (timeout - (now - started)) * 1000))
