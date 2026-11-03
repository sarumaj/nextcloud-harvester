# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cookies a job's account holds: a browser's cookies.txt export (Netscape's format, as
extensions such as "Get cookies.txt" write it) or a Cookie header's name=value; ... pairs.

They go to the HTTP client, to Chromium (a website job), or as a cookies.txt file to yt-dlp and
gallery-dl. The browser views' cookies panel writes a cookies.txt (to_text) for the account.
A website job's downloads over HTTP send the browser's cookies as the browser would
(session_cookies).
"""

import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Cookie:
    """One cookie."""

    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    expires: int = 0
    # Kept from a cookies.txt's "#HttpOnly_" mark, and written back with it.
    http_only: bool = False


def parse(text: str, host: str) -> list[Cookie]:
    """The cookies in the text; the pairs of a Cookie header are for host (and its subdomains)."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    found = []
    for line in lines:
        # cookies.txt: domain, subdomains, path, secure, expires, name, value (tab-separated);
        # "#HttpOnly_" marks a cookie, other "#" lines are comments.
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) == 7:
            domain, _, path, secure, expires, name, value = fields
            found.append(
                Cookie(
                    name,
                    value,
                    domain,
                    path or "/",
                    secure.upper() == "TRUE",
                    int(expires) if expires.isdigit() else 0,
                    http_only,
                )
            )
            continue
        for pair in line.split(";"):
            name, separator, value = pair.strip().partition("=")
            if separator and name:
                found.append(Cookie(name.strip(), value.strip(), "." + host.lstrip(".")))
    return found


def to_client(client, cookies: list[Cookie]) -> None:
    """Gives the cookies to an httpx client."""
    for cookie in cookies:
        client.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)


def to_playwright(cookies: list[Cookie]) -> list[dict]:
    """The cookies as Playwright's add_cookies takes them."""
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure}
        | ({"expires": c.expires} if c.expires else {})
        for c in cookies
    ]


def to_text(cookies: list[Cookie]) -> str:
    """The cookies as a cookies.txt (Netscape's format, as parse() reads it)."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        subdomains = "TRUE" if c.domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.secure else "FALSE"
        mark = "#HttpOnly_" if c.http_only else ""
        lines.append(f"{mark}{c.domain}\t{subdomains}\t{c.path}\t{secure}\t{c.expires}\t{c.name}\t{c.value}")
    return "\n".join(lines) + "\n"


def to_file(cookies: list[Cookie]) -> str:
    """A cookies.txt file (0600, in a temporary directory) for yt-dlp or gallery-dl; the caller
    removes it."""
    handle, path = tempfile.mkstemp(prefix="harvester-cookies-", suffix=".txt")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(to_text(cookies))
    return path


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


def browser_cookies(read: Callable[[str], list[dict]]) -> Callable[[httpx.Request], None]:
    """session_cookies with the browser's cookies for each request's address as they are when it
    is sent (read: a context's cookies(url)), so that one client serves a run's downloads while
    the browser's session changes."""

    def add(request: httpx.Request) -> None:
        session_cookies(read(str(request.url)))(request)

    return add
