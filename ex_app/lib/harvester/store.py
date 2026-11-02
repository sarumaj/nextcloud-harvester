"""The jobs, what each has downloaded and its runs, in SQLite in the app's persistent storage."""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

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
    retag_from TEXT
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
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_job ON log (job, id);
"""
# The lines of the progress log kept per job (its last runs).
LOG_KEPT = 500
# The lines one request of the log returns at most.
LOG_PAGE = 300


@dataclass
class Job:
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
    # The page that asked for a person (status "challenge"), where the user continues.
    challenge_url: str | None = None
    # The time zone its schedule is in (an IANA name; the page sends the browser's).
    timezone: str = "UTC"


class Store:
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
        # After the columns: the table made anew keeps them.
        self._never_reuse_ids()
        self._lock = threading.Lock()

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
        # Not a setting of the job: the executor's (retag_from).
        values.pop("retag_from", None)
        values["options"] = json.loads(values["options"])
        values["tags"] = json.loads(values["tags"])
        values["enabled"] = bool(values["enabled"])
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
        )
        with self._lock, self._db:
            if job.id is None:
                job.created = time.time()
                cursor = self._db.execute(
                    "INSERT INTO jobs (user, name, source, options, target, tags, schedule, enabled, timezone,"
                    " created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, job.created),
                )
                job.id = cursor.lastrowid
            else:
                self._db.execute(
                    "UPDATE jobs SET user = ?, name = ?, source = ?, options = ?, target = ?, tags = ?, schedule = ?,"
                    " enabled = ?, timezone = ? WHERE id = ?",
                    (*values, job.id),
                )
        return job

    def delete(self, job_id: int) -> None:
        """Deletes a job and what it has downloaded (the files stay in Nextcloud)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def begin(self, job_id: int) -> None:
        """A run starts: the last run's message goes, the page shows the run's progress instead."""
        with self._lock, self._db:
            self._db.execute("UPDATE jobs SET last_message = NULL WHERE id = ?", (job_id,))

    def log(self, job_id: int, level: str, message: str) -> None:
        """A line of the job's progress log (info, error, done); the oldest beyond LOG_KEPT go.
        Nothing for a job deleted meanwhile (its run goes on to its end)."""
        try:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO log (job, at, level, message) VALUES (?, ?, ?, ?)",
                    (job_id, time.time(), level, message[:1000]),
                )
                self._db.execute(
                    "DELETE FROM log WHERE job = ? AND id <= (SELECT id FROM log WHERE job = ? ORDER BY id DESC"
                    " LIMIT 1 OFFSET ?)",
                    (job_id, job_id, LOG_KEPT),
                )
        except sqlite3.IntegrityError:
            pass

    def log_lines(self, user: str, job_id: int | None = None, after: int = 0) -> list[dict]:
        """The user's progress log after the line after (its id), oldest first: of one job, or
        of all theirs; the newest LOG_PAGE when there are more."""
        query = (
            "SELECT log.id, log.job, jobs.name, log.at, log.level, log.message FROM log JOIN jobs ON jobs.id = log.job"
            " WHERE jobs.user = ? AND log.id > ?"
        )
        values: list = [user, after]
        if job_id is not None:
            query += " AND log.job = ?"
            values.append(job_id)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY log.id DESC LIMIT ?", (*values, LOG_PAGE)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def finish(self, job_id: int, status: str, message: str, challenge_url: str | None = None) -> None:
        """Records the outcome of a run (and the page that asked for a person, if one did)."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET last_run = ?, last_status = ?, last_message = ?, challenge_url = ? WHERE id = ?",
                (time.time(), status, message, challenge_url, job_id),
            )

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
