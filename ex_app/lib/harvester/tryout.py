# SPDX-License-Identifier: AGPL-3.0-or-later
"""Try the address: a job's listing run once from its form as it is, nothing downloaded.

The job's form (gallery-dl, yt-dlp, a feed, releases, an rclone remote, a mailbox) asks before it
is saved what its source would find: the executor (Tries.trial) runs the source's listing
once with the form's options and account, in a runner's place and through the guard, the proxy
and the DNS servers as a run does, for at most LIMIT seconds. What it finds is judged as a run
would take it (Trial): the files this run would fetch (Files per run), those the job's history
has already, those its file types or sizes leave out where the listing tells (by the name's
extension, by a size the site gives), and how many its patterns left out. The first SHOWN files
are listed with the path and tags a run gives them (the job's File name and Tags templates); a
file named only by its download (pending) says so. Nothing is written: no file, no mark of the
job's, no log line; the progress lines the source says (a mailbox's messages, an entry skipped)
come back with the outcome.
"""

import abc
import asyncio
import mimetypes
import posixpath
import threading
import time
from collections.abc import Callable
from typing import Any

from . import browser
from .l10n import Said, error_text, listed, said, said_n, template_of
from .patterns import render_tags
from .sources import SOURCES, AuthError, ChallengeError, Item, Refused, Source, SourceError
from .sources import download
from .sources.base import count_misses, nap, watch_stop
from .store import Job

# How long a try may take (s): a listing still going then is cut there (what it found is shown).
LIMIT = 60
# How long a try waits for a runner (and its site) to be free before it gives up.
CLAIM_WAIT = 15
# The files shown with their names and tags, and the most a try looks at.
SHOWN = 20
MOST = 2000
# The progress lines a try gives back (a mailbox's messages: mail.TOLD of them, and a few more).
NOTES = 60

# What a try says of each file, as a run would take it: fetched this run, in a later run (Files
# per run), known (the job's history has it), left out by its type or size (as far as the listing
# tells), or a problem of its own (its name cannot be made, a message too large).
FETCH, LATER, KNOWN, TYPE, SIZE, PROBLEM = "fetch", "later", "known", "type", "size", "problem"
# The file types by what their names' extensions say (mimetypes), the job's choices as the form
# offers them; an extension that tells nothing leaves the file to its download's check.
FITS = {
    "pdf": lambda kind, ext: kind == "application/pdf",
    "zip": lambda kind, ext: kind in ("application/zip", "application/x-zip-compressed"),
    "image": lambda kind, ext: kind.startswith("image/"),
    "audio": lambda kind, ext: kind.startswith("audio/"),
    "video": lambda kind, ext: kind.startswith("video/"),
    "html": lambda kind, ext: kind in ("text/html", "application/xhtml+xml"),
    "markdown": lambda kind, ext: ext in (".md", ".markdown") or kind == "text/markdown",
}


def wrong_type(path: str, types: list[str]) -> bool:
    """Whether the file's name says it is of none of the job's types (False when it cannot
    tell: no extension, or one mimetypes does not know)."""
    if "any" in types:
        return False
    ext = posixpath.splitext(path)[1].lower()
    kind = (mimetypes.guess_type(f"file{ext}")[0] or "") if ext else ""
    if not kind and ext not in (".md", ".markdown"):
        return False
    return not any(FITS[name](kind, ext) for name in types if name in FITS)


def size_of(values: dict) -> int:
    """The file's size as the listing gives it (a release's asset, a remote's file, yt-dlp's or
    gallery-dl's filesize), 0 when it gives none."""
    for name in ("size", "filesize", "filesize_approx"):
        value = values.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return 0


def problem_state(problem: SourceError) -> str:
    """A file's own problem as a try counts it: refused for its type or size, else a problem."""
    if isinstance(problem, Refused):
        return TYPE if "file types" in str(problem) else SIZE
    return PROBLEM


# What may be behind a listing's failure, by words of its error (lower case): the first rule
# whose words it has gives the hint (of the tool: gallery-dl or yt-dlp).
HINTS: tuple[tuple[tuple[str, ...], Callable[[str], Said]], ...] = (
    (
        ("blocked by the dns server",),
        lambda tool: said("The DNS servers block this site (the administrator's filter): ask them to allow it."),
    ),
    (
        ("may not be opened", "not allowed"),
        lambda tool: said("Harvester opens only public http(s) addresses, not local or private ones."),
    ),
    (
        ("unsupported url", "no suitable extractor"),
        lambda tool: said("{tool} does not know this site: check the address, or use a website job.", tool=tool),
    ),
    (
        ("401", "403", "login", "log in", "sign in", "private", "cookies", "members"),
        lambda tool: said(
            "The site may want the account: its user name and password, or a signed-in browser's cookies."
        ),
    ),
    (
        ("is not a feed",),
        lambda tool: said("The address is a page, not a feed: look for its RSS or Atom link (Browse)."),
    ),
    (
        ("resolve", "name or service", "connect", "timed out", "unreachable"),
        lambda tool: said("The site did not answer: check the address, or try again later."),
    ),
)


