# SPDX-License-Identifier: AGPL-3.0-or-later
"""How the site source reads pages and asks for files (harvester.sources.site): the driver's
interface, and the driver over HTTP (BeautifulSoup) for recipes without the browser.

A listing over HTTP answers as the browser's would be asked: in the browser's language, a
pause when the site asks for one (429, 503), a page below the listing that is gone or refused
empty (its trouble() says why) rather than the run's end; a next page that is a button needs
the browser, and says so.
"""

import abc
import contextlib
import time
from collections.abc import Callable
from typing import Any, BinaryIO, NamedTuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import browser, pagination, patterns, selectors
from . import download, near
from .base import AuthError, ChallengeError, SourceError
from .download import Response


def accept_language(locale: str) -> str:
    """The Accept-Language a browser of the locale sends ("de-DE": "de-DE,de;q=0.9")."""
    language = locale.split("-", 1)[0]
    return f"{locale},{language};q=0.9" if language != locale else locale


class HttpPage(NamedTuple):
    """A page over HTTP: where it ended up, what it holds, its status, and the address asked
    for (a redirect elsewhere tells a sign-in or a page that is gone)."""

    url: str
    soup: BeautifulSoup
    status: int = 200
    asked: str = ""


def moved(asked: str, url: str) -> bool:
    """Whether a page asked for ended up at another one (its host or path, not its query)."""
    if not asked:
        return False
    before, after = urlparse(patterns.stable_url(asked)), urlparse(patterns.stable_url(url))
    return (before.hostname, before.path.rstrip("/")) != (after.hostname, after.path.rstrip("/"))


class Driver(abc.ABC):
    """Fetches pages and files: over HTTP or in the browser."""

    # What the browser showed when a step failed or a page asked for a person (snapshot()), and
    # who is told of it (the source: a driver whose sign-in fails is never handed over).
    shot: dict | None = None
    saw: Callable[[dict], None] | None = None
    # The optional steps the sign-in went on without: {"number", "label", "why"} each.
    skipped: list[dict] = []
    # The steps whose element an alternative selector found (selectors.repair_line's lines).
    repaired: list[str] = []

    # The page that lists the file being downloaded (download: its Referer).
    referer = ""

    @abc.abstractmethod
    def open(self, url: str | None, deep: bool = False) -> Any:
        """Opens a page (None: the page the steps ended on); deep: a page below the listing,
        which is empty when the site answers an error (else that is the run's failure)."""

    @abc.abstractmethod
    def trouble(self, page: Any) -> str:
        """Why the page may have shown nothing it should have (a sign-in, an error, a redirect
        elsewhere), or ""."""

    @abc.abstractmethod
    def expand(self, page: Any, spec: dict) -> None:
        """Loads more of the page as its level asks (scroll, load_more)."""

    @abc.abstractmethod
    def targets(self, page: Any, selector: str) -> list[str]:
        """The texts of the elements a level that is clicked matches (its file links, or links to
        follow)."""

    @abc.abstractmethod
    def click_download(self, trigger: tuple, dest: BinaryIO, settings: dict) -> str:
        """Clicks the element and writes the file it downloads to dest; returns the file's name."""

    def open_clicked(self, target: Any) -> Any:
        """Opens what an element of a level to follow shows when clicked (clicks.Target): the page
        to read, which done() returns from to the listing. Only the browser clicks."""
        raise SourceError(f"the links to follow ({target.selector}) are clicked, which only the browser can")

    def sign_in_again(self) -> bool:
        """Signs in anew (the session ended in the middle of a run); False without a sign-in."""
        return False

    @abc.abstractmethod
    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        """The page's links matching the selector: absolute URL and text."""

    def valued_links(self, page: Any, selector: str, specs: list[dict]) -> list[tuple[str, str, list]]:
        """The page's links matching the selector, as links() gives them, each with the texts of
        the values (specs: a level's "values"; None where a value's element is missing)."""
        return [(url, text, [None] * len(specs)) for url, text in self.links(page, selector)]

    def read_json(self, url: str) -> Any:
        """A JSON listing's page, asked for as a file is (near.read_json)."""
        return near.read_json(self.download, url)

    @abc.abstractmethod
    def next(self, page: Any, selector: str) -> Any:
        """The page after this one (its link clicked or followed), or None."""

    @abc.abstractmethod
    def numbered(self, page: Any, selector: str, counted: int) -> Any:
        """The page numbered one higher than this one in the pagination (harvester.pagination),
        or None; counted is the walk's count of the listing's pages so far."""

    @abc.abstractmethod
    def address(self, page: Any) -> str:
        """The page's address."""

    @abc.abstractmethod
    def download(self, url: str) -> contextlib.AbstractContextManager[Response]:
        """Asks for the file."""

    @abc.abstractmethod
    def challenge(self, page: Any) -> tuple[str, str] | None:
        """The page's address and the kind of challenge it holds (a CAPTCHA), or None."""

    def done(self, page: Any) -> None:
        """Closes a page no longer needed."""

    def session(self) -> dict | None:
        """The session to keep (cookies, local storage), or None."""

    def close(self) -> None:
        """Releases the driver."""


