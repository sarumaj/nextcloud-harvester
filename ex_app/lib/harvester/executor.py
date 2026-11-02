"""Runs the jobs when their schedule says, and when a user asks; a few at once (RUNNERS, or the
admin's setting), never one twice, and one per site at a time."""

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

import httpx
from croniter import croniter
from nc_py_api import AsyncNextcloudApp

from . import browser, runner, settings
from .schedule import zone
from .nextcloud import NextcloudLibrary
from .sources import MARK, SOURCES, AuthError, ChallengeError, Source, Stopped
from .sources.base import watch_stop
from .sources.download import guarded_client
from .store import Job, Store

LOG = logging.getLogger("harvester.executor")
# The longest a run goes on: then it stops after the file it is at, the rest comes next run
# (a first run of a big site, a slow server), and the other jobs get their turn.
RUN_LIMIT = 2 * 3600
# A paused run that nobody resumes ends after this long (it holds the browser meanwhile).
PAUSE_LIMIT = 6 * 3600
# How many jobs run at once (each a thread; a website job also takes one of the browsers): the
# default, which the admin's setting changes.
RUNNERS = min(max(1, int(os.environ.get("HARVESTER_RUNNERS", "2"))), settings.MAX_RUNNERS)
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
    # The schedule's hours are the job's time zone's: "0 6 * * *" is 06:00 there, in summer and
    # in winter.
    start = datetime.datetime.fromtimestamp(job.last_run or job.created, zone(job.timezone))
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


