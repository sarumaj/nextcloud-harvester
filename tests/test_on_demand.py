# SPDX-License-Identifier: AGPL-3.0-or-later
"""On-demand jobs: never due, only of sources with an address; a fetch's address checked as the
source checks its own (and kept to the site of the job's account); the addresses queued in order,
each once, kept across a restart and taken one per turn; a fetch's run with its address while
the job keeps its own options, files fetched before not again; a one-time job deleted after a
run that went through, kept after one that failed; and the API's fetch and cancel."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import threading
import time

import pytest
from fastapi import HTTPException
from test_runner import FakeLibrary, FakeNextcloud, FakeSource

import main
from harvester import browser, ondemand
from harvester import executor as executor_module
from harvester.executor import Executor, is_due
from harvester.sources import Item, Option, SourceError
from harvester.store import Job, Store

# The files at each address of the fake site: page 2 has b again.
PAGES = {"https://v.example/1": ["a", "b"], "https://v.example/2": ["b", "c"], "https://v.example/own": ["o"]}
# The addresses the fake source's runs listed, in order.
LISTED: list[str] = []


class Pages(FakeSource):
    """A site of pages with files (PAGES), its address checked as the real sources check theirs."""

    name = "pages"
    title = "Pages"
    pause = 0.0
    credentials = ("username", "password")
    options = {"url": Option("Page", "", kind="url")}
    address_option = "url"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = str(self.settings["url"]).strip()
        if not browser.allowed_url(self.url):
            raise SourceError(f"the page {self.url or '(none)'} may not be opened")

    def items(self, seen):
        LISTED.append(self.url)
        if self.url not in PAGES:
            raise SourceError(f"{self.url} answered 404")
        for key in PAGES[self.url]:
            if not seen(key):
                yield Item(key=key, path=f"{key}.txt", url=key)


class Preferences:  # pylint: disable=too-few-public-methods
    """A user's preferences (their jobs' accounts)."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    async def get_value(self, key):
        return self.values.get(key)


class Nextcloud:  # pylint: disable=too-few-public-methods
    """AppAPI's client as one user."""

    def __init__(self, user: str, preferences: dict[str, str] | None = None):
        self._user = user
        self.preferences_ex = Preferences(preferences)

    @property
    async def user(self):
        return self._user


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """The fake site as a source, the app's state with an executor (not started) whose runs use
    fakes of Nextcloud and its files, and Anna's on-demand job without an address of its own."""
    LISTED.clear()
    monkeypatch.setitem(executor_module.SOURCES, "pages", Pages)
    monkeypatch.setattr(executor_module, "NextcloudLibrary", lambda nc, call: FakeLibrary())
    store = Store(str(tmp_path / "h.db"))
    nc = FakeNextcloud()
    executor = Executor(store, lambda: nc, runners=1)
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", executor)
    job = store.save(Job(user="anna", name="Clips", source="pages", target="/Clips", schedule=""))
    return store, executor, job, nc


def status_of(call) -> int:
    """The HTTP status an endpoint's call ends with (200 when it goes through)."""
    try:
        asyncio.run(call)
    except HTTPException as error:
        return error.status_code
    return 200


def messages(store: Store, job_id: int) -> list[str]:
    return [line["message"] for line in store.log_lines("anna", job_id)]


def test_an_on_demand_job_is_never_due():
    scheduled = Job(user="u", name="n", source="feed", target="/x", schedule="0 6 * * *", created=0.0)
    on_demand = Job(user="u", name="n", source="feed", target="/x", schedule="", created=0.0)
    assert is_due(scheduled, time.time()) and not is_due(on_demand, time.time() + 10 * 365 * 86400)