class HttpDriver(Driver):
    """Pages over HTTP, read with BeautifulSoup."""

    def __init__(self, client, session: dict | None = None, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.sleep = sleep
        # The language the browser asks for, so that a site answers the pages the recipe was
        # recorded on.
        self.client.headers["Accept-Language"] = accept_language(browser.LOCALE)
        # A kept browser session's cookies (the user's answer to a challenge, say).
        for cookie in (session or {}).get("cookies", []):
            self.client.cookies.set(
                cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/")
            )

    def open(self, url: str | None, deep: bool = False) -> Any:
        for _ in range(download.MAX_WAITS + 1):
            text = ""
            try:
                with self.client.stream("GET", url) as response:
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    wait = download.pause(Response(response.status_code, str(response.url), headers, iter(())), "")
                    if wait is None:
                        text = download.read_page(response).decode(response.encoding or "utf-8", "replace")
            except httpx.TransportError as error:
                raise SourceError(f"{url} could not be read: {error}") from error
            if wait is not None:
                # 429 or 503: the site asks for a pause, as for a file.
                self.sleep(wait)
                continue
            return self._page(str(url), response, text, deep)
        raise SourceError(f"the site kept asking to wait for {url}")

    @staticmethod
    def _page(url: str, response: httpx.Response, text: str, deep: bool) -> HttpPage:
        """The page of an answer; an error is the run's failure on the listing, an empty page
        below it (its trouble() says why)."""
        kind = browser.challenge_in_html(text) if "html" in response.headers.get("content-type", "") else None
        if kind:
            raise ChallengeError(str(response.url), kind)
        status = response.status_code
        if status >= 400 and not deep:
            if status in (401, 403):
                raise AuthError(f"{url} answered {status}: the account may not see the listing")
            raise SourceError(f"{url} answered {status}")
        return HttpPage(str(response.url), BeautifulSoup(text if status < 400 else "", "html.parser"), status, url)

    def trouble(self, page: Any) -> str:
        if page.status >= 400:
            return f"answered {page.status}"
        if page.soup.select_one("input[type=password], input[autocomplete~=username]") or download.LOGIN_PATH.search(
            urlparse(page.url).path
        ):
            return "a sign-in page"
        return f"redirected to {page.url}" if moved(page.asked, page.url) else ""

    def challenge(self, page: Any) -> tuple[str, str] | None:
        kind = browser.challenge_in_html(str(page[1]))
        return (page[0], kind) if kind else None

    def expand(self, page: Any, spec: dict) -> None:
        if "scroll" in spec or "load_more" in spec:
            raise SourceError(f"{page.url}: the recipe loads more of its pages, which only the browser can")

    def targets(self, page: Any, selector: str) -> list[str]:
        raise SourceError(f"{page.url}: the elements {selector} are clicked, which only the browser can")

    def click_download(self, trigger: tuple, dest: BinaryIO, settings: dict) -> str:
        raise SourceError(f"the file links ({trigger[1]}) are clicked to download, which only the browser can")

    @staticmethod
    def _base(page: Any) -> str:
        """The address the page's links are relative to: its <base href>, else its own."""
        base = page.soup.find("base", href=True)
        return urljoin(page.url, str(base["href"])) if base else page.url

    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        base = self._base(page)
        return [
            (urljoin(base, str(link.get("href"))), link.get_text(" ", strip=True))
            for link in selectors.soup_select(page.soup, selector)
            if link.get("href")
        ]

    def valued_links(self, page: Any, selector: str, specs: list[dict]) -> list[tuple[str, str, list]]:
        base = self._base(page)
        return [
            (
                urljoin(base, str(link.get("href"))),
                link.get_text(" ", strip=True),
                [near.soup_value(link, spec) for spec in specs],
            )
            for link in page.soup.select(selector)
            if link.get("href")
        ]

    def next(self, page: Any, selector: str) -> Any:
        found = self.links(page, selector)
        if found:
            return self.open(found[0][0], deep=True)
        if selectors.soup_select(page.soup, selector):
            # A button, or a link without an address (a script's): only the browser clicks it.
            raise SourceError(
                f"the next page ({selector}) is a button without an address: only the browser can click it "
                '("buttons": true in the recipe\'s next, or Use the browser)'
            )
        return None

    def numbered(self, page: Any, selector: str, counted: int) -> Any:
        box = page.soup.select_one(selector)
        base = self._base(page)
        found = pagination.following(str(box) if box else "", page.url, counted)
        # A button has no address to fetch: only the browser can click it.
        return self.open(urljoin(base, found[1]), deep=True) if found and found[1] else None

    def address(self, page: Any) -> str:
        return page[0]

    def download(self, url: str) -> contextlib.AbstractContextManager[Response]:
        return download.http(self.client, {"Referer": self.referer} if self.referer else None)(url)
