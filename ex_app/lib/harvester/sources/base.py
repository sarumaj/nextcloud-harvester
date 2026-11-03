# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a source is: it lists the items a site offers and downloads one of them."""

import abc
import contextlib
import functools
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, BinaryIO, ClassVar
from urllib.parse import urlparse

import httpx
import regex

from .. import cookies as cookie_jar

# The beginning of the keys a job records that are no items: a complete pass, a source's own marks
# (forgotten when the job's listing changes) and refused files (forgotten when its types or sizes
# do).
MARK = "harvester:"
REFUSED = MARK + "refused:"


class Stopped(Exception):
    """The run was stopped by its user during a wait (the sources' sleep raises it): it ends
    there, and the file it was at comes next run."""


# The running job's stop, for the runner's thread (one job per thread, Executor.run sets it): a
# download in progress (its chunks, yt-dlp's progress, a helper program) ends at once, not only
# between files and in waits.
_RUN = threading.local()


def watch_stop(event: threading.Event | None) -> None:
    """The stop of the job this thread runs now (None: none)."""
    _RUN.stopped = event


def stop_requested() -> bool:
    """Whether this thread's job was stopped."""
    event = getattr(_RUN, "stopped", None)
    return bool(event and event.is_set())


def check_stop() -> None:
    """Ends the job's work here (Stopped) if it was stopped."""
    if stop_requested():
        raise Stopped("stopped")


def nap(seconds: float) -> None:
    """A wait of this thread's job (a step's pause, its retry's delay), ended at once by its stop
    (then Stopped)."""
    event = getattr(_RUN, "stopped", None)
    if event is None:
        event = threading.Event()
    if event.wait(max(0.0, seconds)):
        raise Stopped("stopped")


@contextlib.contextmanager
def killed_on_stop(process: subprocess.Popen) -> Iterator[None]:
    """Kills the helper program as soon as the job is stopped (its pipe may stall, so the
    program cannot wait for the next chunk); Stopped afterwards, instead of its failure."""
    event = getattr(_RUN, "stopped", None)

    def watch() -> None:
        while process.poll() is None:
            if event is not None and event.wait(0.5):
                process.kill()
                return

    if event is not None:
        threading.Thread(target=watch, name="stop-watch", daemon=True).start()
    yield
    check_stop()


