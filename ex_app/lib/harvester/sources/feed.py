"""A news feed (RSS or Atom): its podcast episodes and other attached files, or each entry's link.

The entries are taken newest first. An entry's file is known by the entry's id (or link) and the
file's URL, so an episode that a feed moves or republishes under a new address is fetched again.
"""

import posixpath
import re
import time
from collections.abc import Callable, Iterator
from typing import BinaryIO
from urllib.parse import urljoin

import feedparser

from .. import browser, patterns
from . import download
from .base import ACCOUNT_HELP, Item, Option, Source, SourceError

UNSAFE = re.compile(r"[\\/:*?\"<>|\s]+")
TAKES = ("enclosures", "links")


def safe(text: str, limit: int = 100) -> str:
    """The text as a part of a file name."""
    return UNSAFE.sub(" ", text or "").strip()[:limit]


class Feed(Source):
    """The files of an RSS or Atom feed."""

    name = "feed"
    title = "Feed or podcast"
    description = "The files of a news feed (RSS or Atom): podcast episodes, attached documents or each entry's link."
    # Private feeds (a paid podcast's, a member area's): HTTP Basic or a signed-in browser's cookies.
    credentials = ("username", "password", "cookies")
    credential_help = ACCOUNT_HELP
    options = {
        "url": Option(
            "Feed", "", "The feed's address (RSS or Atom).", kind="url", placeholder="https://example.org/feed.xml"
        ),
        "take": Option(
            "Take",
            "enclosures",
            "enclosures: the attached files (podcast episodes); links: each entry's link.",
            choices=TAKES,
        ),
        "link_pattern": Option(
            "Link pattern",
            "",
            "Regular expression the file's URL must match (empty: all).",
            kind="regex",
            placeholder="\\.mp3$",
        ),
        "path": Option(
            "File name",
            "{feed}/{year}/{title}{ext}",
            "Relative to the folder: {title}, {feed}, {author}, {date}, {year}, {month}, {day}, {filename}, {stem}, "
            "{ext}.",
            kind="template",
            values=(
                "feed",
                "title",
                "author",
                "date",
                "year",
                "month",
                "day",
                "filename",
                "stem",
                "ext",
                "text",
                "host",
                "guid",
            ),
        ),
        **download.DOWNLOAD_OPTIONS,
    }
    pause = 2.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        problems = []
        url = str(self.settings["url"]).strip()
        if not url:
            problems.append("no feed")
        elif not browser.allowed_url(url):
            problems.append(f"the feed {url} may not be opened")
        if self.settings["take"] not in TAKES:
            problems.append(f"take is {' or '.join(TAKES)}, not {self.settings['take']}")
        try:
            re.compile(str(self.settings["link_pattern"]))
        except re.error as error:
            problems.append(f"the link pattern is not a regular expression ({error})")
        problems += download.problems(self.settings)
        if problems:
            raise SourceError("; ".join(problems))
        self.sleep: Callable[[float], None] = time.sleep

    def entries(self) -> tuple[str, list]:
        """The feed's title and its entries, newest first."""
        assert self.client is not None
        url = str(self.settings["url"]).strip()
        self.sign_client(url)
        response = self.client.get(url)
        if response.status_code >= 400:
            raise SourceError(f"the feed {url} answered {response.status_code}")
        parsed = feedparser.parse(response.content)
        # A page that is no feed (HTML, an error) has no feed version.
        if not parsed.get("version"):
            raise SourceError(f"{url} is not a feed")
        title = parsed.feed.get("title") or (urlparse_host(url))

        def when(entry) -> float:
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            return time.mktime(stamp) if stamp else 0.0

        return title, sorted(parsed.entries, key=when, reverse=True)

    def files(self, entry) -> list[str]:
        """The file URLs of an entry that the job takes."""
        if self.settings["take"] == "links":
            urls = [entry.get("link", "")]
        else:
            urls = [enclosure.get("href", "") for enclosure in entry.get("enclosures", [])]
        pattern = re.compile(str(self.settings["link_pattern"]))
        # Feeds often link relatively (Atom's href="/f.pdf"): against the feed's own address.
        urls = [urljoin(str(self.settings["url"]), url) for url in urls if url]
        return [url for url in urls if browser.allowed_url(url) and pattern.search(url)]

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        feed, entries = self.entries()
        for entry in entries:
            for url in self.files(entry):
                key = f"feed:{entry.get('id') or entry.get('link') or ''}|{url}"
                if seen(key):
                    continue
                yield self.item(key, url, entry, feed)

    def item(self, key: str, url: str, entry, feed: str) -> Item:
        """The file of an entry: its path from the entry's values."""
        stamp = entry.get("published_parsed") or entry.get("updated_parsed") or time.gmtime()
        title = safe(entry.get("title", "")) or posixpath.splitext(posixpath.basename(url))[0]
        values = patterns.link_values(url, title, 1) | {
            "title": title,
            "feed": safe(feed),
            "author": safe(entry.get("author", "")),
            "guid": entry.get("id", ""),
            "date": time.strftime("%Y-%m-%d", stamp),
            "year": stamp.tm_year,
            "month": stamp.tm_mon,
            "day": stamp.tm_mday,
        }
        try:
            path = patterns.render_path(str(self.settings["path"]), values)
        except (KeyError, ValueError) as error:
            raise SourceError(f"the file name: {str(error).strip(chr(39))}") from error
        # A name without an extension gets the job's file type's (a link to a page, say).
        path = download.with_extension(path, self.settings)
        return Item(key=key, path=path, url=url, values=values)

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        assert self.client is not None
        download.fetch(download.http(self.client), item.url, dest, self.settings, self.sleep)


def urlparse_host(url: str) -> str:
    """The host of a URL, as a feed's title when it has none."""
    return re.sub(r"^[a-z]+://([^/]+).*$", r"\1", url)
