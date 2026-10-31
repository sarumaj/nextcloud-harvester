"""The site source over HTTP against the fake archive (httpx's MockTransport): no network."""

# pylint: disable=missing-function-docstring

import io
import json

import httpx
import pytest

from fake_site import PDF, SITE, Site
from harvester.sources import FULL_PASS, ChallengeError, Item, SourceError
from harvester.sources.site import Site as SiteSource

ARCHIVE = {
    "start": f"{SITE}/select/mag/archiv",
    "follow": [{"selector": "a[href^='/select/mag/archiv/']", "pattern": r"/archiv/(?P<year>\d{4})$"}],
    "links": {"selector": "a.year__link", "pattern": r"/archiv/(?P<year>\d{4})/(?P<issue>\d+)$"},
}


def source(site: Site, recipe: dict, **options) -> SiteSource:
    """The site source over HTTP to the fake site, without real pauses."""

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = site.answer(str(request.url), request.method)
        return httpx.Response(status, headers=headers, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    made = SiteSource({"recipe": json.dumps(recipe), **options}, {}, client)
    made.sleep = lambda seconds: None
    return made


def test_levels_groups_and_download_url():
    options = {"download_url": "{url}/download", "path": "{year}/Mag {year}-{issue:02d}", "file_type": "pdf"}
    items = list(source(Site(), ARCHIVE, **options).items(lambda key: False))
    assert [item.path for item in items] == [
        "2026/Mag 2026-02.pdf",
        "2026/Mag 2026-01.pdf",
        "2025/Mag 2025-03.pdf",
        "2025/Mag 2025-02.pdf",
        "2025/Mag 2025-01.pdf",
        "2024/Mag 2024-03.pdf",
        "2024/Mag 2024-02.pdf",
        "2024/Mag 2024-01.pdf",
    ]
    assert items[0].key == f"{SITE}/select/mag/archiv/2026/2"
    assert items[0].url == f"{SITE}/select/mag/archiv/2026/2/download"
    assert items[0].values["year"] == 2026 and items[0].values["text"] == "Mag 2 2026"


def test_known_files_end_the_run_only_after_a_full_pass():
    known = {f"{SITE}/select/mag/archiv/2026/{n}" for n in (1, 2)}
    first = list(source(Site(), ARCHIVE).items(known.__contains__))
    assert len(first) == 6, "before a full pass, older years are still fetched"
    known.add(FULL_PASS)
    assert not list(source(Site(), ARCHIVE).items(known.__contains__)), "a year of known files ends it"


def test_pagination_by_next_link():
    recipe = {
        "start": f"{SITE}/list",
        "links": {"selector": "a.file", "pattern": r"\.pdf$"},
        "next": {"selector": "a.next", "level": 0},
    }
    items = list(source(Site(), recipe, stop_at_seen=0).items(lambda key: key.endswith("2-1.pdf")))
    assert [item.path for item in items] == ["1-1.pdf", "1-2.pdf", "2-2.pdf", "3-1.pdf", "3-2.pdf"]


def test_download_waits_when_asked_and_checks_the_file():
    site, pauses = Site(), []
    made = source(site, ARCHIVE, download_url="{url}/download", file_type="pdf", wait_pattern=r"wait_sec=(\d+)")
    made.sleep = pauses.append
    item = Item("k", "p", f"{SITE}/select/mag/archiv/2025/1/download")
    dest = io.BytesIO()
    made.fetch(item, dest)
    assert dest.getvalue() == PDF and pauses == [8.0]
    with pytest.raises(SourceError, match="instead of a file"):
        made.fetch(Item("k", "p", f"{SITE}/select/mag/archiv/2025"), io.BytesIO())
    with pytest.raises(SourceError, match="only"):
        source(site, ARCHIVE, min_size=10_000).fetch(Item("k", "p", f"{SITE}/files/a.pdf"), io.BytesIO())


def test_a_429_is_waited_for():
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    made = SiteSource({"recipe": json.dumps(ARCHIVE)}, {}, httpx.Client(transport=httpx.MockTransport(handler)))
    pauses: list[float] = []
    made.sleep = pauses.append
    made.fetch(Item("k", "p", f"{SITE}/x.pdf"), io.BytesIO())
    assert pauses == [7.0]


def test_bad_recipes_are_refused():
    for recipe, problem in (
        ("{not json", "not valid JSON"),
        (json.dumps({"start": "file:///etc/passwd", "links": {"selector": "a"}}), "may not be opened"),
        (json.dumps({"start": f"{SITE}/", "links": {"selector": "a", "pattern": "("}}), "not a regular expression"),
        (json.dumps({"start": f"{SITE}/"}), "no file links"),
    ):
        with pytest.raises(SourceError, match=problem):
            SiteSource({"recipe": recipe}, {}, None)
    with pytest.raises(SourceError, match="names issue"):
        source(Site(), ARCHIVE, path="{issue}").item(f"{SITE}/a.pdf", "a", 1, {})


def test_a_challenge_over_http_stops_the_run():
    site = Site()
    site.challenge.add("/select/mag/archiv/2025")
    with pytest.raises(ChallengeError) as caught:
        list(source(site, ARCHIVE).items(lambda key: False))
    assert caught.value.url.endswith("/select/mag/archiv/2025") and caught.value.kind == "reCAPTCHA"


def test_a_kept_sessions_cookies_go_with_http_requests():
    site = Site(require_login=True)
    seen_cookies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie", ""))
        status, headers, body = site.answer(str(request.url), request.method, request.headers.get("cookie", ""))
        return httpx.Response(status, headers=headers, content=body)

    made = SiteSource({"recipe": json.dumps(ARCHIVE)}, {}, httpx.Client(transport=httpx.MockTransport(handler)))
    made.restore({"cookies": [{"name": "sid", "value": "ok", "domain": "site.example", "path": "/"}], "origins": []})
    assert len(list(made.items(lambda key: False))) == 8
    assert all("sid=ok" in cookie for cookie in seen_cookies)