def test_only_sources_with_an_address_have_on_demand_jobs():
    for source in ("mail", "remote", "releases"):
        with pytest.raises(HTTPException, match="runs on a schedule"):
            main.checked(main.JobIn(name="n", source=source, target="/x", schedule=""))
    # Without an address of its own (each fetch brings one); its other options checked all the same.
    assert main.checked(main.JobIn(name="n", source="media", target="/x", schedule="", once=True))
    with pytest.raises(HTTPException, match="Entries per run"):
        main.checked(main.JobIn(name="n", source="media", target="/x", schedule="", options={"max_entries": "x"}))
    with pytest.raises(HTTPException, match="may not be opened"):
        main.checked(main.JobIn(name="n", source="media", target="/x", schedule="0 6 * * *"))
    with pytest.raises(HTTPException, match="Only an on-demand job"):
        main.checked(
            main.JobIn(name="n", source="media", target="/x", options={"url": "https://v.example/"}, once=True)
        )
    assert [source["name"] for source in main.list_sources() if source["address"]] == [
        "site",
        "feed",
        "media",
        "gallery",
    ]


def test_a_site_jobs_address_is_its_recipes_listing():
    recipe = '{"start": "https://site.example/2026", "links": {"selector": "a", "pattern": "pdf"}}'
    job = Job(user="u", name="n", source="site", target="/x", schedule="", options={"recipe": recipe})
    fetched = ondemand.for_fetch(job, "https://site.example/2025")
    assert '"start": "https://site.example/2025"' in fetched.options["recipe"]
    assert '"selector": "a"' in fetched.options["recipe"], "its levels and file links stay"
    assert job.options["recipe"] == recipe, "the job's own options do not change"
    assert executor_module.SOURCES["site"].address_of(job.options) == "https://site.example/2026"


def test_a_fetchs_address_is_checked_as_its_source_checks_its_own(app):
    _, _, job, _ = app
    assert ondemand.fetch_problem(job, "https://v.example/1", account=False) == ""
    assert "may not be opened" in ondemand.fetch_problem(job, "http://127.0.0.1/admin", account=False)
    assert "may not be opened" in ondemand.fetch_problem(job, "https://intranet.local/x", account=False)
    assert ondemand.fetch_problem(job, "ftp://v.example/1", account=False) == "Not a web address (http or https)."
    scheduled = Job(user="anna", name="n", source="pages", target="/x", schedule="0 6 * * *")
    assert "not on demand" in ondemand.fetch_problem(scheduled, "https://v.example/1", account=False)
    # With an account and an address of its own, only that site's addresses: the account is its.
    own = Job(**(vars(job) | {"options": {"url": "https://www.v.example/own"}}))
    assert ondemand.fetch_problem(own, "https://m.v.example/1", account=True) == ""
    assert ondemand.fetch_problem(own, "https://other.example/1", account=True) == (
        "The job's account is for www.v.example: it fetches only addresses of that site."
    )
    assert ondemand.fetch_problem(own, "https://other.example/1", account=False) == ""


def test_the_api_fetches_for_the_owner_and_checks_first(app):
    store, executor, job, _ = app
    job_id = job.id or 0

    def fetch(url: str, user: str = "anna", preferences: dict | None = None):
        return main.fetch_url(job_id, main.FetchIn(url=url), Nextcloud(user, preferences))

    assert asyncio.run(fetch(" https://v.example/1 ")) | {"at": 0} == {
        "id": 1,
        "url": "https://v.example/1",
        "at": 0,
        "new": True,
    }
    assert asyncio.run(fetch("https://v.example/1"))["new"] is False, "waits already: keeps its place"
    assert status_of(fetch("http://10.0.0.1/")) == 422
    assert status_of(fetch("https://v.example/2", user="ben")) == 404, "Anna's job"
    store.save(Job(**(vars(job) | {"options": {"url": "https://v.example/own"}})))
    account = {f"credential.job{job_id}.password": "secret"}
    with pytest.raises(HTTPException, match="account is for v.example"):
        asyncio.run(fetch("https://other.example/x", preferences=account))
    assert [fetch["url"] for fetch in store.fetches([job_id])] == ["https://v.example/1"]
    assert executor.queued == [job_id]
    assert (store.job(job_id) or job).options == {"url": "https://v.example/own"}, "the job's options stay"


