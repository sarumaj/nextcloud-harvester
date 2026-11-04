# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server's texts with values as templates the page translates (harvester.l10n): each is its
English text too (the logs, the database, API clients that do not translate) and carries its
template; the progress log and a run's outcome keep theirs in the database, while lines and
messages from before templates stay plain English (none); why a job waits, a run's figures and
the recorder's warnings come as whole templates, with n() for counts."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import sqlite3

import main
from harvester import browser
from harvester.executor import Executor
from harvester.l10n import listed, said, said_n
from harvester.recipe import leaked_passwords, token_warnings
from harvester.runner import SENT_AGAIN, Tally
from harvester.store import Job, Store


class Nextcloud:  # pylint: disable=too-few-public-methods
    """AppAPI's client as one user."""

    def __init__(self, user: str):
        self._user = user

    @property
    async def user(self):
        return self._user


def test_a_text_is_its_english_and_its_template():
    text = said("Saved {path} ({size})", path="/In/a.pdf", size="2.0 KB")
    assert text == "Saved /In/a.pdf (2.0 KB)" and isinstance(text, str)
    assert text.template == {"text": "Saved {path} ({size})", "params": {"path": "/In/a.pdf", "size": "2.0 KB"}}
    # A value's braces are its own: filled once, never read as placeholders.
    assert said("Fetching {address}", address="https://x.example/{path}") == "Fetching https://x.example/{path}"
    counted = said_n("%n file", "%n files", 1)
    assert counted == "1 file" and said_n("%n file", "%n files", 3) == "3 files"
    assert counted.template == {"text": "%n file", "plural": "%n files", "count": 1}
    # A value of its own words, and a list joined in the person's language on the page.
    days = listed([said("Monday"), said("Thursday")])
    nested = said("on {days}", days=days)
    assert nested == "on Monday and Thursday"
    assert nested.template["params"]["days"] == {"list": [{"text": "Monday"}, {"text": "Thursday"}], "kind": "and"}
    assert listed(["3 downloaded", "1 skipped"], "unit") == "3 downloaded, 1 skipped"


def test_a_runs_outcome_is_whole_phrases():
    tally = Tally(downloaded=1, skipped=2, failures=["answered 404"], later=SENT_AGAIN)
    outcome = tally.sentence()
    assert outcome == (
        "1 downloaded, 2 skipped (not of the job's file types, or too large), "
        "1 failed (tried again when the address is sent again): answered 404"
    )
    figures = outcome.template["list"]
    assert outcome.template["kind"] == "unit" and [figure["count"] for figure in figures] == [1, 2, 1]
    assert figures[2]["params"] == {"later": {"text": "when the address is sent again"}, "error": "answered 404"}


def test_the_log_and_the_outcome_keep_their_templates_and_old_ones_stay_plain(tmp_path):
    path = str(tmp_path / "h.db")
    # A database from before templates: a line and an outcome in plain English.
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, name TEXT NOT NULL,"
            " source TEXT NOT NULL, options TEXT NOT NULL, target TEXT NOT NULL, tags TEXT NOT NULL,"
            " schedule TEXT NOT NULL, enabled INTEGER NOT NULL, created REAL NOT NULL, last_run REAL,"
            " last_status TEXT, last_message TEXT);"
            "CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, job INTEGER NOT NULL REFERENCES jobs(id)"
            " ON DELETE CASCADE, at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);"
        )
        db.execute(
            "INSERT INTO jobs (user, name, source, options, target, tags, schedule, enabled, created, last_run,"
            " last_status, last_message) VALUES ('anna', 'a', 'feed', '{}', '/x', '[]', '0 6 * * *', 1, 0, 1, 'ok',"
            " '3 downloaded')"
        )
        db.execute("INSERT INTO log (job, at, level, message) VALUES (1, 1, 'info', 'Looking for new files')")
    store = Store(path)
    job = store.job(1)
    assert job and (job.last_message, job.last_message_l10n) == ("3 downloaded", None), "shown as it is"
    store.log(1, "info", said("Fetching {address}", address="https://v.example/1"))
    store.log(1, "error", "answered 500")  # an error: plain English
    lines = store.log_lines("anna")
    assert [(line["message"], line["message_l10n"]) for line in lines] == [
        ("Looking for new files", None),
        ("Fetching https://v.example/1", {"text": "Fetching {address}", "params": {"address": "https://v.example/1"}}),
        ("answered 500", None),
    ]
    store.finish(1, "ok", said_n("%n downloaded", "%n downloaded", 2))
    job = store.job(1)
    assert job and job.last_message == "2 downloaded" and job.last_message_l10n and job.last_message_l10n["count"] == 2
    store.mark(1, "failed", "plain")
    assert (store.job(1) or job).last_message_l10n is None
    store.finish(1, "ok", said("continued in the browser"))
    store.begin(1)
    job = store.job(1)
    assert job and job.last_message is None and job.last_message_l10n is None


