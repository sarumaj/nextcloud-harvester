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
import functools
import time
from collections.abc import Callable
from typing import Any, BinaryIO, NamedTuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import browser, images, pageinfo, pagination, patterns, selectors
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


class Driver(abc.ABC):  # pylint: disable=too-many-public-methods
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
    def open(self, url: str | None, deep: bool = False, awaits: tuple[str, ...] = ()) -> Any:
        """Opens a page (None: the page the steps ended on); deep: a page below the listing,
        which is empty when the site answers an error (else that is the run's failure); awaits:
        the selectors of what the page lists, which the browser waits for (tabs.listing_shown)."""

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

    def keep_page(self, where: Any, dest: BinaryIO, settings: dict, spec: dict) -> tuple[str, str]:
        """Saves the page a file link leads to (where: its address, the element clicked, or a page
        open: keep.Opened) as spec's "save" says, into dest; the file's name and the page's title
        (harvester.sources.keep). Only the browser renders pages."""
        raise SourceError(f"the pages are saved as {spec.get('save')}, which only the browser can")

    def capture(self, where: Any, settings: dict, spec: dict) -> tuple[list, str, str]:
        """The files of the types spec captures that the page a file link leads to loads (keep
        .Captured each), its title and address. Only the browser loads pages."""
        raise SourceError("the files the pages load are captured, which only the browser can")

    @abc.abstractmethod
    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        """The page's links matching the selector: absolute URL and text."""

    def valued_links(self, page: Any, selector: str, specs: list[dict]) -> list[tuple[str, str, list]]:
        """The page's links matching the selector, as links() gives them, each with the texts of
        the values (specs: a level's "values"; None where a value's element is missing)."""
        return [(url, text, [None] * len(specs)) for url, text in self.links(page, selector)]

    def page_info(self, page: Any, specs: list[dict]) -> dict:  # pylint: disable=unused-argument
        """The page's raw values (harvester.pageinfo: its title, <meta>, JSON-LD, first <time>,
        language) with the texts of a level's page_values (specs); {} where it cannot be read."""
        return {}

    def read_json(self, url: str, request: dict | None = None) -> Any:
        """A JSON listing's page, asked for as a file is (near.read_json); request: its method,
        body (content) and headers, when it is no plain GET (harvester.jsonrequest)."""
        return near.read_json(functools.partial(self.download, **request) if request else self.download, url)

    def here(self) -> str:
        """The address of the page the steps ended on ("" without the browser)."""
        return ""

    @abc.abstractmethod
    def cookie_value(self, url: str, name: str) -> str | None:
        """The value of the job's cookie of that name for the address, None without one."""

    def page_value(self, page: Any, kind: str, name: str) -> str | None:
        """What the page holds under a name: a <meta name>'s content ("meta"), a localStorage key's
        value ("storage", the browser only); None without one."""
        raise SourceError(f"the JSON listing reads its page's {kind} {name}, which only the browser can")

    @abc.abstractmethod
    def next(self, page: Any, selector: str, awaits: tuple[str, ...] = ()) -> Any:
        """The page after this one (its link clicked or followed), or None; awaits as open's."""

    @abc.abstractmethod
    def numbered(self, page: Any, selector: str, counted: int, awaits: tuple[str, ...] = ()) -> Any:
        """The page numbered one higher than this one in the pagination (harvester.pagination),
        or None; counted is the walk's count of the listing's pages so far, awaits as open's."""

    @abc.abstractmethod
    def address(self, page: Any) -> str:
        """The page's address."""

    @abc.abstractmethod
    def download(
        self, url: str, *, method: str = "GET", content: bytes | None = None, headers: dict | None = None
    ) -> contextlib.AbstractContextManager[Response]:
        """Asks for the file (or a JSON listing's page: its method, body and headers)."""

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

    def open(self, url: str | None, deep: bool = False, awaits: tuple[str, ...] = ()) -> Any:
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
        if "scroll" in spec or "load_more" in spec or "wait_for" in spec:
            raise SourceError(
                f"{page.url}: the recipe loads more of its pages or waits on them, which only the browser can"
            )

    def targets(self, page: Any, selector: str) -> list[str]:
        raise SourceError(f"{page.url}: the elements {selector} are clicked, which only the browser can")

    def click_download(self, trigger: tuple, dest: BinaryIO, settings: dict) -> str:
        raise SourceError(f"the file links ({trigger[1]}) are clicked to download, which only the browser can")

    @staticmethod
    def _base(page: Any) -> str:
        """The address the page's links are relative to: its <base href>, else its own."""
        base = page.soup.find("base", href=True)
        return urljoin(page.url, str(base["href"])) if base else page.url

    @staticmethod
    def _address(link: Any, base: str) -> tuple[str, str]:
        """A link's address and text; an image's at its full size and its alt (harvester.images)."""
        if link.get("href"):
            return urljoin(base, str(link.get("href"))), link.get_text(" ", strip=True)
        info = images.soup_info(link)
        return images.best_image(info, base), images.alt(info)

    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        base = self._base(page)
        found = [self._address(link, base) for link in selectors.soup_select(page.soup, selector)]
        return [(url, text) for url, text in found if url]

    def valued_links(self, page: Any, selector: str, specs: list[dict]) -> list[tuple[str, str, list]]:
        base = self._base(page)
        found = [(self._address(link, base), link) for link in page.soup.select(selector)]
        return [(url, text, [near.soup_value(link, spec) for spec in specs]) for (url, text), link in found if url]

    def page_info(self, page: Any, specs: list[dict]) -> dict:
        return pageinfo.soup_info(page.soup, specs)

    def next(self, page: Any, selector: str, awaits: tuple[str, ...] = ()) -> Any:
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

    def numbered(self, page: Any, selector: str, counted: int, awaits: tuple[str, ...] = ()) -> Any:
        box = page.soup.select_one(selector)
        base = self._base(page)
        found = pagination.following(str(box) if box else "", page.url, counted)
        # A button has no address to fetch: only the browser can click it.
        return self.open(urljoin(base, found[1]), deep=True) if found and found[1] else None

    def address(self, page: Any) -> str:
        return page[0]

    def download(
        self, url: str, *, method: str = "GET", content: bytes | None = None, headers: dict | None = None
    ) -> contextlib.AbstractContextManager[Response]:
        sent = ({"Referer": self.referer} if self.referer else {}) | (headers or {})
        return download.http(self.client, sent or None, method, content)(url)

    def cookie_value(self, url: str, name: str) -> str | None:
        host = urlparse(url).hostname or ""
        for cookie in self.client.cookies.jar:
            domain = cookie.domain.lstrip(".")
            if cookie.name == name and (not domain or host == domain or host.endswith("." + domain)):
                return cookie.value
        return None

    def page_value(self, page: Any, kind: str, name: str) -> str | None:
        if kind != "meta":
            return super().page_value(page, kind, name)
        found = page.soup.find("meta", attrs={"name": name})
        return str(found.get("content") or "") or None if found else None
