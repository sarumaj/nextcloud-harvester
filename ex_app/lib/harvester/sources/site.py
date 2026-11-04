# SPDX-License-Identifier: AGPL-3.0-or-later
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
import functools
import json
import mimetypes
import string
import time
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

from playwright.sync_api import Error as PlaywrightError, Route, WebSocketRoute

from .. import browser
from .. import cookies as cookie_jar
from .. import choices, images, jsonlist, otp, pagination, patterns, selectors
from .. import recipe as recipes
from ..l10n import said
from ..browser import consent as banners, engine
from ..browser.guard import Guard
from ..schedule import zone
from . import attend, choose, clicks, download, keep, near, siteoptions, survey
from .base import AuthError, ChallengeError, Item, Source, SourceError, Stopped, nap
from .base import pattern_search
from .download import Response
from .driver import Driver, HttpDriver, accept_language, moved
from .survey import SURVEY_FILES, SURVEY_PAGES
from .walk import StartEmpty, Walk
from .tabs import (
    FrameMissing,
    PopupMissing,
    Tabs,
    arrived,
    found_elements,
    in_frame,
    listing_shown,
    load_more,
    scroll_to_load,
    settle,
    short_error,
    step_event,
    step_line,
    wait_until_shown,
    with_tries,
)

# How long a browser step may take (ms), and how long a job waits for the browser (s).
STEP_TIMEOUT = 30_000
# How long an optional step waits for its frame and element together (ms): a banner that shows
# comes at once with its page, and the run should not wait half a minute for one that does not.
OPTIONAL_TIMEOUT = 5_000
LOCK_TIMEOUT = 1800
# What a light browser (the job's option) never loads: no walk reads them (an image link's address
# is its element's attributes, harvester.images), and each would pass the routes uncached.
LIGHT = frozenset({"image", "font", "media"})
# What the browser showed when a step failed or a page asked for a person: a JPEG of the window
# at this quality, again at the lower one when larger than SHOT_BYTES (else none is kept).
SHOT_QUALITY = (60, 35)
SHOT_BYTES = 400_000
OUTER_HTML = "s => { const e = document.querySelector(s); return e ? e.outerHTML : '' }"
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


