# SPDX-License-Identifier: AGPL-3.0-or-later
"""A file's download, checked the same way for every source: the pauses a site asks for, never a
web page instead of the file (a sign-in or a challenge), the type and size the job expects.

A source opens the download (over HTTP, or in the browser with its session) as a Response;
fetch() does the rest. DOWNLOAD_OPTIONS are the settings this reads, for a source's options.
"""

import contextlib
import email.message
import posixpath
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..browser import allowed_url, challenge_in_html, reachable
from .impersonate import CurlTransport
from .base import ChallengeError, Denied, Option, Refused, SourceError, WebPage, check_stop, pattern_search

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Nextcloud-Harvester"
# Waits a site asks for, per file; and the longest one honoured.
MAX_WAITS = 5
MAX_WAIT = 900
# The most of a web page or feed read into memory (a listing, a feed, the page a site gave
# instead of a file): more is no page a person made, and the app's memory is shared.
MAX_PAGE = 16 * 1024 * 1024
# Network failures (a dropped connection, a timeout) tried again per file, after growing pauses.
RETRIES = 3
RETRY_PAUSE = 10
# A page standing in for the file ("Your download starts in 5 seconds…"): the address of its
# meta refresh, else of its one link that reads like the download (its text, a download
# attribute, or a file's extension), is followed once.
DOWNLOAD_WORDS = re.compile(
    r"download|herunterladen|t[ée]l[ée]charger|descargar|scarica|pobierz|st[aá]hnout|click here|hier klicken", re.I
)
FILE_EXTENSIONS = (".pdf", ".zip", ".epub", ".mobi", ".mp3", ".mp4", ".m4a", ".jpg", ".jpeg", ".png", ".gz", ".7z")
REFRESH = re.compile(r"^\s*\d*\s*[;,]?\s*url\s*=\s*['\"]?([^'\"]+)", re.I)
NO_FILE = "the download URL gives a page (add a level of links, or change Download URL)"
# A sign-in page: a password field, or an address of a sign-in.
LOGIN_PATH = re.compile(r"(^|/)(log-?in|sign-?in|sso|auth|oauth2?|session/new)(/|$)", re.I)
# Content-Disposition's filename* (RFC 5987): a charset, a language, the encoded name.
FILENAME_STAR = re.compile(r"filename\*\s*=\s*([\w-]*)'[^']*'([^;]+)", re.I)
# The longest file name a site may give (Nextcloud's own limit is 250 bytes).
MAX_NAME = 200
# The sizes' megabyte: 1000 × 1000 bytes, as the job's form and the progress log show sizes, so
# that "4096 MB" is the 4.1 GB they show (not 4.3 GB of 1024 × 1024).
MB = 1000 * 1000
# What a file must start with, and its extension when the name has none. Audio and video are
# told by their content type instead (their containers do not start alike).
FILE_TYPES: dict[str, tuple[tuple[bytes, ...], str]] = {
    "any": ((), ""),
    "pdf": ((b"%PDF-",), ".pdf"),
    "zip": ((b"PK\x03\x04",), ".zip"),
    "image": ((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF"), ""),
    "audio": ((), ""),
    "video": ((), ""),
    "html": ((), ".html"),
}
DOWNLOAD_OPTIONS = {
    "file_type": Option(
        "File types",
        "any",
        "One or several (Ctrl or ⌘ and click to pick more, e.g. pdf and zip); any: every type. Anything else is "
        "refused (an error page, say).",
        choices=("any", "pdf", "zip", "image", "audio", "video", "html"),
        multiple=True,
    ),
    "min_size": Option(
        "Smallest file (bytes)",
        0,
        "Smaller answers are refused (an error page, say).",
        unit="bytes",
        minimum=0,
        suggestions=("0", "10000", "100000", "1000000"),
    ),
    "max_size": Option(
        "Largest file (MB)",
        4096,
        "A larger file is refused (the container's disk and memory).",
        unit="MB",
        minimum=1,
        suggestions=("100", "1024", "4096", "16384"),
    ),
    "wait_pattern": Option(
        "Wait pattern",
        "",
        "Regular expression on the URL the site redirects to when it wants a pause; its first group is the "
        "seconds (e.g. wait_sec=(\\d+)).",
        kind="regex",
        placeholder="wait_sec=(\\d+)",
    ),
}


@dataclass
class Response:
    """A download's answer, the same over HTTP and in the browser."""

    status: int
    url: str
    headers: dict[str, str]
    chunks: Iterator[bytes]


Opener = Callable[[str], contextlib.AbstractContextManager[Response]]


def file_types(settings: dict) -> list[str]:
    """The job's file types: one or several, comma-separated ("pdf,zip"); none or "any": any."""
    types = [kind.strip().lower() for kind in str(settings.get("file_type", "any") or "any").split(",") if kind.strip()]
    return ["any"] if not types or "any" in types else list(dict.fromkeys(types))


def problems(settings: dict) -> list[str]:
    """What is wrong with a job's download settings."""
    found = [f"unknown file type {kind}" for kind in file_types(settings) if kind not in FILE_TYPES]
    try:
        re.compile(str(settings.get("wait_pattern", "")))
    except re.error as error:
        found.append(f"the wait pattern is not a regular expression ({error})")
    return found


def largest(settings: dict) -> int:
    """The job's largest file (max_size, in MB), in bytes."""
    return int(settings.get("max_size", 4096) or 4096) * MB


def extension(settings: dict) -> str:
    """The extension a file of the job's type gets when its name has none: only with one type
    (with several, a name without one cannot tell which)."""
    types = file_types(settings)
    return FILE_TYPES.get(types[0], ((), ""))[1] if len(types) == 1 else ""


def accepts(types: list[str], content_type: str, start: bytes) -> bool:
    """Whether a file is of one of the types: by how it starts, or for audio and video by its
    content type (their containers do not start alike)."""
    if "any" in types:
        return True
    for kind in types:
        magic, _ = FILE_TYPES.get(kind, ((), ""))
        if magic and start.startswith(magic):
            return True
        if kind in ("audio", "video") and content_type.startswith((f"{kind}/", "application/octet-stream", "binary/")):
            return True
        if kind == "html" and content_type.startswith("text/html"):
            return True
    return False


def with_extension(path: str, settings: dict) -> str:
    """The path with the job's file type's extension when it does not end with it already (a
    name with a dot of its own, "v2.0 notes", still gets ".pdf")."""
    extension_ = extension(settings)
    return path + extension_ if extension_ and not path.lower().endswith(extension_) else path


def impersonates(options: dict) -> bool:
    """Whether a job's options ask its downloads to impersonate a browser (base.IMPERSONATE)."""
    return bool(int(options.get("impersonate", 0) or 0))


def impersonation(wanted: bool) -> dict:
    """guarded_client's option for impersonating a browser, given only when wanted (a client
    made otherwise takes no such option)."""
    return {"impersonate": True} if wanted else {}


def guarded_client(impersonate: bool = False, **options) -> httpx.Client:
    """An HTTP client for the sources: every request, a redirect's too, only to an address the
    app may reach (browser.reachable), else SourceError; the event_hooks given run after the
    check. impersonate: its requests present themselves as Chrome's (impersonate.CurlTransport,
    through the proxy given), httpx still following the redirects and keeping the cookies."""

    def check(request: httpx.Request) -> None:
        if not reachable(str(request.url)):
            raise SourceError(f"{request.url} may not be reached")

    hooks = options.pop("event_hooks", {})
    settings = {"follow_redirects": True, "timeout": 120, "headers": {"User-Agent": USER_AGENT}} | options
    if impersonate:
        settings["transport"] = CurlTransport(str(settings.pop("proxy", "") or ""), USER_AGENT)
    return httpx.Client(event_hooks=hooks | {"request": [check, *hooks.get("request", [])]}, **settings)


def http(client: httpx.Client, headers: dict[str, str] | None = None) -> Opener:
    """Downloads over HTTP with the client (its cookies, its headers, and these)."""

    @contextlib.contextmanager
    def open_url(url: str) -> Iterator[Response]:
        # Without headers, as it always was (a client of a source's own may take no more).
        with client.stream("GET", url, **({"headers": headers} if headers else {})) as response:
            yield Response(
                response.status_code,
                str(response.url),
                {key.lower(): value for key, value in response.headers.items()},
                response.iter_bytes(),
            )

    return open_url


def read_page(response: httpx.Response) -> bytes:
    """A streamed answer's body, a page or a feed: SourceError beyond MAX_PAGE (announced or
    read so far), before the rest is read."""
    announced = response.headers.get("content-length", "")
    if announced.isdigit() and int(announced) > MAX_PAGE:
        raise SourceError(f"{response.url} is larger than {MAX_PAGE // 1048576} MB, too large for a page")
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > MAX_PAGE:
            raise SourceError(f"{response.url} is larger than {MAX_PAGE // 1048576} MB, too large for a page")
    return bytes(body)


def pause(response: Response, wait_pattern: str) -> float | None:
    """The pause the site asks for, or None."""
    if response.status in (429, 503):
        retry = response.headers.get("retry-after", "")
        return min(float(retry) if retry.isdigit() else 60.0, MAX_WAIT)
    match = pattern_search(wait_pattern, response.url) if wait_pattern else None
    if match:
        return min(float(match.group(1)) + 5 if match.groups() else 60.0, MAX_WAIT)
    return None


def login_page(html: str, url: str) -> bool:
    """Whether a page is a sign-in: a password field, or a sign-in's address."""
    return bool(re.search(r"<input[^>]+type=['\"]?password", html, re.I) or LOGIN_PATH.search(urlparse(url).path))


def stand_in_target(html: str, url: str) -> str:
    """The file's address a page standing in for it gives (DOWNLOAD_WORDS): its meta refresh's,
    else its one link that reads like the download; "" when it gives none, or several."""
    soup = BeautifulSoup(html, "html.parser")
    base = soup.find("base", href=True)
    base_url = urljoin(url, str(base["href"])) if base else url
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv", "")).lower() == "refresh":
            found = REFRESH.match(str(meta.get("content", "")))
            if found:
                return urljoin(base_url, found.group(1).strip())
    found = set()
    for link in soup.find_all("a", href=True):
        href = str(link["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        address = urljoin(base_url, href)
        named = urlparse(address).path.lower().endswith(FILE_EXTENSIONS)
        if link.has_attr("download") or named or DOWNLOAD_WORDS.search(link.get_text(" ", strip=True)):
            found.add(address)
    return found.pop() if len(found) == 1 else ""


def safe_name(name: str) -> str:
    """A file name a site gave, safe as one part of a path: no folders, no control characters, no
    leading dots, at most MAX_NAME characters (the extension kept); "" when nothing is left."""
    name = posixpath.basename(name.replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name).strip().lstrip(".").strip()
    if len(name) > MAX_NAME:
        stem, suffix = posixpath.splitext(name)
        suffix = suffix if len(suffix) <= 16 else ""
        name = stem[: MAX_NAME - len(suffix)].rstrip() + suffix
    return name


def disposition_name(headers: dict[str, str], url: str) -> str:
    """The file's name as the answer gives it: Content-Disposition's (filename*, RFC 5987, before
    filename, RFC 6266), else the last part of the address it came from; made safe (safe_name)."""
    header = headers.get("content-disposition", "")
    name = ""
    extended = FILENAME_STAR.search(header)
    if extended:
        # RFC 5987's form wins over the plain one, which is there for older clients.
        name = unquote(extended.group(2).strip().strip('"'), encoding=extended.group(1) or "utf-8", errors="replace")
    elif header:
        message = email.message.Message()
        message["content-disposition"] = header
        name = message.get_filename() or ""
    name = safe_name(name)
    return name or safe_name(unquote(posixpath.basename(urlparse(url).path))) or "download"


def copy_file(path: str, dest: BinaryIO, settings: dict, content_type: str = "") -> None:
    """Copies a file the browser downloaded to dest, with the checks of a download: the job's
    largest file (before anything is copied), its types, its smallest."""
    types, most = file_types(settings), largest(settings)
    with open(path, "rb") as source:
        source.seek(0, 2)
        size = source.tell()
        if size > most:
            raise Refused(f"the download is {size // MB} MB, more than the job's largest file")
        if size < int(settings.get("min_size", 0) or 0):
            raise SourceError(f"the download gave only {size} bytes")
        source.seek(0)
        first = True
        while chunk := source.read(1 << 20):
            check_stop()
            if first and not accepts(types, content_type, chunk):
                raise Refused(f"the download is not of the job's file types ({', '.join(types)})")
            first = False
            dest.write(chunk)


def fetch(  # pylint: disable=too-many-arguments
    opener: Opener,
    url: str,
    dest: BinaryIO,
    settings: dict,
    sleep: Callable[[float], None],
    *,
    told: Callable[[Response], None] = lambda response: None,
) -> None:
    """Writes the file at url to dest, after the checks; raises SourceError (or Denied,
    Refused, ChallengeError, WebPage) when the answer is not the file. A page standing in for
    the file is followed once to the file it names (stand_in_target). told hears the answer
    the file came with (its headers: the name it gives)."""
    if not allowed_url(url):
        raise SourceError(f"{url} may not be opened")
    failures = 0
    asked, followed = url, False
    for _ in range(MAX_WAITS + RETRIES + 1):
        try:
            done = _attempt(opener, url, dest, settings, sleep, told)
        except (httpx.TransportError, ConnectionError, TimeoutError) as error:
            # A dropped connection or a timeout: tried again after a pause, a few times; what was
            # written so far is dropped.
            failures += 1
            if failures > RETRIES:
                raise SourceError(f"{url} could not be fetched: {error}") from error
            dest.seek(0)
            dest.truncate()
            sleep(RETRY_PAUSE * failures)
            continue
        if isinstance(done, str):
            # Followed once only: a second page is no stand-in.
            if followed or not allowed_url(done):
                raise WebPage(f"got a web page instead of a file for {asked}: {NO_FILE}")
            url, followed = done, True
            continue
        if done:
            return
    raise SourceError(f"the site kept asking to wait for {url}")


def _stand_in(response: Response, url: str) -> str:
    """The file's address a web page that came instead of it gives (stand_in_target); else what
    the page is: a challenge, a sign-in, or the recipe's trouble (WebPage)."""
    # Its start is enough to tell a challenge: a page without end is not read to it.
    page = bytearray()
    for chunk in response.chunks:
        page += chunk
        if len(page) > MAX_PAGE:
            break
    text = page.decode("utf-8", "replace")
    kind = challenge_in_html(text)
    if kind:
        raise ChallengeError(response.url, kind)
    if login_page(text, response.url):
        raise WebPage(f"got a web page instead of a file for {url}: a sign-in (signed out?)", login=True)
    target = stand_in_target(text, response.url)
    if not target:
        raise WebPage(f"got a web page instead of a file for {url}: {NO_FILE}")
    return target


def _attempt(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    opener: Opener,
    url: str,
    dest: BinaryIO,
    settings: dict,
    sleep: Callable[[float], None],
    told: Callable[[Response], None],
) -> bool | str:
    """One request for the file: True once it is written, False after a pause the site asked
    for, the file's address when a page stands in for it."""
    types = file_types(settings)
    with opener(url) as response:
        wait = pause(response, str(settings.get("wait_pattern", "")))
        if wait is not None:
            sleep(wait)
            return False
        # One file refused is that file's trouble (taken away, kept private): the run decides
        # whether all of them are, which is the account's.
        if response.status in (401, 403):
            raise Denied(f"{url} answered {response.status}: the account may not download it")
        if response.status >= 400:
            raise SourceError(f"{url} answered {response.status}")
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html") and "html" not in types:
            return _stand_in(response, url)
        most = largest(settings)
        announced = response.headers.get("content-length", "")
        if announced.isdigit() and int(announced) > most:
            raise Refused(f"{url} is {int(announced) // MB} MB, more than the job's largest file")
        size = 0
        for chunk in response.chunks:
            # A stop ends the download at once; what was written is dropped with the run's file.
            check_stop()
            if size == 0 and not accepts(types, content_type, chunk):
                raise Refused(f"{url} is not of the job's file types ({', '.join(types)})")
            size += len(chunk)
            if size > most:
                raise Refused(f"{url} is larger than the job's largest file")
            dest.write(chunk)
        if size < int(settings.get("min_size", 0) or 0):
            raise SourceError(f"{url} gave only {size} bytes")
        told(response)
        return True
