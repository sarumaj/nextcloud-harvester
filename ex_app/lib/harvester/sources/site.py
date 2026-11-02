"""Any website, described by a recipe (harvester.recipe): the generic source.

The pages are fetched over HTTP, or in Chromium when the recipe has steps (a login, a site that
needs JavaScript) or the job asks for the browser. From the start page the source follows the
recipe's levels of links, takes the file links on the last level, page after page, and
downloads each file from its download URL (a template of the link's values), with the listing
as Referer, or by clicking its element (click links). It waits when the site asks it to (429 or
503 with Retry-After, or a wait in the URL it redirects to), for its listing pages too, and
checks what it gets: never a web page instead of a file, of the expected type and size.

A kept session skips the sign-in (not the steps from there to the listing); when it no longer
reaches the listing, the run signs in once anew, in a browser of its own, and gives the session
up if that fails too. A listing page that gives nothing it should have (a sign-in, an error, a
redirect elsewhere) is logged, and the run is then no complete pass.

A level's links may carry values read from the page around each of them (harvester.patterns
.page_values), and the file links may be a JSON listing (harvester.jsonlist), read over HTTP with
the job's cookies (the browser's, when it has one) and walked page by page.
"""

import contextlib
import dataclasses
import datetime
import json
import mimetypes
import re
import string
import time
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route, WebSocketRoute, sync_playwright

from .. import browser
from .. import cookies as cookie_jar
from .. import jsonlist, otp, pagination, patterns, selectors
from .. import recipe as recipes
from ..browser.guard import Guard
from ..schedule import zone
from . import download, near
from .base import AuthError, ChallengeError, Item, Option, Source, SourceError, Stopped, pattern_search
from .download import Response
from .driver import Driver, HttpDriver, accept_language, moved
from .walk import StartEmpty, Walk

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
# A pause before each listing page after the first (s): a site's years and pages are read one
# after another, not as fast as the connection allows.
PAGE_PAUSE = 1.0
# Whether the page is a sign-in: a visible password or user name field, or a sign-in's address
# (a username-first sign-in asks for the name alone first; download.LOGIN_PATH alike).
LOGIN_SCRIPT = r"""() => {
  const shown = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
  const fields = 'input[type=password], input[autocomplete~=username], input[autocomplete~=current-password]'
  if ([...document.querySelectorAll(fields)].some(shown)) return true
  return /(^|\/)(log-?in|sign-?in|sso|auth|oauth2?|session\/new)(\/|$)/i.test(location.pathname)
}"""
# The elements a click link matches: their text, in the page's order.
TARGETS = "els => els.map(e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim())"


