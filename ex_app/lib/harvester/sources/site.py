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
import re
import time
from collections.abc import Callable, Generator, Iterator
from typing import Any, BinaryIO
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import Route, WebSocketRoute, sync_playwright

from .. import browser
from .. import cookies as cookie_jar
from .. import otp, patterns
from .. import recipe as recipes
from . import download
from .base import AuthError, ChallengeError, Item, Option, Source, SourceError
from .download import Response

# The seen key a run records once it has gone through the whole site (not stopped by
# max_items): from then on, a listing of known files ends the run (stop_at_seen).
FULL_PASS = "harvester:full-pass"
# How long a browser step may take (ms), and how long a job waits for the browser (s).
STEP_TIMEOUT = 30_000
LOCK_TIMEOUT = 1800


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

    def __init__(self, client, session: dict | None = None):
        self.client = client
        # A kept browser session's cookies (the user's answer to a challenge, say).
        for cookie in (session or {}).get("cookies", []):
            self.client.cookies.set(
                cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/")
            )

    def open(self, url: str | None) -> Any:
        with self.client.stream("GET", url) as response:
            text = download.read_page(response).decode(response.encoding or "utf-8", "replace")
        kind = browser.challenge_in_html(text) if "html" in response.headers.get("content-type", "") else None
        if kind:
            raise ChallengeError(str(response.url), kind)
        response.raise_for_status()
        return str(response.url), BeautifulSoup(text, "html.parser")

    def challenge(self, page: Any) -> tuple[str, str] | None:
        kind = browser.challenge_in_html(str(page[1]))
        return (page[0], kind) if kind else None

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

    def download(self, url: str) -> contextlib.AbstractContextManager[Response]:
        return download.http(self.client)(url)


class BrowserDriver(Driver):
    """Pages in Chromium: the steps replayed in the first tab, followed links in tabs of their own.

    With a kept session the sign-in steps are skipped while the listing shows no login form. A
    step that fails on a page asking for a person raises ChallengeError (only then: a login page
    may well carry an invisible CAPTCHA that lets people through)."""

    def __init__(
        self,
        steps: list[dict],
        secrets: dict[str, str],
        session: dict | None = None,
        start: str = "",
        cookies: list[dict] | None = None,
        http1: bool = False,
        home: bool = False,
    ):
        if not browser.SLOTS.acquire(timeout=LOCK_TIMEOUT):  # pylint: disable=consider-using-with
            raise SourceError("no browser came free (recordings use them all)")
        self._playwright = None
        self._browser = None
        # The downloads' proxy and user agent: the browser's (download).
        self.proxy = browser.PROXY if home else ""
        self.user_agent = ""
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(**browser.launch_options(http1, home))
            self.user_agent = browser.user_agent(self._browser.version)
            self.context = self._browser.new_context(
                **browser.context_options(self._browser.version),
                accept_downloads=False,
                storage_state=session or None,  # type: ignore[arg-type]
            )
            self.context.route("**/*", guard)
            self.context.route_web_socket(re.compile(".*"), socket_guard)
            # The account's cookies over the kept session's: the user gave them for this job.
            if cookies:
                self.context.add_cookies(cookies)  # type: ignore[arg-type]
            self.context.set_default_timeout(STEP_TIMEOUT)
            self.main = self.context.new_page()
            self.sign_in(steps, secrets, session, start)
        except BaseException:
            # Whatever failed, the browser closes and its lock is free for the next job.
            self.close()
            raise

    def sign_in(self, steps: list[dict], secrets: dict[str, str], session: dict | None, start: str) -> None:
        """Replays the steps, unless the kept session still reaches the listing."""
        if session and start:
            self.main.goto(start, wait_until="domcontentloaded")
            settle(self.main)
            if not self.main.query_selector("input[type=password]:visible"):
                return
        for number, step in enumerate(steps, 1):
            try:
                replay(self.main, step, secrets)
            except Exception as error:
                found = self.challenge(self.main)
                if found:
                    raise ChallengeError(*found) from error
                why = browser.connection_refused(error) or str(error)
                raise SourceError(f"step {number} ({step['action']} {step.get('text') or ''}) failed: {why}") from error

    def challenge(self, page: Any) -> tuple[str, str] | None:
        try:
            kind = page.evaluate(browser.CHALLENGE_SCRIPT)
        except Exception:  # pylint: disable=broad-exception-caught
            return None  # the page is gone or navigating
        return (page.url, kind) if kind else None

    def session(self) -> dict | None:
        return self.context.storage_state()  # type: ignore[return-value]

    def open(self, url: str | None) -> Any:
        if url is None:
            return self.main
        page = self.context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as error:
            why = browser.connection_refused(error)
            if why:
                raise SourceError(f"{url}: {why}") from error
            raise
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
        # Over HTTP with the browser's cookies, user agent and proxy, not with the context's own
        # requests (context.request), which pass by its routes: the guarded client checks every
        # request and redirect on the addresses the names resolve to, and the file comes in
        # chunks, never whole into memory.
        hooks = {"request": [session_cookies(self.context.storage_state()["cookies"])]}
        options = {"proxy": self.proxy} if self.proxy else {}
        with download.guarded_client(event_hooks=hooks, headers={"User-Agent": self.user_agent}, **options) as client:
            with download.http(client)(url) as response:
                yield response

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        finally:
            browser.SLOTS.release()


