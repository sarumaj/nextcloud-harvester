# SPDX-License-Identifier: AGPL-3.0-or-later
"""Elements without an address that the browser clicks (harvester.sources.site): file links
clicked to download (clicked_file), and a level of links to follow clicked to open each entry
(land, back).

A file link's click may start a download (in its tab, or in a tab or popup it opens), open a tab
or popup that shows the file (a PDF in the viewer, a picture), or take its own tab to the file.
A tab that shows a file gives its address, which is fetched with the browser's cookies as other
file links are (download.fetch: the same checks). A tab that shows a web page is waited for a
while (LANDED_WAIT), as it may start the download itself; else it is no file.

A followed element's click may take its tab elsewhere (a page, or a single-page app's route),
open a tab or popup, or change the page in place (a dialog, a panel): land waits for the first
of these, then for the next level's elements there. back returns to the listing: the tab
closed, else Back or Escape, else the listing opened again at its address with its level's
loading of more (scroll, load_more) done again. Elements are found again by their place and
text (element_at): a listing opened again may hold them elsewhere.

The calls are the Playwright page's own, polled rather than awaited as events, so that the
recipe's test (harvester.browser.trial: each call handed over to the recorder's event loop)
walks the levels as a job does.
"""

import contextlib
import time
from collections.abc import Callable, Iterator
from typing import Any, NamedTuple

from playwright.sync_api import Error as PlaywrightError

from .. import selectors
from .base import SourceError, WebPage
from .tabs import SHORT_IDLE, found_elements, listing_shown, settle

# The elements a click link or level matches: their text (a button's value, an icon's label), in
# the page's order.
TARGETS = (
    "els => els.map(e => (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('title') || '')"
    ".trim())"
)
# How often a click's outcome is looked for (ms).
POLL = 250
# How long a tab that shows a web page after a file link's click may take to start the download
# (s), and how long the next level's elements may take to show after a click landed (ms).
LANDED_WAIT = 10
ARRIVE_WAIT = 5_000
# What a click may change in the page in place: its elements and texts, and what shows or hides
# them (a dialog's open, a panel's hidden or style, a class that opens it).
OBSERVE = """() => {
  window.__harvesterChanged = 0
  if (window.__harvesterObserver) return
  window.__harvesterObserver = new MutationObserver(() => { window.__harvesterChanged++ })
  window.__harvesterObserver.observe(document, {subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['open', 'hidden', 'style', 'class', 'aria-hidden', 'aria-modal']})
}"""
CHANGED = "() => window.__harvesterChanged || 0"
# The answers that are pages rather than files.
PAGE_TYPES = ("text/html", "application/xhtml+xml")


class Target(NamedTuple):
    """An element of a level to follow that is clicked: the listing's page, the level's selector,
    the element's place and text there, the listing's address and level (to open it again), and
    the next level's selector (waited for where the click lands; "" for none)."""

    page: Any
    selector: str
    index: int
    text: str
    address: str
    spec: dict
    wait_for: str


class Landing(NamedTuple):
    """Where a click landed: the page to read (the listing's tab, or a tab or popup it opened),
    how ("tab", "navigated" or "changed"), and before it the listing's texts and how many of the
    next level's elements it had (a dialog's are gone again once it closed)."""

    target: Target
    page: Any
    how: str
    texts: list
    count: int


def element_at(page: Any, selector: str, index: int, text: str) -> Any:
    """The element at its place among the selector's, unless the page changed meanwhile: then
    the one of its text; SourceError when none has it."""
    elements = page.query_selector_all(selector)
    texts = page.eval_on_selector_all(selector, TARGETS)
    if index >= len(texts) or texts[index] != text:
        index = texts.index(text) if text in texts else -1
    if not 0 <= index < len(elements):
        raise SourceError(f"the element to click for “{text}” ({selector}) is gone from the page")
    return elements[index]


def _is_page(content_type: str) -> bool:
    return content_type.split(";")[0].strip().lower() in PAGE_TYPES


@contextlib.contextmanager
def clicked_file(page: Any, element: Any, what: str, timeout: float) -> Iterator[tuple[str, Any]]:
    """Clicks a file link's element and gives what came of it within timeout (ms): ("download",
    the browser's download) or ("address", the address of a tab that shows a file). Afterwards
    the tabs it opened close, and its own tab goes back to the listing. SourceError when nothing
    came; WebPage when only a web page did."""
    context = page.context
    downloads: list = []
    opened: list = []
    # The navigations' answers: address and type.
    shown: dict[str, str] = {}

    def on_download(download: Any) -> None:
        downloads.append(download)

    def on_page(new: Any) -> None:
        opened.append(new)
        new.on("download", on_download)

    def on_response(response: Any) -> None:
        with contextlib.suppress(PlaywrightError):
            if response.request.is_navigation_request():
                shown[response.url] = response.headers.get("content-type", "")

    before = page.url
    page.on("download", on_download)
    context.on("page", on_page)
    context.on("response", on_response)
    try:
        element.click()
        yield _file_came(page, before, (downloads, opened, shown), what, timeout)
    finally:
        page.remove_listener("download", on_download)
        context.remove_listener("page", on_page)
        context.remove_listener("response", on_response)
        for new in opened:
            with contextlib.suppress(PlaywrightError):
                new.close()
        if page.url != before:
            _return_to(page, before)


