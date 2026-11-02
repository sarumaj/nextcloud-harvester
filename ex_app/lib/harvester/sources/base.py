"""What a source is: it lists the items a site offers and downloads one of them."""

import abc
import contextlib
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, ClassVar
from urllib.parse import urlparse

import httpx

from .. import cookies as cookie_jar


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


class SourceError(Exception):
    """The site refused or broke: the run stops and the user is told."""


class AuthError(SourceError):
    """The credentials were refused: running again will not help until they change."""


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


@dataclass(frozen=True)
class Option:
    """A setting of a job, shown in the job's form."""

    title: str
    default: str | int
    description: str = ""
    # How the job's form shows it: text; url (an address, checked as typed); regex (checked as
    # typed); template (a file name or address made of values, with the values to insert);
    # flag (a checkbox, kept as 1 or 0); recipe (the recorder's button and its JSON). A number
    # default makes a number field.
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

    def __init__(self, options: dict, credentials: dict[str, str], client: httpx.Client | None):
        self.settings = {key: options.get(key, option.default) for key, option in self.options.items()}
        self.secrets = credentials
        self.client = client
        # Through the home connection (the job's option "home", when the app has one): the
        # executor gives the client the proxy; a browser the source starts takes it too.
        self.home = False

    @abc.abstractmethod
    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        """Yields the items not seen yet, in the order they should be downloaded."""

    @abc.abstractmethod
    def fetch(self, item: Item, dest: BinaryIO) -> None:
        """Writes the item's content to dest."""

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