def test_addresses_queue_in_order_once_each_and_survive_a_restart(app, monkeypatch, tmp_path):
    store, executor, job, _ = app
    job_id = job.id or 0
    first, new = executor.fetch(job_id, "https://v.example/1")
    assert new
    assert executor.fetch(job_id, "https://v.example/2")[1]
    assert executor.fetch(job_id, "https://v.example/1") == (first, False)
    assert [fetch["url"] for fetch in store.fetches()] == ["https://v.example/1", "https://v.example/2"]
    assert executor.queued == [job_id] and executor.requests.qsize() == 1, "the job queued once"
    other = store.save(Job(user="eve", name="E", source="feed", target="/x", schedule="0 6 * * *"))
    assert executor._claim(other)
    assert executor.waits([job]) == {
        job_id: "1. in the queue: waits for a runner, all 1 busy (0 jobs ahead); 2 addresses queued"
    }
    # A restart: the addresses are the database's, and the job is queued again at the start.
    started: list[str] = []

    class Thread:  # pylint: disable=too-few-public-methods
        """No schedule and no runners: only what start() queues is looked at."""

        def __init__(self, name="", **_kwargs):
            self.name = name

        def start(self):
            started.append(self.name)

    monkeypatch.setattr(executor_module.threading, "Thread", Thread)
    again = Executor(Store(str(tmp_path / "h.db")), runners=1)
    again.start()
    assert again.queued == [job_id] and "schedule" in started
    assert [fetch["url"] for fetch in again.store.fetches([job_id])] == ["https://v.example/1", "https://v.example/2"]


def test_a_fetch_runs_its_address_and_the_job_keeps_its_own(app):
    store, executor, job, _ = app
    job_id = job.id or 0
    own = store.save(Job(**(vars(job) | {"options": {"url": "https://v.example/own"}})))
    fetch, _ = executor.fetch(job_id, "https://v.example/1")
    executor.run(own, wanted=False, fetch=fetch)
    assert LISTED == ["https://v.example/1"]
    saved = store.job(job_id)
    assert saved is not None and saved.options == {"url": "https://v.example/own"}
    assert saved.last_status == "ok" and saved.last_message == "https://v.example/1: 2 downloaded"
    lines = messages(store, job_id)
    assert lines[0] == "Fetching https://v.example/1" and lines[-1] == "https://v.example/1: 2 downloaded"
    assert store.fetches() == [], "fetched: out of the queue"
    # Run now runs its own address; without one it is refused.
    executor.run(store.job(job_id) or own)
    assert LISTED[-1] == "https://v.example/own"
    store.save(Job(**(vars(saved) | {"options": {}})))
    assert status_of(main.run_job(job_id, Nextcloud("anna"))) == 409


def test_files_fetched_before_are_not_fetched_again_and_the_log_says_so(app):
    store, executor, job, _ = app
    job_id = job.id or 0
    for url in ("https://v.example/1", "https://v.example/2"):
        fetch, _ = executor.fetch(job_id, url)
        executor.run(store.job(job_id) or job, wanted=False, fetch=fetch)
    lines = messages(store, job_id)
    assert "1 of the files at https://v.example/2 were fetched before: not again" in lines
    assert lines[-1] == "https://v.example/2: 1 downloaded", "c alone"
    assert store.seen_keys(job_id) >= {"a", "b", "c"}, "seen is the job's, across its addresses"


def test_a_failed_fetch_leaves_the_queue_and_says_why(app):
    store, executor, job, _ = app
    job_id = job.id or 0
    fetch, _ = executor.fetch(job_id, "https://v.example/missing")
    executor.run(job, wanted=False, fetch=fetch)
    saved = store.job(job_id)
    assert saved is not None and saved.last_status == "failed"
    assert saved.last_message == "https://v.example/missing answered 404"
    assert store.fetches() == []


