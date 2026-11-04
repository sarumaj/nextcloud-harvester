# SPDX-License-Identifier: AGPL-3.0-or-later
"""The jobs, what each has downloaded and its runs, in SQLite in the app's persistent storage."""

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from .l10n import loaded, stored
from .sources.base import MARK, REFUSED

# A job's id is never used again (AUTOINCREMENT): a run of a deleted job that is still going
# would otherwise write its log, files and outcome into a new job of someone else's.
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    options TEXT NOT NULL,
    target TEXT NOT NULL,
    tags TEXT NOT NULL,
    schedule TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    created REAL NOT NULL,
    last_run REAL,
    last_status TEXT,
    last_message TEXT,
    challenge_url TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    retag_from TEXT,
    once INTEGER NOT NULL DEFAULT 0,
    tested REAL,
    -- last_message's template (harvester.l10n, JSON), which the page translates; none for a
    -- message older than templates, shown as it is.
    last_message_l10n TEXT
);
CREATE TABLE IF NOT EXISTS seen (
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS placed (
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    file INTEGER,
    vals TEXT NOT NULL,
    tags TEXT NOT NULL,
    PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    -- The message's template (harvester.l10n, JSON), none for a line older than templates.
    message_l10n TEXT
);
CREATE INDEX IF NOT EXISTS log_job ON log (job, id);
-- What the job's browser showed at its last failed step or challenge (the newest only), and the
-- log line that tells of it.
CREATE TABLE IF NOT EXISTS shots (
    job INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    log INTEGER,
    at REAL NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    what TEXT NOT NULL,
    image BLOB NOT NULL
);
-- The addresses sent to on-demand jobs, waiting for their run (one after the other, oldest
-- first): kept here so that a restart loses none; an address waits once per job.
CREATE TABLE IF NOT EXISTS fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    at REAL NOT NULL,
    UNIQUE (job, url)
);
-- What a website job's last runs and recipe tests found, level by level (watch.Watch): its good
-- ones are what a run is judged by ("the site changed"), the newest few of each kind.
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    good INTEGER NOT NULL,
    summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS summaries_job ON summaries (job, good, id);
-- What the files a job saved hold (versions.py), by their item's key: their content's hash (a
-- file of the same hash at another address is a duplicate), the ETag and Last-Modified their
-- address answered with (a recheck asks it only if it changed since), where the file is, and
-- when the address was last asked.
CREATE TABLE IF NOT EXISTS contents (
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    hash TEXT NOT NULL,
    etag TEXT NOT NULL,
    modified TEXT NOT NULL,
    path TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (job, key)
);
CREATE INDEX IF NOT EXISTS contents_hash ON contents (job, hash);
-- The text of the pages a job watches (pagewatch.py) as its last run found it, by page.
CREATE TABLE IF NOT EXISTS texts (
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    text TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (job, key)
);
-- A person's own templates: recipe files (recipe_file.py) saved from a job's form, theirs alone.
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    name TEXT NOT NULL,
    file TEXT NOT NULL,
    at REAL NOT NULL
);
"""
# The lines of the progress log kept per job (its last runs).
LOG_KEPT = 500
# The first lines of a job's current run that stay beyond LOG_KEPT (where it looked, what it
# found): a long run pushes its own middle out of the log, not its start.
RUN_HEAD = 30
# The lines one request of the log returns at most.
LOG_PAGE = 300
# The summaries of runs kept per job, of good runs and of the others each.
SUMMARIES_KEPT = 5
# A person's own templates at most.
TEMPLATES_KEPT = 50


@dataclass
class Job:  # pylint: disable=too-many-instance-attributes  # a row of the jobs table
    """A source's settings, the folder its files go to and when it runs."""

    user: str
    name: str
    source: str
    target: str
    schedule: str
    options: dict = field(default_factory=dict)
    # Nextcloud's collaborative tags given to every file the job downloads.
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    id: int | None = None
    created: float = 0.0
    last_run: float | None = None
    last_status: str | None = None
    last_message: str | None = None
    # last_message's template, which the page translates (harvester.l10n); None: shown as it is.
    last_message_l10n: dict | None = None
    # The page that asked for a person (status "challenge"), where the user continues.
    challenge_url: str | None = None
    # The time zone its schedule is in (an IANA name; the page sends the browser's).
    timezone: str = "UTC"
    # An on-demand job (no schedule) deleted after a run that went through (ondemand.py).
    once: bool = False


