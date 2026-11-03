# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs the jobs when their schedule says, and when a user asks (an on-demand job: with the
address they sent it); a few at once (RUNNERS, or the admin's setting), never one twice, and one
per site at a time."""

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

from . import browser, needs, ondemand, runner, settings, versions
from .l10n import Said, said, said_n
from .schedule import zone
from .watch import Watch, test_due
from .nextcloud import NextcloudLibrary
from .sources import MARK, SOURCES, AuthError, ChallengeError, Source, SourceError, Stopped
from .sources.base import watch_stop
from .sources import download
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


def browsers_in_use() -> Said:
    """The browsers open, by what holds them, without whose: "all 3 in use (2 by jobs, 1 by
    recordings)"."""
    taken = browser.SLOTS.taken()
    jobs, recordings = taken.get("job", 0), taken.get("recording", 0)
    limit = browser.SLOTS.limit
    if jobs + recordings >= limit:
        return said("all {limit} in use ({jobs} by jobs, {recordings} by recordings)", limit=limit, jobs=jobs,
                    recordings=recordings)  # fmt: skip
    return said("{used} of {limit} in use ({jobs} by jobs, {recordings} by recordings)", used=jobs + recordings,
                limit=limit, jobs=jobs, recordings=recordings)  # fmt: skip


class Control:
    """A running job's controls from the page: paused (it waits before its next file) and
    stopped (it ends there, and at once in a wait); the time paused does not count against
    RUN_LIMIT."""

    def __init__(self) -> None:
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.paused_for = 0.0
        # The thread the run is in, once it started (0 before).
        self.thread = 0
        # The fetch it runs (an address sent to an on-demand job), if it does: its id.
        self.fetch = 0

    def nap(self, seconds: float) -> None:
        """The run's waits (between files, retries, what a site asks for): ended at once by a
        stop, which then ends the run (Stopped)."""
        if self.stopped.wait(max(0.0, seconds)):
            raise Stopped("stopped")

    def checkpoint(self, report: Callable[[str, str], None]) -> bool:
        """Asked by the run before each file: waits while it is paused (up to PAUSE_LIMIT, then
        it stops), and says whether to stop."""
        if self.paused.is_set() and not self.stopped.is_set():
            report("info", said("Paused"))
            began = time.monotonic()
            while self.paused.is_set() and not self.stopped.is_set():
                if time.monotonic() - began > PAUSE_LIMIT:
                    self.stopped.set()
                    break
                time.sleep(1)
            self.paused_for += time.monotonic() - began
            if not self.stopped.is_set():
                report("info", said("Resumed"))
        return self.stopped.is_set()


def connect_nextcloud() -> AsyncNextcloudApp:
    """The app's client for the runs. Its uploads in chunks are nc_py_api's first kind (v1):
    its second (v2, 0.30.3) numbers the first chunk 0, and Nextcloud refuses that (1 to 10000)
    on storage with multipart uploads (S3): every file over a chunk (5 MB) failed with 400.
    Not through the environment: nc_py_api reads CHUNKED_UPLOAD_V2 as a string, true unless
    empty."""
    return AsyncNextcloudApp(chunked_upload_v2=False)


def is_due(job: Job, now: float) -> bool:
    """Tells whether the schedule has come round since the last run (or since the job was made);
    never for an on-demand job (it has none)."""
    # A job waiting for its user (a challenge) runs again once answered, or by hand.
    if not job.enabled or ondemand.on_demand(job) or job.last_status == "challenge":
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
    """Text a notification shows as it is (a run's error, not one of the app's texts): Nextcloud
    reads {name} as a placeholder and % as a printf code (an address with %2F, say), either of
    which can break the notification."""
    return text.replace("{", "(").replace("}", ")").replace("%", "\uff05")[:400]


def page_link(nc: AsyncNextcloudApp) -> str:
    """The app's page in Nextcloud, for notifications."""
    return f"{nc.app_cfg.endpoint.rstrip('/')}/index.php/apps/app_api/embedded/harvester/harvester"