class BrowserDriver(Driver):  # pylint: disable=too-many-instance-attributes
    """Pages in Chromium: the steps replayed in the first tab, followed links in tabs of their own.

    With a kept session the sign-in steps (recipe.sign_in_steps) are skipped while the listing
    shows no sign-in; the steps from there to the listing always run. A step that fails on a
    page asking for a person raises ChallengeError (only then: a login page may well carry an
    invisible CAPTCHA that lets people through)."""

    # The listing the steps ended on is read in their tab, as they left it (scrolled to load
    # more, say), once; other pages open in tabs of their own.
    _start = ""
    _reused = False
    # What a sign-in anew needs: the steps, the account, its cookies (sign_in_again).
    _steps: list[dict] = []
    _secrets: dict[str, str] = {}
    _cookies: list[dict] = []
    # The job's time zone: the days of the dates a fill step fills in (recipe.dated).
    timezone = "UTC"

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
        viewport: dict | None = None,
        *,
        timezone: str = "UTC",
        downloads: bool = False,
    ):
        self.saw = saw
        self.timezone = timezone
        if not browser.SLOTS.acquire(timeout=LOCK_TIMEOUT, kind="job"):  # pylint: disable=consider-using-with
            raise SourceError("no browser came free (recordings use them all)")
        self._playwright = None
        self._browser = None
        # The downloads' proxy and user agent: the browser's (download).
        self.proxy = browser.PROXY if home else ""
        self.user_agent = ""
        self._steps, self._secrets, self._cookies = steps, secrets, cookies or []
        # Where each page was asked for and what it answered (trouble).
        self._answers: dict[int, tuple[str, int]] = {}
        # The browser's connections all go through its guard (with the home connection, if
        # chosen), unless the admin switched the guard off: then the routes alone check them.
        self._guard: Guard | None = None
        try:
            port = 0
            if browser.GUARD:
                self._guard = Guard(self.proxy)
                port = self._guard.start()
            self._playwright = sync_playwright().start()
            credentials = self._guard.credentials if self._guard else None
            self._browser = self._playwright.chromium.launch(**browser.launch_options(port, http1, home, credentials))
            self.user_agent = browser.user_agent(self._browser.version)
            # Downloads by the browser only for file links that are clicked: their requests pass
            # the context's routes and the guard as the pages' do. The window is the one the
            # recipe was recorded in (its steps and pages laid out alike).
            self.context = self._browser.new_context(
                **browser.context_options(self._browser.version, viewport),
                accept_downloads=downloads,
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

    def keep_shot(self, page: Any, what: str) -> None:
        """Keeps what the page shows, and tells saw."""
        self.shot = snapshot(page, what) or self.shot
        tell = self.saw
        if self.shot and tell is not None:
            tell(self.shot)  # pylint: disable=not-callable

    def today(self) -> datetime.date:
        """The day in the job's time zone."""
        return datetime.datetime.now(zone(self.timezone)).date()

    def sign_in(self, steps: list[dict], secrets: dict[str, str], session: dict | None, start: str) -> None:
        """Replays the steps, unless the kept session still reaches the listing: then only those
        after the sign-in (recipe.sign_in_steps), which lead to the listing (a tab, a filter, the
        scroll steps that load its later entries)."""
        todo = list(enumerate(steps, 1))
        self._start = start
        self.skipped = []
        self.repaired = []
        signing = recipes.sign_in_steps(steps)
        if session and start and signing:
            self.main.goto(start, wait_until="domcontentloaded")
            settle(self.main)
            if not self.signed_out(self.main):
                todo = todo[signing:]
        self._replay(self.main, todo, secrets)

    def _replay(self, page: Any, todo: list[tuple[int, dict]], secrets: dict[str, str]) -> None:
        """Does the numbered steps on the page: an optional one that fails is skipped (and noted),
        a required one fails the sign-in."""
        today = self.today()
        for number, step in todo:
            try:
                replay(page, dated_step(step, today), secrets, lambda old, new, n=number: self._repaired(n, old, new))
            except Stopped:
                # The run or the recipe's test was stopped during the step: no failure of it.
                raise
            except (PlaywrightError, FrameMissing) as error:
                if not step.get("optional"):
                    self._failed(number, step, error, page)
                # Best effort: no picture, no failure; the run says what it went on without.
                self.skipped.append({"number": number, "label": recipes.step_label(step), "why": short_error(error)})
            except Exception as error:  # pylint: disable=broad-exception-caught
                self._failed(number, step, error, page)

    def _repaired(self, number: int, old: str, new: str) -> None:
        self.repaired.append(selectors.repair_line(f"step {number}", old, new))

    def _failed(self, number: int, step: dict, error: Exception, page: Any = None) -> None:
        """A required step failed: the page asking for a person (ChallengeError), else SourceError
        naming the step, with a picture of what the browser showed."""
        page = self.main if page is None else page
        found = self.challenge(page)
        if found:
            raise ChallengeError(*found) from error
        why = browser.connection_refused(error) or str(error)
        what = f"step {number} ({recipes.step_label(step)}) failed"
        self.keep_shot(page, what)
        raise StepFailed(f"{what}: {why}") from error

    def signed_out(self, page: Any) -> bool:
        """Whether the page is a sign-in (LOGIN_SCRIPT)."""
        try:
            return bool(page.evaluate(LOGIN_SCRIPT))
        except Exception:  # pylint: disable=broad-exception-caught
            return False  # the page is gone or navigating

    def sign_in_again(self) -> bool:
        """Signs in anew in a tab of its own, from no cookies but the account's: the session's
        have ended. The pages open stay; those opened later, and the downloads, have the new
        session."""
        if not recipes.sign_in_steps(self._steps):
            return False
        self.context.clear_cookies()
        if self._cookies:
            self.context.add_cookies(self._cookies)  # type: ignore[arg-type]
        page = self.context.new_page()
        try:
            self._replay(page, list(enumerate(self._steps, 1)), self._secrets)
        finally:
            page.close()
        return True

    def challenge(self, page: Any) -> tuple[str, str] | None:
        try:
            kind = page.evaluate(browser.CHALLENGE_SCRIPT)
        except Exception:  # pylint: disable=broad-exception-caught
            return None  # the page is gone or navigating
        if not kind:
            return None
        self.keep_shot(page, f"the page asks for a person ({kind})")
        return page.url, kind

    def trouble(self, page: Any) -> str:
        asked, status = self.__dict__.get("_answers", {}).get(id(page), ("", 200))
        if status >= 400:
            return f"answered {status}"
        if self.signed_out(page):
            return "a sign-in page"
        return f"redirected to {page.url}" if moved(asked, page.url) else ""

    def session(self) -> dict | None:
        return self.context.storage_state()  # type: ignore[return-value]

    def open(self, url: str | None, deep: bool = False) -> Any:
        if url is None:
            return self.main
        if not self._reused and url == self._start and self.main.url.split("#")[0] == url.split("#")[0]:
            self._reused = True
            return self.main
        page = self.context.new_page()
        try:
            answer = page.goto(url, wait_until="domcontentloaded")
        except Exception as error:
            why = browser.connection_refused(error)
            if why:
                raise SourceError(f"{url}: {why}") from error
            raise
        self.__dict__.setdefault("_answers", {})[id(page)] = (url, answer.status if answer else 200)
        settle(page)
        return page

    def expand(self, page: Any, spec: dict) -> None:
        if "scroll" in spec:
            scroll_to_load(page, int(spec["scroll"]))
        if spec.get("load_more"):
            load_more(page, spec["load_more"])

    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        return page.eval_on_selector_all(selector, "els => els.map(e => [e.href || '', (e.innerText || '').trim()])")

    def valued_links(self, page: Any, selector: str, specs: list[dict]) -> list[tuple[str, str, list]]:
        return [tuple(link) for link in page.eval_on_selector_all(selector, near.VALUES, specs)]

    def targets(self, page: Any, selector: str) -> list[str]:
        return page.eval_on_selector_all(selector, TARGETS)

    def click_download(self, trigger: tuple, dest: BinaryIO, settings: dict) -> str:
        page, selector, index, text = trigger
        elements = page.query_selector_all(selector)
        texts = page.eval_on_selector_all(selector, TARGETS)
        # The element at its place, unless the page changed meanwhile: then the one of its text.
        if index >= len(texts) or texts[index] != text:
            index = texts.index(text) if text in texts else -1
        if index < 0:
            raise SourceError(f"the element to click for “{text}” ({selector}) is gone from the page")
        try:
            with page.expect_download(timeout=STEP_TIMEOUT) as started:
                elements[index].click()
            got = started.value
            path = got.path()
        except PlaywrightError as error:
            raise SourceError(f"clicking “{text}” ({selector}) downloaded nothing: {short_error(error)}") from error
        try:
            name = download.safe_name(got.suggested_filename) or "download"
            download.copy_file(str(path), dest, settings, mimetypes.guess_type(name)[0] or "")
        finally:
            with contextlib.suppress(PlaywrightError):
                got.delete()
        return name

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
        self.__dict__.get("_answers", {}).pop(id(page), None)
        if page is not self.main:
            page.close()

    @contextlib.contextmanager
    def download(self, url: str) -> Iterator[Response]:
        # Over HTTP with the browser's cookies, user agent, language and proxy, not with the
        # context's own requests (context.request), which pass by its routes: the guarded client
        # checks every request and redirect on the addresses the names resolve to, and the file
        # comes in chunks, never whole into memory.
        hooks = {"request": [session_cookies(self.context.storage_state()["cookies"])]}
        options = {"proxy": self.proxy} if self.proxy else {}
        headers = {"User-Agent": self.user_agent, "Accept-Language": accept_language(browser.LOCALE)}
        with download.guarded_client(event_hooks=hooks, headers=headers, **options) as client:
            with download.http(client, {"Referer": self.referer} if self.referer else None)(url) as response:
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
        settle(page)
        still = still + 1 if page.evaluate(PAGE_SIZE) == before else 0
        if still >= SCROLL_STILL or time.monotonic() > deadline:
            return


def dated_step(step: dict, today: datetime.date) -> dict:
    """The step with the dates in its value filled in for today (recipe.dated)."""
    value = step.get("value")
    return step | {"value": recipes.dated(value, today)} if isinstance(value, str) and "{" in value else step


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


class StepFailed(SourceError):
    """A required step of the steps failed (not on a page asking for a person)."""


def names_in(template: str) -> set[str]:
    """The values a template names ({year}, {issue:02d}: year, issue)."""
    try:
        return {name.split(".")[0].split("[")[0] for _, name, _, _ in string.Formatter().parse(template) if name}
    except ValueError:
        return set()


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


def replay(page, step: dict, secrets: dict[str, str], repaired: Callable[[str, str], None] | None = None) -> None:
    """Does one recorded step, in its frame if it has one; an optional step waits for its frame
    and element OPTIONAL_TIMEOUT at most (a required one the page's default, STEP_TIMEOUT). A
    step with alternative selectors finds its element by the first that does (selectors.resolve,
    an alternative told to repaired) within the same time."""
    action = step["action"]
    started, began = time.monotonic(), time.time()
    budget = OPTIONAL_TIMEOUT if step.get("optional") else STEP_TIMEOUT
    # A step without a frame acts on the page as it always did.
    target = in_frame(page, step["frame"], budget) if step.get("frame") else page
    # What is left of an optional step's time for its element (never 0: Playwright's "no limit").
    left = max(1.0, budget - (time.monotonic() - started) * 1000)
    wait = {"timeout": left} if step.get("optional") else {}
    selector = step.get("selector", "")
    if action in recipes.ON_ELEMENTS and step.get("alternatives"):
        selector = selectors.resolve(target, step, left, repaired)
        wait = {"timeout": max(1.0, budget - (time.monotonic() - started) * 1000)}
    if action == "goto":
        page.goto(step["url"], wait_until="domcontentloaded")
    elif action == "fill":
        target.fill(selector, filled(step, secrets), **wait)
        return
    elif action == "click":
        target.click(selector, **wait)
    elif action == "press":
        target.press(selector, step.get("key", "Enter"), **wait)
    elif action == "mail-code":
        # The message first, then the field: the page may show the field only once it is due.
        code = near.mail_code(page, step, secrets, began)
        target.fill(selector, code, **wait)
        return
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
    credentials = ("username", "password", "totp", "cookies", "code_mailbox")
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
        "code_mailbox": (
            "Mailbox for codes",
            "For a sign-in that sends a code by e-mail (the step “type the code from e-mail”): the IMAP mailbox it "
            "arrives in, one setting per line: host = mail.example.org, user = me@example.org, password = …; also "
            "port (993), folder (INBOX), and auth = oauth2-google, oauth2-microsoft or oauth2 with token_url, "
            "client_id, client_secret, refresh_token. Without user and password, the job's Username and Password "
            "sign in. Read-only: nothing is marked as read.",
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
            "the pattern's groups, e.g. {year}/{text}; {disposition}: the name the site gives the file as it "
            "sends it.",
            kind="template",
            values=("filename", "stem", "ext", "text", "page", "host", "url", "disposition"),
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
        # The picks whose links an alternative selector found in this run (each said once).
        self.repairs: list[str] = []
        # The pause before each listing page after the first (the recipe's test has none).
        self.page_pause = PAGE_PAUSE

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
            try:
                self.driver = self._start_driver()
            except StepFailed:
                # A kept session that no longer signs in (a sign-in that changed its steps for
                # it, a step after it that finds the session's page different): once anew, from
                # no session; given up whether or not that works.
                if not self.kept or not self.recipe.get("steps"):
                    raise
                self._give_up_session()
                self.driver = self._start_driver()
            for skipped in getattr(self.driver, "skipped", []):
                self.report("info", skipped_line(skipped))
            for line in getattr(self.driver, "repaired", []):
                self.report("info", line)
        return self.driver

    def _start_driver(self) -> Driver:
        start = self.recipe.get("start") or ""
        found = self.account_cookies(start)
        if recipes.uses_browser(self.recipe) or int(self.settings["browser"]):
            links = self.recipe.get("links") or {}
            return BrowserDriver(
                self.recipe.get("steps") or [],
                self.secrets,
                self.kept,
                start,
                cookie_jar.to_playwright(found),
                http1=bool(int(self.settings["http1"])),
                home=self.home,
                saw=self._saw,
                viewport=self.recipe.get("viewport"),
                timezone=self.timezone,
                downloads=bool(links.get("click")),
            )
        # Without recorded steps, the account's user name and password are HTTP Basic's.
        self.sign_client(start)
        return HttpDriver(self.client, self.kept, self.sleep)

    def _give_up_session(self) -> None:
        """Forgets the kept session for this run (and, unless the run keeps a new one, for the
        next: stale), and the driver that used it."""
        self.report("info", "The kept session no longer reaches the listing: signing in anew")
        self.kept = None
        self.stale = True
        if self.driver is not None:
            self.driver.close()
            self.driver = None
        if self.client is not None:
            self.client.cookies.clear()

    def sign_in_again(self) -> bool:
        return self.driver.sign_in_again() if self.driver else False

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        try:
            yield from self._walk(seen)
        except StartEmpty:
            # Signed out with a kept session (one that expired, a username-first sign-in the
            # password check took for the listing): once anew, from no session.
            if not self.kept or self.stale:
                raise
            self._give_up_session()
            try:
                yield from self._walk(seen)
            except StartEmpty:
                # The session the new sign-in made reaches no listing either: none is kept.
                if self.driver is not None:
                    self.driver.close()
                    self.driver = None
                raise

    def _walk(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        yield from Walk(self, self._driver(), seen).items()

    def listed(self, driver: Driver, page: Any, spec: dict, what: str) -> tuple[list, dict]:
        """A level's links on the page (address, text and, where the level has values, their
        texts; _links), and where they were read: a page's address and the selector, or a JSON
        listing's address and its items' path."""
        if isinstance(page, jsonlist.JsonPage):
            return jsonlist.entries(page, spec), {"url": page.url, "selector": spec.get("items") or "(the answer)"}
        # The values' texts only where the level has values: other recipes read as before.
        specs = spec.get("values") or []
        read = (lambda selector: driver.valued_links(page, selector, specs)) if specs else None
        return self._links(driver, page, spec, what, read), {"url": driver.address(page), "selector": spec["selector"]}

    def _repaired(self, what: str, old: str, new: str) -> None:
        line = selectors.repair_line(what, old, new)
        if line not in self.repairs:
            self.repairs.append(line)
            self.report("info", line)

    def _links(
        self, driver: Driver, page: Any, spec: dict, what: str, read: Callable[[str], list] | None = None
    ) -> list[tuple]:
        """A pick's links on the page: its selector's, else (none of them matching its pattern)
        the first alternative's of which the pattern matches any (a repair, said once). read
        reads a selector's links (with the values' texts, say); driver.links without."""
        read = read or (lambda selector: driver.links(page, selector))
        tries = selectors.candidates(spec)
        links = read(tries[0])
        pattern = spec.get("pattern") or ""
        if len(tries) < 2 or any(url and pattern_search(pattern, url) for url, *_ in links):
            return links
        for alternative in tries[1:]:
            try:
                found = read(alternative)
            except selectors.SELECTOR_ERRORS:
                continue  # an engine this driver lacks (the browser's, over HTTP)
            if any(url and pattern_search(pattern, url) for url, *_ in found):
                self._repaired(what, tries[0], alternative)
                return found
        return links

    def notice(self, page: Any, what: str) -> None:
        """Keeps a picture of the page where the observer saw something go wrong (noticed)."""
        self.noticed = snapshot(page, what)

    def next_page(self, driver: Driver, page: Any, after: dict) -> Any:
        """The page after this one by the next page's selector, else (not on the page) by the
        first alternative that is (a repair, said once); None at the last page."""
        tries = selectors.candidates(after)
        if len(tries) > 1 and not self._finds(driver, page, tries[0]):
            for alternative in tries[1:]:
                if self._finds(driver, page, alternative):
                    self._repaired("next page", tries[0], alternative)
                    return driver.next(page, alternative)
        return driver.next(page, tries[0])

    @staticmethod
    def _finds(driver: Driver, page: Any, selector: str) -> bool:
        try:
            return bool(driver.links(page, selector))
        except selectors.SELECTOR_ERRORS:
            return False

    def item(self, link: str, text: str, page: int, groups: dict, referer: str = "") -> Item:
        """The file of a link: its download URL and path from the link's values (an item that
        fails alone when they cannot be made). A File name with {disposition} is the answer's
        name: a stand-in (the link's file name) until the download (fetch)."""
        values = patterns.link_values(link, text, page) | groups
        template = str(self.settings["path"])
        pending = "disposition" in names_in(template)
        key = patterns.stable_url(link)
        try:
            url = patterns.render(str(self.settings["download_url"]), values)
            stand_in = values | {"disposition": values["filename"]} if pending else values
            path = patterns.render_path(template, stand_in)
        except (KeyError, ValueError) as error:
            problem = SourceError(str(error).strip("'\""))
            return Item(key=key, path="", url=link, values=values, problem=problem, referer=referer)
        path = download.with_extension(path, self.settings)
        return Item(key=key, path=path, url=url, values=values, referer=referer, pending=pending)

    def fetch(self, item: Item, dest: BinaryIO) -> Item | None:
        driver = self._driver()
        answer: dict[str, str] = {}
        if item.trigger is not None:
            answer["name"] = driver.click_download(item.trigger, dest, self.settings)
        else:

            def told(response: Response) -> None:
                answer["name"] = download.disposition_name(response.headers, response.url)

            driver.referer = item.referer
            try:
                download.fetch(driver.download, item.url, dest, self.settings, self.sleep, told=told)
            finally:
                driver.referer = ""
        if not item.pending:
            return None
        values = item.values | {"disposition": answer.get("name") or "download"}
        path = download.with_extension(patterns.render_path(str(self.settings["path"]), values), self.settings)
        return dataclasses.replace(item, path=path, values=values, pending=False)

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
        self.page_pause = 0.0
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
        # The selectors alternatives stood in for (the recipe unchanged).
        result["repaired"] = [*(self.driver.repaired if self.driver else []), *self.repairs]
        return result

    def close(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver = None
