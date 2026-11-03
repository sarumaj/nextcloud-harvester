# SPDX-License-Identifier: AGPL-3.0-or-later
"""The queue's status for everyone (runners, browsers by jobs and recordings, the jobs queued) and
why a person's own job waits, naming nobody else's jobs or sites; jobs are their owner's alone
(another person gets 404 for everything, as for a job that does not exist), also in a database
that still has the shares table of a development version."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi import HTTPException

import main
from harvester import browser
from harvester.executor import Executor
from harvester.store import SCHEMA, Job, Store


class Preferences:
    """A user's preferences in Nextcloud (preferences_ex): none set."""

    async def get_value(self, _key):
        return None

    async def get_values(self, _keys):
        return []

    async def delete(self, _keys):
        pass


class Nextcloud:  # pylint: disable=too-few-public-methods
    """AppAPI's client as one user."""

    def __init__(self, user: str):
        self._user = user
        self.preferences_ex = Preferences()

    @property
    async def user(self):
        return self._user


class Socket:  # pylint: disable=too-few-public-methods
    """The recorder's WebSocket, opened for a job."""

    headers: dict = {}

    def __init__(self, job_id: int):
        self.query_params = {"job": str(job_id)}
        self.sent: list[dict] = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        pass


def run(awaitable):
    return asyncio.run(awaitable)


def status_of(call) -> int:
    """The HTTP status an endpoint's call ends with (200 when it goes through)."""
    try:
        run(call)
    except HTTPException as error:
        return error.status_code
    return 200


def feed(store: Store, user: str, name: str, url: str = "https://feed.example/rss") -> Job:
    return store.save(Job(user=user, name=name, source="feed", target="/x", schedule="0 6 * * *", options={"url": url}))


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """The app's state fresh with an executor of one runner (not started), and Anna's job."""
    store = Store(str(tmp_path / "h.db"))
    executor = Executor(store, runners=1)
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", executor)
    return store, executor, feed(store, "anna", "Podcast")


def test_the_status_and_the_reasons_name_nothing_of_others(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "SLOTS", browser.Slots(2))
    store = Store(str(tmp_path / "db"))
    executor = Executor(store, runners=1)
    theirs = feed(store, "eve", "Eves secret", "https://shared.example/rss")
    queued_first = feed(store, "eve", "Eves other", "https://private.example/rss")
    same_site = feed(store, "anna", "Mine", "https://shared.example/other")
    behind = feed(store, "anna", "Mine too", "https://news.example/rss")
    assert executor._claim(theirs)
    for job in (queued_first, same_site, behind):
        executor.request(job.id or 0)
    assert browser.SLOTS.acquire(blocking=False, kind="job") and browser.SLOTS.acquire(blocking=False)
    status = executor.status()
    assert status == {
        "runners": {"busy": 1, "total": 1},
        "browsers": {"jobs": 1, "recordings": 1, "total": 2},
        "queued": 3,
    }
    reasons = executor.waits([same_site, behind])
    assert reasons == {
        same_site.id: "2. in the queue: waits for its site (shared.example), used by another job",
        behind.id: "3. in the queue: waits for a runner, all 1 busy (2 jobs ahead)",
    }
    words = json.dumps([status, reasons])
    assert "private.example" not in words and "eve" not in words.lower() and "Eves" not in words
    browser.SLOTS.release("job")
    browser.SLOTS.release()


def test_a_running_job_that_waits_for_a_browser_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "SLOTS", browser.Slots(1))
    store = Store(str(tmp_path / "db"))
    executor = Executor(store, runners=2)
    job = store.save(Job(user="anna", name="Site", source="site", target="/x", schedule="0 6 * * *"))
    assert executor._claim(job)
    assert browser.SLOTS.acquire(blocking=False), "a recording holds the one browser"
    waiting = threading.Thread(target=browser.SLOTS.acquire, kwargs={"timeout": 5, "kind": "job"})
    waiting.start()
    executor.controls[job.id or 0].thread = waiting.ident or 0
    time.sleep(0.1)
    assert executor.waits([job]) == {job.id: "Waits for a browser: all 1 in use (0 by jobs, 1 by recordings)"}
    browser.SLOTS.release()
    waiting.join(2)
    assert not executor.waits([job]), "it has its browser"
    assert browser.SLOTS.taken() == {"job": 1, "recording": 0}
    browser.SLOTS.release("job")