def hint(error: BaseException, source: str) -> Said | str:
    """What may be behind a listing's failure, for the person: the account, an address that is
    refused or blocked, a site the tool does not know; "" when nothing is known."""
    if isinstance(error, AuthError):
        return said("The site refused the account: check its fields (a password, a token, the cookies).")
    if isinstance(error, ChallengeError):
        return said("The site asks for a person: a job of this type cannot answer that (a website job can).")
    text = str(error).lower()
    for words, tip in HINTS:
        if any(word in text for word in words):
            return tip("gallery-dl" if source == "gallery" else "yt-dlp")
    return ""


class Trial:  # pylint: disable=too-many-instance-attributes
    """One try of a job's listing: what it found so far, and how it ended. Filled by the
    executor's thread, read by the request that asked (result) once it ends or its time is up."""

    def __init__(self, job: Job, known: Any = None, limit: float | None = None):
        self.job = job
        # Whether the job's history has a file (its key); None: no history (a new job).
        self.known = known
        self.limit = LIMIT if limit is None else limit
        self.started = time.monotonic()
        # Set at the time limit (and by a request that stops waiting): a helper program is killed
        # (stoppable_run), the listing ends at its next file.
        self.stop = threading.Event()
        self.done = threading.Event()
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self.counts = {state: 0 for state in (FETCH, LATER, KNOWN, TYPE, SIZE, PROBLEM)}
        # How many files the job's patterns left out (base.count_misses), the source's lines.
        self.misses = [0]
        self.notes: list[str] = []
        self.error: BaseException | None = None
        self.busy: Said | None = None
        # Why the listing ended before its end: "time" (LIMIT), "most" (MOST files), "" none.
        self.cut = ""
        self.limit_files = int(job.options.get("max_items", 10) or 10)

    def note(self, _level: str, text: str) -> None:
        """A progress line of the source's (Source.report): kept, up to NOTES."""
        with self._lock:
            if len(self.notes) < NOTES:
                self.notes.append(text)

    def time_up(self) -> bool:
        """Whether the try's time is up (then it is stopped)."""
        if time.monotonic() - self.started > self.limit:
            self.stop.set()
        return self.stop.is_set()

    def judge(self, source: Source, item: Item) -> str:
        """What a run would do with the item (FETCH, LATER, KNOWN, TYPE, SIZE, PROBLEM), the
        files before it counted."""
        if item.problem is not None:
            return problem_state(item.problem)
        if self.known is not None and self.known(item.key):
            return KNOWN
        if not item.pending and wrong_type(item.path or item.url, download.file_types(source.settings)):
            return TYPE
        size = size_of(item.values)
        if size and (size > download.largest(source.settings) or size < int(source.settings.get("min_size", 0) or 0)):
            return SIZE
        taken = self.counts[FETCH]
        return FETCH if self.limit_files < 0 or taken < self.limit_files else LATER

    def add(self, source: Source, item: Item) -> None:
        """The item as a run would take it: counted, and among the first SHOWN with its path,
        tags and why it is left out."""
        state = self.judge(source, item)
        with self._lock:
            self.counts[state] += 1
            if len(self.items) >= SHOWN:
                return
            path = posixpath.join(self.job.target, item.path) if item.path else ""
            problem = item.problem
            self.items.append(
                {
                    "path": path,
                    "url": item.url,
                    "tags": render_tags(self.job.tags, item.values),
                    "state": state,
                    # Its name comes with the file: path is a stand-in until the download.
                    "pending": item.pending,
                    "problem": error_text(problem) if problem is not None else "",
                    "problem_l10n": template_of(error_text(problem)) if problem is not None else None,
                }
            )

    def listing(self, source: Source) -> None:
        """Lists the source's files (none of them seen: the job's history is judged here) until
        the end, the time limit or MOST files; a failure is kept as the try's outcome."""
        try:
            for item in source.items(lambda key: False):
                self.add(source, item)
                if sum(self.counts.values()) >= MOST:
                    self.cut = "most"
                    break
                if self.time_up():
                    self.cut = "time"
                    break
        except Exception as error:  # pylint: disable=broad-exception-caught
            # A listing stopped at its time limit (a helper program killed) is cut, no failure.
            if self.stop.is_set():
                self.cut = "time"
            else:
                self.error = error

    def outcome(self) -> Said:
        """The try's outcome in one line: what it found and what a run would do with it."""
        counts = self.counts
        found = sum(counts.values())
        figures = [said_n("found %n file", "found %n files", found)]
        if self.cut == "time":
            figures[0] = said_n(
                "found %n file in {seconds} s (the try's time limit; there may be more)",
                "found %n files in {seconds} s (the try's time limit; there may be more)",
                found,
                seconds=int(self.limit),
            )
        elif self.cut == "most":
            figures[0] = said_n(
                "found %n file (a try looks at no more; there may be more)",
                "found %n files (a try looks at no more; there may be more)",
                found,
            )
        figures.append(said_n("%n would be fetched this run", "%n would be fetched this run", counts[FETCH]))
        if counts[LATER]:
            figures.append(
                said_n(
                    "%n in later runs (Files per run: {limit})",
                    "%n in later runs (Files per run: {limit})",
                    counts[LATER],
                    limit=self.limit_files,
                )
            )
        if counts[KNOWN]:
            figures.append(said_n("%n already in the job's history", "%n already in the job's history", counts[KNOWN]))
        if counts[TYPE]:
            figures.append(
                said_n(
                    "%n skipped: not of the job's file types", "%n skipped: not of the job's file types", counts[TYPE]
                )
            )
        if counts[SIZE]:
            figures.append(
                said_n("%n skipped: outside the job's sizes", "%n skipped: outside the job's sizes", counts[SIZE])
            )
        if self.misses[0]:
            figures.append(
                said_n("%n left out by the job's patterns", "%n left out by the job's patterns", self.misses[0])
            )
        if counts[PROBLEM]:
            figures.append(said_n("%n with a problem (below)", "%n with a problem (below)", counts[PROBLEM]))
        return listed(figures, "unit")

    def result(self) -> dict:
        """What the page shows: the outcome (or the failure, with a hint), the first files and
        the source's lines; each text with its template (<key>_l10n), which the page translates."""
        with self._lock:
            items, notes = list(self.items), list(self.notes)
            found = sum(self.counts.values())
        answer: dict = {
            "ok": self.error is None and self.busy is None,
            "running": not self.done.is_set(),
            "seconds": round(time.monotonic() - self.started, 1),
            "found": found,
            "counts": dict(self.counts) | {"pattern": self.misses[0]},
            "items": items,
            "more": max(0, found - len(items)),
            "cut": self.cut,
            "notes": notes,
            "notes_l10n": [template_of(note) for note in notes],
        }
        if self.busy is not None:
            return answer | {"error": self.busy, "error_l10n": template_of(self.busy), "hint": "", "hint_l10n": None}
        if self.error is not None:
            text = error_text(self.error)
            # Never a value of the account: the sources' errors name the site, not the secrets.
            tip = hint(self.error, self.job.source)
            return answer | {
                "error": text,
                "error_l10n": template_of(text),
                "hint": tip,
                "hint_l10n": template_of(tip),
            }
        outcome = self.outcome()
        return answer | {"outcome": outcome, "outcome_l10n": template_of(outcome)}


