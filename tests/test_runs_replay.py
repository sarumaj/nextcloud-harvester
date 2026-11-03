# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs that meet a site's trouble, over HTTP (httpx's MockTransport, no network): pages that are
gone, refused or ask for a pause, a next page that loops or is a button, links shown on other
listings, addresses that change from visit to visit, pages standing in for files, names the site
gives, a session that ends in the middle of a run, and the kept session given up."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import io
import json
import re

import httpx
import pytest
from test_runner import FakeFiles, FakeLibrary, FakeNextcloud, FakeSource
from test_runs import run
from test_sources import ARCHIVE

from fake_site import PDF, SITE, Site
from harvester import runner
from harvester.executor import Executor
from harvester.patterns import stable_url
from harvester.sources import FULL_PASS, AuthError, Item, SourceError, WebPage, download
from harvester.sources import site as site_module
from harvester.sources.site import Site as SiteSource
from harvester.store import Job, Store

HTML = {"content-type": "text/html"}
LIST = {"start": f"{SITE}/list", "links": {"selector": "a.file", "pattern": r"\.pdf$"}}


class Trouble(Site):  # pylint: disable=too-few-public-methods
    """The fake archive with what goes wrong: answers by path (status, headers, body), and every
    request's headers."""

    def __init__(self, answers: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self.answers = answers or {}
        self.headers: list[dict] = []

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        path = httpx.URL(url).path
        if path in self.answers:
            self.requests.append(url)
            found = self.answers[path]
            return found(url) if callable(found) else found
        return super().answer(url, method, cookie)


def source(site: Site, recipe: dict, **options) -> tuple[SiteSource, list[float], list[str]]:
    """The site source over HTTP to the fake site: its pauses and its log."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(site, Trouble):
            site.headers.append(dict(request.headers))
        status, headers, body = site.answer(str(request.url), request.method, request.headers.get("cookie", ""))
        return httpx.Response(status, headers=headers, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    made = SiteSource({"recipe": json.dumps(recipe), **options}, {}, client)
    pauses: list[float] = []
    log: list[str] = []
    made.sleep = pauses.append
    made.report = lambda level, text: log.append(text)
    return made, pauses, log


def job(**options) -> Job:
    return Job(user="u", name="n", source="site", target="/In", schedule="* * * * *", options=options)


# B10: listing pages that are gone, refused or ask for a pause.


def test_a_dead_year_is_an_empty_page_and_the_run_no_complete_pass():
    site = Trouble({"/select/mag/archiv/2025": (404, HTML, b"<html>gone</html>")})
    made, _, _ = source(
        site,
        ARCHIVE,
        download_url="{url}/download",
        path="{year}-{issue}",
        file_type="pdf",
        wait_pattern=r"wait_sec=(\d+)",
    )
    message, seen, log = run(made)
    assert message == "5 downloaded", "2026 and 2024 still come"
    assert any(re.search(r"a level 1 page \(page 1, .*/2025\) gave nothing: answered 404", line) for line in log)
    assert FULL_PASS not in seen and "1 listing pages gave nothing: the run does not count as a complete pass" in log


def test_an_error_on_the_listing_itself_fails_the_run_readably():
    for status, error, text in ((404, SourceError, "answered 404"), (403, AuthError, "may not see the listing")):
        made, _, _ = source(Trouble({"/select/mag/archiv": (status, HTML, b"no")}), ARCHIVE)
        with pytest.raises(error, match=text):
            list(made.items(lambda key: False))


def test_a_listing_page_asking_for_a_pause_is_waited_for_and_pages_are_paced():
    asked = []

    def busy(url: str):
        asked.append(url)
        if len(asked) == 1:
            return 429, {"retry-after": "7"}, b""
        return 200, HTML, Site().answer(url)[2]

    made, pauses, _ = source(Trouble({"/select/mag/archiv/2025": busy}), ARCHIVE)
    assert len(list(made.items(lambda key: False))) == 8
    assert pauses.count(7.0) == 1, "the site's pause, then the page again"
    assert pauses.count(site_module.PAGE_PAUSE) == 3, "a pause before each page after the first"


def test_links_honour_base_href_and_the_browsers_language_is_asked_for():
    page = b'<html><head><base href="https://cdn.example/docs/"></head><a class="file" href="a.pdf">A</a></html>'
    site = Trouble({"/based": (200, HTML, page)})
    made, _, _ = source(site, {"start": f"{SITE}/based", "links": LIST["links"]})
    assert [item.url for item in made.items(lambda key: False)] == ["https://cdn.example/docs/a.pdf"]
    assert site.headers[0]["accept-language"] == site_module.accept_language(site_module.browser.LOCALE)
    assert site_module.accept_language("de-DE") == "de-DE,de;q=0.9"


# B11, B12: a next page that is a button, or that loops.


def test_a_next_page_without_an_address_needs_the_browser_and_says_so():
    page = b'<html><a class="file" href="/files/1.pdf">1</a><button class="next">Next</button></html>'
    made, _, _ = source(
        Trouble({"/b": (200, HTML, page)}), LIST | {"start": f"{SITE}/b", "next": {"selector": ".next"}}
    )
    with pytest.raises(SourceError, match="a button without an address: only the browser can click it"):
        list(made.items(lambda key: False))


def test_a_next_page_that_loops_ends_its_listing_with_a_line_in_the_log():
    def last(url: str):
        # The last page's next link goes back to the first.
        return (
            200,
            HTML,
            Site().answer(url)[2].replace(b"</body>", b'<a class="pager next" href="/list?p=1">N</a></body>'),
        )

    site = Trouble({})
    site.answers["/list"] = lambda url: last(url) if url.endswith("p=3") else (200, HTML, Site().answer(url)[2])
    made, _, log = source(site, LIST | {"next": {"selector": "a.next", "level": 0}}, max_pages=50, stop_at_seen=0)
    assert len(list(made.items(lambda key: False))) == 6
    assert any("was read already (a loop): its listing ends" in line for line in log)
    assert len([url for url in site.requests if "/list" in url]) == 4, "page 1 once more, then no further"


# B16: a link shown on another listing's page.


def test_a_link_on_another_years_page_is_taken_on_its_own_with_its_values():
    def year(url: str):
        # Every year's page shows the newest issue too (a teaser), with the year it belongs to.
        body = Site().answer(url)[2]
        return 200, HTML, body.replace(b"<ul", b'<a class="year__link" href="/select/mag/archiv/2026/2">New</a><ul')

    site = Trouble({f"/select/mag/archiv/{y}": year for y in (2025, 2024)})
    recipe = ARCHIVE | {"links": {"selector": "a.year__link", "pattern": r"/archiv/(?P<year>\d{4})/(?P<issue>\d+)$"}}
    made, _, _ = source(site, recipe, path="{year}/{issue}")
    paths = [item.path for item in made.items(lambda key: False)]
    assert paths.count("2026/2") == 1 and len(paths) == 8


def test_a_teaser_without_a_year_of_its_own_disagreeing_is_left_to_its_listing():
    recipe = {
        "start": f"{SITE}/select/mag/archiv",
        "follow": [{"selector": "a.years__link", "pattern": r"/archiv/(?P<year>\d{4})$"}],
        "links": {"selector": "a.year__link", "pattern": r"/archiv/(?P<year>\d{4})/\d+$"},
    }

    def year(url: str):
        body = Site().answer(url)[2]
        return 200, HTML, body.replace(b"<ul", b'<a class="year__link" href="/select/mag/archiv/2024/3">Old</a><ul')

    made, _, _ = source(Trouble({"/select/mag/archiv/2026": year}), recipe, path="{year}/{filename}")
    items = list(made.items(lambda key: False))
    assert [item.path for item in items if item.url.endswith("/2024/3")] == ["2024/3"], "under 2024, not 2026"


# B5: addresses that change from visit to visit.


def test_volatile_parts_of_an_address_make_no_new_files():
    assert stable_url(f"{SITE}/f;jsessionid=AB/x.pdf?id=3&utm_source=n#top") == f"{SITE}/f/x.pdf?id=3"
    assert stable_url("https://b.s3.example/k.pdf?X-Amz-Signature=z&X-Amz-Expires=3") == "https://b.s3.example/k.pdf"
    assert stable_url(f"{SITE}/x.pdf?a=1&b=2") == f"{SITE}/x.pdf?a=1&b=2", "unchanged, character for character"
    assert stable_url("<message@example.org>") == "<message@example.org>", "not an address: as it is"
    visit = [0]

    def listing(_url: str):
        visit[0] += 1
        links = "".join(
            f'<a class="file" href="/files/{n}.pdf;jsessionid={visit[0]}?sid={visit[0]}&X-Amz-Signature={visit[0]}">'
            f"{n}</a>"
            for n in (1, 2)
        )
        return 200, HTML, f"<html>{links}</html>".encode()

    site = Trouble({"/v": listing})
    seen: set[str] = set()
    first, _, _ = source(site, {"start": f"{SITE}/v", "links": {"selector": "a.file", "pattern": r"\.pdf"}})
    assert run(first, None, seen)[0] == "2 downloaded"
    again, _, _ = source(site, {"start": f"{SITE}/v", "links": {"selector": "a.file", "pattern": r"\.pdf"}})
    assert run(again, None, seen)[0] == "0 downloaded", "the same files, under new sessions and signatures"
    assert f"{SITE}/files/1.pdf" in seen


def test_a_key_template_tells_files_apart_and_old_keys_still_count():
    recipe = {"start": f"{SITE}/list", "links": {"selector": "a.file", "pattern": r"/(?P<p>\d)-(?P<n>\d)\.pdf$"}}
    keyed = recipe | {"links": recipe["links"] | {"key": "{p}.{n}"}}
    made, _, _ = source(Site(), keyed)
    assert [item.key for item in made.items(lambda key: False)] == ["key:1.1", "key:1.2"]
    made, _, _ = source(Site(), keyed)
    old = {f"{SITE}/files/1-1.pdf"}
    assert [item.key for item in made.items(old.__contains__)] == ["key:1.2"], "a key recorded as the address counts"


def test_the_runner_adopts_a_file_whose_recorded_key_had_a_session():
    library = FakeLibrary({"/In/a.pdf": b"before"})

    class Session(FakeSource):
        """One item, recorded before with a session in its address."""

        def items(self, seen):
            yield Item(key=f"{SITE}/a.pdf", path="a.pdf", url=f"{SITE}/a.pdf")

    owners = {0: f"{SITE}/a.pdf;jsessionid=1?sid=2"}
    message = runner.run(job(), Session({}, {}, None), library, lambda key: False, lambda key: None, owners=owners)
    assert message == "0 downloaded, 1 already there" and "/In/a (2).pdf" not in library.files


# B9: a deeper page that redirected to a sign-in.


def test_a_year_that_redirects_to_a_sign_in_is_a_gap():
    site = Trouble({"/select/mag/archiv/2024": (302, {"location": f"{SITE}/login"}, b"")})
    made, _, log = source(site, ARCHIVE)
    assert len(list(made.items(lambda key: False))) == 5
    assert made.gaps == 1 and any(line.endswith("gave nothing: a sign-in page") for line in log)


def test_an_empty_listing_page_by_design_is_no_gap():
    site = Trouble({"/select/mag/archiv/2024": (200, HTML, b"<html><ul class='issues'></ul></html>")})
    made, _, _ = source(site, ARCHIVE)
    assert len(list(made.items(lambda key: False))) == 5 and made.gaps == 0


# Missing #4, #5, B8: pages standing in for files, the Referer, the names the site gives.


def fetched(site: Trouble, url: str, **options) -> tuple[bytes, SiteSource]:
    made, _, _ = source(site, LIST, **options)
    dest = io.BytesIO()
    made.fetch(Item("k", "p", url), dest)
    return dest.getvalue(), made


def test_a_page_standing_in_for_the_file_is_followed_once():
    refresh = b'<html><meta http-equiv="refresh" content="3; url=/files/r.pdf">Your download starts...</html>'
    single = b'<html><a href="/help">Help</a><a href="/get?id=1">Download now</a></html>'
    site = Trouble({"/wrap1": (200, HTML, refresh), "/wrap2": (200, HTML, single), "/get": (200, {}, PDF)})
    assert fetched(site, f"{SITE}/wrap1")[0] == PDF
    assert fetched(site, f"{SITE}/wrap2")[0] == PDF
    loops = Trouble({"/w": (200, HTML, b'<meta http-equiv="refresh" content="0;url=/w">')})
    with pytest.raises(WebPage, match="the download URL gives a page \\(add a level of links, or change Download URL"):
        fetched(loops, f"{SITE}/w")
    two = Trouble({"/w": (200, HTML, b'<a href="/a.pdf">a</a><a href="/b.pdf">b</a>')})
    with pytest.raises(WebPage, match="add a level") as caught:
        fetched(two, f"{SITE}/w")
    assert not caught.value.login


def test_a_sign_in_page_instead_of_the_file_says_signed_out():
    with pytest.raises(WebPage, match="a sign-in \\(signed out\\?\\)") as caught:
        fetched(Trouble({"/f": (200, HTML, Site().answer(f"{SITE}/login")[2])}), f"{SITE}/f")
    assert caught.value.login


def test_downloads_send_their_listing_as_referer():
    site = Trouble({})
    made, _, _ = source(site, LIST)
    item, *_ = list(made.items(lambda key: False))
    assert item.referer == f"{SITE}/list"
    made.fetch(item, io.BytesIO())
    assert site.headers[-1]["referer"] == f"{SITE}/list"


@pytest.mark.parametrize(
    "header, name",
    [
        ('attachment; filename="Rechnung 2026.pdf"', "Rechnung 2026.pdf"),
        ("attachment; filename*=UTF-8''%E2%82%AC%20rates.pdf; filename=\"x.pdf\"", "€ rates.pdf"),
        ('attachment; filename="../../etc/passwd"', "passwd"),
        ('attachment; filename=".hidden"', "hidden"),
        ("inline", "a.pdf"),
        ("", "a.pdf"),
    ],
)
def test_the_name_the_site_gives_is_made_safe(header, name):
    assert download.disposition_name({"content-disposition": header} if header else {}, f"{SITE}/x/a.pdf") == name


def test_a_long_name_is_cut_keeping_its_extension():
    name = download.safe_name("n" * 300 + ".pdf")
    assert len(name) == download.MAX_NAME and name.endswith("n.pdf")


def test_a_file_named_as_the_site_sends_it():
    named = {"content-type": "application/pdf", "content-disposition": 'attachment; filename="Issue 7.pdf"'}
    site = Trouble({"/files/1-1.pdf": (200, named, PDF)})
    made, _, _ = source(site, LIST, path="docs/{disposition}", stop_at_seen=0)
    library = FakeLibrary()
    message, seen, log = run(made, library, max_items=-1)
    assert message == "2 downloaded"
    assert "/In/docs/Issue 7.pdf" in library.files and "/In/docs/1-2.pdf" in library.files
    assert f"{SITE}/files/1-1.pdf" in seen, "the seen key is the link's, whatever the name"
    assert f"Downloading {SITE}/files/1-1.pdf" in log
    assert any(line.startswith("Saved /In/docs/Issue 7.pdf") for line in log)


# Missing #1: a session that ends in the middle of a run.


class Expiring(FakeSource):
    """Items a to f; after two files a sign-in page comes instead, until signed in anew."""

    def __init__(self, can_sign_in: bool = True):
        super().__init__({}, {}, None)
        self.fetched = 0
        self.signed_in = 0
        self.can_sign_in = can_sign_in

    def items(self, seen):
        for key in "abcdef":
            if not seen(key):
                yield Item(key=key, path=f"{key}.txt", url=key)

    def fetch(self, item, dest):
        self.fetched += 1
        if self.fetched > 2 and not self.signed_in:
            raise WebPage(f"got a web page instead of a file for {item.url}: a sign-in (signed out?)", login=True)
        dest.write(item.key.encode())

    def sign_in_again(self):
        self.signed_in += self.can_sign_in
        return self.can_sign_in


def test_a_session_that_ends_mid_run_is_signed_in_anew_once():
    made = Expiring()
    message, _, log = run(made, max_items=-1)
    assert message == "6 downloaded" and made.signed_in == 1
    assert any(line.endswith(": signed in anew") for line in log)


def test_without_a_sign_in_to_repeat_failures_in_a_row_end_the_run(monkeypatch):
    monkeypatch.setattr(runner, "MAX_IN_A_ROW", 3)
    made = Expiring(can_sign_in=False)
    message, _, log = run(made, max_items=-1)
    assert message.startswith("2 downloaded, 3 failed") and made.fetched == 5
    assert "3 files in a row failed: the rest comes next run" in log


# B4: the kept session given up, and forgotten.


def test_a_kept_session_that_reaches_no_listing_is_given_up():
    site = Site(require_login=True)
    made, _, log = source(site, ARCHIVE)
    made.restore(
        {"cookies": [{"name": "sid", "value": "expired", "domain": "site.example", "path": "/"}], "origins": []}
    )
    with pytest.raises(SourceError, match="signed out, or the site changed"):
        list(made.items(lambda key: False))
    assert made.stale and made.session() is None
    assert "The kept session no longer reaches the listing: signing in anew" in log
    assert len([url for url in site.requests if url.endswith("/archiv")]) == 2, "once with it, once without"


class Stale(FakeSource):
    """A source that gave its kept session up."""

    name = "stale"

    def items(self, seen):
        self.stale = True
        yield from super().items(seen)


def test_a_session_given_up_is_forgotten_and_a_new_one_kept(tmp_path, monkeypatch):
    from harvester import executor as executor_module  # pylint: disable=import-outside-toplevel

    monkeypatch.setitem(executor_module.SOURCES, "stale", Stale)
    store = Store(str(tmp_path / "db"))
    saved = store.save(Job(user="u", name="n", source="stale", target="/x", schedule="* * * * *"))
    nc = FakeNextcloud()
    nc.files = FakeFiles()
    nc.preferences_ex.values[f"session.job{saved.id}"] = '{"cookies": []}'
    Executor(store, lambda: nc).run(saved)
    assert f"session.job{saved.id}" not in nc.preferences_ex.values

    monkeypatch.setattr(Stale, "session", lambda self: {"cookies": [{"name": "sid", "value": "new"}]})
    nc.preferences_ex.values[f"session.job{saved.id}"] = '{"cookies": []}'
    Executor(store, lambda: nc).run(saved)
    assert "new" in nc.preferences_ex.values[f"session.job{saved.id}"]


def test_a_recipe_with_other_steps_forgets_the_kept_session(tmp_path, monkeypatch):
    import main  # pylint: disable=import-outside-toplevel

    store = Store(str(tmp_path / "db"))
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", Executor(store, runners=1))
    nc = FakeNextcloud()
    nc._user = "u"
    steps = [{"action": "goto", "url": f"{SITE}/login"}]
    recipe = {"steps": steps, "start": f"{SITE}/list", "links": LIST["links"]}
    saved = store.save(
        Job(
            user="u", name="n", source="site", target="/x", schedule="* * * * *", options={"recipe": json.dumps(recipe)}
        )
    )
    values = nc.preferences_ex.values
    key = f"session.job{saved.id}"

    def update(changed: dict) -> None:
        values[key] = "{}"
        body = main.JobIn(name="n", source="site", target="/x", options={"recipe": json.dumps(changed)})
        asyncio.run(main.update_job(saved.id or 0, body, nc))

    update(recipe | {"links": {"selector": "a", "pattern": "x"}})
    assert key in values, "other links: the session stays (it may be a person's answer to a challenge)"
    update(recipe | {"links": {"selector": "a", "pattern": "x"}, "steps": steps * 2})
    assert key not in values, "other steps: it goes"
    update(recipe | {"steps": steps * 2, "start": "https://other.example/list"})
    assert key not in values, "another site: it goes"