def guard(route: Route) -> None:
    """Lets the browser load only what it may open (harvester.browser.allowed_url)."""
    if browser.reachable(route.request.url):
        route.continue_()
    else:
        route.abort("blockedbyclient")


def socket_guard(route: WebSocketRoute) -> None:
    """Lets a page's WebSocket connect only where the browser may go (the routes do not see
    them); another is never connected: the page's socket opens to nothing (Playwright's stand-in,
    which a close from the handler would leave waiting)."""
    if browser.reachable(browser.http_address(route.url)):
        route.connect_to_server()


def sent_with(cookie: dict, url: httpx.URL, now: float) -> bool:
    """Whether the browser sends the cookie (storage_state's) to the address: a host's own only to
    that host (Playwright's domain without a dot), a domain's to it and its subdomains, a secure
    one only over https, within its path, unexpired."""
    domain, host = str(cookie.get("domain", "")).lower(), url.host.lower()
    within = str(cookie.get("path") or "/").rstrip("/")
    path = url.path or "/"
    expires = float(cookie.get("expires", -1) or -1)
    return (
        (host == domain or (domain.startswith(".") and f".{host}".endswith(domain)))
        and (path == within or path.startswith(within + "/"))
        and (url.scheme == "https" or not cookie.get("secure"))
        and not 0 < expires < now
    )


def session_cookies(cookies: list[dict]) -> Callable[[httpx.Request], None]:
    """Gives each request (a redirect's too) the browser's cookies that the browser would send
    with it (sent_with): an HTTP client's cookie jar would send a host's own to its subdomains
    too."""

    def add(request: httpx.Request) -> None:
        now = time.time()
        pairs = [f"{cookie['name']}={cookie['value']}" for cookie in cookies if sent_with(cookie, request.url, now)]
        if pairs:
            # After those the client's own jar holds (set by the answers since).
            request.headers["Cookie"] = "; ".join(filter(None, [request.headers.get("Cookie", ""), *pairs]))

    return add


def settle(page) -> None:
    """Waits for what a step started: a new page, or the requests of a script."""
    page.wait_for_timeout(500)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # a page that never goes quiet (ads, polling) is used as it is


def filled(step: dict, secrets: dict[str, str]) -> str:
    """What a fill step types: its value, an account's credential, or the current one-time code."""
    if step.get("credential") == "totp":
        if not secrets.get("totp"):
            raise AuthError("the sign-in asks for a one-time code: set the code's secret in the job's account")
        try:
            return otp.code(secrets["totp"])
        except ValueError as error:
            raise AuthError(str(error)) from error
    if step.get("credential"):
        return secrets.get(step["credential"], "")
    return str(step.get("value", ""))


