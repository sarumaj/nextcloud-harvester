"""Runs the jobs when their schedule says, and when a user asks; one at a time."""

import asyncio
import datetime
import json
import logging
import queue
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from croniter import croniter
from nc_py_api import AsyncNextcloudApp

from . import runner
from .nextcloud import NextcloudLibrary
from .sources import SOURCES, AuthError, ChallengeError
from .sources.download import guarded_client
from .store import Job, Store

LOG = logging.getLogger("harvester.executor")
# The longest a run goes on: then it stops after the file it is at, the rest comes next run
# (a first run of a big site, a slow server), and the other jobs get their turn.
RUN_LIMIT = 2 * 3600


def is_due(job: Job, now: float) -> bool:
    """Tells whether the schedule has come round since the last run (or since the job was made)."""
    # A job waiting for its user (a challenge) runs again once answered, or by hand.
    if not job.enabled or job.last_status == "challenge":
        return False
    start = datetime.datetime.fromtimestamp(job.last_run or job.created, datetime.UTC)
    return croniter(job.schedule, start).get_next(float) <= now


def credential_key(job: Job, name: str) -> str:
    """The user preference that holds a job's credential (stored by Nextcloud, encrypted)."""
    return f"credential.job{job.id}.{name}"


def session_key(job: Job) -> str:
    """The user preference that holds a job's kept browser session (cookies; encrypted)."""
    return f"session.job{job.id}"


async def load_session(nc: AsyncNextcloudApp, job: Job) -> dict | None:
    """The job's kept session, or None."""
    value = await nc.preferences_ex.get_value(session_key(job))
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


async def save_session(nc: AsyncNextcloudApp, job: Job, session: dict | None) -> None:
    """Keeps the job's session for its next run."""
    if session:
        await nc.preferences_ex.set_value(session_key(job), json.dumps(session), sensitive=True)


def plain(text: str) -> str:
    """Text a notification shows as it is: Nextcloud reads {name} as a placeholder and % as a
    printf code (an address with %2F, say), either of which can break the notification."""
    return text.replace("{", "(").replace("}", ")").replace("%", "\uff05")[:400]


def page_link(nc: AsyncNextcloudApp) -> str:
    """The app's page in Nextcloud, for notifications."""
    return f"{nc.app_cfg.endpoint.rstrip('/')}/index.php/apps/app_api/embedded/harvester/harvester"


class Executor:
    """The background thread: a queue of the runs asked for, and the schedule checked each minute.

    nc_py_api's client is asynchronous, the sources are not: the thread has an event loop of its
    own, and each Nextcloud request runs to its end on it (call), between the sources' work."""

    def __init__(self, store: Store, connect: Callable[[], AsyncNextcloudApp] = AsyncNextcloudApp):
        self.store = store
        self.connect = connect
        self.requests: queue.Queue[int] = queue.Queue()
        self.running: int | None = None
        self._events: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._loop, name="executor", daemon=True)

    def start(self) -> None:
        """Starts the thread."""
        self._thread.start()

    def request(self, job_id: int) -> None:
        """Runs the job as soon as the jobs before it are done."""
        self.requests.put(job_id)

    def call(self, awaitable: Awaitable[Any]) -> Any:
        """Runs a Nextcloud request to its end on the thread's event loop."""
        if self._events is None:
            self._events = asyncio.new_event_loop()
        return self._events.run_until_complete(awaitable)

    def _loop(self) -> None:
        while True:
            # Whatever goes wrong here (the database, a bug), the thread goes on: else no job
            # would run again until the app restarts, and nobody would be told.
            try:
                self._turn()
            except Exception:  # pylint: disable=broad-exception-caught
                LOG.exception("the executor's turn failed")
                time.sleep(60)

    def _turn(self) -> None:
        """Runs the run asked for (or waits a minute for one), then the jobs that are due."""
        try:
            job = self.store.job(self.requests.get(timeout=60))
            if job:
                self.run(job)
        except queue.Empty:
            pass
        for listed in self.store.jobs():
            # Read again just before its run: the runs before it may have taken long, and the
            # job may have been changed, disabled or deleted meanwhile.
            job = self.store.job(listed.id or 0)
            if job and is_due(job, time.time()):
                self.run(job)

    def run(self, job: Job) -> None:
        """Runs the job and records how it went; a new failure is also a notification to its user."""
        assert job.id is not None
        job_id = job.id
        self.running = job_id
        nc = self.connect()
        try:
            self.call(nc.set_user(job.user))
            source_class = SOURCES[job.source]
            credentials = {
                name: self.call(nc.preferences_ex.get_value(credential_key(job, name))) or ""
                for name in source_class.credentials
            }
            with guarded_client() as client:
                source = source_class(job.options, credentials, client)
                kept = self.call(load_session(nc, job))
                if kept:
                    source.restore(kept)
                try:
                    message = runner.run(
                        job,
                        source,
                        NextcloudLibrary(nc, self.call),
                        lambda key: self.store.seen(job_id, key),
                        lambda key: self.store.mark_seen(job_id, key),
                        until=time.monotonic() + RUN_LIMIT,
                    )
                finally:
                    # Kept also after a failure: cookies set before it (a sign-in) still count;
                    # not for a job deleted while it ran (its session went with it).
                    try:
                        if self.store.job(job_id):
                            self.call(save_session(nc, job, source.session()))
                    finally:
                        source.close()
            self.store.finish(job_id, "ok", message)
        except ChallengeError as error:
            # Not a failure of the job: it waits for its user to answer the page in the browser.
            if not self.store.job(job_id):
                return
            self.store.finish(job_id, "challenge", str(error), challenge_url=error.url)
            self.notify(nc, f"Harvester: {job.name} needs you", f"{error.url} asks for a person ({error.kind}).")
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Whatever broke, the job is marked and the thread goes on with the others; a job
            # deleted while it ran ends quietly.
            if not self.store.job(job_id):
                return
            message = str(error) or type(error).__name__
            status = "auth" if isinstance(error, AuthError) else "failed"
            LOG.warning("job %s (%s) %s: %s", job_id, job.user, status, message)
            self.store.finish(job_id, status, message)
            # Told once: the same failure on the next scheduled runs is no news.
            if (job.last_status, job.last_message) != (status, message):
                self.notify(nc, f"Harvester: {job.name} failed", message)
        finally:
            self.running = None

    def notify(self, nc: AsyncNextcloudApp, subject: str, message: str) -> None:
        """Tells the job's user, with a link to the app's page; a notification that fails is lost."""
        try:
            self.call(nc.notifications.create(plain(subject), plain(message), link=page_link(nc)))
        except Exception:  # pylint: disable=broad-exception-caught
            LOG.warning("a notification could not be sent: %s", subject)
