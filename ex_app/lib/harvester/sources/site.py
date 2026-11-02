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
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route, WebSocketRoute, sync_playwright

from .. import browser
from .. import cookies as cookie_jar
from .. import otp, pagination, patterns
from .. import recipe as recipes
from ..browser.guard import Guard
from . import download
from .base import AuthError, ChallengeError, Item, Option, Source, SourceError, Stopped, pattern_search
from .download import Response

# The seen key a run records once it has gone through the whole site (not stopped by
# max_items): from then on, a listing of known files ends the run (stop_at_seen).
FULL_PASS = "harvester:full-pass"
# How long a browser step may take (ms), and how long a job waits for the browser (s).
STEP_TIMEOUT = 30_000
# How long an optional step waits for its frame and element together (ms): a banner that shows
# comes at once with its page, and the run should not wait half a minute for one that does not.
OPTIONAL_TIMEOUT = 5_000
# How often a step looks again for the frame it acts in while the page builds it (ms).
FRAME_POLL = 250
LOCK_TIMEOUT = 1800
# What the browser showed when a step failed or a page asked for a person: a JPEG of the window
# at this quality, again at the lower one when larger than SHOT_BYTES (else none is kept).
SHOT_QUALITY = (60, 35)
SHOT_BYTES = 400_000
# A scroll step ends once the page stopped growing for this many rounds in a row, or after
# SCROLL_SECONDS (a feed that never ends).
SCROLL_STILL = 2
SCROLL_SECONDS = 60
# The page's size as a scroll step compares it: its links, its elements and its height.
PAGE_SIZE = (
    "() => [document.links.length, document.getElementsByTagName('*').length,"
    " (document.scrollingElement || document.body).scrollHeight]"
)
SCROLL_DOWN = "() => window.scrollTo(0, (document.scrollingElement || document.body).scrollHeight)"
OUTER_HTML = "s => { const e = document.querySelector(s); return e ? e.outerHTML : '' }"
# The recipe test (Site.survey): the file links it lists, and the listing pages it opens at most.
SURVEY_FILES = 20
SURVEY_PAGES = 30


class Driver(abc.ABC):
    """Fetches pages and files: over HTTP or in the browser."""

    # What the browser showed when a step failed or a page asked for a person (snapshot()), and
    # who is told of it (the source: a driver whose sign-in fails is never handed over).
    shot: dict | None = None
    saw: Callable[[dict], None] | None = None
    # The optional steps the sign-in went on without: {"number", "label", "why"} each.
    skipped: list[dict] = []

    def keep_shot(self, page: Any, what: str) -> None:
        """Keeps what the page shows, and tells saw."""
        self.shot = snapshot(page, what) or self.shot
        tell = self.saw
        if self.shot and tell is not None:
            tell(self.shot)  # pylint: disable=not-callable

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

    def numbered(self, page: Any, selector: str, counted: int) -> Any:
        page_url, soup = page
        box = soup.select_one(selector)
        found = pagination.following(str(box) if box else "", page_url, counted)
        # A button has no address to fetch: only the browser can click it.
        return self.open(urljoin(page_url, found[1])) if found and found[1] else None

    def address(self, page: Any) -> str:
        return page[0]

    def download(self, url: str) -> contextlib.AbstractContextManager[Response]:
        return download.http(self.client)(url)


