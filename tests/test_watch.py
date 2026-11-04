# SPDX-License-Identifier: AGPL-3.0-or-later
"""Warnings that "the site changed": what a website job's runs find, level by level, kept per job;
a level that found links before and finds none (or no match) ends the run "changed", told once; a
listing that never found anything is no change; the next good run is OK again. The weekly recipe
test: its schedule, its queue, and its judgement without a run. The site source walks the fake
archive over HTTP (httpx's MockTransport), in the executor as a job's run."""

# pylint: disable=missing-function-docstring,protected-access

import datetime
import json
import threading
import time

import httpx
import pytest
from fake_site import SITE, YEARS, Site, year_page
from test_runner import FakeNextcloud

from harvester import executor as executor_module
from harvester import watch
from harvester.sources import site as site_module
from harvester.store import Job, Store

ARCHIVE = {
    "start": f"{SITE}/select/mag/archiv",
    "follow": [{"selector": "a[href^='/select/mag/archiv/']", "pattern": r"/archiv/(?P<year>\d{4})$"}],
    "links": {"selector": "a.year__link", "pattern": r"/archiv/(?P<year>\d{4})/(?P<issue>\d+)$"},
}
ISSUES = [f"{SITE}/select/mag/archiv/{year}/{n}" for year, count in YEARS.items() for n in range(1, count + 1)]


class Redesigned(Site):  # pylint: disable=too-few-public-methods
    """The archive after a redesign: its years' pages list the issues with another class (the
    recipe's selector finds nothing), or (moved) at other addresses (its pattern matches none)."""

    def __init__(self, moved: bool = False):
        super().__init__()
        self.moved = moved

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        status, headers, body = super().answer(url, method, cookie)
        for year in YEARS:
            if url == f"{SITE}/select/mag/archiv/{year}":
                page = year_page(year)
                page = page.replace("/archiv/", "/heft/") if self.moved else page.replace("year__link", "issue")
                return status, headers, page.encode()
        return status, headers, body


class Archive:
    """The site the runs reach (the test changes it between them), and the job's run."""

    def __init__(self, tmp_path, monkeypatch, recipe: dict | None = None):
        self.site: Site = Site()

        def client(**_options) -> httpx.Client:
            def handler(request: httpx.Request) -> httpx.Response:
                status, headers, body = self.site.answer(str(request.url), request.method)
                return httpx.Response(status, headers=headers, content=body)

            return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

        monkeypatch.setattr(executor_module, "guarded_client", client)
        self.store = Store(str(tmp_path / "db"))
        options = {"recipe": json.dumps(recipe or ARCHIVE), "file_type": "pdf", "stop_at_seen": 0}
        self.job = self.store.save(
            Job(user="u", name="Mag", source="site", target="/Mag", schedule="0 6 * * *", options=options)
        )
        # Every issue is known: a run walks the whole archive and downloads nothing.
        for key in ISSUES:
            self.store.mark_seen(self.job.id or 0, key)
        self.nc = FakeNextcloud()
        self.executor = executor_module.Executor(self.store, lambda: self.nc)

    def run(self) -> Job:
        self.executor.run(self.store.job(self.job.id or 0) or self.job)
        return self.store.job(self.job.id or 0) or self.job

    def test(self) -> Job:
        self.executor.test(self.store.job(self.job.id or 0) or self.job)
        return self.store.job(self.job.id or 0) or self.job

    def log(self) -> list[dict]:
        return self.store.log_lines("u", self.job.id)


# Runs: what they found, and when it is less.


def test_a_run_keeps_what_each_level_found(tmp_path, monkeypatch):
    archive = Archive(tmp_path, monkeypatch)
    assert archive.run().last_status == "ok"
    [summary] = archive.store.summaries(archive.job.id or 0)
    levels = summary["levels"]
    # The years, and the newest issue the pattern leaves out.
    assert levels["0"] == {
        "kind": "follow",
        "selector": ARCHIVE["follow"][0]["selector"],
        "pages": 1,
        "found": 4,
        "matching": 3,
        "url": "",
    }
    assert levels["1"] == {
        "kind": "links",
        "selector": "a.year__link",
        "pages": 3,
        "found": 8,
        "matching": 8,
        "url": "",
    }


