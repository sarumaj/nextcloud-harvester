"""Runs the jobs when their schedule says, and when a user asks; a few at once (RUNNERS), never
one twice, and one per site at a time."""

import asyncio
import datetime
import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from croniter import croniter
from nc_py_api import AsyncNextcloudApp

from . import browser, runner
from .nextcloud import NextcloudLibrary
from .sources import SOURCES, AuthError, ChallengeError, Stopped
from .sources.download import guarded_client
from .store import Job, Store

LOG = logging.getLogger("harvester.executor")
# The longest a run goes on: then it stops after the file it is at, the rest comes next run
# (a first run of a big site, a slow server), and the other jobs get their turn.
RUN_LIMIT = 2 * 3600
# A paused run that nobody resumes ends after this long (it holds the browser meanwhile).
PAUSE_LIMIT = 6 * 3600
# How many jobs run at once (each a thread; a website job also takes one of the browsers).
RUNNERS = max(1, int(os.environ.get("HARVESTER_RUNNERS", "2")))
ADDRESS = re.compile(r"https?://[^\s\"'<>]+")


def site_of(job: Job) -> str:
    """The site a job fetches from (the host of the first address in its options), so that two
    jobs of one site do not run at once; a job without an address is a site of its own."""
    for value in job.options.values():
        found = ADDRESS.search(str(value))
        if found and urlparse(found.group(0)).hostname:
            return str(urlparse(found.group(0)).hostname).lower().removeprefix("www.")
    return f"job:{job.id}"


class Control:
    """A running job's controls from the page: paused (it waits before its next file) and
    stopped (it ends there, and at once in a wait); the time paused does not count against
    RUN_LIMIT."""

    def __init__(self) -> None:
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.paused_for = 0.0

    def nap(self, seconds: float) -> None:
        """The run's waits (between files, retries, what a site asks for): ended at once by a
        stop, which then ends the run (Stopped)."""
        if self.stopped.wait(max(0.0, seconds)):
            raise Stopped("stopped")

    def checkpoint(self, report: Callable[[str, str], None]) -> bool:
        """Asked by the run before each file: waits while it is paused (up to PAUSE_LIMIT, then
        it stops), and says whether to stop."""
        if self.paused.is_set() and not self.stopped.is_set():
            report("info", "Paused")
            began = time.monotonic()
            while self.paused.is_set() and not self.stopped.is_set():
                if time.monotonic() - began > PAUSE_LIMIT:
                    self.stopped.set()
                    break
                time.sleep(1)
            self.paused_for += time.monotonic() - began
            if not self.stopped.is_set():
                report("info", "Resumed")
        return self.stopped.is_set()