class BrowserDriver(Driver):
    """Pages in Chromium: the steps replayed in the first tab, followed links in tabs of their own.

    With a kept session the sign-in steps are skipped while the listing shows no login form. A
    step that fails on a page asking for a person raises ChallengeError (only then: a login page
    may well carry an invisible CAPTCHA that lets people through)."""

    # The listing the steps ended on is read in their tab, as they left it (scrolled to load
    # more, say), once; other pages open in tabs of their own.
    _start = ""
    _reused = False

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        steps: list[dict],
        secrets: dict[str, str],
        session: dict | None = None,
        start: str = "",
        cookies: list[dict] | None = None,
        http1: bool = False,
        home: bool = False,
        saw: Callable[[dict], None] | None = None,
    ):
        self.saw = saw
        if not browser.SLOTS.acquire(timeout=LOCK_TIMEOUT, kind="job"):  # pylint: disable=consider-using-with
            raise SourceError("no browser came free (recordings use them all)")
        self._playwright = None
        self._browser = None
        # The downloads' proxy and user agent: the browser's (download).
        self.proxy = browser.PROXY if home else ""
        self.user_agent = ""
        # The browser's connections all go through its guard (with the home connection, if chosen).
        self._guard: Guard | None = None
        try:
            self._guard = Guard(self.proxy)
            port = self._guard.start()
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(**browser.launch_options(port, http1))
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
        """Replays the steps, unless the kept session still reaches the listing: then only the
        scroll steps at their end, which load the listing's later entries."""
        todo = list(enumerate(steps, 1))
        self._start = start
        self.skipped = []
        if session and start:
            self.main.goto(start, wait_until="domcontentloaded")
            settle(self.main)
            if not self.main.query_selector("input[type=password]:visible"):
                last = max((number for number, step in todo if step.get("action") != "scroll"), default=0)
                todo = todo[last:]
        for number, step in todo:
            try:
                replay(self.main, step, secrets)
            except Stopped:
                # The run or the recipe's test was stopped during the step: no failure of it.
                raise
            except (PlaywrightError, FrameMissing) as error:
                if not step.get("optional"):
                    self._failed(number, step, error)
                # Best effort: no picture, no failure; the run says what it went on without.
                self.skipped.append({"number": number, "label": recipes.step_label(step), "why": short_error(error)})
            except Exception as error:  # pylint: disable=broad-exception-caught
                self._failed(number, step, error)

    def _failed(self, number: int, step: dict, error: Exception) -> None:
        """A required step failed: the page asking for a person (ChallengeError), else SourceError
        naming the step, with a picture of what the browser showed."""
        found = self.challenge(self.main)
        if found:
            raise ChallengeError(*found) from error
        why = browser.connection_refused(error) or str(error)
        what = f"step {number} ({recipes.step_label(step)}) failed"
        self.keep_shot(self.main, what)
        raise SourceError(f"{what}: {why}") from error

    def challenge(self, page: Any) -> tuple[str, str] | None:
        try:
            kind = page.evaluate(browser.CHALLENGE_SCRIPT)
        except Exception:  # pylint: disable=broad-exception-caught
            return None  # the page is gone or navigating
        if not kind:
            return None
        self.keep_shot(page, f"the page asks for a person ({kind})")
        return page.url, kind

    def session(self) -> dict | None:
        return self.context.storage_state()  # type: ignore[return-value]

    def open(self, url: str | None) -> Any:
        if url is None:
            return self.main
        if not self._reused and url == self._start and self.main.url.split("#")[0] == url.split("#")[0]:
            self._reused = True
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

    def numbered(self, page: Any, selector: str, counted: int) -> Any:
        found = pagination.following(page.evaluate(OUTER_HTML, selector), page.url, counted)
        if not found or not page.evaluate(pagination.MARK_NUMBER, [selector, str(found[0])]):
            return None
        page.click("[data-harvester-next]")
        settle(page)
        return page

    def address(self, page: Any) -> str:
        return page.url

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
            try:
                if self._guard:
                    self._guard.stop()
            finally:
                browser.SLOTS.release("job")


def skipped_line(skipped: dict) -> str:
    """The progress log's line of an optional step the run went on without."""
    return f"step {skipped['number']} ({skipped['label']}, optional) skipped: {skipped['why']}"


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


def snapshot(page, what: str) -> dict | None:
    """What the page shows (a JPEG of the window, small enough to keep), its address and title,
    and what went wrong there; None when the page cannot be shot (gone, or a picture too large)."""
    try:
        for quality in SHOT_QUALITY:
            image = page.screenshot(type="jpeg", quality=quality, timeout=10_000)
            if len(image) <= SHOT_BYTES:
                return {"image": image, "url": page.url, "title": page.title(), "what": what, "at": time.time()}
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # the page closed or hangs: the error says what went wrong all the same
    return None