def through_proxy(client: httpx.Client) -> Said:
    """The run's first line when it goes through the proxy: which address sites see (Cloudflare's
    view, through the run's own client), so the log shows the proxy is used. The default name
    is in the person's language, an admin's name as it is."""
    proxy = said("the proxy") if browser.PROXY_NAME == "the proxy" else browser.PROXY_NAME
    try:
        answer = client.get(browser.TRACE_URL, timeout=15)
        seen = browser.seen_address(answer.text)
    except httpx.HTTPError as error:
        return said("Through {proxy}: the check of the address failed ({error})", proxy=proxy, error=str(error))
    if not seen:
        return said("Through {proxy}: the address sites see is unknown", proxy=proxy)
    if seen["country"]:
        return said(
            "Through {proxy}: sites see {address} ({country})",
            proxy=proxy,
            address=seen["ip"],
            country=seen["country"],
        )
    return said("Through {proxy}: sites see {address}", proxy=proxy, address=seen["ip"])


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
        # The queued jobs in the order they were asked for (a dict keeps it): their places in the
        # queue, which the page shows.
        self._queued: dict[int, None] = {}
        # The queued jobs that are to run (the schedule, Run now), not only to bring their tags
        # to their files: a job's tags before a change are the database's (Store.retag_from),
        # and its next turn brings the files' tags up to date first.
        self._wanted: set[int] = set()
        # The queued jobs whose recipe is to be tested (watch.py), unless a run comes first (it
        # judges what it finds the same way).
        self._tests: set[int] = set()
        self._lock = threading.Lock()
        self._events: asyncio.AbstractEventLoop | None = None
        self._events_lock = threading.Lock()
        # What the running jobs ask their people (a code from the phone), and their answers.
        self.desk = needs.Desk()

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

    def status(self) -> dict:
        """What everyone's jobs and recordings hold, without whose they are: the runners busy,
        the browsers open (for jobs, for recordings and sign-ins) and the jobs queued."""
        with self._lock:
            busy, queued = len(self.controls), len(self._queued)
        taken = browser.SLOTS.taken()
        return {
            "runners": {"busy": busy, "total": self.runners},
            "browsers": {
                "jobs": taken.get("job", 0),
                "recordings": taken.get("recording", 0),
                "total": browser.SLOTS.limit,
            },
            "queued": queued,
        }

    def waits(self, jobs: list[Job]) -> dict[int, Said]:
        """Why each of the jobs (those a user may see) waits, if it does: its place in the queue
        and what it waits for (a runner, its own site, a browser), and how many addresses an
        on-demand job has to fetch. Nothing of other people's jobs but how many there are: the
        site named is the job's own (or its next address's). The reasons are said() texts (the
        page translates them)."""
        with self._lock:
            order = list(self._queued)
            busy_sites = set(self._sites.values())
            running = {job_id: control.thread for job_id, control in self.controls.items()}
        waiting: dict[int, list[str]] = {}
        for fetch in self.store.fetches([job.id or 0 for job in jobs]):
            waiting.setdefault(fetch["job"], []).append(fetch["url"])
        reasons: dict[int, Said] = {}
        for job in jobs:
            job_id = job.id or 0
            if job_id in running:
                if running[job_id] and browser.SLOTS.waiting(running[job_id]):
                    reasons[job_id] = said("Waits for a browser: {browsers}", browsers=browsers_in_use())
                continue
            if job_id not in order:
                continue
            ahead = order.index(job_id)
            place = ahead + 1
            # The site its next run is of: an on-demand job's next address's.
            site = site_of(ondemand.for_fetch(job, waiting[job_id][0]) if job_id in waiting else job)
            if site in busy_sites and not site.startswith("job:"):
                reasons[job_id] = said(
                    "{place}. in the queue: waits for its site ({site}), used by another job", place=place, site=site
                )
            elif len(running) >= self.runners:
                reasons[job_id] = said_n(
                    "{place}. in the queue: waits for a runner, all {runners} busy (%n job ahead)",
                    "{place}. in the queue: waits for a runner, all {runners} busy (%n jobs ahead)",
                    ahead,
                    place=place,
                    runners=self.runners,
                )
            else:
                reasons[job_id] = said("{place}. in the queue: starts in a moment", place=place)
            # An on-demand job's addresses, fetched one after the other.
            count = len(waiting.get(job_id, []))
            if count:
                reasons[job_id] = said_n(
                    "{reason}; %n address queued", "{reason}; %n addresses queued", count, reason=reasons[job_id]
                )
        return reasons

    def start(self) -> None:
        """Starts the schedule's thread and as many runners as the setting allows; those beyond
        self.runners wait until it is raised. The passes for tags the app did not finish (it
        stopped meanwhile) are queued, and so are the on-demand jobs' addresses still waiting."""
        for job_id in self.store.pending_retags():
            self.request(job_id, run=False)
        for job_id in dict.fromkeys(fetch["job"] for fetch in self.store.fetches()):
            self.request(job_id, run=False)
        threading.Thread(target=self._schedule, name="schedule", daemon=True).start()
        for number in range(settings.MAX_RUNNERS):
            threading.Thread(target=self._runner, args=(number,), name=f"runner-{number}", daemon=True).start()

    def read_settings(self) -> None:
        """Takes the admin's settings (runners, browsers, the proxy, the browsers' guard); unset,
        the environment's stay."""
        nc = self.connect()
        values = {record.key: record.value for record in self.call(nc.appconfig_ex.get_values(list(settings.KEYS)))}
        runners = settings.number(values.get(settings.RUNNERS), RUNNERS, settings.MAX_RUNNERS)
        browsers = settings.number(values.get(settings.BROWSERS), browser.BROWSERS, settings.MAX_BROWSERS)
        proxy = settings.proxy(values.get(settings.PROXY), settings.DEFAULT_PROXY)
        name = (values.get(settings.PROXY_NAME) or "").strip() or settings.DEFAULT_PROXY_NAME
        guard = settings.switch(values.get(settings.GUARD), settings.DEFAULT_GUARD)
        if (runners, browsers) != (self.runners, browser.SLOTS.limit):
            LOG.info("%s jobs and %s browsers at once", runners, browsers)
        if proxy != browser.PROXY:
            LOG.info("the proxy: %s", settings.shown(proxy) or "none")
        if guard != browser.GUARD:
            LOG.log(
                logging.INFO if guard else logging.WARNING,
                "the browsers' network guard: %s (for the browsers started from now on)",
                "on" if guard else "off, the External Apps' firewall alone keeps them off private networks",
            )
        self.runners = runners
        browser.SLOTS.limit = browsers
        browser.PROXY, browser.PROXY_NAME = proxy, name
        browser.GUARD = guard

    def request(self, job_id: int, run: bool = True) -> None:
        """Runs the job as soon as a runner and its site are free (once, if asked twice); with
        run False, only its pending pass for the tags (a run asked for meanwhile still comes)."""
        with self._lock:
            if run:
                self._wanted.add(job_id)
            if job_id in self._queued:
                return
            self._queued[job_id] = None
        self.requests.put(job_id)

    def request_test(self, job_id: int) -> None:
        """Tests the website job's recipe as soon as a runner and its site are free (its weekly
        test, or Test the recipe from its row): nothing is downloaded."""
        with self._lock:
            self._tests.add(job_id)
        self.request(job_id, run=False)

    def fetch(self, job_id: int, url: str) -> tuple[dict, bool]:
        """Queues the address for the on-demand job (checked by ondemand.fetch_problem): its run
        comes after the addresses sent before. Returns the fetch, and whether it is new (an
        address waiting already keeps its place)."""
        fetch, new = self.store.add_fetch(job_id, url)
        # Not a run of the job's own address: the runner takes the job's next address.
        self.request(job_id, run=False)
        return fetch, new

    def fetching(self, job_id: int) -> int:
        """The fetch the job runs now (its id), 0 for none."""
        with self._lock:
            control = self.controls.get(job_id)
            return control.fetch if control else 0

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
                self.schedule_turn(time.time())
            except Exception:  # pylint: disable=broad-exception-caught
                LOG.exception("the schedule's turn failed")
            time.sleep(60)

    def schedule_turn(self, now: float) -> None:
        """Queues the jobs that are due (unless running or queued), and the recipe tests that are
        (watch.test_due)."""
        for job in self.store.jobs():
            if job.id is None or job.id in self.controls:
                continue
            if is_due(job, now):
                self.request(job.id)
            elif test_due(job, self.store.tested(job.id), now):
                # Counted from now: a test that waits for its turn is not asked for again.
                self.store.set_tested(job.id, now)
                self.request_test(job.id)

    def _runner(self, number: int = 0) -> None:  # pylint: disable=too-many-branches
        """Takes the queued jobs one after another; one whose site is busy waits its turn. A
        runner numbered beyond the setting (self.runners) waits until it is raised. An on-demand
        job runs its addresses one per turn, each going back to the queue's end for the next
        (other people's jobs are not held up by a person who sends many)."""
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
                        self._queued.pop(job_id, None)
                        self._wanted.discard(job_id)
                    continue
                # A run asked for (the schedule, Run now) goes first; else the oldest address
                # sent, whose site is the one the job waits for.
                with self._lock:
                    wanted = job_id in self._wanted
                fetches = [] if wanted else self.store.fetches([job_id])
                fetch = fetches[0] if fetches else None
                if not self._claim(ondemand.for_fetch(job, fetch["url"]) if fetch else job):
                    # Its site (or the job itself) is busy: back to the queue, a little later.
                    time.sleep(2)
                    self.requests.put(job_id)
                    continue
                if fetch is None:
                    with self._lock:
                        wanted = job_id in self._wanted
                        self._wanted.discard(job_id)
                # A recipe's test, unless a run judges the site anyway (a fetch's address is
                # another page: the test waits for the next turn).
                with self._lock:
                    testing = job_id in self._tests and fetch is None
                    if testing:
                        self._tests.discard(job_id)
                try:
                    self.run(job, wanted, fetch)
                    if testing and not wanted:
                        self.test(job)
                finally:
                    with self._lock:
                        self.controls.pop(job_id, None)
                        self._sites.pop(job_id, None)
                        again = job_id in self._wanted or job_id in self._tests
                    # Its next turn: a run asked for meanwhile, or the next address.
                    if again or self.store.fetches([job_id]):
                        self.request(job_id, run=False)
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
            self._queued.pop(job.id, None)
            return True

    def run(  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
        self, job: Job, wanted: bool = True, fetch: dict | None = None
    ) -> None:
        """Runs the job and records how it went; a new failure is also a notification to its user.
        A pending pass for its tags comes first; without wanted, only that pass (if any). With a
        fetch (an address sent to the on-demand job), the run is of that address, the job's
        options otherwise as they are; the fetch leaves its queue when the run ends, however."""
        assert job.id is not None
        job_id = job.id
        previous = self.store.retag_from(job_id)
        if previous is None and not wanted and fetch is None:
            return
        # Its controls: a runner made them when it took the job; a direct call (tests) makes them.
        with self._lock:
            control = self.controls.setdefault(job_id, Control())
        # The run's thread: while it waits for a browser, the page says so (waits).
        control.thread = threading.get_ident()
        # A fetch is a run, of its address.
        job = ondemand.for_fetch(job, fetch["url"]) if fetch else job
        wanted = wanted or fetch is not None
        self._begin(job_id, control, wanted, previous is not None, fetch)
        nc = self.connect()
        source = None
        watch = None
        message = ""
        needs.serve(self.desk, job_id, needs.teller(job.name, lambda level, text: self.store.log(job_id, level, text),
                                                    self.notify, nc))  # fmt: skip
        try:
            self.call(nc.set_user(job.user))
            source_class = SOURCES[job.source]
            credentials = self._credentials(nc, job)
            started = time.monotonic()
            home = bool(int(job.options.get("home", 0) or 0)) and bool(browser.PROXY)
            posing = download.impersonation(download.impersonates(job.options))
            with guarded_client(**posing, **({"proxy": browser.PROXY} if home else {})) as client:
                if home:
                    self.store.log(job_id, "info", through_proxy(client))
                source = source_class(job.options, credentials, client)
                source.home = home
                source.timezone = job.timezone
                source.contents = versions.Contents(
                    self.store,
                    job_id,
                    lambda url, change: self.notify(
                        nc,
                        "Harvester: {job} — the page changed",
                        {"job": job.name, "address": url, "change": change},
                        "{address}: {change}",
                    ),
                )
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
                        watch = self._watch(job, source, fetch)
                        message = self._work(job, source, library, control, started, None, fetch)
                finally:
                    try:
                        self._keep_session(nc, job, credentials, source)
                    finally:
                        source.close()
            if not self._changed(job, watch, source, nc, message):
                outcome = said("{address}: {outcome}", address=fetch["url"], outcome=message) if fetch else message
                self._succeeded(job_id, control, wanted, outcome, nc)
        except Exception as error:  # pylint: disable=broad-exception-caught
            # A listing that finds none of its links fails the run; where it found them before,
            # the site changed. A page asking for a person, or a refused sign-in, is no change.
            plain_error = isinstance(error, SourceError) and not isinstance(error, (AuthError, ChallengeError))
            if not (plain_error and self._changed(job, watch, source, nc, str(error), failed=True)):
                self._failed(job, nc, error, not wanted, getattr(source, "shot", None))
        finally:
            watch_stop(None)
            needs.serve(None)
            if fetch is not None:
                self.store.drop_fetch(fetch["id"])
            with self._lock:
                self.controls.pop(job_id, None)

    def _keep_session(self, nc: AsyncNextcloudApp, job: Job, credentials: dict[str, str], source: Source) -> None:
        """Keeps the source's session for the job's next run, also after a failure: cookies set
        before it (a sign-in) still count; not for a job deleted while it ran (its session went
        with it), nor when its account changed meanwhile (the session is the old account's). One
        the source gave up (stale: it no longer signed in) goes, unless a new one came."""
        if not self.store.job(job.id or 0) or self._credentials(nc, job) != credentials:
            return
        session = source.session()
        if session:
            self.call(save_session(nc, job, session))
        elif source.stale:
            self.call(nc.preferences_ex.delete(session_key(job)))

    def _watch(self, job: Job, source: Source, fetch: dict | None) -> Watch | None:
        """What the run's walk finds, level by level, for a website job's own runs (not a fetch's:
        another address, another page), judged by its last good runs."""
        if fetch is not None or not hasattr(source, "observe"):
            return None
        watch = Watch(self.store.summaries(job.id or 0))
        setattr(source, "observe", watch)
        return watch

    def _changed(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        job: Job,
        watch: Watch | None,
        source: Source | None,
        nc: AsyncNextcloudApp,
        message: str,
        failed: bool = False,
        run: bool = True,
    ) -> bool:
        """Keeps what the run (or, run False, the recipe's test) found; when a level found less
        than in the job's last good runs, it ends "changed" (neither OK nor failed): the progress
        log says which level and selector, with the picture of its page, and its person is told
        once (not again while it stays changed). message is what the run did, or why it failed
        (failed: then it is no good run, and unless changed the caller records the failure)."""
        if watch is None or not watch.levels:
            return False
        job_id = job.id or 0
        change = watch.change()
        self.store.add_summary(job_id, watch.summary(), good=change is None and not failed)
        if change is None:
            return False
        if not self.store.job(job_id):
            return True  # deleted while it ran: nobody to tell
        line = self.store.log(
            job_id, "warning", said("The site seems to have changed: {detail}", detail=change["detail"])
        )
        picture = getattr(source, "noticed", None)
        if picture:
            self.store.keep_shot(job_id, line, picture)
        text = said("The site seems to have changed: {reason}", reason=change["reason"])
        if run:
            outcome = said("{change}; {outcome}", change=text, outcome=message) if message else text
            self.store.finish(job_id, "changed", outcome)
        else:
            # A test is no run: the job's schedule counts from its last run still.
            self.store.mark(job_id, "changed", text)
        LOG.warning("job %s (%s) changed: %s", job_id, job.user, change["detail"])
        if job.last_status != "changed":
            self.notify(
                nc,
                "Harvester: {job} — the site seems to have changed: {reason}",
                {"job": job.name, "reason": change["reason"], "detail": change["detail"]},
                "{detail}. Test the recipe (its form's recorder) and pick the links anew.",
            )
        return True

    def test(self, job: Job) -> None:
        """Tests the website job's recipe (Site.survey: its walk, nothing downloaded) with its
        account and kept session, and judges what it found as a run's ("changed", told once); a
        test that finds the site as before ends the warning. Its last run (and its schedule)
        stay. Logged in the job's progress log."""
        assert job.id is not None
        job_id = job.id
        with self._lock:
            control = self.controls.setdefault(job_id, Control())
        control.thread = threading.get_ident()
        watch_stop(control.stopped)
        self.store.log(job_id, "info", said("Testing the recipe (nothing is downloaded)"))
        nc = self.connect()
        try:
            self.call(nc.set_user(job.user))
            credentials = self._credentials(nc, job)
            home = bool(int(job.options.get("home", 0) or 0)) and bool(browser.PROXY)
            posing = download.impersonation(download.impersonates(job.options))
            with guarded_client(**posing, **({"proxy": browser.PROXY} if home else {})) as client:
                source = SOURCES[job.source](job.options, credentials, client)
                source.home = home
                source.timezone = job.timezone
                setattr(source, "sleep", control.nap)
                kept = self.call(load_session(nc, job))
                if kept:
                    source.restore(kept)
                watch = Watch(self.store.summaries(job_id))
                setattr(source, "observe", watch)
                try:
                    result = getattr(source, "survey")()
                finally:
                    try:
                        self._keep_session(nc, job, credentials, source)
                    finally:
                        source.close()
            self._tested(job, result, watch, source, nc)
        except Exception as error:  # pylint: disable=broad-exception-caught
            self.store.log(
                job_id, "error", said("The recipe's test failed: {error}", error=str(error) or type(error).__name__)
            )
        finally:
            watch_stop(None)
            with self._lock:
                self.controls.pop(job_id, None)

    def _tested(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, job: Job, result: dict, watch: Watch, source: Source, nc: AsyncNextcloudApp
    ) -> None:
        """The recipe test's outcome in the progress log, judged as a run's."""
        job_id = job.id or 0
        for problem in result["problems"]:
            self.store.log(job_id, "info", said("Test: {problem}", problem=problem))
        pages = len(result["pages"])
        summary = said_n(
            "The recipe's test: {files} file links on %n page",
            "The recipe's test: {files} file links on %n pages",
            pages,
            files=result["files"],
        )
        if result.get("error") in ("ChallengeError", "AuthError"):
            # Not the site's change: the run says so (Needs you, Sign-in refused) when it comes.
            self.store.log(
                job_id, "error", said("{summary}; it ended: {problem}", summary=summary, problem=result["problems"][-1])
            )
            return
        if result["ended"] == "stopped":
            self.store.log(job_id, "done", said("{summary}; stopped", summary=summary))
            return
        failure = result["problems"][-1] if result["ended"] == "failed" else ""
        if self._changed(job, watch, source, nc, failure, failed=bool(failure), run=False):
            return
        latest = self.store.job(job_id)
        if failure:
            line = self.store.log(
                job_id, "error", said("{summary}; it failed: {problem}", summary=summary, problem=failure)
            )
            if result.get("shot"):
                self.store.keep_shot(job_id, line, result["shot"])
        else:
            self.store.log(job_id, "done", said("{summary}: as before", summary=summary))
            # Found as before: a warning of a changed site is over (the next run says the rest).
            if latest and latest.last_status == "changed":
                self.store.mark(job_id, "ok", said("The recipe's test found the site as before"))

    def _succeeded(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, job_id: int, control: Control, wanted: bool, message: str, nc: AsyncNextcloudApp
    ) -> None:
        """Records a run (or a pass for the tags) that went through; a one-time job has then done
        what it was for, unless it was stopped or has more addresses to fetch."""
        # A pass for the tags is no run: the job's last run (and its schedule) stay.
        if wanted:
            self.store.finish(job_id, "ok", message)
        self.store.log(job_id, "done", message)
        # The job as it is now: the person may have changed it meanwhile.
        latest = self.store.job(job_id)
        more = [other for other in self.store.fetches([job_id]) if other["id"] != control.fetch]
        if wanted and latest and latest.once and not control.stopped.is_set() and not more:
            self._once_done(latest, nc, message)

    def _once_done(self, job: Job, nc: AsyncNextcloudApp, message: str) -> None:
        """Deletes the one-time job after a run that went through, with its account and kept
        session (as Delete on the page does); its files stay. Its log goes with it: the person
        is told what it did in a notification instead."""
        job_id = job.id or 0
        self.call(
            nc.preferences_ex.delete(
                [credential_key(job, name) for name in SOURCES[job.source].credentials] + [session_key(job)]
            )
        )
        self.store.delete(job_id)
        LOG.info("one-time job %s (%s) deleted after its run", job_id, job.user)
        self.notify(
            nc,
            "Harvester: {job} done",
            {"job": job.name, "result": message},
            "{result}. The one-time job is deleted; its files stay.",
        )

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

    def _failed(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, job: Job, nc: AsyncNextcloudApp, error: Exception, retagging: bool, shot: dict | None = None
    ) -> None:
        """Records why a run failed (with what the browser showed, if the source shot it), and
        tells its user of a new failure; a job deleted while it ran ends quietly, and a pass for
        the tags only logs it (the job's last run stays)."""
        job_id = job.id or 0
        if not self.store.job(job_id):
            return
        message = str(error) or type(error).__name__
        line = self.store.log(job_id, "error", message)
        if shot:
            self.store.keep_shot(job_id, line, shot)
        if retagging:
            return
        if isinstance(error, ChallengeError):
            # Not a failure of the job: it waits for its user to answer the page in the browser.
            self.store.finish(job_id, "challenge", message, challenge_url=error.url)
            self.notify(
                nc,
                "Harvester: {job} needs you",
                {"job": job.name, "address": error.url, "kind": error.kind},
                "{address} asks for a person ({kind}).",
            )
            return
        status = "auth" if isinstance(error, AuthError) else "failed"
        LOG.warning("job %s (%s) %s: %s", job_id, job.user, status, message)
        self.store.finish(job_id, status, message)
        # Told once: the same failure on the next scheduled runs is no news.
        if (job.last_status, job.last_message) != (status, message):
            self.notify(nc, "Harvester: {job} failed", {"job": job.name}, detail=message)

    def _work(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        self,
        job: Job,
        source: Source,
        library: runner.Library,
        control: Control,
        started: float,
        previous: list[str] | None,
        fetch: dict | None = None,
    ) -> str:
        """The run itself, its files' tags first brought up to date; with previous (the job's
        tags before a change), only the tags: of the files it recorded, then of those it saved
        before it recorded them, found by a listing without downloads. A fetch's run says how
        many of the address's files the job fetched before (not again: "seen" is the job's)."""
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
            message, whole = runner.earlier_tags(
                job, source, library, earlier, previous, changed, report, record, stopped
            )
            # Pending until a pass goes through: one stopped, failed or cut short by a restart
            # comes again at the job's next turn.
            if whole:
                self.store.clear_retag(job_id, job.tags)
            return message
        if changed:
            report("info", said_n("Tags brought up to date on %n file", "Tags brought up to date on %n files", changed))
        known_before = 0

        def seen(key: str) -> bool:
            nonlocal known_before
            found = self.store.seen(job_id, key)
            known_before += found and not key.startswith(MARK)
            return found

        message = runner.run(
            job,
            source,
            library,
            seen,
            lambda key: self.store.mark_seen(job_id, key),
            until=lambda: started + RUN_LIMIT + control.paused_for,
            report=report,
            sleep=control.nap,
            stopped=stopped,
            record=record,
            owners={entry["file"]: key for key, entry in known.items() if entry["file"] is not None},
            later=runner.SENT_AGAIN if fetch else runner.NEXT_RUN,
        )
        if fetch and known_before:
            report(
                "info",
                said_n(
                    "%n of the files at {address} was fetched before: not again",
                    "%n of the files at {address} were fetched before: not again",
                    known_before,
                    address=fetch["url"],
                ),
            )
        return message

    def _begin(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, job_id: int, control: Control, run: bool, retag: bool, fetch: dict | None = None
    ) -> None:
        """A turn's start: a run's clears the last run's message (the page shows this run's
        progress, its log, instead); downloads in progress end at once when it is stopped (one
        job per thread). A fetch's names its address."""
        control.fetch = fetch["id"] if fetch else 0
        if run:
            self.store.begin(job_id)
        if retag:
            self.store.log(job_id, "info", said("The job's tags changed: bringing them to its files"))
        if fetch:
            self.store.log(job_id, "info", said("Fetching {address}", address=fetch["url"]))
        watch_stop(control.stopped)

    def notify(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, nc: AsyncNextcloudApp, subject: str, values: dict[str, str], message: str = "", detail: str = ""
    ) -> None:
        """Tells the job's user, with a link to the app's page; a notification that fails is lost.
        The subject and the message are the app's texts with {placeholders} (scripts/l10n.py
        collects them): Nextcloud translates them into the user's language with the app's
        l10n/<language>.json and puts in the values, shown as they are. detail, a text of the
        run's own (its error), comes as it is in place of a message."""
        params = {
            key: {"type": "highlight", "id": key, "name": str(value)[:400] or "-"} for key, value in values.items()
        }

        def used(text: str) -> dict:
            return {key: param for key, param in params.items() if f"{{{key}}}" in text}

        try:
            self.call(
                nc.notifications.create(
                    subject,
                    message or plain(detail),
                    subject_params=used(subject),
                    message_params=used(message),
                    link=page_link(nc),
                )
            )
        except Exception:  # pylint: disable=broad-exception-caught
            LOG.warning("a notification could not be sent: %s", subject)