def test_the_api_gives_the_status_to_all_and_the_reasons_to_the_owner(app):
    store, executor, job = app
    other = feed(store, "eve", "E", "https://eve.example/rss")
    assert executor._claim(other)
    executor.request(job.id or 0)
    listed = run(main.list_jobs(Nextcloud("anna")))
    assert listed[0]["waiting"] == "1. in the queue: waits for a runner, all 1 busy (0 jobs ahead)"
    log = run(main.progress_log(Nextcloud("anna")))
    assert log["queued"] == [job.id] and log["waiting"] == {str(job.id): listed[0]["waiting"]}
    assert log["status"]["runners"] == {"busy": 1, "total": 1} and log["status"]["queued"] == 1
    assert main.queue_status() == executor.status()
    stranger = run(main.progress_log(Nextcloud("ben")))
    assert stranger["queued"] == [] and stranger["waiting"] == {} and stranger["status"]["queued"] == 1
    assert "eve" not in json.dumps([listed, log, stranger]).lower()


def test_the_brief_list_has_each_jobs_address_not_its_options_and_a_job_has_them(app):
    _, _, job = app
    full = run(main.list_jobs(Nextcloud("anna")))
    brief = run(main.list_jobs(Nextcloud("anna"), brief=True))
    assert full[0]["options"] == job.options, "as before, for API clients (Talk AI)"
    assert "options" not in brief[0] and brief[0]["address"] == job.options["url"]
    rest = {key: value for key, value in brief[0].items() if key != "address"}
    assert rest == {key: value for key, value in full[0].items() if key != "options"}
    assert run(main.get_job(job.id or 0, Nextcloud("anna")))["options"] == job.options


def test_the_list_and_the_log_read_the_waiting_addresses_once(app, monkeypatch):
    store, executor, job = app
    executor.request(job.id or 0)
    reads: list[object] = []
    reading = store.fetches
    monkeypatch.setattr(store, "fetches", lambda jobs=None: reads.append(jobs) or reading(jobs))
    run(main.list_jobs(Nextcloud("anna"), brief=True))
    run(main.progress_log(Nextcloud("anna")))
    assert reads == [[job.id], [job.id]], "once for the reasons and the addresses together"


def test_the_recorder_refused_says_what_holds_the_browsers(app, monkeypatch):
    _, _, job = app
    monkeypatch.setattr(browser, "SLOTS", browser.Slots(1))
    socket = Socket(job.id or 0)
    allowed, opened = run(main.admit(socket, Nextcloud("ben")))
    assert not allowed and opened is None and socket.sent[-1]["message"] == "No such job.", "not Ben's"
    allowed, opened = run(main.admit(socket, Nextcloud("anna")))
    assert allowed and opened and opened.id == job.id
    # The one browser is Anna's now: the next is refused with what holds them, not whose.
    allowed, _ = run(main.admit(socket, Nextcloud("anna")))
    assert not allowed and socket.sent[-1]["message"] == (
        "No browser is free: all 1 in use (0 by jobs, 1 by recordings). Try again in a few minutes."
    )
    browser.SLOTS.release()


def test_a_job_is_its_owners_alone(app):
    store, _, job = app
    job_id = job.id or 0
    ben = Nextcloud("ben")
    assert run(main.list_jobs(ben)) == []
    calls = [
        main.update_job(
            job_id, main.JobIn(name="b", source="feed", target="/b", options={"url": "https://b.example/"}), ben
        ),
        main.delete_job(job_id, ben),
        main.get_shot(job_id, ben),
        main.get_job(job_id, ben),
        main.run_job(job_id, ben),
        main.pause_job(job_id, ben),
        main.resume_job(job_id, ben),
        main.stop_job(job_id, ben),
        main.get_credentials(job_id, ben),
        main.set_credentials(job_id, {"password": "x"}, ben),
        main.progress_log(ben, job_id),
        main.fetch_url(job_id, main.FetchIn(url="https://feed.example/other"), ben),
        main.cancel_fetch(job_id, 1, ben),
    ]
    assert [status_of(call) for call in calls] == [404] * len(calls)
    assert store.job(job_id) == job, "unchanged"
    assert not any(
        path.startswith(("/api/sharees", "/api/jobs/{job_id}/shares")) for path in main.APP.openapi()["paths"]
    )


def test_a_database_with_the_shares_table_of_a_development_version_works(tmp_path):
    path = str(tmp_path / "h.db")
    # The table of the sharing that was never released, as a development build made it.
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute(
            "CREATE TABLE shares (job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, kind TEXT NOT NULL,"
            " sharee TEXT NOT NULL, label TEXT NOT NULL, permissions INTEGER NOT NULL, by TEXT NOT NULL,"
            " at REAL NOT NULL, PRIMARY KEY (job, kind, sharee))"
        )
        db.execute(
            "INSERT INTO jobs (id, user, name, source, options, target, tags, schedule, enabled, created)"
            " VALUES (3, 'anna', 'a', 'feed', '{}', '/x', '[]', '0 6 * * *', 1, 0)"
        )
        db.execute("INSERT INTO shares VALUES (3, 'user', 'ben', 'Ben', 31, 'anna', 0)")
    store = Store(path)
    assert [job.id for job in store.jobs("anna")] == [3] and store.jobs("ben") == []
    store.delete(3)
    assert store.job(3) is None
