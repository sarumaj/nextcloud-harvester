"""Runs the jobs when their schedule says, and when a user asks; one at a time."""

import datetime
import queue
import threading
import time
from collections.abc import Callable

import httpx
from croniter import croniter
from nc_py_api import NextcloudApp

from . import runner
from .nextcloud import NextcloudLibrary
from .sources import SOURCES, AuthError
from .store import Job, Store

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Nextcloud-Harvester"


def is_due(job: Job, now: float) -> bool:
    """Tells whether the schedule has come round since the last run (or since the job was made)."""
    if not job.enabled:
        return False
    start = datetime.datetime.fromtimestamp(job.last_run or job.created, datetime.UTC)
    return croniter(job.schedule, start).get_next(float) <= now


def credential_key(job: Job, name: str) -> str:
    """The user preference that holds a job's credential (stored by Nextcloud, encrypted)."""
    return f"credential.job{job.id}.{name}"


class Executor:
    """The background thread: a queue of the runs asked for, and the schedule checked each minute."""

    def __init__(self, store: Store, connect: Callable[[], NextcloudApp] = NextcloudApp):
        self.store = store
        self.connect = connect
        self.requests: queue.Queue[int] = queue.Queue()
        self.running: int | None = None
        self._thread = threading.Thread(target=self._loop, name="executor", daemon=True)

    def start(self) -> None:
        """Starts the thread."""
        self._thread.start()

    def request(self, job_id: int) -> None:
        """Runs the job as soon as the jobs before it are done."""
        self.requests.put(job_id)

    def _loop(self) -> None:
        while True:
            try:
                job = self.store.job(self.requests.get(timeout=60))
                if job:
                    self.run(job)
            except queue.Empty:
                pass
            for job in self.store.jobs():
                if is_due(job, time.time()):
                    self.run(job)

    def run(self, job: Job) -> None:
        """Runs the job and records how it went; a failure is also a notification to its user."""
        assert job.id is not None
        job_id = job.id
        self.running = job_id
        nc = self.connect()
        try:
            nc.set_user(job.user)
            source_class = SOURCES[job.source]
            credentials = {
                name: nc.preferences_ex.get_value(credential_key(job, name)) or "" for name in source_class.credentials
            }
            with httpx.Client(follow_redirects=True, timeout=120, headers={"User-Agent": USER_AGENT}) as client:
                source = source_class(job.options, credentials, client)
                try:
                    message = runner.run(
                        job,
                        source,
                        NextcloudLibrary(nc),
                        lambda key: self.store.seen(job_id, key),
                        lambda key: self.store.mark_seen(job_id, key),
                    )
                finally:
                    source.close()
            self.store.finish(job_id, "ok", message)
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Whatever broke, the job is marked and the thread goes on with the others.
            message = str(error) or type(error).__name__
            self.store.finish(job_id, "auth" if isinstance(error, AuthError) else "failed", message)
            try:
                nc.notifications.create(f"Harvester: {job.name} failed", message)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        finally:
            self.running = None