def stoppable_run(  # pylint: disable=too-many-arguments
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
    text: bool = False,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """subprocess.run with these of its options, but the program is killed when the job is
    stopped (then Stopped)."""
    pipe = subprocess.PIPE if capture_output else None
    with subprocess.Popen(command, stdout=pipe, stderr=pipe, text=text, env=env) as process:
        with killed_on_stop(process):
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
    if check and process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


# How long a person's pattern may take on one text (s): a pattern that backtracks without end
# (`(a|aa)+$` on a long name) would else hold its runner for good. With the regex package, which
# also lets the other threads (runners, the page's requests) go on meanwhile.
PATTERN_TIMEOUT = 1.0


@functools.lru_cache(maxsize=256)
def _compiled(pattern: str) -> "regex.Pattern[str]":
    return regex.compile(pattern)


def pattern_search(pattern: str, text: str) -> "regex.Match[str] | None":
    """A person's pattern (as Python's re reads it; checked with re when the job is saved)
    searched in text, within PATTERN_TIMEOUT; SourceError if it takes longer."""
    try:
        return _compiled(pattern).search(text, timeout=PATTERN_TIMEOUT, concurrent=True)
    except TimeoutError as error:
        raise SourceError(f"the pattern {pattern} takes too long (more than {PATTERN_TIMEOUT:g} s)") from error


class SourceError(Exception):
    """The site refused or broke: the run stops and the user is told."""


class AuthError(SourceError):
    """The credentials were refused: running again will not help until they change."""


class Denied(SourceError):
    """One file's download was refused (401, 403): a file the site took away or keeps to itself
    (an episode removed from its storage), tried again next run. Only when every file of a run
    is refused is it the account's trouble (the runner's AuthError then)."""


class Refused(SourceError):
    """The file is not of the job's types, or larger than its largest file: the same next run, so
    it is skipped for good (until the job's types or sizes change), not tried again."""


class WebPage(SourceError):
    """A web page came instead of the file: a sign-in page (login: the session ended), or a
    page the download URL gives (a level of links short, or a wrong Download URL)."""

    def __init__(self, message: str, login: bool = False):
        super().__init__(message)
        self.login = login


# What the account's sign-ins without a browser are: HTTP Basic and cookies, the page's words.
ACCOUNT_HELP = {
    "username": ("User name", "For sites that ask with the browser's own sign-in box (HTTP Basic)."),
    "password": ("Password", "The user name's password (HTTP Basic)."),
    "cookies": (
        "Cookies",
        "A signed-in browser's cookies for the site: a cookies.txt export (Netscape format, as the "
        '"Get cookies.txt" extensions write it) or a Cookie header\'s name=value; name=value. They '
        "expire as they do in the browser.",
    ),
}


class HostBasicAuth(httpx.Auth):
    """HTTP Basic for one host (and scheme) only: a download elsewhere, a redirect to another
    site, never gets the password."""

    def __init__(self, url: str, username: str, password: str):
        parsed = urlparse(url)
        self.scheme, self.host = parsed.scheme, (parsed.hostname or "").lower()
        self.header = httpx.BasicAuth(username, password)

    def auth_flow(self, request: httpx.Request):
        if request.url.scheme == self.scheme and request.url.host.lower() == self.host:
            yield from self.header.auth_flow(request)
        else:
            yield request


class ChallengeError(SourceError):
    """The site asks for a person (a CAPTCHA, a "verify you are human" page): the job waits for its
    user to answer it in the recorder's browser, whose session the next run then uses."""

    def __init__(self, url: str, kind: str):
        super().__init__(f"{url} asks for a person ({kind}): continue in the browser")
        self.url = url
        self.kind = kind


@dataclass(frozen=True)
class Item:
    """One file on the site."""

    # Stable across runs: the job records it once the file is downloaded.
    key: str
    # Relative to the job's folder in Nextcloud.
    path: str
    url: str
    # What the link gives (its pattern's groups, its text, ...): the job's tags are made of them.
    values: dict = field(default_factory=dict, hash=False)
    # Why the item cannot be had (its file name cannot be made, it is too large): raised in its
    # place, so that it fails or is skipped alone, not the whole listing.
    problem: SourceError | None = field(default=None, hash=False, compare=False)
    # The page that lists it, sent as Referer with the download (sites that refuse downloads
    # from elsewhere).
    referer: str = field(default="", compare=False)
    # Its name comes with the file (the answer's own, {disposition}): path is a stand-in until
    # the source's fetch returns the item as named then.
    pending: bool = field(default=False, compare=False)
    # What starts its download when it has no address (an element the browser clicks).
    trigger: Any = field(default=None, hash=False, compare=False, repr=False)


@dataclass(frozen=True)
class Option:
    """A setting of a job, shown in the job's form."""

    title: str
    default: str | int
    description: str = ""
    # How the job's form shows it: text; url (an address, checked as typed); regex (checked as
    # typed); template (a file name or address made of values, with the values to insert);
    # flag (a checkbox, kept as 1 or 0); recipe (the recorder's button and its JSON); job (one of
    # the person's on-demand website jobs, kept as its ID, 0: none). A number default makes a
    # number field.
    kind: str = "text"
    # The unit of a size (bytes, MB): the form shows the value in readable units beside it.
    unit: str = ""
    # The only values it may have: the form offers them in a list (several kept comma-separated
    # with multiple).
    choices: tuple[str, ...] = ()
    multiple: bool = False
    # Values the form suggests, the field taking others too (yt-dlp's formats, IMAP folders).
    suggestions: tuple[str, ...] = ()
    # A template's values, offered for insertion ({year}, {title}, ...).
    values: tuple[str, ...] = ()
    # A number's bounds; an example shown in an empty field.
    minimum: int | None = None
    maximum: int | None = None
    placeholder: str = ""
    # What a job saved before the option existed has instead of default (None: the default), so
    # that a new option on by default leaves the jobs as they ran; the form shows it too.
    before: str | int | None = None


# A job's choice to have its HTTP clients present themselves as a browser does (headers and TLS
# fingerprint), with the established libraries that do it: yt-dlp's impersonation (curl_cffi),
# gallery-dl's browser emulation, curl_cffi for the downloads. For sites behind Cloudflare and
# similar checks that refuse plain HTTP clients (403). Harvester's own browser: STEALTH.
IMPERSONATE = Option(
    "Impersonate a browser",
    0,
    "The job's downloads present themselves as a browser does (headers and TLS, with yt-dlp's, gallery-dl's "
    "and curl_cffi's impersonation), for sites behind Cloudflare and similar checks that answer plain "
    "downloads with 403. Only where a site refuses them otherwise.",
    kind="flag",
)

# A job's choice to have its browser disguised as a person's (browser/engine.py: Patchright, a
# fork of Playwright that leaves out what gives an automated browser away), for sites whose bot
# check refuses Harvester's browser otherwise. Off by default: it only fetches what the family
# has access to, and never answers a challenge (a person does, in "Continue in the browser").
STEALTH = Option(
    "Disguise the browser",
    0,
    "Disguises the browser as a person's, for sites whose bot check refuses it otherwise (the job's "
    "browser, its test in the recorder and Continue in the browser alike). It answers no challenge: you "
    "still do, in the browser. From a server's address a site may refuse it all the same.",
    kind="flag",
)


# A job's choice to have the cookie banners its browser meets answered on their own (browser/
# consent.py), "Reject all" else "Only necessary": on for new jobs, off for those saved before.
CONSENT = Option(
    "Dismiss cookie banners",
    1,
    "The browser answers the common cookie banners (OneTrust, Cookiebot, Usercentrics, Didomi, Quantcast, "
    "TrustArc, Borlabs, Klaro, and banners with a button “Reject all”, “Only necessary”, …) with “Reject all”, "
    "else “Only necessary”, as they show: in the job's steps and pages, and in the recorder. It clicks nothing "
    "else.",
    kind="flag",
    before=0,
)


# A job's choice to ask the addresses of the files it saved again (versions.py): a file that
# changed there is saved over its own, which Nextcloud keeps as a version. On for new jobs, off
# for those saved before (they ran without it: each run lists the whole site with it).
RECHECK = Option(
    "Check known files for changes",
    1,
    "Each run asks the addresses of the files the job saved again (each at most once a day): only whether "
    "they changed where the site tells (ETag, Last-Modified), else by downloading and comparing. A file that "
    "changed is saved over its own: Nextcloud keeps the one before among its versions. The run lists the whole "
    "site then, not only up to known files. Not for pages the job saves.",
    kind="flag",
    before=0,
)


class Source(abc.ABC):
    """A site files are fetched from."""

    name: ClassVar[str]
    title: ClassVar[str]
    # What the source fetches, for the card that starts a job of it.
    description: ClassVar[str] = ""
    # The names of the job's credentials (kept per job in Nextcloud, encrypted). The page asks
    # for "password", "token", "secret" and "totp" hidden, and for "config" and "cookies"
    # (several lines) in a text area.
    credentials: ClassVar[tuple[str, ...]] = ()
    # What the page says of each credential: {name: (title, help)}; the name alone otherwise.
    credential_help: ClassVar[dict[str, tuple[str, str]]] = {}
    options: ClassVar[dict[str, Option]] = {}
    # Seconds between two downloads, so as not to hammer the site.
    pause: ClassVar[float] = 0.0
    # The options that change what is listed: a change of one forgets the job's marks of a
    # complete pass (and the source's own, MARK), its other options (file names, files per
    # run) do not.
    listing_options: ClassVar[tuple[str, ...]] = ()
    # The option holding the address an on-demand job's fetch replaces for its run (a page, a
    # feed); "" when the source has none a browser's page could send (a mailbox, a remote's
    # folder, a repository's releases): it offers no on-demand jobs.
    address_option: ClassVar[str] = ""

    def __init__(self, options: dict, credentials: dict[str, str], client: httpx.Client | None):
        self.settings = {
            key: options.get(key, option.default if option.before is None else option.before)
            for key, option in self.options.items()
        }
        self.secrets = credentials
        self.client = client
        # Through the home connection (the job's option "home", when the app has one): the
        # executor gives the client the proxy; a browser the source starts takes it too.
        self.home = False
        # The run's progress log (level, text) and its record of what is done (a key a later
        # listing asks seen() about, e.g. a whole message handled); the runner sets both.
        self.report: Callable[[str, str], None] = lambda level, text: None
        self.remember: Callable[[str], None] = lambda key: None
        # The keys of the items a listing asks seen() about next (a page's), told ahead so that
        # their files are looked for in Nextcloud at once (deleted.py); the runner sets it.
        self.ahead: Callable[[list[str]], None] = lambda keys: None
        # The job's time zone (the executor's; dates a recipe fills in are its days).
        self.timezone = "UTC"
        # Listing pages that gave nothing they should have (a sign-in, an error, a redirect): a
        # run with any is no complete pass, however it ends.
        self.gaps = 0
        # The kept session was given up (it no longer signed in): forgotten unless the run keeps
        # a new one.
        self.stale = False
        # The job's record of what its files hold (versions.Contents; the executor's): None, no
        # duplicates or changes are looked for.
        self.contents: Any = None
        # The run's outcome in place of its files' (a watched page's: pagewatch.py), or None.
        self.note: Any = None
        # Hands an address to one of the person's on-demand jobs (its ID) to fetch, as Send to
        # Harvester does (ondemand.hand_over, the executor's): why it could not, "" once queued.
        self.hand_over: Callable[[int, str], str] = lambda job_id, url: "no on-demand job takes it here"

    def rechecks(self) -> bool:
        """Whether a run asks the addresses of the files the job saved again (RECHECK); never for
        pages it keeps (saved anew, a page differs each time)."""
        return bool(int(self.settings.get("recheck", 0) or 0)) and not getattr(self, "keeps", False)

    @classmethod
    def address_of(cls, options: dict) -> str:
        """The job's own address (cls.address_option's value), "" when it has none."""
        return str(options.get(cls.address_option) or "").strip() if cls.address_option else ""

    @classmethod
    def with_address(cls, options: dict, url: str) -> dict:
        """The job's options with the address in place of its own, for one run."""
        return options | {cls.address_option: url}

    @abc.abstractmethod
    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        """Yields the items not seen yet, in the order they should be downloaded."""

    @abc.abstractmethod
    def fetch(self, item: Item, dest: BinaryIO) -> Item | None:
        """Writes the item's content to dest; returns the item as named now when its name came
        with the file (pending), else None."""

    def sign_in_again(self) -> bool:
        """Signs in anew in the middle of a run (the session ended); False when the source has no
        sign-in to repeat."""
        return False

    def account_cookies(self, url: str) -> list[cookie_jar.Cookie]:
        """The account's cookies (pairs of a Cookie header for url's host); AuthError when the
        text holds none."""
        text = self.secrets.get("cookies", "")
        if not text.strip():
            return []
        found = cookie_jar.parse(text, urlparse(url).hostname or "")
        if not found:
            raise AuthError("the account's cookies are neither a cookies.txt export nor name=value pairs")
        return found

    def sign_client(self, url: str) -> None:
        """Gives the run's client the account's cookies, and HTTP Basic for url's host."""
        if self.client is None:
            return
        cookie_jar.to_client(self.client, self.account_cookies(url))
        if self.secrets.get("username") and self.secrets.get("password"):
            self.client.auth = HostBasicAuth(url, self.secrets["username"], self.secrets["password"])

    def restore(self, session: dict) -> None:
        """Starts from the session kept after the last run (or the user's answer to a challenge)."""

    def session(self) -> dict | None:
        """The browser session to keep for the next run (cookies, local storage), or None."""

    def close(self) -> None:
        """Releases what the source holds (a browser); called after every run."""
