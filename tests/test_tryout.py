# SPDX-License-Identifier: AGPL-3.0-or-later
"""Try the address (POST /api/try, tryout.py): a job's listing once from its form, nothing
downloaded, with a stand-in for gallery-dl, a fake feed (httpx's MockTransport) and the fake IMAP
server: what a run would fetch, what the job's history has, what its types, sizes and patterns
leave out, the first files' paths and tags, a mailbox's messages; a failure with its hint; the
form's checks, one try of a person's at a time, a runner's place, the time limit; no account
value in the answer, no mark of the job's."""

# pylint: disable=missing-function-docstring

import asyncio
import json
import subprocess
import threading
import time

import httpx
import pytest
from fastapi import HTTPException
from test_feed import ATOM, PODCAST
from test_mail import mailbox
from test_runner import FakeNextcloud

import main
from harvester import tryout
from harvester.routes import trying
from harvester.executor import Control, Executor, credential_key
from harvester.sources import SOURCES, Item, Source
from harvester.sources import download
from harvester.sources.base import nap
from harvester.sources.gallery import Gallery
from harvester.sources.mail import Mail
from harvester.store import Job, Store

PAGE = "https://pics.example/album/42"


def picture(number: int, extension: str = "jpg", **data) -> list:
    """A Url message of gallery-dl's JSON output."""
    values = {"category": "pics", "id": number, "num": 0, "filename": f"photo{number}", "extension": extension}
    values |= {"date": "2025-07-14 10:20:30"} | data
    return [3, f"https://cdn.pics.example/{number}.{extension}", values]


LISTED = json.dumps([picture(n) for n in (1, 2, 3, 4)] + [picture(5, "mp4"), picture(6, filesize=9_000_000_000)])


class FakeGallery(Gallery):
    """gallery-dl's listing: LISTED (or its error), never the network."""

    stdout = LISTED
    returncode = 0
    stderr = ""
    commands: list[list[str]] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def run(command, **_kwargs):
            FakeGallery.commands.append(command)
            return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)

        self.run = run