def connect_nextcloud() -> AsyncNextcloudApp:
    """The app's client for the runs. Its uploads in chunks are nc_py_api's first kind (v1):
    its second (v2, 0.30.3) numbers the first chunk 0, and Nextcloud refuses that (1 to 10000)
    on storage with multipart uploads (S3): every file over a chunk (5 MB) failed with 400.
    Not through the environment: nc_py_api reads CHUNKED_UPLOAD_V2 as a string, true unless
    empty."""
    return AsyncNextcloudApp(chunked_upload_v2=False)


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

    nc_py_api's client is asynchronous, the sources are not: each Nextcloud request runs to its
    end (call) on an event loop in a thread of its own. Not on one in the executor's thread:
    Playwright's sync API keeps its own loop running there while a browser is open, and asyncio
    refuses to run a second loop in a thread ("Cannot run the event loop while another loop is
    running")."""

    def __init__(self, store: Store, connect: Callable[[], AsyncNextcloudApp] | None = None, runners: int = RUNNERS):
        self.store = store
        self.connect = connect or connect_nextcloud
        self.requests: queue.Queue[int] = queue.Queue()
        self.runners = runners
        # The running jobs' controls, and their sites; changed under _lock.
        self.controls: dict[int, Control] = {}
        self._sites: dict[int, str] = {}
        self._queued: set[int] = set()
        self._lock = threading.Lock()
        self._events: asyncio.AbstractEventLoop | None = None
        self._events_lock = threading.Lock()

    @property
    def running(self) -> list[int]:
        """The jobs running now."""
        with self._lock:
            return sorted(self.controls)

    def start(self) -> None:
        """Starts the schedule's thread and the runners."""
        threading.Thread(target=self._schedule, name="schedule", daemon=True).start()
        for number in range(self.runners):
            threading.Thread(target=self._runner, name=f"runner-{number}", daemon=True).start()

    def request(self, job_id: int) -> None:
        """Runs the job as soon as a runner and its site are free (once, if asked twice)."""
        with self._lock:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
        self.requests.put(job_id)

    def paused(self, job_id: int) -> bool:
        """Whether the job runs and is paused."""
        control = self.controls.get(job_id)
        return bool(control and control.paused.is_set())

    def pause(self, job_id: int) -> bool:
        """Pauses the job before its next file, if it runs."""
        control = self.controls.get(job_id)
        if control:
            control.paused.set()
        return control is not None

    def resume(self, job_id: int) -> bool:
        """Lets the paused job go on."""
        control = self.controls.get(job_id)
        if control:
            control.paused.clear()
        return control is not None

    def stop(self, job_id: int) -> bool:
        """Ends the job's run before its next file, and at once in a wait (also a paused one)."""
        control = self.controls.get(job_id)
        if control:
            control.stopped.set()
            control.paused.clear()
        return control is not None

    def call(self, awaitable: Awaitable[Any]) -> Any:
        """Runs a Nextcloud request to its end on the requests' event loop (started at first use)
        and returns its result or raises its error."""
        with self._events_lock:
            if self._events is None:
                self._events = asyncio.new_event_loop()
                threading.Thread(target=self._events.run_forever, name="nextcloud", daemon=True).start()

        async def wait() -> Any:
            return await awaitable

        return asyncio.run_coroutine_threadsafe(wait(), self._events).result()

    def _schedule(self) -> None:
        """Each minute, the jobs that are due go into the queue (unless running or queued)."""
        while True:
            # Whatever goes wrong here (the database, a bug), the thread goes on: else no job
            # would run again until the app restarts, and nobody would be told.
            try:
                for job in self.store.jobs():
                    if job.id is not None and job.id not in self.controls and is_due(job, time.time()):
                        self.request(job.id)
            except Exception:  # pylint: disable=broad-exception-caught
                LOG.exception("the schedule's turn failed")
            time.sleep(60)

    def _runner(self) -> None:
        """Takes the queued jobs one after another; one whose site is busy waits its turn."""
        while True:
            try:
                job_id = self.requests.get()
                # Read again just before its run: it may have been changed, disabled or deleted.
                job = self.store.job(job_id)
                if not job:
                    with self._lock:
                        self._queued.discard(job_id)
                    continue
                if not self._claim(job):
                    # Its site (or the job itself) is busy: back to the queue, a little later.
                    time.sleep(2)
                    self.requests.put(job_id)
                    continue
                try:
                    self.run(job)
                finally:
                    with self._lock:
                        self.controls.pop(job_id, None)
                        self._sites.pop(job_id, None)
            except Exception:  # pylint: disable=broad-exception-caught
                LOG.exception("a runner's turn failed")
                time.sleep(60)

    def _claim(self, job: Job) -> bool:
        """Takes the job for this runner unless it runs, or a job of its site does."""
        assert job.id is not None
        site = site_of(job)
        with self._lock:
            if job.id in self.controls or site in self._sites.values():
                return False
            self.controls[job.id] = Control()
            self._sites[job.id] = site
            self._queued.discard(job.id)
            return True

    def run(self, job: Job) -> None:
        """Runs the job and records how it went; a new failure is also a notification to its user."""
        assert job.id is not None
        job_id = job.id
        # Its controls: a runner made them when it took the job; a direct call (tests) makes them.
        with self._lock:
            control = self.controls.setdefault(job_id, Control())
        # The last run's message goes: the page shows this run's progress (its log) instead.
        self.store.begin(job_id)
        nc = self.connect()
        try:
            self.call(nc.set_user(job.user))
            source_class = SOURCES[job.source]
            credentials = {
                name: self.call(nc.preferences_ex.get_value(credential_key(job, name))) or ""
                for name in source_class.credentials
            }
            started = time.monotonic()
            home = bool(int(job.options.get("home", 0) or 0)) and bool(browser.PROXY)
            with guarded_client(**({"proxy": browser.PROXY} if home else {})) as client:
                source = source_class(job.options, credentials, client)
                source.home = home
                # Every wait of the source ends at once when the run is stopped.
                if hasattr(source, "sleep"):
                    source.sleep = control.nap
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
                        until=lambda: started + RUN_LIMIT + control.paused_for,
                        report=lambda level, text: self.store.log(job_id, level, text),
                        sleep=control.nap,
                        stopped=lambda: control.checkpoint(lambda level, text: self.store.log(job_id, level, text)),
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
            self.store.log(job_id, "done", message)
        except ChallengeError as error:
            # Not a failure of the job: it waits for its user to answer the page in the browser.
            if not self.store.job(job_id):
                return
            self.store.finish(job_id, "challenge", str(error), challenge_url=error.url)
            self.store.log(job_id, "error", str(error))
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
            self.store.log(job_id, "error", message)
            # Told once: the same failure on the next scheduled runs is no news.
            if (job.last_status, job.last_message) != (status, message):
                self.notify(nc, f"Harvester: {job.name} failed", message)
        finally:
            with self._lock:
                self.controls.pop(job_id, None)

    def notify(self, nc: AsyncNextcloudApp, subject: str, message: str) -> None:
        """Tells the job's user, with a link to the app's page; a notification that fails is lost."""
        try:
            self.call(nc.notifications.create(plain(subject), plain(message), link=page_link(nc)))
        except Exception:  # pylint: disable=broad-exception-caught
            LOG.warning("a notification could not be sent: %s", subject)
