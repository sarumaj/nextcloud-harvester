# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assets of a repository's releases, on GitHub, on GitLab or on a Forgejo or Gitea server.

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
from urllib.parse import quote, urlparse

import httpx

from .. import patterns
from ..browser import allowed_url
from . import download
from .base import AuthError, IMPERSONATE, Item, Option, Source, SourceError, pattern_search
from .download import Response

PER_PAGE = 30
# The forges: auto finds it by the host's name, or by asking an unknown server's API.
FORGES = ("auto", "github", "gitlab", "forgejo")


class Releases(Source):
    """The assets of a repository's releases."""

    name = "releases"
    title = "Release downloads (GitHub, GitLab, Codeberg, Forgejo)"
    description = (
        "The files attached to a repository's releases on GitHub, GitLab or Codeberg, or on a Forgejo, Gitea or "
        "GitLab server of your own."
    )
    credentials = ("token",)
    options = {
        "repository": Option(
            "Repository",
            "",
            "Its address, e.g. https://github.com/owner/name or https://gitlab.com/group/subgroup/name.",
            kind="url",
            placeholder="https://github.com/owner/name",
        ),
        "asset_pattern": Option(
            "Files",
            ".*",
            "Regular expression the file's name must match, e.g. \\.AppImage$.",
            kind="regex",
            placeholder="\\.AppImage$",
            suggestions=(
                ".*",
                "\\.AppImage$",
                "x86_64.*\\.tar\\.gz$",
                "(amd64|x86_64).*\\.deb$",
                "\\.(exe|msi)$",
                "\\.dmg$",
                "\\.apk$",
            ),
        ),
        "prereleases": Option("Prereleases", 0, "Also the files of prereleases.", kind="flag"),
        "path": Option(
            "File name",
            "{tag}/{filename}",
            "Relative to the folder: {tag}, {version} (the tag without v), {name}, {repo}, {filename}, {stem}, "
            "{ext}, {year}, {month}, {day} (of the release).",
            kind="template",
            values=("repo", "tag", "version", "name", "filename", "stem", "ext", "year", "month", "day"),
            suggestions=("{tag}/{filename}", "{filename}", "{repo}/{version}/{filename}", "{year}/{tag}/{filename}"),
        ),
        "max_pages": Option(
            "Pages",
            1,
            "At most this many pages of 30 releases (the newest first).",
            minimum=1,
            suggestions=("1", "3", "10"),
        ),
        "max_releases": Option(
            "Newest releases",
            0,
            "Only the files of this many releases, the newest (drafts, and prereleases unless asked for, not "
            "counted); 0: every release on the pages read.",
            minimum=0,
            suggestions=("1", "3", "10", "0"),
        ),
        "forge": Option(
            "Forge",
            "auto",
            "auto: github.com, gitlab.com and codeberg.org by their name, another server asked (Forgejo or Gitea, "
            "then GitLab).",
            choices=FORGES,
        ),
        **download.DOWNLOAD_OPTIONS,
        "impersonate": IMPERSONATE,
    }
    pause = 1.0
    listing_options = ("repository", "forge", "asset_pattern", "prereleases", "max_pages", "max_releases")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A GitLab page's address names the project before "/-/" (".../-/releases").
        repository = (
            str(self.settings["repository"]).strip().split("/-/", maxsplit=1)[0].rstrip("/").removesuffix(".git")
        )
        parsed = urlparse(repository)
        parts = [part for part in parsed.path.split("/") if part]
        self.host = (parsed.hostname or "").lower()
        self.forge = self._forge(str(self.settings["forge"]))
        problems = []
        if self.forge not in FORGES:
            problems.append(f"unknown forge {self.settings['forge']}")
        # GitLab's projects may sit in groups of groups (group/subgroup/project).
        enough = len(parts) >= 2 if self.forge in ("gitlab", "auto") else len(parts) == 2
        if not allowed_url(repository) or not enough:
            problems.append(f"{repository or 'no repository'} is not a repository's address (https://host/owner/name)")
        try:
            self.pattern = re.compile(str(self.settings["asset_pattern"]))
        except re.error as error:
            problems.append(f"the files' pattern is not a regular expression ({error})")
        problems += download.problems(self.settings)
        if problems:
            raise SourceError("; ".join(problems))
        self.project = "/".join(parts)
        self.owner, self.repo = "/".join(parts[:-1]), parts[-1]
        self.token = str(self.secrets.get("token", "") or "").strip()
        self.sleep: Callable[[float], None] = time.sleep

    def _forge(self, chosen: str) -> str:
        """The forge: chosen, or by the host's name (auto stays auto for another server)."""
        if chosen != "auto":
            return chosen
        if self.host in ("github.com", "www.github.com"):
            return "github"
        if self.host in ("gitlab.com", "www.gitlab.com"):
            return "gitlab"
        if self.host in ("codeberg.org", "gitea.com"):
            return "forgejo"  # Codeberg runs Forgejo; gitea.com, Gitea: the same API
        return "auto"

    @property
    def github(self) -> bool:
        """Whether the repository is on GitHub."""
        return self.forge == "github"

    def api(self, page: int) -> str:
        """The page of the repository's releases."""
        if self.github:
            return f"https://api.github.com/repos/{self.owner}/{self.repo}/releases?per_page={PER_PAGE}&page={page}"
        if self.forge == "gitlab":
            project = quote(self.project, safe="")
            return f"https://{self.host}/api/v4/projects/{project}/releases?per_page={PER_PAGE}&page={page}"
        return f"https://{self.host}/api/v1/repos/{self.owner}/{self.repo}/releases?limit={PER_PAGE}&page={page}"

    def headers(self, accept: str = "application/json") -> dict[str, str]:
        """The request's headers: the token, the media type wanted."""
        headers = {"Accept": accept}
        if self.github:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if self.token:
            # GitLab's too as Authorization (it takes a Bearer token), never as PRIVATE-TOKEN:
            # httpx drops Authorization when a redirect leaves the host, a header of its own not
            # (a release's link redirects to wherever its maintainer points it).
            bearer = self.github or self.forge == "gitlab"
            headers["Authorization"] = f"Bearer {self.token}" if bearer else f"token {self.token}"
        return headers

    def resolve(self) -> httpx.Response | None:
        """An unknown server's forge: Forgejo or Gitea when its API knows the repository, else
        GitLab when its API knows the project. Returns the answer it got: the first page."""
        if self.forge != "auto":
            return None
        assert self.client is not None
        self.forge = "forgejo"
        answer = self.client.get(self.api(1), headers=self.headers())
        if answer.status_code != 404:
            return answer
        self.forge = "gitlab"
        answer = self.client.get(self.api(1), headers=self.headers())
        if answer.status_code == 404:
            raise SourceError(f"neither a Forgejo, Gitea nor GitLab repository at {self.host}/{self.project}")
        return answer

    def releases(self) -> Iterator[dict]:
        """The repository's releases, newest first, page after page."""
        assert self.client is not None
        first = self.resolve()
        for page in range(1, int(self.settings["max_pages"]) + 1):
            response = (
                first if page == 1 and first is not None else self.client.get(self.api(page), headers=self.headers())
            )
            if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
                raise SourceError("GitHub's limit for requests is reached: try later, or set a token")
            if response.status_code in (401, 403):
                raise AuthError(f"{self.host} refused the token ({response.status_code})")
            if response.status_code == 404:
                raise SourceError(f"no repository {self.project} on {self.host} (a private one needs a token)")
            response.raise_for_status()
            found = response.json()
            if not isinstance(found, list):
                raise SourceError(f"{self.host} answered no list of releases")
            yield from found
            if len(found) < PER_PAGE:
                return

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        newest = int(self.settings["max_releases"] or 0)
        taken = 0
        for release in self.releases():
            if release.get("draft") or (release.get("prerelease") and not int(self.settings["prereleases"])):
                continue
            # The newest releases only: no more pages are asked for once they are in.
            if newest and taken >= newest:
                return
            taken += 1
            tag = str(release.get("tag_name") or "")
            values: dict = {
                "repo": self.repo,
                "tag": tag,
                "version": tag[1:] if re.match(r"v\d", tag) else tag,
                "name": str(release.get("name") or tag),
            }
            published = str(
                release.get("published_at") or release.get("released_at") or release.get("created_at") or ""
            )
            date = re.match(r"(\d{4})-(\d{2})-(\d{2})", published)
            if date:
                values |= {"year": int(date[1]), "month": int(date[2]), "day": int(date[3])}
            for asset in self.assets(release):
                item = self.asset(asset, values)
                if item and not seen(item.key):
                    yield item

    def assets(self, release: dict) -> list[dict]:
        """A release's files: GitHub's and Forgejo's assets, GitLab's asset links (the archives of
        the sources left out)."""
        assets = release.get("assets") or []
        if isinstance(assets, dict):  # GitLab: {"links": [...], "sources": [...]}
            return [
                {
                    "name": link.get("name"),
                    "browser_download_url": link.get("direct_asset_url") or link.get("url"),
                    "id": link.get("id"),
                }
                for link in assets.get("links") or []
            ]
        return assets

    def asset(self, asset: dict, values: dict) -> Item | None:
        """The release's file, when its name matches the job's pattern."""
        filename = str(asset.get("name") or "")
        if not filename or not pattern_search(self.pattern.pattern, filename):
            return None
        link = str(asset.get("browser_download_url") or "")
        stem, ext = posixpath.splitext(filename)
        values = values | {"filename": filename, "stem": stem, "ext": ext, "size": asset.get("size", 0)}
        key = link or f"{self.host}/{self.project}/assets/{asset.get('id')}"
        try:
            path = patterns.render_path(str(self.settings["path"]), values, "this release")
        except (KeyError, ValueError) as error:
            problem = SourceError(str(error).strip("'\""))
            return Item(key=key, path="", url=link, values=values, problem=problem)
        # A file of the job's type gets its extension when the name has none.
        path = download.with_extension(path, self.settings)
        return Item(
            key=key,
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