class BrowserDriver(Driver):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Pages in Chromium: the steps replayed in the first tab, followed links in tabs of their own.

    With a kept session the sign-in steps (recipe.sign_in_steps) are skipped while the listing
    shows no sign-in; the steps from there to the listing always run. A step that fails on a
    page asking for a person raises ChallengeError (only then: a login page may well carry an
    invisible CAPTCHA that lets people through)."""

    # The listing the steps ended on is read in their tab, as they left it (scrolled to load
    # more, say), once; other pages open in tabs of their own.
    _start = ""
    _reused = False
    # The step "each" the steps stop before, and the steps after it (choose.each).
    chooser: choose.Chooser | None = None
    # What a sign-in anew needs: the steps, the account, its cookies (sign_in_again).
    _steps: list[dict] = []
    _secrets: dict[str, str] = {}
    _cookies: list[dict] = []
    # The job's time zone: the days of the dates a fill step fills in (recipe.dated).
    timezone = "UTC"
    # Whether its downloads (by the guarded client) impersonate a browser (base.IMPERSONATE).
    impersonate = False
    # A step's pause and its retries' delays: waits that a stop ends at once (base.nap: the run's
    # stop; the recipe's test's own in the recorder).
    nap: Callable[[float], None] = staticmethod(nap)
    # Told of each step as it is played (tabs.step_event): the recorder's test shows it on its
    # Steps, a job's run logs its pauses and retries.
    told: Callable[[dict], None] | None = None
    # Whether cookie banners are dismissed on their own (consent.py): a step's banner may be gone;
    # told of each page the run is done with, for its dismissals (consent.Told).
    consent = False
    dismissed: Callable[[Any], None] | None = None

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
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
        stealth: bool = False,
        told: Callable[[dict], None] | None = None,
        consent: bool = False,
        dismissed: Callable[[Any], None] | None = None,
        light: bool = False,
    ):
        self.saw = saw
        self.told = told
        self.consent, self.dismissed = consent, dismissed
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
            self._playwright = engine.start(stealth, port)  # Playwright, or Patchright disguised
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
            engine.guard_routes(self.context, light_guard if light else guard, socket_guard)
            # The account's cookies over the kept session's: the user gave them for this job.
            if cookies:
                self.context.add_cookies(cookies)  # type: ignore[arg-type]
            self.context.set_default_timeout(STEP_TIMEOUT)
            if consent:
                self.context.add_init_script(banners.SCRIPT)
            self.main = self.context.new_page()
            self.sign_in(steps, secrets, session, start)
            if dismissed is not None:
                dismissed(self.main)
        except BaseException as error:
            # Whatever failed, the browser closes and its lock is free for the next job; a page
            # handed over to the person keeps its session, for them to go on in.
            if isinstance(error, attend.HandedOver):
                with contextlib.suppress(PlaywrightError):
                    error.session = self.context.storage_state()  # type: ignore[assignment]
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
        # The steps from a step "each" on are done per choice (choose.each: replay_rest).
        cut = choices.each_index(steps)
        if cut is not None:
            self.chooser = choose.Chooser(cut + 1, steps[cut], todo[cut + 1 :])
            todo = todo[:cut]
        signing = recipes.sign_in_steps(steps)
        if session and start and signing:
            self.main.goto(start, wait_until="domcontentloaded")
            settle(self.main, pause=0)
            if not self.signed_out(self.main):
                for number, step in todo[:signing]:
                    self._tell(step_event(number, step, "session"))
                todo = todo[signing:]
        self._replay(self.main, todo, secrets)

    def replay_rest(self, todo: list[tuple[int, dict]]) -> None:
        """The numbered steps after a choice, in the first tab; the listing is read where they end."""
        self._replay(self.main, todo, self._secrets)
        self._reused = False

    def _tell(self, event: dict) -> None:
        tell = self.told
        if tell is not None:
            tell(event)  # pylint: disable=not-callable

    def _replay(self, page: Any, todo: list[tuple[int, dict]], secrets: dict[str, str]) -> None:
        """Does the numbered steps from the page (in the tabs they open: a sign-in's popup), each
        after its pause and tried again as its retry says: an optional one that fails is skipped
        (and noted), a required one fails the sign-in."""
        today = self.today()
        tabs = Tabs(page)
        for index, (number, step) in enumerate(todo):
            step = banners.relaxed(step) if self.consent else step
            try:
                # A popup comes from the step before the one that goes on in it: the tabs open then.
                if index + 1 < len(todo) and todo[index + 1][1].get("action") == "popup":
                    tabs.opening()
                play = functools.partial(self._play, tabs, dated_step(step, today), secrets, number)
                with_tries(play, number, step, self.nap, self._tell)
            except (Stopped, ChallengeError, attend.Unanswered) as error:
                # Stopped during the step, the page asks for a person, nobody answered in time: no
                # failure of the step's (no retry, no sign-in anew).
                if not isinstance(error, Stopped):
                    self._tell(step_event(number, step, "failed", why=short_error(error)))
                raise
            except (PlaywrightError, FrameMissing, PopupMissing) as error:
                if not step.get("optional"):
                    self._tell(step_event(number, step, "failed", why=short_error(error)))
                    self._failed(number, step, error, page)
                # Best effort: no picture, no failure; the run says what it went on without.
                self.skipped.append({"number": number, "label": recipes.step_label(step), "why": short_error(error)})
                self._tell(step_event(number, step, "skipped", why=short_error(error)))
            except Exception as error:  # pylint: disable=broad-exception-caught
                self._tell(step_event(number, step, "failed", why=short_error(error)))
                self._failed(number, step, error, page)
            else:
                self._tell(step_event(number, step, "passed"))

    def _play(self, tabs: Tabs, step: dict, secrets: dict[str, str], number: int) -> None:
        """One try of the step (with its dates filled in), in the tab it acts in."""
        if not tabs.switch(step, OPTIONAL_TIMEOUT if step.get("optional") else STEP_TIMEOUT):
            replay(tabs.page, step, secrets, functools.partial(self._repaired, number))

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
        cut = choices.each_index(self._steps)
        try:
            self._replay(page, list(enumerate(self._steps, 1))[:cut], self._secrets)
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

    def open(self, url: str | None, deep: bool = False, awaits: tuple[str, ...] = ()) -> Any:
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
        listing_shown(page, awaits)
        return page

    def expand(self, page: Any, spec: dict) -> None:
        if spec.get("wait_for"):
            wait_until_shown(page, spec["wait_for"])
        if "scroll" in spec:
            scroll_to_load(page, int(spec["scroll"]))
        if spec.get("load_more"):
            load_more(page, spec["load_more"])

    def links(self, page: Any, selector: str) -> list[tuple[str, str]]:
        # An image's address is the one of its full size (harvester.images).
        return images.resolved(page.eval_on_selector_all(selector, images.LINKS))

    def valued_links(self, page: Any, selector: str, specs: list[dict]) -> list[tuple[str, str, list]]:
        found = page.eval_on_selector_all(selector, near.VALUES, specs)
        return [(*images.link(href, text, info), values) for href, text, values, info in found]

    def targets(self, page: Any, selector: str) -> list[str]:
        return page.eval_on_selector_all(selector, clicks.TARGETS)

    def click_download(self, trigger: tuple, dest: BinaryIO, settings: dict) -> str:
        # The element at its place, unless the page changed meanwhile: then the one of its text.
        page, selector, index, text = trigger
        what = f"clicking “{text}” ({selector})"
        try:
            with clicks.clicked_file(page, clicks.element_at(page, selector, index, text), what, STEP_TIMEOUT) as came:
                kind, got = came
                if kind == "address":
                    return self._shown_file(got, dest, settings, self.address(page))
                path = got.path()
                try:
                    name = download.safe_name(got.suggested_filename) or "download"
                    download.copy_file(str(path), dest, settings, mimetypes.guess_type(name)[0] or "")
                finally:
                    with contextlib.suppress(PlaywrightError):
                        got.delete()
                return name
        except PlaywrightError as error:
            raise SourceError(f"{what} downloaded nothing: {short_error(error)}") from error

    def _shown_file(self, url: str, dest: BinaryIO, settings: dict, listing: str) -> str:
        """The file a tab showed after a click (a PDF in the viewer), fetched by its address with
        the browser's cookies and the listing as Referer, checked as a download is; its name."""
        answer: dict[str, str] = {}

        def told(response: Response) -> None:
            answer["name"] = download.disposition_name(response.headers, response.url)

        self.referer = listing
        try:
            download.fetch(self.download, url, dest, settings, self.nap, told=told)
        finally:
            self.referer = ""
        return answer.get("name") or "download"

    def keep_page(self, where: Any, dest: BinaryIO, settings: dict, spec: dict) -> tuple[str, str]:
        return keep.save(self, where, dest, settings, spec, keep.saved_as(spec), STEP_TIMEOUT)

    def capture(self, where: Any, settings: dict, spec: dict) -> tuple[list, str, str]:
        # A file the page embeds is fetched as a file link's is: the browser's cookies, the page
        # as Referer.
        return keep.capture(self, where, settings, spec, STEP_TIMEOUT, keep.fetcher(self.download, settings))

    def open_clicked(self, target: clicks.Target) -> Any:
        landing = clicks.land(target, STEP_TIMEOUT)
        self.__dict__.setdefault("_landings", []).append(landing)
        return landing.page

    def next(self, page: Any, selector: str, awaits: tuple[str, ...] = ()) -> Any:
        link = page.query_selector(selector)
        if not link:
            return None
        clicked(page, link.click, awaits)
        return page

    def numbered(self, page: Any, selector: str, counted: int, awaits: tuple[str, ...] = ()) -> Any:
        found = pagination.following(page.evaluate(OUTER_HTML, selector), page.url, counted)
        if not found or not page.evaluate(pagination.MARK_NUMBER, [selector, str(found[0])]):
            return None
        clicked(page, lambda: page.click("[data-harvester-next]"), awaits)
        return page

    def address(self, page: Any) -> str:
        return page.url

    def done(self, page: Any) -> None:
        if self.dismissed is not None:
            self.dismissed(page)  # pylint: disable=not-callable
        # A page a click landed on: back to the listing it was clicked on (the newest first).
        landings = self.__dict__.get("_landings", [])
        if landings and landings[-1].page is page:
            clicks.back(landings.pop(), self.expand)
            return
        self.__dict__.get("_answers", {}).pop(id(page), None)
        if page is not self.main:
            page.close()

    def here(self) -> str:
        return self.main.url

    def cookie_value(self, url: str, name: str) -> str | None:
        return next((cookie["value"] for cookie in self.context.cookies(url) if cookie["name"] == name), None)

    def page_value(self, page: Any, kind: str, name: str) -> str | None:
        return page.evaluate(near.PAGE_VALUE, [kind, name])

    @contextlib.contextmanager
    def download(
        self, url: str, *, method: str = "GET", content: bytes | None = None, headers: dict | None = None
    ) -> Iterator[Response]:
        # Over HTTP with the browser's cookies, user agent, language and proxy, not with the
        # context's own requests (context.request), which pass by its routes: the guarded client
        # checks every request and redirect on the addresses the names resolve to, and the file
        # comes in chunks, never whole into memory.
        client = self._client()
        # Each download starts from the browser's cookies alone, as with a client of its own: what
        # an answer set before stays out (a client's jar would send it to subdomains too).
        client.cookies.clear()
        sent = ({"Referer": self.referer} if self.referer else {}) | (headers or {})
        with download.http(client, sent or None, method, content)(url) as response:
            yield response

    def _client(self) -> Any:
        """The guarded client of the driver's downloads, made at the first and kept for the others
        (its connections too), anew when the impersonation changes; each request gets the
        browser's cookies for its address as it is sent (cookies.browser_cookies)."""
        made = self.__dict__.get("_downloads")
        if made is not None and made[0] == self.impersonate:
            return made[1]
        self.close_client()
        hooks = {"request": [cookie_jar.browser_cookies(self.context.cookies)]}
        options = {"proxy": self.proxy} if self.proxy else {}
        own = {"User-Agent": self.user_agent, "Accept-Language": accept_language(browser.LOCALE)}
        client = download.guarded_client(
            **download.impersonation(self.impersonate), event_hooks=hooks, headers=own, **options
        )
        self.__dict__["_downloads"] = (self.impersonate, client)
        return client

    def close_client(self) -> None:
        """Closes the downloads' client, if one was made."""
        made = self.__dict__.pop("_downloads", None)
        if made is not None:
            made[1].close()

    def close(self) -> None:
        try:
            self.close_client()
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


def clicked(page: Any, click: Callable[[], None], awaits: tuple[str, ...]) -> None:
    """Clicks to the listing's next page and waits for it: until what the level's selectors
    (awaits) find changed (tabs.listing_shown), else (none) for the network to go quiet."""
    if not awaits:
        click()
        settle(page)
        return
    before = found_elements(page, awaits)
    click()
    listing_shown(page, awaits, before)


def skipped_line(skipped: dict) -> str:
    """The progress log's line of an optional step the run went on without (a said() text)."""
    values = {"number": skipped["number"], "label": skipped["label"], "why": str(skipped["why"])}
    return said("step {number} ({label}, optional) skipped: {why}", **values)


def guard(route: Route) -> None:
    """Lets the browser load only what it may open (harvester.browser.allowed_url)."""
    if browser.reachable(route.request.url):
        route.continue_()
    else:
        route.abort("blockedbyclient")


def light_guard(route: Route) -> None:
    """guard, for a light browser: images, fonts and videos (LIGHT) never loaded."""
    if route.request.resource_type in LIGHT:
        route.abort("blockedbyclient")
    else:
        guard(route)


def socket_guard(route: WebSocketRoute) -> None:
    """Lets a page's WebSocket connect only where the browser may go (the routes do not see
    them); another is never connected: the page's socket opens to nothing (Playwright's stand-in,
    which a close from the handler would leave waiting)."""
    if browser.reachable(browser.http_address(route.url)):
        route.connect_to_server()


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


def dated_step(step: dict, today: datetime.date) -> dict:
    """The step with the dates in its value filled in for today (recipe.dated)."""
    value = step.get("value")
    return step | {"value": recipes.dated(value, today)} if isinstance(value, str) and "{" in value else step


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


class StepFailed(SourceError):
    """A required step of the steps failed (not on a page asking for a person)."""


def names_in(template: str) -> set[str]:
    """The values a template names ({year}, {issue:02d}: year, issue)."""
    try:
        return {name.split(".")[0].split("[")[0] for _, name, _, _ in string.Formatter().parse(template) if name}
    except ValueError:
        return set()


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
    elif action == "select":
        # By its value, else by the text it shows (whichever the list has first).
        chosen = {key: str(step[key]) for key in ("value", "label") if step.get(key) not in (None, "")}
        target.select_option(selector, **chosen, **wait)
    elif action == "hover":
        target.hover(selector, **wait)
        return
    elif action == "click":
        try:
            target.click(selector, **wait)
        except PlaywrightError:
            # A click that closes its own window (a sign-in popup's Allow, window.close()) may
            # end before Playwright hears back from the page: done, the next step goes back to
            # the opener.
            if not page.is_closed():
                raise
            return
    elif action == "press":
        target.press(selector, step.get("key", "Enter"), **wait)
    elif action == "mail-code":
        # The message first, then the field: the page may show the field only once it is due.
        code = near.mail_code(page, step, secrets, began)
        target.fill(selector, code, **wait)
        return
    elif action in attend.needs.ACTIONS:
        attend.play(page, target, selector, step, wait)
        return
    elif action == "scroll":
        scroll_to_load(page, int(step.get("rounds") or recipes.SCROLL_ROUNDS))
        return
    arrived(page, step, max(1.0, budget - (time.monotonic() - started) * 1000))


class Site(Source):  # pylint: disable=too-many-instance-attributes
    """A website described by a recipe."""

    name = "site"
    title = "Website (recipe)"
    description = (
        "Files linked from a website's pages: record it in the browser (sign in if need be, then pick the links to "
        "follow and the file links) or start from a template."
    )
    credentials = siteoptions.CREDENTIALS
    credential_help = siteoptions.CREDENTIAL_HELP
    options = siteoptions.OPTIONS
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
        problems += download.problems(self.settings) + keep.problems(self.recipe, self.settings)
        if problems:
            raise SourceError("; ".join(problems))
        self.driver: Driver | None = None
        self.kept: dict | None = None
        # The session a step handed over to the person in (attend.HandedOver), kept for them.
        self.handed: dict | None = None
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
        # The walk lists the pages to capture files of without opening them (the recipe's test).
        self.listing_only = False
        # What the pages of the file links, or the start page, are saved as ("": downloaded).
        self.saving = keep.saved_as(self.recipe) or keep.saved_as(self.recipe.get("links") or {})
        # The texts of the choices the walk went through (a step "each"; choose.each).
        self.choices: list[str] = []
        # Whether pages are kept (saved, or their files captured): {title} is their title.
        self.keeps = bool(self.saving or (self.recipe.get("links") or {}).get("capture"))

    def _saw(self, shot: dict) -> None:
        self._shot = shot

    def _step_told(self, event: dict) -> None:
        """A step's pause or retry in the run's log (step_line)."""
        line = step_line(event)
        if line:
            self.report("info", line)

    @property
    def shot(self) -> dict | None:
        """What the browser showed when a step failed or a page asked for a person, if it did."""
        return self._shot or (self.driver.shot if self.driver else None)

    def restore(self, session: dict) -> None:
        self.kept = session

    def session(self) -> dict | None:
        return self.driver.session() if self.driver else self.handed

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
            try:
                driver = BrowserDriver(
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
                    stealth=bool(int(self.settings["stealth"])),
                    told=self._step_told,
                    consent=bool(int(self.settings["consent"])),
                    # Once a manager a run, through a sign-in anew too.
                    dismissed=self.__dict__.setdefault("_banners", banners.Told(self.report)),
                    # Never for a disguised browser (a page that lacks its images looks automated),
                    # nor where pages are kept: saved as they look, or their files captured.
                    light=bool(int(self.settings["light"])) and not int(self.settings["stealth"]) and not self.keeps,
                )
            except attend.HandedOver as error:
                self.handed = error.session
                raise
            # Its own downloads (the guarded client's) as the job asks; the browser is a browser.
            driver.impersonate = download.impersonates(self.settings)
            return driver
        # Without recorded steps, the account's user name and password are HTTP Basic's.
        self.sign_client(start)
        return HttpDriver(self.client, self.kept, self.sleep)

    def _give_up_session(self) -> None:
        """Forgets the kept session for this run (and, unless the run keeps a new one, for the
        next: stale), and the driver that used it."""
        self.report("info", said("The kept session no longer reaches the listing: signing in anew"))
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
        driver = self._driver()
        for number, values in enumerate(choose.each(self, driver, STEP_TIMEOUT)):
            try:
                yield from Walk(self, driver, seen, values).items()
            except StartEmpty:
                # The first choice's empty listing may be a sign-in; a later one's is its own.
                if number == 0:
                    raise
                self.report(
                    "info", said("“{choice}”: the listing has none of the recipe's links", choice=values["choice"])
                )

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

    def next_page(self, driver: Driver, page: Any, after: dict, awaits: tuple[str, ...] = ()) -> Any:
        """The page after this one by the next page's selector, else (not on the page) by the
        first alternative that is (a repair, said once); None at the last page. awaits: what the
        listing's level lists, which the browser waits for to change (tabs.listing_shown)."""
        tries = selectors.candidates(after)
        if len(tries) > 1 and not self._finds(driver, page, tries[0]):
            for alternative in tries[1:]:
                if self._finds(driver, page, alternative):
                    self._repaired("next page", tries[0], alternative)
                    return driver.next(page, alternative, awaits)
        return driver.next(page, tries[0], awaits)

    @staticmethod
    def _finds(driver: Driver, page: Any, selector: str) -> bool:
        try:
            return bool(driver.links(page, selector))
        except selectors.SELECTOR_ERRORS:
            return False

    def item(self, link: str, text: str, page: int, groups: dict, referer: str = "") -> Item:
        """The file of a link: its download URL and path from the link's values (an item that
        fails alone when they cannot be made). A File name with {disposition} is the answer's
        name, or with {title} a saved page's title: a stand-in (the link's file name, its text)
        until the download (fetch)."""
        values = patterns.link_values(link, text, page) | groups
        template = str(self.settings["path"])
        pending = bool(names_in(template) & ({"disposition", "title"} if self.keeps else {"disposition"}))
        key = patterns.stable_url(link)
        try:
            url = patterns.render(str(self.settings["download_url"]), values)
            title = {"title": values["text"] or values["stem"]} if self.keeps else {}
            path = patterns.render_path(
                template, values | {"disposition": values["filename"]} | title if pending else values
            )
        except (KeyError, ValueError) as error:
            problem = SourceError(str(error).strip("'\""))
            return Item(key=key, path="", url=link, values=values, problem=problem, referer=referer)
        path = keep.with_extension(download.with_extension(path, self.settings), self.saving)
        return Item(key=key, path=path, url=url, values=values | title, referer=referer, pending=pending)

    def fetch(self, item: Item, dest: BinaryIO) -> Item | None:
        driver = self._driver()
        answer: dict[str, str] = {}
        if isinstance(item.trigger, keep.Captured):
            answer["name"] = keep.write(item.trigger, dest, self.settings)
        elif self.saving:
            spec = (self.recipe.get("links") or {}) | {"save": self.saving}
            answer["name"], answer["title"] = driver.keep_page(item.trigger or item.url, dest, self.settings, spec)
        elif item.trigger is not None:
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
        values |= {"title": answer["title"] or item.values.get("title") or "page"} if "title" in answer else {}
        path = download.with_extension(patterns.render_path(str(self.settings["path"]), values), self.settings)
        return dataclasses.replace(item, path=keep.with_extension(path, self.saving), values=values, pending=False)

    def survey(
        self, files: int = SURVEY_FILES, pages: int = SURVEY_PAGES, begin: Callable[[], None] = lambda: None
    ) -> dict:
        """The recipe's test: the walk, downloading nothing (harvester.sources.survey)."""
        return survey.survey(self, files, pages, begin)

    def close(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver = None