def _file_came(page: Any, before: str, seen: tuple[list, list, dict], what: str, timeout: float) -> tuple[str, Any]:
    """Waits for the click's download, or for a tab (opened, or its own) that shows a file."""
    downloads, opened, shown = seen
    deadline = time.monotonic() + timeout / 1000
    landed: tuple[float, str] | None = None
    while True:
        if downloads:
            return "download", downloads[0]
        for tab in [*opened, page]:
            if tab.is_closed() or (tab is page and tab.url == before):
                continue
            # A tab still loading (about:blank) has no answer yet.
            kind = shown.get(tab.url)
            if kind is not None and not _is_page(kind):
                return "address", tab.url
            if kind is not None and landed is None:
                landed = (time.monotonic(), tab.url)
        now = time.monotonic()
        if now > deadline or (landed and now > landed[0] + LANDED_WAIT):
            break
        page.wait_for_timeout(POLL)
    if landed:
        raise WebPage(f"{what} led to a web page ({landed[1]}), not to a file")
    raise SourceError(f"{what} downloaded nothing")


def _return_to(page: Any, address: str) -> None:
    """Takes the tab back to the listing at the address: Back, else the address opened."""
    with contextlib.suppress(PlaywrightError):
        page.go_back(wait_until="domcontentloaded")
    with contextlib.suppress(PlaywrightError):
        if page.url != address:
            page.goto(address, wait_until="domcontentloaded")
        settle(page, SHORT_IDLE)


def land(target: Target, timeout: float) -> Landing:
    """Clicks the target's element and waits within timeout (ms) for where it lands: a tab or
    popup it opened, its tab at another address, or its page changed in place; then for the next
    level's elements there (tabs.listing_shown, and ARRIVE_WAIT at most after it). SourceError when
    the click changed nothing."""
    page = target.page
    element = element_at(page, target.selector, target.index, target.text)
    texts = page.eval_on_selector_all(target.selector, TARGETS)
    count = _count(page, target.wait_for)
    # What the next level's selector finds before: a change in place shows it once that changed.
    shown = found_elements(page, (target.wait_for,)) if target.wait_for else None
    tabs = len(page.context.pages)
    before = page.url
    with contextlib.suppress(PlaywrightError):
        page.evaluate(OBSERVE)
    element.click()
    deadline = time.monotonic() + timeout / 1000
    while True:
        pages = page.context.pages
        if len(pages) > tabs:
            landed, how = pages[-1], "tab"
            break
        if page.url != before:
            landed, how = page, "navigated"
            break
        try:
            changed = page.evaluate(CHANGED)
        except PlaywrightError:
            changed = 0  # between two documents: its address tells next time
        if changed:
            landed, how = page, "changed"
            break
        if time.monotonic() > deadline:
            raise SourceError(f"clicking “{target.text}” ({target.selector}) changed nothing on the page")
        page.wait_for_timeout(POLL)
    with contextlib.suppress(PlaywrightError):
        landed.wait_for_load_state("domcontentloaded")
    if target.wait_for:
        listing_shown(landed, (target.wait_for,), shown if how == "changed" else None)
    else:
        settle(landed)
    if target.wait_for:
        with contextlib.suppress(PlaywrightError):
            landed.wait_for_selector(target.wait_for, state="attached", timeout=ARRIVE_WAIT)
    return Landing(target, landed, how, texts, count)


def _count(page: Any, selector: str) -> int:
    """How many elements the selector finds on the page (0 without one, or one it cannot read)."""
    try:
        return len(page.query_selector_all(selector)) if selector else 0
    except PlaywrightError:
        return 0


def _listed_again(landing: Landing) -> bool:
    """Whether the listing's tab shows the listing again as it was: at its address, with the
    level's elements and texts as before the click, and as many of the next level's elements."""
    page, target = landing.target.page, landing.target
    try:
        texts = page.eval_on_selector_all(target.selector, TARGETS)
    except PlaywrightError:
        return False
    return page.url == target.address and texts == landing.texts and _count(page, target.wait_for) == landing.count


def back(landing: Landing, expand: Callable[[Any, dict], None]) -> None:
    """Returns from where the click landed to the listing: a tab or popup closed (the listing's
    tab is as it was); its own tab back (Back after an address, Escape after a change in place)
    while that shows the listing as it was; else the listing opened again at its address and its
    level's more loaded again (expand)."""
    target = landing.target
    if landing.how == "tab":
        with contextlib.suppress(PlaywrightError):
            landing.page.close()
        return
    page = target.page
    with contextlib.suppress(PlaywrightError):
        if landing.how == "navigated":
            page.go_back(wait_until="domcontentloaded")
        else:
            page.keyboard.press("Escape")
        settle(page, SHORT_IDLE)
    if _listed_again(landing):
        return
    page.goto(target.address, wait_until="domcontentloaded")
    listing_shown(page, tuple(selectors.candidates(target.spec)))
    expand(page, target.spec)