def test_a_level_that_finds_nothing_is_a_changed_site_told_once_until_it_is_back(tmp_path, monkeypatch):
    # What the browser shows (the fake site's pages are read over HTTP, which shows none).
    monkeypatch.setattr(
        site_module, "snapshot", lambda page, what: {"image": b"x", "url": "u", "title": "t", "what": what, "at": 1.0}
    )
    archive = Archive(tmp_path, monkeypatch)
    archive.run()
    assert archive.store.shot(archive.job.id or 0) is None, "nothing to show while the site is as before"
    last_run = archive.store.job(archive.job.id or 0).last_run  # type: ignore[union-attr]
    archive.site = Redesigned()
    changed = archive.run()
    assert changed.last_status == "changed"
    assert (
        changed.last_message
        == "The site seems to have changed: level 1 found nothing (it found 8 before); 0 downloaded"
    )
    assert (changed.last_run or 0) >= (last_run or 0), "a run, all the same"
    warning = [line for line in archive.log() if line["level"] == "warning"]
    assert [line["message"] for line in warning] == [
        "The site seems to have changed: level 1 found nothing (it found 8 before); file links a.year__link, on 3"
        f" pages, first at {SITE}/select/mag/archiv/2026"
    ]
    assert warning[0]["shot"], "with what the browser saw at the first page that found nothing"
    assert (archive.store.shot(archive.job.id or 0) or {})["what"] == "level 1 found nothing (it found 8 before)"
    assert archive.nc.sent == [
        (
            "Harvester: Mag — the site seems to have changed: level 1 found nothing (it found 8 before)",
            "level 1 found nothing (it found 8 before); file links a.year__link, on 3 pages, first at "
            f"{SITE}/select/mag/archiv/2026. Test the recipe (its form's recorder) and pick the links anew.",
            "https://cloud.example/index.php/apps/app_api/embedded/harvester/harvester",
        )
    ]
    assert archive.run().last_status == "changed"
    assert len(archive.nc.sent) == 1, "told once: the same change on the next runs is no news"
    assert len(archive.store.summaries(archive.job.id or 0)) == 1, "a changed run is no measure"
    assert len(archive.store.summaries(archive.job.id or 0, good=False)) == 2

    archive.site = Site()
    back = archive.run()
    assert back.last_status == "ok" and back.last_message == "0 downloaded, 8 known"
    archive.site = Redesigned()
    archive.run()
    assert len(archive.nc.sent) == 2, "changed again after it was back: told again"


def test_links_the_pattern_no_longer_matches_are_a_changed_site(tmp_path, monkeypatch):
    archive = Archive(tmp_path, monkeypatch)
    archive.run()
    archive.site = Redesigned(moved=True)
    changed = archive.run()
    assert changed.last_status == "changed"
    assert changed.last_message == (
        "The site seems to have changed: level 1: the pattern no longer matches (none of 8 links; 8 matched before);"
        " 0 downloaded"
    )


def test_a_listing_without_its_links_fails_or_is_changed_where_it_had_them(tmp_path, monkeypatch):
    class Empty(Site):  # pylint: disable=too-few-public-methods
        """The archive without its years."""

        def answer(self, url, method="GET", cookie=""):
            if url == ARCHIVE["start"]:
                return 200, {"content-type": "text/html"}, b"<html><body>Under construction</body></html>"
            return super().answer(url, method, cookie)

    archive = Archive(tmp_path, monkeypatch)
    archive.run()
    archive.site = Empty()
    changed = archive.run()
    assert changed.last_status == "changed"
    assert changed.last_message.startswith(
        "The site seems to have changed: the listing found nothing (it found 4 before); the start page has none"
    )


def test_a_listing_that_never_found_anything_is_no_change(tmp_path, monkeypatch):
    recipe = ARCHIVE | {"follow": [], "links": {"selector": "a.nothing", "pattern": "pdf"}}
    archive = Archive(tmp_path, monkeypatch, recipe)
    for _ in range(2):
        job = archive.run()
        assert job.last_status == "failed" and "the start page has none" in (job.last_message or "")
    assert not [line for line in archive.log() if line["level"] == "warning"]
    assert [subject for subject, _, _ in archive.nc.sent] == ["Harvester: Mag failed"]


