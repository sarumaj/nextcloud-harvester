"""Any website, described by a recipe (harvester.recipe): the generic source.

The pages are fetched over HTTP, or in Chromium when the recipe has steps (a login, a site that
needs JavaScript) or the job asks for the browser. From the start page the source follows the
recipe's levels of links, takes the file links on the last level, page after page, and
downloads each file from its download URL (a template of the link's values). It waits when the
site asks it to (429 or 503 with Retry-After, or a wait in the URL it redirects to) and checks
what it gets: never a web page instead of a file, of the expected type and size.
"""

import abc
import contextlib
import json
import posixpath
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import Route, sync_playwright

from .. import browser, patterns, recipe as recipes
from .base import AuthError, Item, Option, Source, SourceError

# The seen key a run records once it has gone through the whole site (not stopped by
# max_items): from then on, a listing of known files ends the run (stop_at_seen).
FULL_PASS = "harvester:full-pass"
# Waits the site asks for, per file; and the longest one honoured.
MAX_WAITS = 5
MAX_WAIT = 900
# How long a browser step may take (ms), and how long a job waits for the browser (s).
STEP_TIMEOUT = 30_000
LOCK_TIMEOUT = 1800
# What a file must start with, and its extension when the name has none.
FILE_TYPES = {
    "any": ((), ""),
    "pdf": ((b"%PDF-",), ".pdf"),
    "zip": ((b"PK\x03\x04",), ".zip"),
    "image": ((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF"), ""),
    "html": ((), ".html"),
}


@dataclass
class Response:
    """A download's answer, the same over HTTP and in the browser."""

    status: int
    url: str
    headers: dict[str, str]
    chunks: Iterator[bytes]


class Driver(abc.ABC):
    """Fetches pages and files: over HTTP or in the browser."""

    @abc.abstractmethod
    def open(self, url: str | None) -> Any:
        """Opens a page (None: the page the steps ended on)."""

    @abc.abstractmethod
    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        """The page's links matching the selector: absolute URL and text."""

    @abc.abstractmethod
    def next(self, page: Any, selector: str) -> Any:
        """The page after this one (its link clicked or followed), or None."""

    @abc.abstractmethod
    def download(self, url: str) -> contextlib.AbstractContextManager[Response]:
        """Asks for the file."""

    def done(self, page: Any) -> None:
        """Closes a page no longer needed."""

    def close(self) -> None:
        """Releases the driver."""


class HttpDriver(Driver):
    """Pages over HTTP, read with BeautifulSoup."""

    def __init__(self, client):
        self.client = client

    def open(self, url: str | None) -> Any:
        response = self.client.get(url)
        response.raise_for_status()
        return str(response.url), BeautifulSoup(response.text, "html.parser")

    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        page_url, soup = page
        return [
            (urljoin(page_url, str(link.get("href"))), link.get_text(" ", strip=True))
            for link in soup.select(selector)
            if link.get("href")
        ]

    def next(self, page: Any, selector: str) -> Any:
        found = self.links(page, selector)
        return self.open(found[0][0]) if found else None

    @contextlib.contextmanager
    def download(self, url: str) -> Iterator[Response]:
        with self.client.stream("GET", url) as response:
            yield Response(
                response.status_code,
                str(response.url),
                {key.lower(): value for key, value in response.headers.items()},
                response.iter_bytes(),
            )


class BrowserDriver(Driver):
    """Pages in Chromium: the steps replayed in the first tab, followed links in tabs of their own."""

    def __init__(self, steps: list[dict], secrets: dict[str, str]):
        if not browser.LOCK.acquire(timeout=LOCK_TIMEOUT):  # pylint: disable=consider-using-with
            raise SourceError("the browser stayed busy (a recording or another job)")
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(args=browser.LAUNCH_ARGS)
        except Exception:
            browser.LOCK.release()
            raise
        self.context = self._browser.new_context(viewport=browser.VIEWPORT, accept_downloads=False)
        self.context.route("**/*", guard)
        self.context.set_default_timeout(STEP_TIMEOUT)
        self.main = self.context.new_page()
        for number, step in enumerate(steps, 1):
            try:
                replay(self.main, step, secrets)
            except Exception as error:
                raise SourceError(
                    f"step {number} ({step['action']} {step.get('text') or ''}) failed: {error}"
                ) from error

    def open(self, url: str | None) -> Any:
        if url is None:
            return self.main
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        settle(page)
        return page

    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        return page.eval_on_selector_all(selector, "els => els.map(e => [e.href || '', (e.innerText || '').trim()])")

    def next(self, page: Any, selector: str) -> Any:
        link = page.query_selector(selector)
        if not link:
            return None
        link.click()
        settle(page)
        return page

    def done(self, page: Any) -> None:
        if page is not self.main:
            page.close()

    @contextlib.contextmanager
    def download(self, url: str) -> Iterator[Response]:
        response = self.context.request.get(url, timeout=300_000)
        try:
            yield Response(response.status, response.url, response.headers, iter([response.body()]))
        finally:
            response.dispose()

    def close(self) -> None:
        try:
            self._browser.close()
            self._playwright.stop()
        finally:
            browser.LOCK.release()


def guard(route: Route) -> None:
    """Lets the browser load only what it may open (harvester.browser.allowed_url)."""
    if browser.allowed_url(route.request.url):
        route.continue_()
    else:
        route.abort("blockedbyclient")


def settle(page) -> None:
    """Waits for what a step started: a new page, or the requests of a script."""
    page.wait_for_timeout(500)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # a page that never goes quiet (ads, polling) is used as it is


def replay(page, step: dict, secrets: dict[str, str]) -> None:
    """Does one recorded step."""
    action = step["action"]
    if action == "goto":
        page.goto(step["url"], wait_until="domcontentloaded")
    elif action == "fill":
        page.fill(step["selector"], secrets.get(step["credential"], "") if step.get("credential") else step["value"])
        return
    elif action == "click":
        page.click(step["selector"])
    elif action == "press":
        page.press(step["selector"], step.get("key", "Enter"))
    settle(page)


class Site(Source):  # pylint: disable=too-many-instance-attributes
    """A website described by a recipe."""

    name = "site"
    title = "Website (recipe)"
    credentials = ("username", "password")
    options = {
        "recipe": Option(
            "Recipe", "", "Record it in the browser, or start from a template; the JSON can be edited.", kind="recipe"
        ),
        "browser": Option("Use the browser", 0, "1: fetch the pages in Chromium even without steps (JavaScript)."),
        "download_url": Option(
            "Download URL", "{url}", "The file's address from the link's values, e.g. {url}/download."
        ),
        "path": Option(
            "File name",
            "{filename}",
            "Relative to the folder, from the link's values: {filename}, {stem}, {ext}, {text}, {page}, {host} and "
            "the pattern's groups, e.g. {year}/{text}.",
        ),
        "file_type": Option("File type", "any", "any, pdf, zip, image or html: anything else is refused."),
        "min_size": Option("Smallest file (bytes)", 0, "Smaller answers are refused (an error page, say)."),
        "wait_pattern": Option(
            "Wait pattern",
            "",
            "Regular expression on the URL the site redirects to when it wants a pause; its "
            "first group is the seconds (e.g. wait_sec=(\\d+)).",
        ),
        "max_pages": Option("Pages", 10, "At most this many pages of a listing."),
        "stop_at_seen": Option(
            "Stop at known files", 1, "1: once the site went through completely, a listing of known files ends the run."
        ),
    }
    pause = 2.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.recipe = json.loads(str(self.settings["recipe"]) or "{}")
        except ValueError as error:
            raise SourceError(f"the recipe is not valid JSON: {error}") from error
        problems = recipes.check_recipe(self.recipe)
        try:
            re.compile(str(self.settings["wait_pattern"]))
        except re.error as error:
            problems.append(f"the wait pattern is not a regular expression ({error})")
        if str(self.settings["file_type"]) not in FILE_TYPES:
            problems.append(f"unknown file type {self.settings['file_type']}")
        if problems:
            raise SourceError("; ".join(problems))
        self.driver: Driver | None = None
        self.sleep: Callable[[float], None] = time.sleep

    def _driver(self) -> Driver:
        if self.driver is None:
            if recipes.uses_browser(self.recipe) or int(self.settings["browser"]):
                self.driver = BrowserDriver(self.recipe.get("steps") or [], self.secrets)
            else:
                self.driver = HttpDriver(self.client)
        return self.driver

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        driver = self._driver()
        stop_at_seen = int(self.settings["stop_at_seen"]) and seen(FULL_PASS)
        follow = self.recipe.get("follow") or []
        visited: set[str] = set()

        def matching(page, spec: dict) -> list[tuple[str, str, dict]]:
            pattern = re.compile(spec.get("pattern") or "")
            found = []
            for url, text in driver.links(page, spec["selector"]):
                match = pattern.search(url) if url else None
                if match and url not in visited:
                    visited.add(url)
                    found.append((url, text, patterns.group_values(match)))
            return found

        # Each listing reports whether it found files and whether any was new.
        def walk(url: str | None, level: int, values: dict, outcome: dict) -> Iterator[Item]:
            page = driver.open(url)
            try:
                for number in range(1, int(self.settings["max_pages"]) + 1):
                    if level < len(follow):
                        for link, _, groups in matching(page, follow[level]):
                            child = {"found": False, "new": False}
                            yield from walk(link, level + 1, values | groups, child)
                            outcome["found"] |= child["found"]
                            outcome["new"] |= child["new"]
                            if stop_at_seen and child["found"] and not child["new"]:
                                return
                    else:
                        found = matching(page, self.recipe["links"])
                        new = [entry for entry in found if not seen(entry[0])]
                        outcome["found"] |= bool(found)
                        outcome["new"] |= bool(new)
                        for link, text, groups in new:
                            yield self.item(link, text, number, values | groups)
                        if not found or (stop_at_seen and not new):
                            return
                    after = self.recipe.get("next") or {}
                    if not after.get("selector") or int(after.get("level", 0)) != level:
                        return
                    page = driver.next(page, after["selector"])
                    if page is None:
                        return
            finally:
                if page is not None:
                    driver.done(page)

        yield from walk(self.recipe.get("start") or None, 0, {}, {"found": False, "new": False})

    def item(self, link: str, text: str, page: int, groups: dict) -> Item:
        """The file of a link: its download URL and path from the link's values."""
        values = patterns.link_values(link, text, page) | groups
        try:
            url = patterns.render(str(self.settings["download_url"]), values)
            path = patterns.render_path(str(self.settings["path"]), values)
        except (KeyError, ValueError) as error:
            raise SourceError(str(error).strip("'\"")) from error
        extension = FILE_TYPES[str(self.settings["file_type"])][1]
        if extension and not posixpath.splitext(path)[1]:
            path += extension
        return Item(key=link, path=path, url=url, values=values)

    def wait(self, response: Response) -> float | None:
        """The pause the site asks for, or None."""
        if response.status in (429, 503):
            retry = response.headers.get("retry-after", "")
            return min(float(retry) if retry.isdigit() else 60.0, MAX_WAIT)
        pattern = str(self.settings["wait_pattern"])
        match = re.search(pattern, response.url) if pattern else None
        if match:
            return min(float(match.group(1)) + 5 if match.groups() else 60.0, MAX_WAIT)
        return None

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        if not browser.allowed_url(item.url):
            raise SourceError(f"{item.url} may not be opened")
        magic, _ = FILE_TYPES[str(self.settings["file_type"])]
        for _ in range(MAX_WAITS):
            with self._driver().download(item.url) as response:
                pause = self.wait(response)
                if pause is not None:
                    self.sleep(pause)
                    continue
                if response.status in (401, 403):
                    raise AuthError(f"{item.url} answered {response.status}: the account may not download it")
                if response.status >= 400:
                    raise SourceError(f"{item.url} answered {response.status}")
                web_page = response.headers.get("content-type", "").startswith("text/html")
                if web_page and self.settings["file_type"] != "html":
                    raise SourceError(f"got a web page instead of a file for {item.url} (signed out?)")
                size = 0
                for chunk in response.chunks:
                    if size == 0 and magic and not chunk.startswith(magic):
                        raise SourceError(f"{item.url} is not a {self.settings['file_type']} file")
                    dest.write(chunk)
                    size += len(chunk)
                if size < int(self.settings["min_size"]):
                    raise SourceError(f"{item.url} gave only {size} bytes")
                return
        raise SourceError(f"the site kept asking to wait for {item.url}")

    def close(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver = None
