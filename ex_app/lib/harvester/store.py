"""The jobs, what each has downloaded and its runs, in SQLite in the app's persistent storage."""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
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
    challenge_url TEXT
);
CREATE TABLE IF NOT EXISTS seen (
    job INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (job, key)
);
"""


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


class Store:
    """Thread-safe access to the database: the web requests and the scheduler share it."""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        # Columns added since a database was made.
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(jobs)")}
        if "challenge_url" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN challenge_url TEXT")
        self._lock = threading.Lock()

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        values = dict(row)
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
        )
        with self._lock, self._db:
            if job.id is None:
                job.created = time.time()
                cursor = self._db.execute(
                    "INSERT INTO jobs (user, name, source, options, target, tags, schedule, enabled, created)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, job.created),
                )
                job.id = cursor.lastrowid
            else:
                self._db.execute(
                    "UPDATE jobs SET user = ?, name = ?, source = ?, options = ?, target = ?, tags = ?, schedule = ?,"
                    " enabled = ? WHERE id = ?",
                    (*values, job.id),
                )
        return job

    def delete(self, job_id: int) -> None:
        """Deletes a job and what it has downloaded (the files stay in Nextcloud)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

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
            self._db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (job_id, key, time.time()))
