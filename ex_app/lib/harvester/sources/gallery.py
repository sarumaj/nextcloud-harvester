"""Images (and other files) of galleries on the sites gallery-dl knows (Flickr, Imgur, DeviantArt,
...): gallery-dl lists them, Harvester downloads them with its own checks.

gallery-dl only lists here (its JSON output, intermediary pages resolved); the download is
Harvester's, with a Referer of the gallery's page. Sites that need gallery-dl's own headers,
cookies or sign-in for the files themselves may refuse it.

The account's user name and password go to gallery-dl in a configuration file, its cookies as a
cookies.txt file (both 0600, removed after the listing), never on its command line; the cookies go
to Harvester's downloads too.
"""

import datetime
import json
import os
import posixpath
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

import httpx

from .. import browser, cookies, patterns
from ..browser import allowed_url, child_env
from . import download
from .base import ACCOUNT_HELP, Item, Option, Source, SourceError

# gallery-dl's message types in its JSON output (gallery_dl.extractor.message.Message).
URL_MESSAGE = 3
# How long listing a gallery may take (s).
LIST_TIMEOUT = 600


def dated(value: Any) -> dict:
    """year, month and day of gallery-dl's date ("YYYY-MM-DD HH:MM:SS"), when there is one."""
    try:
        day = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return {}
    return {"year": day.year, "month": day.month, "day": day.day}


class Gallery(Source):
    """A gallery, album or user page on a site gallery-dl supports."""

    name = "gallery"
    title = "Image galleries (gallery-dl)"
    description = (
        "Images of galleries, albums and profiles on Flickr, Imgur, DeviantArt and the other sites gallery-dl supports."
    )
    credentials = ("username", "password", "cookies")
    credential_help = ACCOUNT_HELP | {
        "username": ("User name", "The site's account, for galleries only members see (gallery-dl signs in)."),
        "password": ("Password", "The account's password."),
    }
    options = {
        "url": Option(
            "Page",
            "",
            "A gallery, album or profile page.",
            kind="url",
            placeholder="https://www.flickr.com/photos/name/albums/123",
        ),
        "path": Option(
            "File name",
            "{category}/{filename}.{extension}",
            "Relative to the folder, from gallery-dl's values for the file: {category}, {filename}, {extension}, "
            "{id}, {year}, {month}, {day}, ... (they differ per site).",
            kind="template",
            values=("category", "filename", "extension", "stem", "ext", "year", "month", "day"),
            suggestions=(
                "{category}/{filename}.{extension}",
                "{year}/{filename}.{extension}",
                "{year}/{month:02d}/{filename}.{extension}",
                "{filename}.{extension}",
            ),
        ),
        **download.DOWNLOAD_OPTIONS,
    }
    pause = 1.0

    def __init__(self, options: dict, credentials: dict[str, str], client: httpx.Client | None):
        super().__init__({"file_type": "image"} | options, credentials, client)
        self.url = str(self.settings["url"]).strip()
        if not allowed_url(self.url):
            raise SourceError(f"the page {self.url or '(none)'} may not be opened")
        problems = download.problems(self.settings)
        if problems:
            raise SourceError("; ".join(problems))
        # How gallery-dl is run, so that tests can stand in for it.
        self.run: Callable[..., subprocess.CompletedProcess] = subprocess.run
        self.sleep: Callable[[float], None] = time.sleep

    def listing(self) -> list:
        """gallery-dl's messages for the page."""
        options, files = self.account_files()
        if self.home and browser.PROXY:
            options = [*options, "--proxy", browser.PROXY]
        # The address after "--": never read as an option (it is http(s) in any case).
        command = [sys.executable, "-m", "gallery_dl", "--resolve-json", "--no-download", *options, "--", self.url]
        try:
            done = self.run(command, capture_output=True, text=True, timeout=LIST_TIMEOUT, check=False, env=child_env())
        except subprocess.TimeoutExpired as error:
            raise SourceError(f"gallery-dl took longer than {LIST_TIMEOUT} s for {self.url}") from error
        finally:
            for path in files:
                os.unlink(path)
        lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
        try:
            messages = json.loads(done.stdout or "[]")
        except ValueError:
            messages = None
        # gallery-dl exits non-zero also after a partial failure (one page of many): what it did
        # list is used; only an empty or broken listing is the job's failure.
        if not isinstance(messages, list):
            raise SourceError(f"gallery-dl gave no JSON for {self.url}" + (f": {lines[-1]}" if lines else ""))
        if done.returncode != 0 and not messages:
            raise SourceError(f"gallery-dl: {lines[-1] if lines else f'exit {done.returncode}'}")
        return messages

    def account_files(self) -> tuple[list[str], list[str]]:
        """gallery-dl's options for the account, and the files they name (to remove after)."""
        options, files = [], []
        found = self.account_cookies(self.url)
        if found:
            files.append(cookies.to_file(found))
            options += ["--cookies", files[-1]]
        if self.secrets.get("username"):
            handle, path = tempfile.mkstemp(prefix="harvester-gallery-dl-", suffix=".json")
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                account = {"username": self.secrets["username"], "password": self.secrets.get("password", "")}
                json.dump({"extractor": account}, file)
            files.append(path)
            options += ["--config", path]
        return options, files

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        if self.client is not None:
            cookies.to_client(self.client, self.account_cookies(self.url))
        for message in self.listing():
            if not (isinstance(message, list) and len(message) >= 3 and message[0] == URL_MESSAGE):
                continue
            url, data = str(message[1]), message[2] if isinstance(message[2], dict) else {}
            if not allowed_url(url):
                continue
            # The files of one post share its id and differ in num (Pixiv, DeviantArt, ...).
            post = data.get("id")
            key = (
                f"{data.get('category', 'gallery')}:{post}:{data.get('num', 0)}"
                if post
                else f"{data.get('category', 'gallery')}:{url}"
            )
            if seen(key):
                continue
            yield self.item(key, url, data)

    def item(self, key: str, url: str, data: dict) -> Item:
        """The file of a gallery-dl Url message: its name from the template and its values."""
        values = {name: value for name, value in data.items() if isinstance(value, (str, int, float))}
        values |= dated(data.get("date"))
        filename = str(data.get("filename") or posixpath.splitext(posixpath.basename(url.split("?")[0]))[0] or "file")
        extension = str(data.get("extension") or posixpath.splitext(url.split("?")[0])[1].lstrip(".") or "")
        values |= {
            "category": str(data.get("category") or "gallery"),
            "filename": filename,
            "extension": extension,
            "stem": filename,
            "ext": f".{extension}" if extension else "",
            "url": url,
        }
        try:
            path = patterns.render_path(str(self.settings["path"]), values)
        except (KeyError, ValueError) as error:
            raise SourceError(str(error).strip("'\"")) from error
        return Item(key=key, path=path, url=url, values=values)

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        if self.client is None:
            raise SourceError("no HTTP client")
        # The client is the run's own: the gallery's page goes with every download as Referer.
        self.client.headers["Referer"] = self.url
        download.fetch(download.http(self.client), item.url, dest, self.settings, self.sleep)