def replay(page, step: dict, secrets: dict[str, str]) -> None:
    """Does one recorded step."""
    action = step["action"]
    if action == "goto":
        page.goto(step["url"], wait_until="domcontentloaded")
    elif action == "fill":
        page.fill(step["selector"], filled(step, secrets))
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
    description = (
        "Files linked from a website's pages: record it in the browser (sign in if need be, then pick the links to "
        "follow and the file links) or start from a template."
    )
    credentials = ("username", "password", "totp", "cookies")
    credential_help = {
        "username": (
            "Username",
            "Filled into the recorded sign-in's username field; without recorded steps, sent to sites that ask with "
            "the browser's own sign-in box (HTTP Basic).",
        ),
        "password": ("Password", "Filled into the recorded sign-in's password field (or sent with HTTP Basic)."),
        "totp": (
            "One-time code's secret",
            "For a sign-in with an authenticator app: the secret it was given (the base32 text beside the QR code, "
            "or the QR code's otpauth:// link). The recorded code field gets the current code.",
        ),
        "cookies": (
            "Cookies",
            "A signed-in browser's cookies for the site, instead of a recorded sign-in: a cookies.txt export "
            "(Netscape format) or a Cookie header's name=value; name=value. They expire as they do in the browser.",
        ),
    }
    options = {
        "recipe": Option(
            "Recipe", "", "Record it in the browser, or start from a template; the JSON can be edited.", kind="recipe"
        ),
        "browser": Option(
            "Use the browser",
            0,
            "Fetch the pages in Chromium even without steps (pages built by JavaScript).",
            kind="flag",
        ),
        "http1": Option(
            "HTTP/1.1 only",
            0,
            "For sites whose protection cuts the browser's connection (ERR_HTTP2_PROTOCOL_ERROR): the browser "
            "speaks the older HTTP/1.1 with them, also in the recorder.",
            kind="flag",
        ),
        "download_url": Option(
            "Download URL",
            "{url}",
            "The file's address from the link's values, e.g. {url}/download.",
            kind="template",
            values=("filename", "stem", "ext", "text", "page", "host", "url"),
            suggestions=("{url}", "{url}/download", "{url}?download=1"),
        ),
        "path": Option(
            "File name",
            "{filename}",
            "Relative to the folder, from the link's values: {filename}, {stem}, {ext}, {text}, {page}, {host} and "
            "the pattern's groups, e.g. {year}/{text}.",
            kind="template",
            values=("filename", "stem", "ext", "text", "page", "host", "url"),
            suggestions=("{filename}", "{text}{ext}", "{host}/{filename}", "{page}/{filename}"),
        ),
        **download.DOWNLOAD_OPTIONS,
        "max_pages": Option(
            "Pages", 10, "At most this many pages of a listing.", minimum=1, suggestions=("1", "5", "10", "50")
        ),
        "stop_at_seen": Option(
            "Stop at known files",
            1,
            "Once the site went through completely, a listing of known files ends the run. It assumes the "
            "newest come first, or last: of the links to follow, the first and last are looked at, the new ones "
            "taken from the end that has them. A listing over several pages is taken to start with its newest "
            "page: for one whose first page is its oldest, turn this off.",
            kind="flag",
        ),
    }
    pause = 2.0
    listing_options = ("recipe", "browser", "max_pages")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.recipe = json.loads(str(self.settings["recipe"]) or "{}")
        except ValueError as error:
            raise SourceError(f"the recipe is not valid JSON: {error}") from error
        problems = recipes.check_recipe(self.recipe)
        problems += download.problems(self.settings)
        if problems:
            raise SourceError("; ".join(problems))
        self.driver: Driver | None = None
        self.kept: dict | None = None
        self.sleep: Callable[[float], None] = time.sleep

    def restore(self, session: dict) -> None:
        self.kept = session

    def session(self) -> dict | None:
        return self.driver.session() if self.driver else None

    def _driver(self) -> Driver:
        if self.driver is None:
            start = self.recipe.get("start") or ""
            found = self.account_cookies(start)
            if recipes.uses_browser(self.recipe) or int(self.settings["browser"]):
                steps = self.recipe.get("steps") or []
                self.driver = BrowserDriver(
                    steps,
                    self.secrets,
                    self.kept,
                    start,
                    cookie_jar.to_playwright(found),
                    http1=bool(int(self.settings["http1"])),
                    home=self.home,
                )
            else:
                # Without recorded steps, the account's user name and password are HTTP Basic's.
                self.sign_client(start)
                self.driver = HttpDriver(self.client, self.kept)
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

        # A page without the links it should have may be asking for a person instead; the start
        # page without any is a signed-out session or a changed site, not an empty listing.
        def blocked(page, level: int = 1, number: int = 2) -> None:
            found = driver.challenge(page)
            if found:
                raise ChallengeError(*found)
            if level == 0 and number == 1:
                raise SourceError("the start page has none of the recipe's links: signed out, or the site changed?")

        # The file links of a listing's page; True when the listing ends there (no file links, or
        # only known ones while known files end the run).
        def files(page, number: int, values: dict, outcome: dict, level: int) -> Generator[Item, None, bool]:
            found = matching(page, self.recipe["links"])
            if not found:
                blocked(page, level, number)
            new = [entry for entry in found if not seen(entry[0])]
            outcome["found"] |= bool(found)
            outcome["new"] |= bool(new)
            for link, text, groups in new:
                yield self.item(link, text, number, values | groups)
            return not found or bool(stop_at_seen and not new)

        # A followed link's listing; True when it holds files, all known.
        def known(link: str, level: int, values: dict, outcome: dict) -> Generator[Item, None, bool]:
            child = {"found": False, "new": False}
            yield from walk(link, level + 1, values, child)
            outcome["found"] |= child["found"]
            outcome["new"] |= child["new"]
            return child["found"] and not child["new"]

        # The links to follow of a page; True when the listing ends there. While known files end
        # the run, the first known one ends it only if the page's last is known too (the newest
        # come first); else the page lists the newest last, and its links are followed from the
        # end back to a known one, and on to the next page.
        def children(links: list, level: int, values: dict, outcome: dict) -> Generator[Item, None, bool]:
            for index, (link, _, groups) in enumerate(links):
                if not (yield from known(link, level, values | groups, outcome)) or not stop_at_seen:
                    continue
                rest = links[index + 1 :]
                if not rest:
                    return True
                last, _, last_groups = rest[-1]
                if (yield from known(last, level, values | last_groups, outcome)):
                    return True
                for link_, _, groups_ in reversed(rest[:-1]):
                    if (yield from known(link_, level, values | groups_, outcome)):
                        break
                return False
            return False

        # Each listing reports whether it found files and whether any was new.
        def walk(url: str | None, level: int, values: dict, outcome: dict) -> Iterator[Item]:
            page = driver.open(url)
            try:
                for number in range(1, int(self.settings["max_pages"]) + 1):
                    if level < len(follow):
                        links = matching(page, follow[level])
                        if not links:
                            blocked(page, level, number)
                        if (yield from children(links, level, values, outcome)):
                            return
                    elif (yield from files(page, number, values, outcome, level)):
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
        """The file of a link: its download URL and path from the link's values (an item that
        fails alone when they cannot be made)."""
        values = patterns.link_values(link, text, page) | groups
        try:
            url = patterns.render(str(self.settings["download_url"]), values)
            path = patterns.render_path(str(self.settings["path"]), values)
        except (KeyError, ValueError) as error:
            problem = SourceError(str(error).strip("'\""))
            return Item(key=link, path="", url=link, values=values, problem=problem)
        path = download.with_extension(path, self.settings)
        return Item(key=link, path=path, url=url, values=values)

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        download.fetch(self._driver().download, item.url, dest, self.settings, self.sleep)

    def close(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver = None