class Tries:  # pylint: disable=too-few-public-methods
    """The executor's tries of a job's listing (mixed into Executor, whose state and methods
    these are): each in a runner's place, one of each person's at a time."""

    runners: int
    controls: dict
    connect: Callable[[], Any]
    _lock: threading.Lock
    _sites: dict[int, str]
    _trials: dict[int, str]
    _trying: set[str]

    @abc.abstractmethod
    def call(self, awaitable: Any) -> Any:
        """A Nextcloud request run to its end (the executor's)."""

    @abc.abstractmethod
    def _site(self, job: Job) -> str:
        """The site the job fetches from (the executor's)."""

    @abc.abstractmethod
    def _credentials(self, nc: Any, job: Job) -> dict[str, str]:
        """The saved job's account (the executor's)."""

    @abc.abstractmethod
    def _kept_session(self, nc: Any, job: Job) -> dict | None:
        """The saved job's kept session (the executor's)."""

    @abc.abstractmethod
    def _keep_session(self, nc: Any, job: Job, credentials: dict[str, str], source: Source) -> None:
        """Keeps the source's session for the job, as a run does (the executor's)."""

    def enter_trial(self, user: str) -> bool:
        """Whether the person may try a job's listing now (then the try is theirs until trial
        ends): one try each at a time."""
        with self._lock:
            if user in self._trying:
                return False
            self._trying.add(user)
            return True

    def trial(self, job: Job, account: dict[str, str], trial: Trial) -> None:
        """Tries the job's listing from its form in a runner's place, once one and the job's site
        are free (up to CLAIM_WAIT): its source as a run makes it (the guarded client, the
        proxy, impersonation), with the account typed in the form over the saved job's (job.id:
        its history, account and kept session), nothing downloaded or marked. A kept session the
        source renewed is kept as a run keeps it, when the account is the saved one. The
        person's try (enter_trial) ends here."""
        token = id(trial)
        # At the time limit the listing is stopped (a helper program killed, a wait ended).
        timer = threading.Timer(max(0.0, trial.limit - (time.monotonic() - trial.started)), trial.stop.set)
        timer.daemon = True
        timer.start()
        try:
            if not self._claim_trial(token, job, trial):
                trial.busy = said(
                    "No runner is free (all {runners} busy), or the job's site is in use: try again in a few minutes.",
                    runners=self.runners,
                )
                return
            watch_stop(trial.stop)
            count_misses(trial.misses)
            self._listed(job, account, trial)
        except Exception as error:  # pylint: disable=broad-exception-caught
            trial.error = error
        finally:
            timer.cancel()
            count_misses(None)
            watch_stop(None)
            with self._lock:
                self._trials.pop(token, None)
                self._trying.discard(job.user)
            trial.done.set()

    def _claim_trial(self, token: int, job: Job, trial: Trial) -> bool:
        """Takes a runner's place for the try, waiting up to CLAIM_WAIT for one and for the job's
        site (a try of a job's own while it runs waits for it). A new job without an address
        (a mailbox, a remote) is a site of its person's."""
        site = self._site(job)
        if job.id is None and site.startswith("job:"):
            site = f"try:{job.user}"
        until = time.monotonic() + CLAIM_WAIT
        while True:
            with self._lock:
                free = len(self.controls) + len(self._trials) < self.runners
                if free and site not in self._sites.values() and site not in self._trials.values():
                    self._trials[token] = site
                    return True
            if time.monotonic() > until or trial.stop.is_set():
                return False
            time.sleep(0.5)

    def _listed(self, job: Job, account: dict[str, str], trial: Trial) -> None:
        """The try's listing, its source made as a run makes it."""
        nc = self.connect() if job.id is not None else None
        stored: dict[str, str] = {}
        if nc is not None:
            self.call(nc.set_user(job.user))
            stored = self._credentials(nc, job)
        credentials = stored | {name: value for name, value in account.items() if value}
        # The kept session goes with the saved account only (another one typed is not its).
        saved = nc is not None and credentials == stored
        home = bool(int(job.options.get("home", 0) or 0)) and bool(browser.PROXY)
        posing = download.impersonation(download.impersonates(job.options))
        with download.guarded_client(**posing, **({"proxy": browser.PROXY} if home else {})) as client:
            source = SOURCES[job.source](job.options, credentials, client)
            source.home = home
            source.timezone = job.timezone
            source.report = trial.note
            if hasattr(source, "sleep"):
                setattr(source, "sleep", nap)
            kept = self._kept_session(nc, job) if saved else None
            if kept:
                source.restore(kept)
            try:
                trial.listing(source)
            finally:
                try:
                    if saved:
                        self._keep_session(nc, job, credentials, source)
                finally:
                    source.close()


async def attempt(executor: Tries, job: Job, account: dict[str, str], known: Callable[[str], bool] | None) -> dict:
    """The try's result for the page: the executor's trial in a thread of its own, awaited up to
    a little more than LIMIT (the listing stops itself then; one that does not, a request that
    hangs, is left to end on its own, holding its runner's place until then)."""
    trial = Trial(job, known)
    loop = asyncio.get_running_loop()
    ended = loop.create_future()

    def work() -> None:
        try:
            executor.trial(job, account, trial)
        finally:
            loop.call_soon_threadsafe(lambda: ended.done() or ended.set_result(None))

    threading.Thread(target=work, name="try", daemon=True).start()
    try:
        await asyncio.wait_for(asyncio.shield(ended), LIMIT + 5)
    except asyncio.TimeoutError:
        trial.stop.set()
        trial.cut = trial.cut or "time"
    return trial.result()
