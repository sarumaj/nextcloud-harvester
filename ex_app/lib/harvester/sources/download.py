"""A file's download, checked the same way for every source: the pauses a site asks for, never a
web page instead of the file (a sign-in or a challenge), the type and size the job expects.

A source opens the download (over HTTP, or in the browser with its session) as a Response;
fetch() does the rest. DOWNLOAD_OPTIONS are the settings this reads, for a source's options.
"""

import contextlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import BinaryIO

import httpx

from ..browser import allowed_url, challenge_in_html, reachable
from .base import AuthError, ChallengeError, Option, SourceError, check_stop

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
        suggestions=("0", "10240", "102400", "1048576"),
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


def guarded_client(**options) -> httpx.Client:
    """An HTTP client for the sources: every request, a redirect's too, only to an address the
    app may reach (browser.reachable), else SourceError; the event_hooks given run after the
    check."""

    def check(request: httpx.Request) -> None:
        if not reachable(str(request.url)):
            raise SourceError(f"{request.url} may not be reached")

    hooks = options.pop("event_hooks", {})
    settings = {"follow_redirects": True, "timeout": 120, "headers": {"User-Agent": USER_AGENT}} | options
    return httpx.Client(event_hooks=hooks | {"request": [check, *hooks.get("request", [])]}, **settings)


def http(client: httpx.Client) -> Opener:
    """Downloads over HTTP with the client (its cookies, its headers)."""

    @contextlib.contextmanager
    def open_url(url: str) -> Iterator[Response]:
        with client.stream("GET", url) as response:
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
    match = re.search(wait_pattern, response.url) if wait_pattern else None
    if match:
        return min(float(match.group(1)) + 5 if match.groups() else 60.0, MAX_WAIT)
    return None


def fetch(
    opener: Opener, url: str, dest: BinaryIO, settings: dict, sleep: Callable[[float], None]
) -> None:  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Writes the file at url to dest, after the checks; raises SourceError (or AuthError,
    ChallengeError) when the answer is not the file."""
    if not allowed_url(url):
        raise SourceError(f"{url} may not be opened")
    failures = 0
    for _ in range(MAX_WAITS + RETRIES):
        try:
            done = _attempt(opener, url, dest, settings, sleep)
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
        if done:
            return
    raise SourceError(f"the site kept asking to wait for {url}")


def _attempt(
    opener: Opener, url: str, dest: BinaryIO, settings: dict, sleep: Callable[[float], None]
) -> bool:  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """One request for the file: True once it is written, False after a pause the site asked for."""
    types = file_types(settings)
    with opener(url) as response:
        wait = pause(response, str(settings.get("wait_pattern", "")))
        if wait is not None:
            sleep(wait)
            return False
        if response.status in (401, 403):
            raise AuthError(f"{url} answered {response.status}: the account may not download it")
        if response.status >= 400:
            raise SourceError(f"{url} answered {response.status}")
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html") and "html" not in types:
            # Its start is enough to tell a challenge: a page without end is not read to it.
            page = bytearray()
            for chunk in response.chunks:
                page += chunk
                if len(page) > MAX_PAGE:
                    break
            kind = challenge_in_html(page.decode("utf-8", "replace"))
            if kind:
                raise ChallengeError(response.url, kind)
            raise SourceError(f"got a web page instead of a file for {url} (signed out?)")
        largest = int(settings.get("max_size", 4096) or 4096) * 1024 * 1024
        announced = response.headers.get("content-length", "")
        if announced.isdigit() and int(announced) > largest:
            raise SourceError(f"{url} is {int(announced) // 1048576} MB, more than the job's largest file")
        size = 0
        for chunk in response.chunks:
            # A stop ends the download at once; what was written is dropped with the run's file.
            check_stop()
            if size == 0 and not accepts(types, content_type, chunk):
                raise SourceError(f"{url} is not of the job's file types ({', '.join(types)})")
            size += len(chunk)
            if size > largest:
                raise SourceError(f"{url} is larger than the job's largest file")
            dest.write(chunk)
        if size < int(settings.get("min_size", 0) or 0):
            raise SourceError(f"{url} gave only {size} bytes")
        return True
