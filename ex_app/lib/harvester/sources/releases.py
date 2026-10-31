"""The assets of a repository's releases, on GitHub or on a Forgejo or Gitea server.

The releases come from the forge's API, newest first; drafts never, prereleases when the job asks.
A token (the job's account) is needed for private repositories, and avoids GitHub's low limit for
anonymous requests. With a token, GitHub's assets are fetched through the API (a private
repository's download links need it); otherwise through their download links.
"""

import contextlib
import posixpath
import re
import time
from collections.abc import Callable, Iterator
from typing import BinaryIO
from urllib.parse import urlparse

import httpx

from .. import patterns
from ..browser import allowed_url
from . import download
from .base import AuthError, Item, Option, Source, SourceError
from .download import Response

PER_PAGE = 30


class Releases(Source):
    """The assets of a repository's releases."""

    name = "releases"
    title = "Release downloads (GitHub, Forgejo)"
    description = "The files attached to a repository's releases on GitHub, or on a Forgejo or Gitea server."
    credentials = ("token",)
    options = {
        "repository": Option("Repository", "", "Its address, e.g. https://github.com/owner/name."),
        "asset_pattern": Option("Files", ".*", "Regular expression the file's name must match, e.g. \\.AppImage$."),
        "prereleases": Option("Prereleases", 0, "1: also the files of prereleases."),
        "path": Option(
            "File name",
            "{tag}/{filename}",
            "Relative to the folder: {tag}, {version} (the tag without v), {name}, {repo}, {filename}, {stem}, "
            "{ext}, {year}, {month}, {day} (of the release).",
        ),
        "max_pages": Option("Pages", 1, "At most this many pages of 30 releases (the newest first)."),
        **download.DOWNLOAD_OPTIONS,
    }
    pause = 1.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        repository = str(self.settings["repository"]).strip().rstrip("/").removesuffix(".git")
        parsed = urlparse(repository)
        parts = [part for part in parsed.path.split("/") if part]
        problems = []
        if not allowed_url(repository) or len(parts) != 2:
            problems.append(f"{repository or 'no repository'} is not a repository's address (https://host/owner/name)")
        try:
            self.pattern = re.compile(str(self.settings["asset_pattern"]))
        except re.error as error:
            problems.append(f"the files' pattern is not a regular expression ({error})")
        problems += download.problems(self.settings)
        if problems:
            raise SourceError("; ".join(problems))
        self.host = (parsed.hostname or "").lower()
        self.owner, self.repo = parts
        self.github = self.host in ("github.com", "www.github.com")
        self.token = str(self.secrets.get("token", "") or "").strip()
        self.sleep: Callable[[float], None] = time.sleep

    def api(self, page: int) -> str:
        """The page of the repository's releases."""
        if self.github:
            return f"https://api.github.com/repos/{self.owner}/{self.repo}/releases?per_page={PER_PAGE}&page={page}"
        return f"https://{self.host}/api/v1/repos/{self.owner}/{self.repo}/releases?limit={PER_PAGE}&page={page}"

    def headers(self, accept: str = "application/json") -> dict[str, str]:
        """The request's headers: the token, the media type wanted."""
        headers = {"Accept": accept}
        if self.github:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}" if self.github else f"token {self.token}"
        return headers

    def releases(self) -> Iterator[dict]:
        """The repository's releases, newest first, page after page."""
        assert self.client is not None
        for page in range(1, int(self.settings["max_pages"]) + 1):
            response = self.client.get(self.api(page), headers=self.headers())
            if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
                raise SourceError("GitHub's limit for requests is reached: try later, or set a token")
            if response.status_code in (401, 403):
                raise AuthError(f"{self.host} refused the token ({response.status_code})")
            if response.status_code == 404:
                raise SourceError(
                    f"no repository {self.owner}/{self.repo} on {self.host} (a private one needs a token)"
                )
            response.raise_for_status()
            found = response.json()
            if not isinstance(found, list):
                raise SourceError(f"{self.host} answered no list of releases")
            yield from found
            if len(found) < PER_PAGE:
                return

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        for release in self.releases():
            if release.get("draft") or (release.get("prerelease") and not int(self.settings["prereleases"])):
                continue
            tag = str(release.get("tag_name") or "")
            values: dict = {
                "repo": self.repo,
                "tag": tag,
                "version": tag[1:] if re.match(r"v\d", tag) else tag,
                "name": str(release.get("name") or tag),
            }
            published = str(release.get("published_at") or release.get("created_at") or "")
            date = re.match(r"(\d{4})-(\d{2})-(\d{2})", published)
            if date:
                values |= {"year": int(date[1]), "month": int(date[2]), "day": int(date[3])}
            for asset in release.get("assets") or []:
                item = self.asset(asset, values)
                if item and not seen(item.key):
                    yield item

    def asset(self, asset: dict, values: dict) -> Item | None:
        """The release's file, when its name matches the job's pattern."""
        filename = str(asset.get("name") or "")
        if not filename or not self.pattern.search(filename):
            return None
        link = str(asset.get("browser_download_url") or "")
        stem, ext = posixpath.splitext(filename)
        values = values | {"filename": filename, "stem": stem, "ext": ext, "size": asset.get("size", 0)}
        try:
            path = patterns.render_path(str(self.settings["path"]), values)
        except (KeyError, ValueError) as error:
            raise SourceError(str(error).strip("'\"")) from error
        # A file of the job's type gets its extension when the name has none.
        if not posixpath.splitext(path)[1]:
            path += download.extension(self.settings)
        return Item(
            key=link or f"{self.host}/{self.owner}/{self.repo}/assets/{asset.get('id')}",
            path=path,
            # A private repository's GitHub links need the API and the token.
            url=str(asset.get("url") or link) if self.github and self.token else link,
            values=values,
        )

    def open(self, url: str) -> contextlib.AbstractContextManager[Response]:
        """Downloads with the token (and, from GitHub's API, as a file)."""
        assert self.client is not None
        api_asset = self.github and urlparse(url).hostname == "api.github.com"
        headers = self.headers("application/octet-stream" if api_asset else "*/*")
        # The token goes to the forge only, not to where a download link redirects (a storage host).
        if not (api_asset or urlparse(url).hostname == self.host):
            headers.pop("Authorization", None)
        return download.http(WithHeaders(self.client, headers))(url)

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        download.fetch(self.open, item.url, dest, self.settings, self.sleep)


class WithHeaders:  # pylint: disable=too-few-public-methods
    """The client, sending these headers and following redirects with each download."""

    def __init__(self, client: httpx.Client, headers: dict[str, str]):
        self.client = client
        self.headers = headers

    def stream(self, method: str, url: str):
        """The client's stream, with the headers."""
        return self.client.stream(method, url, headers=self.headers, follow_redirects=True)