def test_an_on_demand_fetch_is_not_judged(tmp_path, monkeypatch):
    archive = Archive(tmp_path, monkeypatch)
    archive.run()
    archive.site = Redesigned()
    job = archive.store.job(archive.job.id or 0)
    assert job is not None
    archive.executor.run(job, fetch={"id": 1, "url": f"{SITE}/select/mag/archiv"})
    assert (archive.store.job(job.id or 0) or job).last_status == "ok", "another address is another page"
    assert len(archive.store.summaries(job.id or 0)) == 1


def test_a_page_of_a_changed_level_asks_for_a_picture():
    found = watch.Watch([{"levels": {"1": {"selector": "a.issue", "found": 12, "matching": 12}}}])
    event = {"level": 1, "page": 1, "url": "https://s.example/2026", "kind": "links", "selector": "a.issue"}
    assert found(event | {"found": 0, "matching": 0}) == "level 1 found nothing (it found 12 before)"
    assert found(event | {"found": 0, "matching": 0}) == "", "one picture a run"
    assert found(event | {"found": 3, "matching": 3}) == ""
    assert found.change() is None, "the level as a whole found links"
    other = watch.Watch([{"levels": {"1": {"selector": "a.old", "found": 12, "matching": 12}}}])
    assert other(event | {"found": 0, "matching": 0}) == "", "another selector: nothing to compare with"


# The weekly test.


def test_the_weekly_test_comes_on_sundays_at_the_jobs_own_hour():
    job = Job(
        user="u",
        name="n",
        source="site",
        target="/x",
        schedule="0 6 * * *",
        options={"recipe": json.dumps(ARCHIVE), "weekly_test": 1},
        id=7,
        created=datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc).timestamp(),  # a Monday
        timezone="Europe/Berlin",
    )
    minute, hour, *rest = watch.weekly(7).split()
    assert rest == ["*", "*", "0"] and 0 <= int(minute) < 60 and 0 <= int(hour) < 24
    assert watch.weekly(7) == watch.weekly(7), "the same every week"
    assert len({watch.weekly(job_id) for job_id in range(1, 40)}) > 30, "jobs spread over the week's Sunday"
    # Sunday's hour in the job's time zone.
    sunday = datetime.datetime(2026, 11, 8, int(hour), int(minute), tzinfo=executor_module.zone("Europe/Berlin"))
    assert not watch.test_due(job, None, sunday.timestamp() - 60)
    assert watch.test_due(job, None, sunday.timestamp())
    assert not watch.test_due(job, sunday.timestamp(), sunday.timestamp() + 6 * 86400), "a week from the last"
    for other in (
        Job(**{**vars(job), "options": {"recipe": json.dumps(ARCHIVE)}}),
        Job(**{**vars(job), "enabled": False}),
        Job(**{**vars(job), "last_status": "challenge"}),
        Job(**{**vars(job), "options": {"recipe": "{}", "weekly_test": 1}, "schedule": ""}),
    ):
        assert not watch.test_due(other, None, sunday.timestamp()), other


def test_the_weekly_test_is_queued_as_a_run_is_and_a_run_takes_its_place(tmp_path, monkeypatch):
    archive = Archive(tmp_path, monkeypatch)
    job = archive.job
    # On demand: no run comes due meanwhile, only the test.
    job.options["weekly_test"] = 1
    job.schedule = ""
    archive.store.save(job)
    sunday = time.time() + 8 * 86400
    archive.executor.schedule_turn(sunday)
    assert archive.executor._tests == {job.id} and archive.executor.queued == [job.id]
    assert archive.store.tested(job.id or 0) == sunday, "not asked for again while it waits"
    assert archive.executor.waits([job]) == {job.id: "1. in the queue: starts in a moment"}
    archive.executor.schedule_turn(sunday + 60)
    assert archive.executor.requests.qsize() == 1

    done: list[tuple[str, int]] = []
    finished = threading.Event()

    def run(job_, wanted=True, fetch=None):
        del fetch
        done.append(("run" if wanted else "pass", job_.id))
        finished.set()

    def test(job_):
        done.append(("test", job_.id))
        finished.set()

    monkeypatch.setattr(archive.executor, "run", run)
    monkeypatch.setattr(archive.executor, "test", test)
    threading.Thread(target=archive.executor._runner, daemon=True).start()
    assert finished.wait(10)
    time.sleep(0.2)
    assert done == [("pass", job.id), ("test", job.id)], "a pending pass for the tags first, then the test"

    # A run asked for besides: it judges the site, the test is not needed.
    finished.clear()
    done.clear()
    archive.executor.request_test(job.id or 0)
    archive.executor.request(job.id or 0)
    assert finished.wait(10)
    time.sleep(0.2)
    assert done == [("run", job.id)] and not archive.executor._tests