def test_why_a_job_waits_comes_as_a_template(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "h.db"))
    executor = Executor(store, runners=1)
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", executor)
    mine = store.save(Job(user="anna", name="P", source="feed", target="/x", schedule="0 6 * * *",
                          options={"url": "https://feed.example/rss"}))  # fmt: skip
    other = store.save(Job(user="eve", name="E", source="feed", target="/x", schedule="0 6 * * *",
                           options={"url": "https://eve.example/rss"}))  # fmt: skip
    assert executor._claim(other)
    executor.request(mine.id or 0)
    listed_jobs = asyncio.run(main.list_jobs(Nextcloud("anna")))
    template = {
        "text": "{place}. in the queue: waits for a runner, all {runners} busy (%n job ahead)",
        "plural": "{place}. in the queue: waits for a runner, all {runners} busy (%n jobs ahead)",
        "count": 0,
        "params": {"place": 1, "runners": 1},
    }
    assert listed_jobs[0]["waiting"] == "1. in the queue: waits for a runner, all 1 busy (0 jobs ahead)"
    assert listed_jobs[0]["waiting_l10n"] == template
    log = asyncio.run(main.progress_log(Nextcloud("anna")))
    assert log["waiting_l10n"] == {str(mine.id): template}


def test_the_recorders_warnings_and_refusal_are_templates(monkeypatch):
    warnings = token_warnings({"steps": [{"action": "goto", "url": "https://x.example/in?ticket=abc&code=1"}]})
    assert warnings[0].startswith("step 1 opens an address with ")
    assert warnings[0].template["params"]["number"] == 1
    assert warnings[0].template["params"]["names"]["kind"] == "unit"
    leaked = leaked_passwords({"steps": [{"action": "fill", "selector": "#password", "value": "hunter2"}]})
    assert leaked[0].template["params"] == {"number": 1, "selector": "#password"}
    assert "hunter2" not in str(leaked[0].template), "never the value"
    monkeypatch.setattr(browser, "SLOTS", browser.Slots(1))
    refusal = main.busy_browsers()
    assert refusal == "No browser is free: 0 of 1 in use (0 by jobs, 0 by recordings). Try again in a few minutes."
    assert refusal.template["params"]["browsers"]["text"].startswith("{used} of {limit} in use")


def test_a_failed_runs_reason_keeps_its_template(tmp_path):
    from harvester.sources import ChallengeError  # pylint: disable=import-outside-toplevel
    from harvester.sources.walk import StartEmpty  # pylint: disable=import-outside-toplevel

    store = Store(str(tmp_path / "h.db"))
    executor = Executor(store, runners=1)
    executor.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    job = store.save(Job(user="anna", name="S", source="site", target="/x", schedule="0 6 * * *"))
    empty = StartEmpty(said("the start page has none of the recipe's links: signed out, or the site changed?"))
    executor._failed(job, None, empty, False)  # type: ignore[arg-type]
    failed = store.job(job.id or 0)
    assert (
        failed
        and failed.last_status == "failed"
        and failed.last_message_l10n
        == {"text": "the start page has none of the recipe's links: signed out, or the site changed?"}
    ), "translated on the page, not English in every language"
    assert store.log_lines("anna")[-1]["message_l10n"] == failed.last_message_l10n
    executor._failed(job, None, ChallengeError("https://x.example/", "reCAPTCHA"), False)  # type: ignore[arg-type]
    asked = store.job(job.id or 0)
    assert asked and asked.last_message == "https://x.example/ asks for a person (reCAPTCHA): continue in the browser"
    assert asked.last_message_l10n and asked.last_message_l10n["params"] == {
        "url": "https://x.example/",
        "kind": "reCAPTCHA",
    }
    executor._failed(job, None, RuntimeError(), False)  # type: ignore[arg-type]
    assert (store.job(job.id or 0) or job).last_message == "RuntimeError", "plain errors as before"
