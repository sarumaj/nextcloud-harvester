"""Runs the jobs when their schedule says, and when a user asks; one at a time."""

import datetime
import json
import queue
import threading
import time
from collections.abc import Callable

import httpx
from croniter import croniter
from nc_py_api import NextcloudApp

from . import runner
from .nextcloud import NextcloudLibrary
from .sources import SOURCES, AuthError, ChallengeError
from .store import Job, Store

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Nextcloud-Harvester"


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


def load_session(nc: NextcloudApp, job: Job) -> dict | None:
    """The job's kept session, or None."""
    value = nc.preferences_ex.get_value(session_key(job))
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def save_session(nc: NextcloudApp, job: Job, session: dict | None) -> None:
    """Keeps the job's session for its next run."""
    if session:
        nc.preferences_ex.set_value(session_key(job), json.dumps(session), sensitive=True)


def page_link(nc: NextcloudApp) -> str:
    """The app's page in Nextcloud, for notifications."""
    return f"{nc.app_cfg.endpoint.rstrip('/')}/index.php/apps/app_api/embedded/harvester/harvester"


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
                kept = load_session(nc, job)
                if kept:
                    source.restore(kept)
                try:
                    message = runner.run(
                        job,
                        source,
                        NextcloudLibrary(nc),
                        lambda key: self.store.seen(job_id, key),
                        lambda key: self.store.mark_seen(job_id, key),
                    )
                finally:
                    # Kept also after a failure: cookies set before it (a sign-in) still count.
                    try:
                        save_session(nc, job, source.session())
                    finally:
                        source.close()
            self.store.finish(job_id, "ok", message)
        except ChallengeError as error:
            # Not a failure of the job: it waits for its user to answer the page in the browser.
            self.store.finish(job_id, "challenge", str(error), challenge_url=error.url)
            self.notify(nc, f"Harvester: {job.name} needs you", f"{error.url} asks for a person ({error.kind}).")
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Whatever broke, the job is marked and the thread goes on with the others.
            message = str(error) or type(error).__name__
            self.store.finish(job_id, "auth" if isinstance(error, AuthError) else "failed", message)
            self.notify(nc, f"Harvester: {job.name} failed", message)
        finally:
            self.running = None

    @staticmethod
    def notify(nc: NextcloudApp, subject: str, message: str) -> None:
        """Tells the job's user, with a link to the app's page; a notification that fails is lost."""
        try:
            nc.notifications.create(subject, message, link=page_link(nc))
        except Exception:  # pylint: disable=broad-exception-caught
            pass
