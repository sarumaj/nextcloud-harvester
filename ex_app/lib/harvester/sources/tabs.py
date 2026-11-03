# SPDX-License-Identifier: AGPL-3.0-or-later
"""Part of the site source's replay (harvester.sources.site): how long a step settles, what a
replayed step waits for (arrived), the tabs the steps act in (Tabs): the first, and a sign-in's
popup a step went on in; the frame a step acts in (in_frame); a step's pause and its tries
(with_tries), and the events the replay tells of each step (step_event).
"""

import re
import time
from collections.abc import Callable
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .. import recipe as recipes
from .base import SourceError

# How long the network may take to go quiet after a step that waited for its address and element
# (ms): they are there already, the rest is what a script still loads.
SHORT_IDLE = 2_000
# How often a step looks again for the popup it goes on in, or for it to close (ms).
POLL = 250
# How often a step looks again for the frame it acts in while the page builds it (ms).
FRAME_POLL = 250


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
    """The progress log's line of a step's pause or retry (step_event), "" for its other
    events."""
    if event["state"] == "pausing":
        return f"step {event['number']} ({event['label']}): pausing {event['seconds']:g} s before it"
    if event["state"] == "retrying":
        return (
            f"step {event['number']} ({event['label']}) failed, trying again in {event['seconds']:g} s "
            f"(attempt {event['attempt']} of {event['of']}): {event['why']}"
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
