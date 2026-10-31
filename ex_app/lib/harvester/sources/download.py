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

from ..browser import allowed_url, challenge_in_html
from .base import AuthError, ChallengeError, Option, SourceError

# Waits a site asks for, per file; and the longest one honoured.
MAX_WAITS = 5
MAX_WAIT = 900
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
    "file_type": Option("File type", "any", "any, pdf, zip, image, audio, video or html: anything else is refused."),
    "min_size": Option("Smallest file (bytes)", 0, "Smaller answers are refused (an error page, say)."),
    "wait_pattern": Option(
        "Wait pattern",
        "",
        "Regular expression on the URL the site redirects to when it wants a pause; its first group is the "
        "seconds (e.g. wait_sec=(\\d+)).",
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


def problems(settings: dict) -> list[str]:
    """What is wrong with a job's download settings."""
    found = []
    if str(settings.get("file_type", "any")) not in FILE_TYPES:
        found.append(f"unknown file type {settings.get('file_type')}")
    try:
        re.compile(str(settings.get("wait_pattern", "")))
    except re.error as error:
        found.append(f"the wait pattern is not a regular expression ({error})")
    return found


def extension(settings: dict) -> str:
    """The extension a file of the job's type gets when its name has none."""
    return FILE_TYPES[str(settings.get("file_type", "any"))][1]


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
    file_type = str(settings.get("file_type", "any"))
    magic, _ = FILE_TYPES[file_type]
    for _ in range(MAX_WAITS):
        with opener(url) as response:
            wait = pause(response, str(settings.get("wait_pattern", "")))
            if wait is not None:
                sleep(wait)
                continue
            if response.status in (401, 403):
                raise AuthError(f"{url} answered {response.status}: the account may not download it")
            if response.status >= 400:
                raise SourceError(f"{url} answered {response.status}")
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/html") and file_type != "html":
                kind = challenge_in_html(b"".join(response.chunks).decode("utf-8", "replace"))
                if kind:
                    raise ChallengeError(response.url, kind)
                raise SourceError(f"got a web page instead of a file for {url} (signed out?)")
            if file_type in ("audio", "video") and not content_type.startswith(
                (f"{file_type}/", "application/octet-stream", "binary/")
            ):
                raise SourceError(f"{url} is {content_type or 'of no type'}, not {file_type}")
            size = 0
            for chunk in response.chunks:
                if size == 0 and magic and not chunk.startswith(magic):
                    raise SourceError(f"{url} is not a {file_type} file")
                dest.write(chunk)
                size += len(chunk)
            if size < int(settings.get("min_size", 0) or 0):
                raise SourceError(f"{url} gave only {size} bytes")
            return
    raise SourceError(f"the site kept asking to wait for {url}")