def test_the_runner_takes_the_addresses_one_after_another(app):
    store, executor, job, _ = app
    job_id = job.id or 0
    for url in ("https://v.example/2", "https://v.example/1"):
        executor.fetch(job_id, url)
    threading.Thread(target=executor._runner, daemon=True).start()
    deadline = time.monotonic() + 30
    while (store.fetches() or executor.running) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert LISTED == ["https://v.example/2", "https://v.example/1"], "in the order they were sent"
    assert not executor.queued and not store.fetches()


def test_a_one_time_job_goes_after_a_run_that_went_through(app):
    store, executor, job, nc = app
    job_id = job.id or 0
    once = store.save(Job(**(vars(job) | {"once": True})))
    nc.preferences_ex.values = {f"credential.job{job_id}.password": "p", f"session.job{job_id}": "{}", "other": "x"}
    failing, _ = executor.fetch(job_id, "https://v.example/missing")
    second, _ = executor.fetch(job_id, "https://v.example/1")
    executor.run(once, wanted=False, fetch=failing)
    assert store.job(job_id) is not None, "failed: kept, so its person sees why"
    executor.run(store.job(job_id) or once, wanted=False, fetch=second)
    assert store.job(job_id) is None and store.fetches() == []
    assert nc.preferences_ex.values == {"other": "x"}, "its account and session go with it"
    assert nc.sent[-1][:2] == (
        "Harvester: Clips done",
        "https://v.example/1: 2 downloaded. The one-time job is deleted; its files stay.",
    )


def test_a_one_time_job_with_more_addresses_waits_for_them(app):
    store, executor, job, _ = app
    job_id = job.id or 0
    once = store.save(Job(**(vars(job) | {"once": True})))
    first, _ = executor.fetch(job_id, "https://v.example/1")
    executor.fetch(job_id, "https://v.example/2")
    executor.run(once, wanted=False, fetch=first)
    assert store.job(job_id) is not None, "the next address is still to come"


def test_a_waiting_address_is_cancelled_and_a_running_one_is_stopped_instead(app):
    store, executor, job, _ = app
    job_id = job.id or 0
    anna = Nextcloud("anna")
    first, _ = executor.fetch(job_id, "https://v.example/1")
    second, _ = executor.fetch(job_id, "https://v.example/2")
    executor.controls[job_id] = executor_module.Control()
    executor.controls[job_id].fetch = first["id"]
    listed = asyncio.run(main.list_jobs(anna))[0]["fetches"]
    assert [(fetch["url"], fetch["running"]) for fetch in listed] == [
        ("https://v.example/1", True),
        ("https://v.example/2", False),
    ]
    assert asyncio.run(main.progress_log(anna))["fetches"] == {str(job_id): [first["id"], second["id"]]}
    assert status_of(main.cancel_fetch(job_id, first["id"], anna)) == 409
    assert status_of(main.cancel_fetch(job_id, second["id"], Nextcloud("ben"))) == 404
    assert status_of(main.cancel_fetch(job_id, second["id"], anna)) == 200
    assert status_of(main.cancel_fetch(job_id, second["id"], anna)) == 404
    assert [fetch["id"] for fetch in store.fetches()] == [first["id"]]


def test_a_job_put_on_a_schedule_forgets_its_waiting_addresses(app):
    store, executor, job, _ = app
    job_id = job.id or 0
    executor.fetch(job_id, "https://v.example/1")
    body = main.JobIn(name="Clips", source="pages", target="/Clips", options={"url": "https://v.example/own"})
    asyncio.run(main.update_job(job_id, body, Nextcloud("anna")))
    assert store.fetches() == [] and (store.job(job_id) or job).schedule == "0 6 * * *"
