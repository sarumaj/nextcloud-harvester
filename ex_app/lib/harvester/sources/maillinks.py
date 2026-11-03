# SPDX-License-Identifier: AGPL-3.0-or-later
"""The files linked in an e-mail's body ("Download your invoice"), for the mail source's option
"Also the files linked in the message".

A message's links are its HTML part's a[href] (with their text) and the addresses written in its
plain text (with their line as text); only http(s), never an unsubscribe link (by its address,
its text, or the message's List-Unsubscribe), and at most MAX_LINKS a message of those the job's
link patterns take. Each is fetched through the sources' checked download (download.fetch with
the run's guarded client: only public addresses, a redirect's too, at most REDIRECTS of them; the
job's types and sizes), named as the answer names it (Content-Disposition, else the last part of
the address it ended at).

A link that gives a web page instead of a file (a portal's sign-in) is no file: handed to an
on-demand website job of the person when the mail job names one (its recipe signs in and
downloads it), else a line in the log; either way it is skipped for good (Refused), as is a link
that expired (404, 410) or is refused to anyone without the site's session (401, 403).
"""

import re
from collections.abc import Callable
from email.message import EmailMessage
from typing import BinaryIO

import httpx
from bs4 import BeautifulSoup

from .. import browser
from . import download
from .base import ChallengeError, Denied, Refused, SourceError, WebPage, pattern_search

# The links of one message the job takes at most (of those its patterns take): a newsletter that
# matches is not fetched link by link.
MAX_LINKS = 10
# The redirects a link may go through (a tracker's, then the shop's), checked one by one.
REDIRECTS = 10
# The begin of a linked file's key: then the message's ID and the link's address.
KEY = "mail-link:"
# An address in plain text, and what ends a sentence after it rather than belonging to it.
PLAIN_URL = re.compile(r"https?://[^\s<>\"'\[\]{}|\\^`]+", re.I)
TRAILING = ".,;:!?)»”’"
# Links that unsubscribe or manage a newsletter: never fetched, whatever the patterns say.
UNSUBSCRIBE = re.compile(
    r"unsubscri|opt[-_ ]?out|abmeld|abbestell|austragen|wypisz|wypisać|rezygn|"
    r"(manage|update|e-?mail)[-_ ]?(your[-_ ]?)?(preferences|subscription)|newsletter[-_ ]?(settings|einstellungen)",
    re.I,
)
# What hand_over answers when the job names no on-demand job: the page is only logged.
NO_JOB = "no on-demand job"


def unsubscribes(message: EmailMessage) -> set[str]:
    """The http(s) addresses of the message's List-Unsubscribe header."""
    header = str(message.get("List-Unsubscribe", ""))
    return {address.strip() for address in re.findall(r"<([^>]+)>", header) if address.lower().startswith("http")}


def all_links(message: EmailMessage) -> list[tuple[str, str]]:
    """The message's http(s) links in their order, each once: (address, text). The HTML part's
    a[href] first (their text), then the addresses in the plain text (their line, without
    them)."""
    found: dict[str, str] = {}
    html = message.get_body(preferencelist=("html",))
    if html is not None and html.get_content_subtype() == "html":
        page = BeautifulSoup(str(html.get_content()), "html.parser")
        for link in page.find_all("a", href=True):
            address = str(link["href"]).strip()
            if address.lower().startswith(("http://", "https://")):
                found.setdefault(address, " ".join(link.get_text(" ", strip=True).split()))
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None and plain.get_content_subtype() == "plain":
        for line in str(plain.get_content()).splitlines():
            for match in PLAIN_URL.finditer(line):
                address = match.group(0).rstrip(TRAILING)
                found.setdefault(address, " ".join(line.replace(match.group(0), " ").split()))
    return list(found.items())


def wanted_links(message: EmailMessage, address_pattern: str, text_pattern: str) -> list[tuple[str, str]]:
    """The message's links the job takes, at most MAX_LINKS: each pattern given matches (the
    address's, the text's), no unsubscribe link, and an address the app may reach."""
    leave = unsubscribes(message)
    taken = []
    for address, text in all_links(message):
        if address in leave or UNSUBSCRIBE.search(address) or UNSUBSCRIBE.search(text):
            continue
        if address_pattern and not pattern_search(address_pattern, address):
            continue
        if text_pattern and not pattern_search(text_pattern, text):
            continue
        if not browser.allowed_url(address):
            continue
        taken.append((address, text))
        if len(taken) >= MAX_LINKS:
            break
    return taken


def fetch_link(  # pylint: disable=too-many-arguments
    client: httpx.Client | None,
    url: str,
    dest: BinaryIO,
    settings: dict,
    sleep: Callable[[float], None],
    hand_over: Callable[[str], str],
) -> str:
    """Writes the linked file to dest with the checked download and returns its name (the
    answer's). Refused (skipped for good, the log saying why) for a web page (after handing it
    over: hand_over returns why it could not, "" when it did), an expired link, or one refused."""
    if client is None:
        raise SourceError(f"no connection for {url}")
    client.max_redirects = min(client.max_redirects, REDIRECTS)
    named: dict[str, str] = {}

    def told(response: download.Response) -> None:
        named["name"] = download.disposition_name(response.headers, response.url)

    try:
        download.fetch(download.http(client), url, dest, settings, sleep, told=told)
    except (WebPage, ChallengeError) as error:
        problem = hand_over(url)
        if not problem:
            raise Refused(f"a page, not a file: {url} (sent to the on-demand job)") from error
        raise Refused(f"a page, not a file: {url}" + ("" if problem == NO_JOB else f" ({problem})")) from error
    except (download.Gone, Denied) as error:
        raise Refused(f"the link expired or is closed ({error})") from error
    except httpx.TooManyRedirects as error:
        raise Refused(f"{url} redirects more than {REDIRECTS} times") from error
    return named.get("name") or "download"