def through_proxy(client: httpx.Client) -> str:
    """The run's first line when it goes through the proxy: which address sites see (Cloudflare's
    view, through the run's own client), so the log shows the proxy is used."""
    try:
        answer = client.get(browser.TRACE_URL, timeout=15)
        seen = browser.seen_address(answer.text)
    except httpx.HTTPError as error:
        return f"Through {browser.PROXY_NAME}: the check of the address failed ({error})"
    if not seen:
        return f"Through {browser.PROXY_NAME}: the address sites see is unknown"
    country = f" ({seen['country']})" if seen["country"] else ""
    return f"Through {browser.PROXY_NAME}: sites see {seen['ip']}{country}"


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
        # The queued jobs that are to run (the schedule, Run now), not only to bring their tags
        # to their files: a job's tags before a change are the database's (Store.retag_from),
        # and its next turn brings the files' tags up to date first.
        self._wanted: set[int] = set()
        self._lock = threading.Lock()
        self._events: asyncio.AbstractEventLoop | None = None
        self._events_lock = threading.Lock()

    @property
    def running(self) -> list[int]:
        """The jobs running now."""
        with self._lock:
            return sorted(self.controls)

    @property
    def queued(self) -> list[int]:
        """The jobs waiting for a runner or their site (asked for, not running yet)."""
        with self._lock:
            return sorted(self._queued)

    def start(self) -> None:
        """Starts the schedule's thread and as many runners as the setting allows; those beyond
        self.runners wait until it is raised. The passes for tags the app did not finish (it
        stopped meanwhile) are queued."""
        for job_id in self.store.pending_retags():
            self.request(job_id, run=False)
        threading.Thread(target=self._schedule, name="schedule", daemon=True).start()
        for number in range(settings.MAX_RUNNERS):
            threading.Thread(target=self._runner, args=(number,), name=f"runner-{number}", daemon=True).start()

    def read_settings(self) -> None:
        """Takes the admin's settings (runners, browsers, the proxy); unset, the environment's
        stay."""
        nc = self.connect()
        values = {record.key: record.value for record in self.call(nc.appconfig_ex.get_values(list(settings.KEYS)))}
        runners = settings.number(values.get(settings.RUNNERS), RUNNERS, settings.MAX_RUNNERS)
        browsers = settings.number(values.get(settings.BROWSERS), browser.BROWSERS, settings.MAX_BROWSERS)
        proxy = settings.proxy(values.get(settings.PROXY), settings.DEFAULT_PROXY)
        name = (values.get(settings.PROXY_NAME) or "").strip() or settings.DEFAULT_PROXY_NAME
        if (runners, browsers) != (self.runners, browser.SLOTS.limit):
            LOG.info("%s jobs and %s browsers at once", runners, browsers)
        if proxy != browser.PROXY:
            LOG.info("the proxy: %s", settings.shown(proxy) or "none")
        self.runners = runners
        browser.SLOTS.limit = browsers
        browser.PROXY, browser.PROXY_NAME = proxy, name

    def request(self, job_id: int, run: bool = True) -> None:
        """Runs the job as soon as a runner and its site are free (once, if asked twice); with
        run False, only its pending pass for the tags (a run asked for meanwhile still comes)."""
        with self._lock:
            if run:
                self._wanted.add(job_id)
            if job_id in self._queued:
                return
            self._queued.add(job_id)
        self.requests.put(job_id)

    def retag(self, job_id: int, previous: list[str]) -> None:
        """Brings the job's new tags to the files it saved, as soon as a runner is free (the job's
        tags were previous): no listing for the files it recorded, one without downloads for
        those it saved before it recorded them. Kept in the database until a pass goes through."""
        self.store.set_retag(job_id, list(previous))
        self.request(job_id, run=False)

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
        """Each minute, the admin's settings are read and the jobs that are due go into the queue
        (unless running or queued)."""
        while True:
            try:
                self.read_settings()
            except Exception:  # pylint: disable=broad-exception-caught
                LOG.warning("the settings could not be read, the last ones stay", exc_info=True)
            # Whatever goes wrong here (the database, a bug), the thread goes on: else no job
            # would run again until the app restarts, and nobody would be told.
            try:
                for job in self.store.jobs():
                    if job.id is not None and job.id not in self.controls and is_due(job, time.time()):
                        self.request(job.id)
            except Exception:  # pylint: disable=broad-exception-caught
                LOG.exception("the schedule's turn failed")
            time.sleep(60)

    def _runner(self, number: int = 0) -> None:
        """Takes the queued jobs one after another; one whose site is busy waits its turn. A
        runner numbered beyond the setting (self.runners) waits until it is raised."""
        while True:
            if number >= self.runners:
                time.sleep(5)
                continue
            try:
                job_id = self.requests.get(timeout=5)
            except queue.Empty:
                continue
            try:
                # Read again just before its run: it may have been changed, disabled or deleted.
                job = self.store.job(job_id)
                if not job:
                    with self._lock:
                        self._queued.discard(job_id)
                        self._wanted.discard(job_id)
                    continue
                if not self._claim(job):
                    # Its site (or the job itself) is busy: back to the queue, a little later.
                    time.sleep(2)
                    self.requests.put(job_id)
                    continue
                with self._lock:
                    wanted = job_id in self._wanted
                    self._wanted.discard(job_id)
                try:
                    self.run(job, wanted)
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

    def run(self, job: Job, wanted: bool = True) -> None:  # pylint: disable=too-many-locals
        """Runs the job and records how it went; a new failure is also a notification to its user.
        A pending pass for its tags comes first; without wanted, only that pass (if any)."""
        assert job.id is not None
        job_id = job.id
        previous = self.store.retag_from(job_id)
        if previous is None and not wanted:
            return
        # Its controls: a runner made them when it took the job; a direct call (tests) makes them.
        with self._lock:
            control = self.controls.setdefault(job_id, Control())
        self._begin(job_id, control, wanted, previous is not None)
        nc = self.connect()
        try:
            self.call(nc.set_user(job.user))
            source_class = SOURCES[job.source]
            credentials = self._credentials(nc, job)
            started = time.monotonic()
            home = bool(int(job.options.get("home", 0) or 0)) and bool(browser.PROXY)
            with guarded_client(**({"proxy": browser.PROXY} if home else {})) as client:
                if home:
                    self.store.log(job_id, "info", through_proxy(client))
                source = source_class(job.options, credentials, client)
                source.home = home
                # Every wait of the source ends at once when the run is stopped.
                if hasattr(source, "sleep"):
                    source.sleep = control.nap
                kept = self.call(load_session(nc, job))
                if kept:
                    source.restore(kept)
                library = NextcloudLibrary(nc, self.call)
                try:
                    if previous is not None:
                        message = self._retag_turn(job, source, library, control, previous, wanted, nc)
                    if wanted:
                        message = self._work(job, source, library, control, started, None)
                finally:
                    # Kept also after a failure: cookies set before it (a sign-in) still count;
                    # not for a job deleted while it ran (its session went with it), nor when its
                    # account changed meanwhile (the session is the old account's).
                    try:
                        if self.store.job(job_id) and self._credentials(nc, job) == credentials:
                            self.call(save_session(nc, job, source.session()))
                    finally:
                        source.close()
            # A pass for the tags is no run: the job's last run (and its schedule) stay.
            if wanted:
                self.store.finish(job_id, "ok", message)
            self.store.log(job_id, "done", message)
        except Exception as error:  # pylint: disable=broad-exception-caught
            self._failed(job, nc, error, not wanted)
        finally:
            watch_stop(None)
            with self._lock:
                self.controls.pop(job_id, None)

    def _credentials(self, nc: AsyncNextcloudApp, job: Job) -> dict[str, str]:
        """The job's account, as its user set it."""
        return {
            name: self.call(nc.preferences_ex.get_value(credential_key(job, name))) or ""
            for name in SOURCES[job.source].credentials
        }

    def _retag_turn(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        job: Job,
        source: Source,
        library: runner.Library,
        control: Control,
        previous: list[str],
        wanted: bool,
        nc: AsyncNextcloudApp,
    ) -> str:
        """The pass for the tags before a turn's run (or alone): when a run follows, a failed
        pass is logged and the run goes on (the pass stays pending for the next turn)."""
        try:
            message = self._work(job, source, library, control, 0.0, previous)
        except Exception as error:  # pylint: disable=broad-exception-caught
            if not wanted:
                raise
            self._failed(job, nc, error, True)
            return ""
        if wanted:
            self.store.log(job.id or 0, "info", message)
        return message

    def _failed(self, job: Job, nc: AsyncNextcloudApp, error: Exception, retagging: bool) -> None:
        """Records why a run failed, and tells its user of a new failure; a job deleted while it
        ran ends quietly, and a pass for the tags only logs it (the job's last run stays)."""
        job_id = job.id or 0
        if not self.store.job(job_id):
            return
        message = str(error) or type(error).__name__
        self.store.log(job_id, "error", message)
        if retagging:
            return
        if isinstance(error, ChallengeError):
            # Not a failure of the job: it waits for its user to answer the page in the browser.
            self.store.finish(job_id, "challenge", message, challenge_url=error.url)
            self.notify(nc, f"Harvester: {job.name} needs you", f"{error.url} asks for a person ({error.kind}).")
            return
        status = "auth" if isinstance(error, AuthError) else "failed"
        LOG.warning("job %s (%s) %s: %s", job_id, job.user, status, message)
        self.store.finish(job_id, status, message)
        # Told once: the same failure on the next scheduled runs is no news.
        if (job.last_status, job.last_message) != (status, message):
            self.notify(nc, f"Harvester: {job.name} failed", message)

    def _work(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        self,
        job: Job,
        source: Source,
        library: runner.Library,
        control: Control,
        started: float,
        previous: list[str] | None,
    ) -> str:
        """The run itself, its files' tags first brought up to date; with previous (the job's
        tags before a change), only the tags: of the files it recorded, then of those it saved
        before it recorded them, found by a listing without downloads."""
        job_id = job.id or 0

        def report(level: str, text: str) -> None:
            self.store.log(job_id, level, text)

        def record(placed: runner.Placed) -> None:
            self.store.record_placed(job_id, placed.key, placed.file_id, placed.values, placed.tags)

        known = self.store.placed(job_id)
        placed = [runner.Placed(key, entry["file"], entry["vals"], entry["tags"]) for key, entry in known.items()]
        changed = runner.retag_placed(job, placed, library, record)

        def stopped() -> bool:
            return control.checkpoint(report)

        if previous is not None:
            earlier = {key for key in self.store.seen_keys(job_id) if not key.startswith(MARK)} - set(known)
            message, whole = self._earlier_tags(
                job, source, library, earlier, previous, changed, report, record, stopped
            )
            # Pending until a pass goes through: one stopped, failed or cut short by a restart
            # comes again at the job's next turn.
            if whole:
                self.store.clear_retag(job_id, job.tags)
            return message
        if changed:
            report("info", f"Tags brought up to date on {changed} files")
        return runner.run(
            job,
            source,
            library,
            lambda key: self.store.seen(job_id, key),
            lambda key: self.store.mark_seen(job_id, key),
            until=lambda: started + RUN_LIMIT + control.paused_for,
            report=report,
            sleep=control.nap,
            stopped=stopped,
            record=record,
            owners={entry["file"]: key for key, entry in known.items() if entry["file"] is not None},
        )

    @staticmethod
    def _earlier_tags(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        job: Job,
        source: Source,
        library: runner.Library,
        earlier: set[str],
        previous: list[str],
        changed: int,
        report: runner.Report,
        record: Callable[[runner.Placed], None],
        stopped: Callable[[], bool],
    ) -> tuple[str, bool]:
        """A pass for the tags after their change: those of the files saved before the job
        recorded them (earlier, their keys), found by a listing without downloads; changed
        recorded files were brought up to date already. Returns what it did, and whether it went
        through."""
        tagged, whole = 0, True
        if earlier:
            report(
                "info", f"Looking for {len(earlier)} files saved before their tags were kept (nothing is downloaded)"
            )
            tagged, whole = runner.retag_earlier(
                job, source, library, earlier.__contains__, record, previous=previous, report=report, stopped=stopped
            )
        message = f"Tags brought up to date on {changed + tagged} files"
        return (message, True) if whole else (f"{message}; stopped, the others get them at the job's next turn", False)

    def _begin(self, job_id: int, control: Control, run: bool, retag: bool) -> None:
        """A turn's start: a run's clears the last run's message (the page shows this run's
        progress, its log, instead); downloads in progress end at once when it is stopped (one
        job per thread)."""
        if run:
            self.store.begin(job_id)
        if retag:
            self.store.log(job_id, "info", "The job's tags changed: bringing them to its files")
        watch_stop(control.stopped)

    def notify(self, nc: AsyncNextcloudApp, subject: str, message: str) -> None:
        """Tells the job's user, with a link to the app's page; a notification that fails is lost."""
        try:
            self.call(nc.notifications.create(plain(subject), plain(message), link=page_link(nc)))
        except Exception:  # pylint: disable=broad-exception-caught
            LOG.warning("a notification could not be sent: %s", subject)
