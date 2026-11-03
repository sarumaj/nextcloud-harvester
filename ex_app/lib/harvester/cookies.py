# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cookies a job's account holds: a browser's cookies.txt export (Netscape's format, as
extensions such as "Get cookies.txt" write it) or a Cookie header's name=value; ... pairs.

They go to the HTTP client, to Chromium (a website job), or as a cookies.txt file to yt-dlp and
gallery-dl. The browser views' cookies panel writes a cookies.txt (to_text) for the account.
"""

import os
import tempfile
from dataclasses import dataclass


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
