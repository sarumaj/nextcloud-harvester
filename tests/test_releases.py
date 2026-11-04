# SPDX-License-Identifier: AGPL-3.0-or-later
"""The releases source against fake GitHub and Forgejo APIs (httpx's MockTransport): no network."""

# pylint: disable=missing-function-docstring

import io

import httpx
import pytest

from harvester.sources import AuthError, SourceError
from harvester.sources.releases import PER_PAGE, Releases

PDF = b"%PDF-1.7\n" + b"x" * 100


def release(tag: str, published: str, assets: list[str], prerelease=False, draft=False, host="github.com") -> dict:
    """A release as the forges' APIs give it."""
    base = f"https://{host}/o/r/releases/download/{tag}"
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "published_at": published,
        "prerelease": prerelease,
        "draft": draft,
        "assets": [
            {
                "id": index,
                "name": name,
                "size": 10,
                "browser_download_url": f"{base}/{name}",
                "url": f"https://api.github.com/repos/o/r/releases/assets/{index}",
            }
            for index, name in enumerate(assets)
        ],
    }


def source(handler, **options) -> Releases:
    made = Releases(
        {"repository": "https://github.com/o/r", **options},
        {"token": options.pop("token", "")} if "token" in options else {},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    made.sleep = lambda seconds: None
    return made


def test_github_releases_newest_first_without_prereleases_and_drafts():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.github.com" and request.url.path == "/repos/o/r/releases"
        return httpx.Response(
            200,
            json=[
                release("v2.0.0", "2026-10-20T10:00:00Z", ["app-2.0.0.AppImage", "app-2.0.0.zip"]),
                release("v2.1.0-rc1", "2026-10-25T10:00:00Z", ["app-2.1.0-rc1.AppImage"], prerelease=True),
                release("v1.9.0", "2026-09-01T10:00:00Z", ["app-1.9.0.AppImage"]),
                release("v3.0.0", "2026-10-30T10:00:00Z", ["app-3.0.0.AppImage"], draft=True),
            ],
        )

    made = source(handler, asset_pattern=r"\.AppImage$", path="{year}/{version}/{filename}")
    log: list[str] = []
    made.report = lambda level, text: log.append(text)
    items = list(made.items(lambda key: key.endswith("1.9.0.AppImage")))
    assert log == ["2 releases looked at (2 drafts or prereleases left out), 2 files match"]
    assert [item.path for item in items] == ["2026/2.0.0/app-2.0.0.AppImage"]
    assert items[0].url == "https://github.com/o/r/releases/download/v2.0.0/app-2.0.0.AppImage"
    assert items[0].values["tag"] == "v2.0.0" and items[0].values["month"] == 10
    assert "authorization" not in requests[0].headers

    with_pre = source(handler, asset_pattern=r"\.AppImage$", prereleases=1)
    assert [item.values["tag"] for item in with_pre.items(lambda key: False)] == ["v2.0.0", "v2.1.0-rc1", "v1.9.0"]


def test_forgejo_pages_and_token():
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "git.example.org" and request.url.path == "/api/v1/repos/o/r/releases"
        assert request.headers["authorization"] == "token secret"
        page = int(request.url.params["page"])
        pages.append(page)
        count = PER_PAGE if page == 1 else 2
        return httpx.Response(
            200,
            json=[
                release(f"v{page}.{n}", "2026-10-01T00:00:00Z", [f"f-{page}-{n}.zip"], host="git.example.org")
                for n in range(count)
            ],
        )

    made = Releases(
        {"repository": "https://git.example.org/o/r.git", "max_pages": 3},
        {"token": "secret"},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    items = list(made.items(lambda key: False))
    assert len(items) == PER_PAGE + 2 and pages == [1, 2], "a short page is the last"
    assert items[0].url.startswith("https://git.example.org/o/r/releases/download/")


def test_github_with_a_token_downloads_through_the_api():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            assert request.headers["authorization"] == "Bearer secret"
            return httpx.Response(200, json=[release("v1", "2026-10-01T00:00:00Z", ["doc.pdf"])])
        assert request.url.host == "api.github.com" and request.url.path == "/repos/o/r/releases/assets/0"
        assert request.headers["accept"] == "application/octet-stream"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, content=PDF, headers={"content-type": "application/octet-stream"})

    made = source(handler, token="secret", file_type="pdf")
    (item,) = list(made.items(lambda key: False))
    assert item.key == "https://github.com/o/r/releases/download/v1/doc.pdf"
    dest = io.BytesIO()
    made.fetch(item, dest)
    assert dest.getvalue() == PDF


def test_the_token_does_not_follow_a_download_to_another_host():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=[release("v1", "2026-10-01T00:00:00Z", ["a.zip"], host="git.example.org")])
        assert "authorization" not in request.headers or request.url.host == "git.example.org"
        if request.url.host == "git.example.org":
            return httpx.Response(302, headers={"location": "https://storage.example.net/blob"})
        return httpx.Response(200, content=b"PK\x03\x04zip", headers={"content-type": "application/zip"})

    made = Releases(
        {"repository": "https://git.example.org/o/r"},
        {"token": "secret"},
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    (item,) = list(made.items(lambda key: False))
    dest = io.BytesIO()
    made.fetch(item, dest)
    assert dest.getvalue() == b"PK\x03\x04zip"


def test_refusals():
    def private(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(SourceError, match="needs a token"):
        list(source(private).items(lambda key: False))

    def refused(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(AuthError):
        list(source(refused).items(lambda key: False))

    def limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    with pytest.raises(SourceError, match="limit"):
        list(source(limited).items(lambda key: False))

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    with pytest.raises(SourceError) as failed:
        list(source(broken).items(lambda key: False))
    assert str(failed.value) == "github.com answered 502 for the releases of o/r"

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    with pytest.raises(SourceError) as failed:
        list(source(unreachable).items(lambda key: False))
    assert str(failed.value) == "https://api.github.com/repos/o/r/releases?per_page=30&page=1 could not be reached"

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(SourceError, match="did not answer in time"):
        list(source(slow, forge="forgejo", repository="https://git.example/o/r").items(lambda key: False))


@pytest.mark.parametrize(
    ("repository", "problem"),
    [
        ("", "not a repository"),
        ("https://github.com/only-owner", "not a repository"),
        ("http://10.0.0.5/o/r", "not a repository"),
        ("file:///etc/o/r", "not a repository"),
    ],
)
def test_bad_repositories_are_refused(repository, problem):
    with pytest.raises(SourceError, match=problem):
        Releases({"repository": repository}, {}, None)


def test_a_bad_pattern_is_refused_and_saving_needs_no_network():
    with pytest.raises(SourceError, match="regular expression"):
        Releases({"repository": "https://github.com/o/r", "asset_pattern": "("}, {}, None)
    Releases({"repository": "https://github.com/o/r"}, {}, None)


def gitlab_site(seen_headers: list):
    """gitlab.example: Forgejo's API knows nothing (404), GitLab's lists a project's releases."""
    releases = [
        {
            "tag_name": "v2.0.0",
            "name": "2.0",
            "released_at": "2026-09-01T10:00:00Z",
            "assets": {
                "links": [
                    {
                        "id": 7,
                        "name": "tool-2.0.0.tar.gz",
                        "url": "https://gitlab.example/x",
                        "direct_asset_url": (
                            "https://gitlab.example/group/sub/tool/-/releases/v2.0.0/downloads/tool.tar.gz"
                        ),
                    },
                    {"id": 8, "name": "notes.txt", "url": "https://gitlab.example/y"},
                ],
                "sources": [{"format": "zip", "url": "https://gitlab.example/archive.zip"}],
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if request.url.path.startswith("/api/v1/"):
            return httpx.Response(404, json={"message": "not found"})
        if request.url.raw_path.startswith(b"/api/v4/projects/group%2Fsub%2Ftool/releases"):
            return httpx.Response(200, json=releases)
        return httpx.Response(404)

    return handler


def test_gitlab_projects_in_groups_with_their_asset_links():
    headers: list = []
    client = httpx.Client(transport=httpx.MockTransport(gitlab_site(headers)))
    gitlab = Releases(
        {"repository": "https://gitlab.example/group/sub/tool/-/releases", "asset_pattern": r"\.tar\.gz$"},
        {"token": "glpat-secret"},
        client,
    )
    items = list(gitlab.items(lambda key: False))
    assert [item.path for item in items] == ["v2.0.0/tool-2.0.0.tar.gz"], "the archives of the sources left out"
    assert items[0].url.endswith("/downloads/tool.tar.gz") and items[0].values["year"] == 2026
    assert gitlab.forge == "gitlab", "an unknown server: Forgejo's API asked first, then GitLab's"
    # As Authorization, which httpx drops on a redirect to another host (PRIVATE-TOKEN it keeps).
    assert headers[-1].get("authorization") == "Bearer glpat-secret" and "private-token" not in headers[-1]


def test_gitlab_com_and_codeberg_are_known_by_their_names():
    gitlab = Releases({"repository": "https://gitlab.com/group/tool"}, {}, None)
    assert gitlab.forge == "gitlab"
    assert gitlab.api(1) == "https://gitlab.com/api/v4/projects/group%2Ftool/releases?per_page=30&page=1"
    codeberg = Releases({"repository": "https://codeberg.org/forgejo/forgejo"}, {}, None)
    assert codeberg.forge == "forgejo"
    assert codeberg.api(1) == "https://codeberg.org/api/v1/repos/forgejo/forgejo/releases?limit=30&page=1"