class FakeMail(Mail):
    """The mailbox of test_mail, its password "secret"."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connect = lambda host, port: mailbox()  # type: ignore[method-assign]


class Slow(Source):
    """A listing that takes its time: a file, then a long wait (ended by the try's stop)."""

    name = "slow"
    title = "Slow"

    def items(self, seen):
        yield Item(key="a", path="a.txt", url="https://slow.example/a")
        nap(30)
        yield Item(key="b", path="b.txt", url="https://slow.example/b")

    def fetch(self, item, dest):
        raise AssertionError("a try downloads nothing")


def feeds(request: httpx.Request) -> httpx.Response:
    """The fake feeds (test_feed's), nothing else."""
    if str(request.url) == "https://example.org/podcast.rss":
        return httpx.Response(200, content=PODCAST)
    if str(request.url) == "https://example.org/reports.atom":
        return httpx.Response(200, content=ATOM)
    return httpx.Response(404)


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """The app's store and executor (two runners, none running), the fake sources in place."""
    store = Store(str(tmp_path / "db"))
    nc = FakeNextcloud()
    nc._user = "anna"  # pylint: disable=protected-access
    executor = Executor(store, lambda: nc, runners=2)
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", executor)
    monkeypatch.setitem(SOURCES, "gallery", FakeGallery)
    monkeypatch.setitem(SOURCES, "mail", FakeMail)
    monkeypatch.setattr(
        download, "guarded_client", lambda **options: httpx.Client(transport=httpx.MockTransport(feeds))
    )
    FakeGallery.stdout, FakeGallery.returncode, FakeGallery.stderr = LISTED, 0, ""
    FakeGallery.commands = []
    return store, nc, executor


def form(source: str = "gallery", **changes) -> dict:
    """A job as the form sends it."""
    options = {
        "gallery": {"url": PAGE, "path": "{year}/{filename}.{extension}", "max_items": 2},
        "feed": {"url": "https://example.org/podcast.rss", "link_pattern": r"\.mp3$", "file_type": "audio"},
        "mail": {"host": "imap.example.org", "since_days": 0, "subject_pattern": "Rechnung|Invoice"},
    }[source]
    fields = {"name": "Try", "source": source, "target": "/Pictures", "tags": ["{category}", "beach {year}"]}
    return fields | {"options": options} | changes


def tried(nc, body: dict, job_id: int | None = None, account: dict | None = None) -> dict:
    return asyncio.run(trying.try_listing(trying.TryIn(job=main.JobIn(**body), id=job_id, account=account or {}), nc))


def refused(nc, body: dict, status: int = 422, **kwargs) -> str:
    with pytest.raises(HTTPException) as refusal:
        tried(nc, body, **kwargs)
    assert refusal.value.status_code == status
    return str(refusal.value.detail)


def test_a_gallery_tried_says_what_a_run_would_fetch_with_names_and_tags(app):
    store, nc, executor = app
    saved = store.save(Job(user="anna", **main.JobIn(**form()).model_dump()))
    store.mark_seen(saved.id or 0, "pics:1:0")
    answer = tried(nc, form(), saved.id)
    assert answer["ok"] and not answer["running"]
    assert answer["outcome"] == (
        "found 6 files, 2 would be fetched this run, 1 in later runs (Files per run: 2), 1 already in the job's "
        "history, 1 skipped: not of the job's file types, 1 skipped: outside the job's sizes"
    )
    assert answer["outcome_l10n"]["kind"] == "unit", "translated on the page"
    assert [(item["path"], item["state"]) for item in answer["items"]] == [
        ("/Pictures/2025/photo1.jpg", "known"),
        ("/Pictures/2025/photo2.jpg", "fetch"),
        ("/Pictures/2025/photo3.jpg", "fetch"),
        ("/Pictures/2025/photo4.jpg", "later"),
        ("/Pictures/2025/photo5.mp4", "type"),
        ("/Pictures/2025/photo6.jpg", "size"),
    ]
    assert answer["items"][1]["tags"] == ["pics", "beach 2025"], "the job's Tags templates, as a run gives them"
    assert answer["counts"] | {"pattern": 0} == {
        "fetch": 2,
        "later": 1,
        "known": 1,
        "type": 1,
        "size": 1,
        "problem": 0,
        "pattern": 0,
    }
    assert store.seen_keys(saved.id or 0) == {"pics:1:0"}, "a try marks nothing"
    assert executor.status()["runners"]["busy"] == 0, "its runner's place is free again"
    assert FakeGallery.commands[0][-1] == PAGE and "--no-download" in FakeGallery.commands[0]


def test_a_new_job_has_no_history_and_a_name_that_cannot_be_made_is_a_problem(app):
    _, nc, _ = app
    answer = tried(nc, form(options=form()["options"] | {"path": "{album}/{filename}.{extension}", "max_items": -1}))
    assert answer["counts"]["problem"] == 6 and answer["counts"]["known"] == 0
    assert answer["outcome"] == "found 6 files, 0 would be fetched this run, 6 with a problem (below)"
    assert "album" in answer["items"][0]["problem"]


def test_a_feed_tried_counts_what_its_pattern_leaves_out(app):
    _, nc, _ = app
    answer = tried(nc, form("feed", tags=["{feed}"]))
    assert answer["outcome"] == (
        "found 2 files, 2 would be fetched this run, 1 left out by the job's patterns"
    ), "the notes' PDF is not an .mp3"
    assert [(item["path"], item["tags"], item["state"]) for item in answer["items"]] == [
        ("/Pictures/The Show/2025/Episode 2 More less.mp3", ["The Show"], "fetch"),
        ("/Pictures/The Show/2025/Episode 1 Hello.mp3", ["The Show"], "fetch"),
    ], "newest first, named and tagged by the job's templates"


def test_a_mailbox_tried_names_its_messages_and_what_each_gives(app):
    _, nc, _ = app
    answer = tried(nc, form("mail", tags=[]), account={"username": "me", "password": "secret"})
    assert answer["ok"], answer
    assert answer["notes"] == [
        "Folder INBOX: 3 new messages, 2 from the sender and with the subject asked for",
        "Message from billing@shop.example of 2025-10-14, “Invoice November”: 1 attachment",
        "Message from billing@shop.example of 2025-10-14, “Rechnung für Oktober”: 2 attachments",
    ]
    assert answer["notes_l10n"][0]["plural"] == "Folder {folder}: %n new messages, {matching}"
    assert [item["path"] for item in answer["items"]] == [
        "/Pictures/2025/2025-10-14 Rechnung November.pdf",
        "/Pictures/2025/2025-10-14 Rechnung Oktober.pdf",
        "/Pictures/2025/2025-10-14 logo.png",
    ]
    assert answer["outcome"] == "found 3 files, 3 would be fetched this run, 1 left out by the job's patterns"
    assert "secret" not in json.dumps(answer), "the account's values never come back"


def test_a_failure_says_why_with_a_hint(app):
    _, nc, _ = app
    FakeGallery.returncode, FakeGallery.stdout = 1, "[]"
    FakeGallery.stderr = "[gallery-dl][error] Unsupported URL 'https://pics.example/album/42'\n"
    answer = tried(nc, form())
    assert not answer["ok"] and "Unsupported URL" in answer["error"]
    assert answer["hint"] == "gallery-dl does not know this site: check the address, or use a website job."
    assert answer["hint_l10n"]["params"] == {"tool": "gallery-dl"}
    answer = tried(nc, form("mail"), account={"username": "me", "password": "wrong"})
    assert not answer["ok"]
    assert answer["hint"].startswith("The site refused the account"), answer
    assert "wrong" not in json.dumps(answer)


def test_the_saved_jobs_account_is_used_unless_another_is_typed(app):
    store, nc, _ = app
    saved = store.save(Job(user="anna", **main.JobIn(**form("mail")).model_dump()))
    for name, value in {"username": "me", "password": "secret"}.items():
        nc.preferences_ex.values[credential_key(saved, name)] = value
    assert tried(nc, form("mail"), saved.id)["ok"], "the stored account"
    assert not tried(nc, form("mail"), saved.id, account={"password": "typo"})["ok"], "the one typed instead"


def test_what_cannot_be_tried_is_refused_as_save_refuses_it(app):
    _, nc, _ = app
    assert "may not be opened" in refused(nc, form(options={"url": "http://10.0.0.1/album"}))
    assert refused(nc, form(schedule="", options={"url": ""})) == "Type the address to try first."
    site = {"name": "Site", "source": "site", "target": "/x", "options": {"recipe": json.dumps({"start": PAGE})}}
    assert "Test the recipe" in refused(nc, site)
    assert refused(nc, form(), job_id=99, status=404) == "no such job"


def test_one_try_at_a_time_in_a_runners_place(app, monkeypatch):
    _, nc, executor = app
    assert executor.enter_trial("anna")
    assert refused(nc, form(), status=409) == "A try of yours is still going on: wait for its end."
    executor._trying.clear()  # pylint: disable=protected-access
    monkeypatch.setattr(tryout, "CLAIM_WAIT", 0)
    executor.controls.update({1: Control(), 2: Control()})
    answer = tried(nc, form())
    assert not answer["ok"] and answer["error"].startswith("No runner is free (all 2 busy)")
    assert answer["error_l10n"]["params"] == {"runners": 2}
    assert not FakeGallery.commands, "nothing listed"
    executor.controls.clear()
    # While a try holds the runners' last place, a job waits for it.
    executor._trials[1] = "elsewhere"  # pylint: disable=protected-access
    executor.controls[3] = Control()
    waiting = Job(id=5, user="u", name="n", source="feed", target="/x", schedule="")
    assert not executor._claim(waiting)  # pylint: disable=protected-access


def test_the_time_limit_cuts_the_listing_and_frees_its_place(app, monkeypatch):
    _, nc, executor = app
    monkeypatch.setitem(SOURCES, "slow", Slow)
    monkeypatch.setattr(tryout, "LIMIT", 0.5)
    began = time.monotonic()
    answer = tried(nc, {"name": "Slow", "source": "slow", "target": "/x", "tags": []})
    assert time.monotonic() - began < 10
    assert answer["ok"] and answer["cut"] == "time" and answer["found"] == 1
    assert answer["outcome"].startswith("found 1 file in 0 s (the try's time limit; there may be more)")
    for _ in range(50):
        if not executor._trying:  # pylint: disable=protected-access
            break
        time.sleep(0.1)
    assert not executor._trying and not executor._trials  # pylint: disable=protected-access


def test_types_by_their_names():
    assert tryout.wrong_type("a/b.mp4", ["image"]) and not tryout.wrong_type("a/b.jpeg", ["image"])
    assert not tryout.wrong_type("a/b", ["pdf"]), "no extension: the download tells"
    assert not tryout.wrong_type("a/b.xyz123", ["pdf"]), "an unknown extension: the download tells"
    assert tryout.wrong_type("notes.pdf", ["audio", "video"]) and not tryout.wrong_type("x.md", ["markdown"])
    assert not tryout.wrong_type("anything.exe", ["any"])


def test_a_thread_counts_pattern_misses_only_while_asked():
    from harvester.sources.base import count_misses, pattern_search  # pylint: disable=import-outside-toplevel

    counter = [0]
    done = threading.Event()

    def elsewhere():
        pattern_search("x", "y")
        done.set()

    count_misses(counter)
    try:
        pattern_search("a", "b")
        pattern_search("a", "a")
        threading.Thread(target=elsewhere).start()
        done.wait(5)
    finally:
        count_misses(None)
    pattern_search("a", "b")
    assert counter == [1]