class Store:  # pylint: disable=too-many-public-methods
    """Thread-safe access to the database: the web requests and the scheduler share it."""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        # A write-ahead log: a crash mid-write leaves the database whole, and readers do not
        # wait for a writer.
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.executescript(SCHEMA)
        # Columns added since a database was made.
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(jobs)")}
        if "challenge_url" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN challenge_url TEXT")
        # A job made before time zones keeps its schedule in UTC, as it ran.
        if "timezone" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
        if "retag_from" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN retag_from TEXT")
        if "once" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN once INTEGER NOT NULL DEFAULT 0")
        if "tested" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN tested REAL")
        if "last_message_l10n" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN last_message_l10n TEXT")
        if "message_l10n" not in {row["name"] for row in self._db.execute("PRAGMA table_info(log)")}:
            self._db.execute("ALTER TABLE log ADD COLUMN message_l10n TEXT")
        # After the columns: the table made anew keeps them.
        self._never_reuse_ids()
        self._lock = threading.Lock()
        # The last line before each job's current run (begin): its run's lines come after it.
        self._run_after: dict[int, int] = {}

    def _never_reuse_ids(self) -> None:
        """Makes a database of an earlier version keep its jobs' ids for good (AUTOINCREMENT):
        the table is made anew with the same ids, in one transaction, the other tables' rows
        staying with their jobs; the ids go on after the highest one there is."""
        row = self._db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
        if "AUTOINCREMENT" in row["sql"].upper():
            return
        table = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS jobs") : SCHEMA.index(");") + 2]
        columns = ", ".join(column["name"] for column in self._db.execute("PRAGMA table_info(jobs)"))
        self._db.commit()
        # Off for the table's exchange (outside a transaction, as SQLite wants): the other
        # tables' references to it would otherwise stop it from being dropped.
        self._db.execute("PRAGMA foreign_keys = OFF")
        try:
            self._db.execute("BEGIN")
            self._db.execute(table.replace("IF NOT EXISTS jobs", "jobs_new"))
            self._db.execute(f"INSERT INTO jobs_new ({columns}) SELECT {columns} FROM jobs")
            self._db.execute("DROP TABLE jobs")
            self._db.execute("ALTER TABLE jobs_new RENAME TO jobs")
            # The sequence starts after the highest id (one the INSERT did not set already).
            self._db.execute(
                "INSERT INTO sqlite_sequence (name, seq) SELECT 'jobs', COALESCE(MAX(id), 0) FROM jobs"
                " WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'jobs')"
            )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        finally:
            self._db.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        values = dict(row)
        # Not settings of the job: the executor's (retag_from, tested).
        values.pop("retag_from", None)
        values.pop("tested", None)
        values["options"] = json.loads(values["options"])
        values["tags"] = json.loads(values["tags"])
        values["enabled"] = bool(values["enabled"])
        values["once"] = bool(values["once"])
        values["last_message_l10n"] = loaded(values.get("last_message_l10n"))
        return Job(**values)

    def jobs(self, user: str | None = None) -> list[Job]:
        """Returns the user's jobs, or everyone's."""
        with self._lock:
            if user is None:
                rows = self._db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM jobs WHERE user = ? ORDER BY id", (user,)).fetchall()
        return [self._job(row) for row in rows]

    def job(self, job_id: int) -> Job | None:
        """Returns one job."""
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def save(self, job: Job) -> Job:
        """Inserts a new job or updates its settings (not its last run)."""
        values = (
            job.user,
            job.name,
            job.source,
            json.dumps(job.options),
            job.target,
            json.dumps(job.tags),
            job.schedule,
            int(job.enabled),
            job.timezone,
            int(job.once),
        )
        with self._lock, self._db:
            if job.id is None:
                job.created = time.time()
                cursor = self._db.execute(
                    "INSERT INTO jobs (user, name, source, options, target, tags, schedule, enabled, timezone,"
                    " once, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, job.created),
                )
                job.id = cursor.lastrowid
            else:
                self._db.execute(
                    "UPDATE jobs SET user = ?, name = ?, source = ?, options = ?, target = ?, tags = ?, schedule = ?,"
                    " enabled = ?, timezone = ?, once = ? WHERE id = ?",
                    (*values, job.id),
                )
        return job

    def delete(self, job_id: int) -> None:
        """Deletes a job and what it has downloaded (the files stay in Nextcloud)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def add_fetch(self, job_id: int, url: str) -> tuple[dict, bool]:
        """Queues the address for the job: the fetch (id, job, url, at), and whether it is new
        (False: the address waits already, and keeps its place)."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO fetches (job, url, at) VALUES (?, ?, ?)", (job_id, url, time.time())
            )
            row = self._db.execute("SELECT * FROM fetches WHERE job = ? AND url = ?", (job_id, url)).fetchone()
        return dict(row), cursor.rowcount > 0

    def fetches(self, jobs: list[int] | None = None) -> list[dict]:
        """The addresses waiting for the jobs (or for all), oldest first."""
        if jobs is not None and not jobs:
            return []
        query, values = "SELECT * FROM fetches", []
        if jobs is not None:
            query += f" WHERE job IN ({', '.join('?' * len(jobs))})"
            values = list(jobs)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY id", values).fetchall()
        return [dict(row) for row in rows]

    def drop_fetch(self, fetch_id: int) -> bool:
        """Takes the address out of its job's queue (fetched, or cancelled); False when it was not
        there."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM fetches WHERE id = ?", (fetch_id,)).rowcount > 0

    def begin(self, job_id: int) -> None:
        """A run starts: the last run's message goes, the page shows the run's progress instead."""
        with self._lock, self._db:
            self._db.execute("UPDATE jobs SET last_message = NULL, last_message_l10n = NULL WHERE id = ?", (job_id,))
            self._run_after[job_id] = self._db.execute("SELECT COALESCE(MAX(id), 0) FROM log").fetchone()[0]

    def log(self, job_id: int, level: str, message: str) -> int | None:
        """A line of the job's progress log (info, error, done), and its id; the oldest beyond
        LOG_KEPT go, but for the first RUN_HEAD of the job's current run. Nothing for a job deleted
        meanwhile (its run goes on to its end). A said() message keeps its template too (the page
        translates it)."""
        try:
            with self._lock, self._db:
                line = self._db.execute(
                    "INSERT INTO log (job, at, level, message, message_l10n) VALUES (?, ?, ?, ?, ?)",
                    (job_id, time.time(), level, message[:1000], stored(message)),
                ).lastrowid
                self._db.execute(
                    "DELETE FROM log WHERE job = ? AND id <= (SELECT id FROM log WHERE job = ? ORDER BY id DESC"
                    " LIMIT 1 OFFSET ?) AND id NOT IN (SELECT id FROM log WHERE job = ? AND id > ? ORDER BY id"
                    " LIMIT ?)",
                    (job_id, job_id, LOG_KEPT, job_id, self._run_after.get(job_id, line), RUN_HEAD),
                )
            return line
        except sqlite3.IntegrityError:
            return None

    def keep_shot(self, job_id: int, line: int | None, shot: dict) -> None:
        """Keeps what the job's browser showed (site.snapshot), told of by the log line, in place
        of the one before. Nothing for a job deleted meanwhile."""
        try:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO shots (job, log, at, url, title, what, image) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, line, shot["at"], shot["url"], shot["title"], shot["what"], shot["image"]),
                )
        except sqlite3.IntegrityError:
            pass

    def shot(self, job_id: int) -> dict | None:
        """What the job's browser showed last (its image as bytes), or None."""
        with self._lock:
            row = self._db.execute("SELECT * FROM shots WHERE job = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def log_lines(self, user: str, job_id: int | None = None, after: int = 0) -> list[dict]:
        """The user's progress log after the line after (its id), oldest first: of one job, or
        of all theirs; the newest LOG_PAGE when there are more."""
        query = (
            "SELECT log.id, log.job, jobs.name, log.at, log.level, log.message, log.message_l10n,"
            " EXISTS (SELECT 1 FROM shots WHERE shots.log = log.id) AS shot FROM log JOIN jobs ON jobs.id = log.job"
            " WHERE jobs.user = ? AND log.id > ?"
        )
        values: list = [user, after]
        if job_id is not None:
            query += " AND log.job = ?"
            values.append(job_id)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY log.id DESC LIMIT ?", (*values, LOG_PAGE)).fetchall()
        return [dict(row) | {"message_l10n": loaded(row["message_l10n"])} for row in reversed(rows)]

    def finish(self, job_id: int, status: str, message: str, challenge_url: str | None = None) -> None:
        """Records the outcome of a run (and the page that asked for a person, if one did)."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET last_run = ?, last_status = ?, last_message = ?, last_message_l10n = ?,"
                " challenge_url = ? WHERE id = ?",
                (time.time(), status, message, stored(message), challenge_url, job_id),
            )

    def mark(self, job_id: int, status: str, message: str) -> None:
        """Records the job's status without a run (a recipe's test): its last run, which its
        schedule counts from, stays."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET last_status = ?, last_message = ?, last_message_l10n = ? WHERE id = ?",
                (status, message, stored(message), job_id),
            )

    def add_summary(self, job_id: int, summary: dict, good: bool) -> None:
        """Keeps what a run found (watch.Watch.summary), the newest SUMMARIES_KEPT of its kind
        (good, or not). Nothing for a job deleted meanwhile."""
        try:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO summaries (job, at, good, summary) VALUES (?, ?, ?, ?)",
                    (job_id, time.time(), int(good), json.dumps(summary)),
                )
                self._db.execute(
                    "DELETE FROM summaries WHERE job = ? AND good = ? AND id <= (SELECT id FROM summaries"
                    " WHERE job = ? AND good = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (job_id, int(good), job_id, int(good), SUMMARIES_KEPT),
                )
        except sqlite3.IntegrityError:
            pass

    def summaries(self, job_id: int, good: bool = True) -> list[dict]:
        """The summaries the job kept of its good runs (or of the others), newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT summary FROM summaries WHERE job = ? AND good = ? ORDER BY id DESC", (job_id, int(good))
            ).fetchall()
        return [json.loads(row["summary"]) for row in rows]

    def forget_summaries(self, job_id: int) -> None:
        """Forgets what the job's runs found: its listing changed (another recipe), so they are
        nothing to compare with."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM summaries WHERE job = ?", (job_id,))

    def tested(self, job_id: int) -> float | None:
        """When the job's recipe was last tested by the schedule (or its weekly test turned on)."""
        with self._lock:
            row = self._db.execute("SELECT tested FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["tested"] if row else None

    def tested_all(self) -> dict[int, float | None]:
        """When each job's recipe was last tested by the schedule (tested), all in one query."""
        with self._lock:
            rows = self._db.execute("SELECT id, tested FROM jobs").fetchall()
        return {row["id"]: row["tested"] for row in rows}

    def set_tested(self, job_id: int, at: float) -> None:
        """The job's weekly test counts from then."""
        with self._lock, self._db:
            self._db.execute("UPDATE jobs SET tested = ? WHERE id = ?", (at, job_id))

    def add_template(self, user: str, name: str, file: dict) -> dict:
        """Keeps a recipe file as the user's own template; the oldest beyond TEMPLATES_KEPT go."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO templates (user, name, file, at) VALUES (?, ?, ?, ?)",
                (user, name, json.dumps(file), time.time()),
            )
            self._db.execute(
                "DELETE FROM templates WHERE user = ? AND id <= (SELECT id FROM templates WHERE user = ?"
                " ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (user, user, TEMPLATES_KEPT),
            )
        return {"id": cursor.lastrowid, "name": name, "file": file}

    def templates(self, user: str) -> list[dict]:
        """The user's own templates (id, name, file), oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, file FROM templates WHERE user = ? ORDER BY id", (user,)
            ).fetchall()
        return [{"id": row["id"], "name": row["name"], "file": json.loads(row["file"])} for row in rows]

    def delete_template(self, user: str, template_id: int) -> bool:
        """Removes the user's template; False when they have none of that id."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM templates WHERE id = ? AND user = ?", (template_id, user)).rowcount > 0

    def seen(self, job_id: int, key: str) -> bool:
        """Tells whether the job has downloaded the item already."""
        with self._lock:
            row = self._db.execute("SELECT 1 FROM seen WHERE job = ? AND key = ?", (job_id, key)).fetchone()
        return row is not None

    def mark_seen(self, job_id: int, key: str) -> None:
        """Records a downloaded item, so that it is not downloaded again."""
        with self._lock, self._db:
            # The job may have been deleted while it ran: nothing to record then.
            if self._db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                self._db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (job_id, key, time.time()))

    def seen_keys(self, job_id: int) -> set[str]:
        """The keys of the items the job has downloaded."""
        with self._lock:
            rows = self._db.execute("SELECT key FROM seen WHERE job = ?", (job_id,)).fetchall()
        return {row["key"] for row in rows}

    def placed(self, job_id: int) -> dict[str, dict]:
        """The files the job saved or found, by their item's key: file (its ID, or None), vals
        (the values of its tags) and tags (those it was given)."""
        with self._lock:
            rows = self._db.execute("SELECT key, file, vals, tags FROM placed WHERE job = ?", (job_id,)).fetchall()
        return {
            row["key"]: {"file": row["file"], "vals": json.loads(row["vals"]), "tags": json.loads(row["tags"])}
            for row in rows
        }

    def record_placed(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, job_id: int, key: str, file_id: int | None, values: dict, tags: list[str]
    ) -> None:
        """Records a file the job saved or found (or, file_id None, did not find), and its tags."""
        with self._lock, self._db:
            # The job may have been deleted while it ran: nothing to record then.
            if self._db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                self._db.execute(
                    "INSERT OR REPLACE INTO placed VALUES (?, ?, ?, ?, ?)",
                    (job_id, key, file_id, json.dumps(values, default=str), json.dumps(tags)),
                )

    def set_placed_file(self, job_id: int, key: str, file_id: int) -> None:
        """Records the ID of a file the job saved, found again (at its recorded path)."""
        with self._lock, self._db:
            self._db.execute("UPDATE placed SET file = ? WHERE job = ? AND key = ?", (file_id, job_id, key))

    def content(self, job_id: int, key: str) -> dict | None:
        """What the job's file of the item holds (hash, etag, modified, path, at), or None."""
        with self._lock:
            row = self._db.execute("SELECT * FROM contents WHERE job = ? AND key = ?", (job_id, key)).fetchone()
        return dict(row) if row else None

    def same_content(self, job_id: int, digest: str, key: str) -> dict | None:
        """The job's first file of that hash that another item gave, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM contents WHERE job = ? AND hash = ? AND key != ? ORDER BY at LIMIT 1",
                (job_id, digest, key),
            ).fetchone()
        return dict(row) if row else None

    def checked_since(self, job_id: int, since: float) -> set[str]:
        """The keys of the items whose address the job asked at since (epoch s) or later."""
        with self._lock:
            rows = self._db.execute("SELECT key FROM contents WHERE job = ? AND at >= ?", (job_id, since)).fetchall()
        return {row["key"] for row in rows}

    def keep_content(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, job_id: int, key: str, digest: str, path: str, etag: str = "", modified: str = ""
    ) -> None:
        """Records what the job's file of the item holds now, asked for now. Nothing for a job
        deleted meanwhile."""
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                self._db.execute(
                    "INSERT OR REPLACE INTO contents (job, key, hash, etag, modified, path, at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, key, digest, etag, modified, path, time.time()),
                )

    def forget_content(self, job_id: int, key: str) -> None:
        """Forgets what the item's file held (it is gone), and the duplicates that name that file
        (its hash, at its path): a download of the item anew is no duplicate of it."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT hash, path FROM contents WHERE job = ? AND key = ?", (job_id, key)
            ).fetchone()
            if row is not None:
                self._db.execute(
                    "DELETE FROM contents WHERE job = ? AND (key = ? OR (hash = ? AND path = ?))",
                    (job_id, key, row["hash"], row["path"]),
                )

    def page_text(self, job_id: int, key: str) -> str | None:
        """The text of the watched page as the job's last run found it, or None."""
        with self._lock:
            row = self._db.execute("SELECT text FROM texts WHERE job = ? AND key = ?", (job_id, key)).fetchone()
        return row["text"] if row else None

    def keep_page_text(self, job_id: int, key: str, text: str) -> None:
        """Keeps the watched page's text as this run found it. Nothing for a job deleted meanwhile."""
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                self._db.execute(
                    "INSERT OR REPLACE INTO texts (job, key, text, at) VALUES (?, ?, ?, ?)",
                    (job_id, key, text, time.time()),
                )

    def forget_places(self, job_id: int) -> None:
        """Forgets where the job's files are and what they hold (placed, contents), not that it
        fetched them (seen): its files go elsewhere now (another Save to), those saved stay
        where they are and are not fetched again."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM placed WHERE job = ?", (job_id,))
            self._db.execute("DELETE FROM contents WHERE job = ?", (job_id,))

    def job_files(self, job_id: int, label: str = "", most: int = 1000) -> list[dict]:
        """The files the job saved (newest first, most at most): their path, ID and tags (or
        labels), those with the label alone if one is given."""
        with self._lock:
            rows = self._db.execute(
                "SELECT placed.file, placed.tags, COALESCE(contents.path, '') AS path, COALESCE(contents.at, 0) AS at"
                " FROM placed LEFT JOIN contents ON contents.job = placed.job AND contents.key = placed.key"
                " WHERE placed.job = ? AND placed.file IS NOT NULL ORDER BY at DESC",
                (job_id,),
            ).fetchall()
        files = [
            {"file": row["file"], "path": row["path"], "at": row["at"], "tags": json.loads(row["tags"])} for row in rows
        ]
        return [entry for entry in files if not label or label in entry["tags"]][:most]

    def placed_file(self, job_id: int, file_id: int) -> dict | None:
        """Where the job's file of this ID came from: its key (the link, or what the source
        tells files apart by) and its values; None when the job has no such file."""
        with self._lock:
            row = self._db.execute(
                "SELECT key, vals FROM placed WHERE job = ? AND file = ?", (job_id, file_id)
            ).fetchone()
        return {"key": row["key"], "values": json.loads(row["vals"])} if row is not None else None

    @contextlib.contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        """The database under the store's lock, in one transaction: for the tables of the app's
        other parts (library.py), which make their own with tables()."""
        with self._lock, self._db:
            yield self._db

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """The rows of a query of the app's other parts (library.py)."""
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def tables(self, schema: str) -> None:
        """Makes the tables of the app's other parts (if they are not there)."""
        with self._lock:
            self._db.executescript(schema)

    def forget(self, job_id: int, key: str) -> None:
        """Forgets one recorded key."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM seen WHERE job = ? AND key = ?", (job_id, key))

    def forget_marks(self, job_id: int) -> None:
        """Forgets the marks of what the job's listing found (sources.MARK: a complete pass, a
        source's own, such as mail's handled messages), not its files nor its refusals: its
        listing changed."""
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM seen WHERE job = ? AND substr(key, 1, ?) = ? AND substr(key, 1, ?) != ?",
                (job_id, len(MARK), MARK, len(REFUSED), REFUSED),
            )

    def forget_refused(self, job_id: int) -> None:
        """Forgets the files refused by type or size, and their marks (sources.REFUSED): the job's
        types or sizes changed, so they are tried again."""
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM seen WHERE job = ? AND key IN (SELECT substr(key, ?) FROM seen WHERE job = ?"
                " AND substr(key, 1, ?) = ?)",
                (job_id, len(REFUSED) + 1, job_id, len(REFUSED), REFUSED),
            )
            self._db.execute(
                "DELETE FROM seen WHERE job = ? AND substr(key, 1, ?) = ?", (job_id, len(REFUSED), REFUSED)
            )

    def retag_from(self, job_id: int) -> list[str] | None:
        """The tags the job's earlier files may still have (those before its tags' first change
        since its last complete pass for the tags), or None when no pass is pending."""
        with self._lock:
            row = self._db.execute("SELECT retag_from FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return json.loads(row["retag_from"]) if row and row["retag_from"] is not None else None

    def pending_retags(self) -> list[int]:
        """The jobs with a pass for their tags pending."""
        with self._lock:
            rows = self._db.execute("SELECT id FROM jobs WHERE retag_from IS NOT NULL ORDER BY id").fetchall()
        return [row["id"] for row in rows]

    def set_retag(self, job_id: int, previous: list[str]) -> None:
        """A change of the job's tags, which were previous: kept unless a pass is pending already
        (its files have the tags of the first change, which that pass takes off)."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET retag_from = ? WHERE id = ? AND retag_from IS NULL", (json.dumps(previous), job_id)
            )

    def clear_retag(self, job_id: int, tags: list[str]) -> None:
        """The pass for the tags went through, to the job's tags: no more pending, unless they
        changed again meanwhile (then the next pass brings those)."""
        with self._lock, self._db:
            self._db.execute("UPDATE jobs SET retag_from = NULL WHERE id = ? AND tags = ?", (job_id, json.dumps(tags)))