def test_the_recipes_test_warns_without_a_run_and_ends_the_warning(tmp_path, monkeypatch):
    archive = Archive(tmp_path, monkeypatch)
    archive.run()
    last_run = archive.store.job(archive.job.id or 0).last_run  # type: ignore[union-attr]
    archive.site = Redesigned()
    tested = archive.test()
    assert tested.last_status == "changed"
    assert tested.last_message == "The site seems to have changed: level 1 found nothing (it found 8 before)"
    assert tested.last_run == last_run, "a test is no run: the schedule counts from the last"
    assert len(archive.nc.sent) == 1
    messages = [line["message"] for line in archive.log()]
    assert "Testing the recipe (nothing is downloaded)" in messages
    assert any(message.startswith("Test: a level 1 page (page 1") for message in messages)
    assert not archive.executor.controls, "its runner is free again"

    archive.site = Site()
    back = archive.test()
    assert back.last_status == "ok" and back.last_message == "The recipe's test found the site as before"
    assert archive.log()[-1]["message"] == "The recipe's test: 8 file links on 4 pages: as before"
    assert len(archive.nc.sent) == 1


def test_a_test_that_meets_a_person_check_is_no_change(tmp_path, monkeypatch):
    archive = Archive(tmp_path, monkeypatch)
    archive.run()
    archive.site = Site()
    archive.site.challenge.add("/select/mag/archiv")
    tested = archive.test()
    assert tested.last_status == "ok" and not archive.nc.sent
    assert "asks for a person" in archive.log()[-1]["message"]


def test_the_api_tests_a_website_job_only(tmp_path, monkeypatch):
    import main  # pylint: disable=import-outside-toplevel
    from fastapi import HTTPException  # pylint: disable=import-outside-toplevel

    archive = Archive(tmp_path, monkeypatch)
    monkeypatch.setitem(main.STATE, "store", archive.store)
    monkeypatch.setitem(main.STATE, "executor", archive.executor)
    archive.nc._user = "u"
    import asyncio  # pylint: disable=import-outside-toplevel

    asyncio.run(main.test_job(archive.job.id or 0, archive.nc))
    assert archive.executor._tests == {archive.job.id}
    feed = archive.store.save(Job(user="u", name="F", source="feed", target="/f", schedule="0 6 * * *"))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.test_job(feed.id or 0, archive.nc))
    assert refused.value.status_code == 409


def test_turning_the_weekly_test_on_counts_from_then(tmp_path, monkeypatch):
    import main  # pylint: disable=import-outside-toplevel

    store = Store(str(tmp_path / "db"))
    monkeypatch.setitem(main.STATE, "store", store)
    job = store.save(Job(user="u", name="n", source="site", target="/x", schedule="0 6 * * *", options={}))
    main.weekly_test_from_now(job, job)
    assert store.tested(job.id or 0) is None
    on = Job(**{**vars(job), "options": {"weekly_test": 1}})
    before = time.time()
    main.weekly_test_from_now(job, on)
    assert (store.tested(job.id or 0) or 0) >= before
    assert "weekly_test" in {
        key for source in main.list_sources() if source["name"] == "site" for key in source["options"]
    }
    assert all("weekly_test" not in source["options"] for source in main.list_sources() if source["name"] != "site")