def scroll_to_load(page, rounds: int) -> None:
    """Scrolls to the page's end until it stops growing (no new links, elements or height for
    SCROLL_STILL rounds), at most rounds times or SCROLL_SECONDS."""
    deadline = time.monotonic() + SCROLL_SECONDS
    still = 0
    for _ in range(rounds):
        before = page.evaluate(PAGE_SIZE)
        page.evaluate(SCROLL_DOWN)
        settle(page)
        still = still + 1 if page.evaluate(PAGE_SIZE) == before else 0
        if still >= SCROLL_STILL or time.monotonic() > deadline:
            return


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


class FrameMissing(SourceError):
    """The frame a step acts in is not on the page."""


def short_error(error: Exception) -> str:
    """An error's first line without Playwright's call ("Locator.click: "), for a log line."""
    text = (str(error) or type(error).__name__).splitlines()[0]
    return re.sub(r"^[A-Za-z]\w*\.\w+: ", "", text)


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


def replay(page, step: dict, secrets: dict[str, str]) -> None:
    """Does one recorded step, in its frame if it has one; an optional step waits for its frame
    and element OPTIONAL_TIMEOUT at most (a required one the page's default, STEP_TIMEOUT)."""
    action = step["action"]
    started = time.monotonic()
    budget = OPTIONAL_TIMEOUT if step.get("optional") else STEP_TIMEOUT
    # A step without a frame acts on the page as it always did.
    target = in_frame(page, step["frame"], budget) if step.get("frame") else page
    # What is left of an optional step's time for its element (never 0: Playwright's "no limit").
    left = max(1.0, budget - (time.monotonic() - started) * 1000)
    wait = {"timeout": left} if step.get("optional") else {}
    if action == "goto":
        page.goto(step["url"], wait_until="domcontentloaded")
    elif action == "fill":
        target.fill(step["selector"], filled(step, secrets), **wait)
        return
    elif action == "click":
        target.click(step["selector"], **wait)
    elif action == "press":
        target.press(step["selector"], step.get("key", "Enter"), **wait)
    elif action == "scroll":
        scroll_to_load(page, int(step.get("rounds") or recipes.SCROLL_ROUNDS))
        return
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
    # The address is the recipe's listing: a fetch's page is walked with the recipe's levels and
    # file links (after its sign-in steps), as its start page would be.
    address_option = "recipe"

    @classmethod
    def address_of(cls, options: dict) -> str:
        try:
            return str(json.loads(str(options.get("recipe") or "{}")).get("start") or "").strip()
        except (ValueError, AttributeError):
            return ""

    @classmethod
    def with_address(cls, options: dict, url: str) -> dict:
        try:
            recipe = json.loads(str(options.get("recipe") or "{}"))
        except ValueError:
            recipe = {}
        if not isinstance(recipe, dict):
            recipe = {}
        return options | {"recipe": json.dumps(recipe | {"start": url})}

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
        # Told of each listing page the walk reads (survey(): the recipe's test; a job's run:
        # what each level found, watch.Watch). It may answer what went wrong there: the walk then
        # keeps a picture of the page (noticed), apart from a failure's (shot).
        self.observe: Callable[[dict], str | None] = lambda event: None
        self.noticed: dict | None = None
        self._shot: dict | None = None

    def _saw(self, shot: dict) -> None:
        self._shot = shot

    @property
    def shot(self) -> dict | None:
        """What the browser showed when a step failed or a page asked for a person, if it did."""
        return self._shot or (self.driver.shot if self.driver else None)

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
                    saw=self._saw,
                )
                for skipped in self.driver.skipped:
                    self.report("info", skipped_line(skipped))
            else:
                # Without recorded steps, the account's user name and password are HTTP Basic's.
                self.sign_client(start)
                self.driver = HttpDriver(self.client, self.kept)
        return self.driver

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:  # pylint: disable=too-many-statements
        driver = self._driver()
        stop_at_seen = int(self.settings["stop_at_seen"]) and seen(FULL_PASS)
        follow = self.recipe.get("follow") or []
        visited: set[str] = set()

        def matching(page, spec: dict, level: int, number: int) -> list[tuple[str, str, dict]]:
            pattern = spec.get("pattern") or ""
            found = []
            links = driver.links(page, spec["selector"])
            for url, text in links:
                # A person's pattern, within its time limit (a slow one fails the run).
                match = pattern_search(pattern, url) if url else None
                if match and url not in visited:
                    visited.add(url)
                    found.append((url, text, patterns.group_values(match)))
            kind = "follow" if spec is not self.recipe["links"] else "links"
            where = {"level": level, "page": number, "url": driver.address(page), "kind": kind}
            what = self.observe(where | {"selector": spec["selector"], "found": len(links), "matching": len(found)})
            if what:
                self.noticed = snapshot(page, what)
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
            found = matching(page, self.recipe["links"], level, number)
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
                        links = matching(page, follow[level], level, number)
                        if not links:
                            blocked(page, level, number)
                        if (yield from children(links, level, values, outcome)):
                            return
                    elif (yield from files(page, number, values, outcome, level)):
                        return
                    after = self.recipe.get("next") or {}
                    if not after.get("selector") or int(after.get("level", 0)) != level:
                        return
                    if after.get("kind") == "number":
                        later = driver.numbered(page, after["selector"], number)
                    else:
                        later = driver.next(page, after["selector"])
                    if later is None:
                        return
                    page = later
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

    def survey(
        self, files: int = SURVEY_FILES, pages: int = SURVEY_PAGES, begin: Callable[[], None] = lambda: None
    ) -> dict:
        """The recipe's test: walks the site as a run does (after begin: the steps), downloading
        nothing, and tells what it found: each listing page read with the links its selector and
        pattern matched, the first file links with their file names, and the problems met. It
        ends after that many files or pages, or when stopped (Stopped)."""
        result: dict = {"pages": [], "problems": [], "files": 0, "ended": "complete", "shot": None, "skipped": []}
        # An observer set before (a job's weekly test) hears of the pages too.
        outer = self.observe

        def observe(event: dict) -> str | None:
            if len(result["pages"]) >= pages:
                raise Stopped("pages")
            where = "the listing" if event["level"] == 0 else f"a level {event['level']} page"
            where += f" (page {event['page']}, {event['url']})"
            if not event["found"]:
                result["problems"].append(f"{where}: {event['selector']} matched nothing")
            elif not event["matching"]:
                result["problems"].append(f"{where}: none of its {event['found']} links matched the pattern")
            result["pages"].append(event | {"files": []})
            return outer(event)

        self.observe = observe
        items = self.items(lambda key: False)
        try:
            begin()
            for item in items:
                problem = str(item.problem) if item.problem else ""
                result["pages"][-1]["files"].append({"url": item.url, "path": item.path, "problem": problem})
                result["files"] += 1
                if result["files"] >= files:
                    result["ended"] = "files"
                    break
        except Stopped as stop:
            # "pages", "time" (the test's limits) or "stopped" (by the person).
            result["ended"] = str(stop) if str(stop) in ("pages", "time") else "stopped"
        except (SourceError, PlaywrightError) as error:
            result["ended"] = "failed"
            # Its kind: a page that asks for a person, or a refused sign-in, is no changed site.
            result["error"] = type(error).__name__
            result["problems"].append((str(error) or type(error).__name__).splitlines()[0])
        finally:
            # The walk's pages close; a test stopped meanwhile cannot close them (its browser
            # context goes as a whole).
            with contextlib.suppress(Exception):
                items.close()
            self.observe = outer
        result["shot"] = self.shot
        # The optional steps it went on without: listed apart, no problems of the recipe.
        result["skipped"] = list(self.driver.skipped if self.driver else [])
        return result

    def close(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver = None
