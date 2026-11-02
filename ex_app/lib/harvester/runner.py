"""One run of a job: the source's new items, downloaded into the user's folder and tagged."""

import abc
import dataclasses
import posixpath
import tempfile
import time
from collections.abc import Callable
from typing import BinaryIO

from .patterns import render_tags
from .sources import (
    FULL_PASS,
    REFUSED,
    AuthError,
    ChallengeError,
    Denied,
    Option,
    Refused,
    Source,
    SourceError,
    Stopped,
)
from .store import Job

# Files that failed on their own before a run without any success gives up (the site, not the
# files, is the trouble then).
MAX_FAILURES = 5
# Files of other items at the path an item's name gives: it takes the name with " (2)", " (3)", ...
MAX_TWINS = 100

# Options every job has, whatever its source.
COMMON_OPTIONS = {
    "max_items": Option(
        "Files per run",
        10,
        "The rest comes in the next runs (a first run may find hundreds); -1: no limit (a run still ends "
        "after two hours, and goes on in the next).",
        minimum=-1,
        suggestions=("3", "10", "50", "-1"),
    ),
    # Its title names the proxy as the admin calls it (browser.PROXY_NAME, given by the page).
    "home": Option(
        "Through the proxy",
        0,
        "The site sees the proxy's address instead of the server's (some sites refuse servers, or ask them "
        "to prove they are human more often). Slower: the files come through the proxy.",
        kind="flag",
    ),
}


class Library(abc.ABC):
    """Where the files go: the user's files in Nextcloud."""

    @abc.abstractmethod
    def find(self, path: str) -> int | None:
        """Returns the file's ID, or None when there is no such file."""

    @abc.abstractmethod
    def put(self, path: str, content: BinaryIO) -> int:
        """Stores the file (its folders created as needed) and returns its ID."""

    @abc.abstractmethod
    def tag(self, file_id: int, tags: list[str]) -> None:
        """Gives the file the collaborative tags, creating those that do not exist yet; FileGone
        when there is no such file any more."""

    @abc.abstractmethod
    def untag(self, file_id: int, tags: list[str]) -> None:
        """Takes the collaborative tags off the file (those it does not have are no matter);
        FileGone when there is no such file any more."""


class FileGone(Exception):
    """The file a job saved is not there any more (deleted or moved away by its user)."""


@dataclasses.dataclass
class Placed:
    """A file a job saved or found in its folder: its item's key, its ID (None: not in the
    folder when it was looked for), the values its tags are made of, and the tags it was given.
    A change of the job's tags is brought to these files (retag_placed)."""

    key: str
    file_id: int | None
    values: dict
    tags: list[str]


# A line of a run's progress: its level (info, error) and its text.
Report = Callable[[str, str], None]


def size_text(size: int) -> str:
    """A file's size for people: 512 B, 3.4 MB."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} GB"


def numbered(path: str, number: int) -> str:
    """The path with " (number)" before the file name's extension: a/Rechnung (2).pdf."""
    folder, name = posixpath.split(path)
    stem, extension = posixpath.splitext(name)
    return posixpath.join(folder, f"{stem} ({number}){extension}")


def free_path(job: Job, library: Library, item, owners: dict[int, str]) -> tuple[str, int | None]:
    """Where the item's file goes, and the ID of the file there already (None: none). A file the
    job saved for another item (two invoices of one day, links all called "Download") is not
    this one's: it takes the next free name with " (2)", " (3)", ...; a file the job has no
    record of (put there by hand, or before the job recorded its files) is adopted."""
    path = posixpath.join(job.target, item.path)
    file_id = library.find(path)
    number = 1
    while file_id is not None and owners.get(file_id, item.key) != item.key:
        number += 1
        if number > MAX_TWINS:
            raise SourceError(f"{path} and {MAX_TWINS - 1} numbered names are taken by other files of the job")
        path = numbered(posixpath.join(job.target, item.path), number)
        file_id = library.find(path)
    return path, file_id


def place(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    job: Job,
    source: Source,
    library: Library,
    item,
    pause: Callable[[], None],
    report: Report,
    record: Callable[[Placed], None] = lambda placed: None,
    owners: dict[int, str] | None = None,
) -> bool:
    """Puts the item's file into the job's folder (after the pause) and tags it; False when it
    was there already (downloaded by hand or by an earlier job: kept, tagged and not downloaded
    again). record keeps which file it is and its tags, for a later change of the job's tags;
    owners are the files the job saved, by ID, with their items' keys (free_path)."""
    if item.problem is not None:
        raise item.problem
    if not item.path.strip("/"):
        raise SourceError(f"the file name for {item.url} came out empty (check the job's File name)")
    path, file_id = free_path(job, library, item, owners or {})
    fetched = file_id is None
    if file_id is None:
        pause()
        report("info", f"Downloading {path}")
        with tempfile.TemporaryFile() as content:
            source.fetch(item, content)
            size = content.tell()
            content.seek(0)
            file_id = library.put(path, content)
        report("info", f"Saved {path} ({size_text(size)})")
    else:
        report("info", f"Already in the folder, tagged: {path}")
    # The job's tags are templates of the link's values: {year} is the link's year.
    tags = render_tags(job.tags, item.values)
    if tags:
        library.tag(file_id, tags)
    record(Placed(item.key, file_id, dict(item.values), tags))
    return fetched


def retag_placed(job: Job, placed: list[Placed], library: Library, record: Callable[[Placed], None]) -> int:
    """Brings the job's tags to the files it saved: the missing ones added, those it gave
    earlier and has no more taken off (tags their user added stay). Returns how many changed;
    a file that is gone is recorded so and left."""
    changed = 0
    for entry in placed:
        if entry.file_id is None:
            continue
        wanted = render_tags(job.tags, entry.values)
        if wanted == entry.tags:
            continue
        try:
            if wanted:
                library.tag(entry.file_id, wanted)
            obsolete = [tag for tag in entry.tags if tag not in wanted]
            if obsolete:
                library.untag(entry.file_id, obsolete)
        except FileGone:
            record(dataclasses.replace(entry, file_id=None, tags=[]))
            continue
        record(dataclasses.replace(entry, tags=wanted))
        changed += 1
    return changed


def retag_earlier(  # pylint: disable=too-many-arguments
    job: Job,
    source: Source,
    library: Library,
    is_earlier: Callable[[str], bool],
    record: Callable[[Placed], None],
    *,
    previous: list[str],
    report: Report = lambda level, text: None,
    stopped: Callable[[], bool] = lambda: False,
) -> tuple[int, bool]:
    """The files a job saved before it recorded them (is_earlier: their keys): the site is
    listed again (nothing is downloaded), each is looked for in the folder, given the job's
    tags, and the tags of its previous tags (previous) that it has no more are taken off.
    Returns how many were tagged, and whether the listing went to its end."""
    tagged = 0
    try:
        for item in source.items(lambda key: False):
            if stopped():
                return tagged, False
            # An item whose name cannot be made was never saved.
            if item.problem is not None or not is_earlier(item.key):
                continue
            file_id = library.find(posixpath.join(job.target, item.path))
            if file_id is None:
                record(Placed(item.key, None, dict(item.values), []))
                continue
            wanted = render_tags(job.tags, item.values)
            obsolete = [tag for tag in render_tags(previous, item.values) if tag not in wanted]
            try:
                if wanted:
                    library.tag(file_id, wanted)
                if obsolete:
                    library.untag(file_id, obsolete)
            except FileGone:
                record(Placed(item.key, None, dict(item.values), []))
                continue
            record(Placed(item.key, file_id, dict(item.values), wanted))
            report("info", f"Tagged: {posixpath.join(job.target, item.path)}")
            tagged += 1
    except Stopped:
        return tagged, False
    return tagged, True


@dataclasses.dataclass
class Tally:
    """What a run did: files downloaded, found already there, skipped (refused by type or size),
    and the errors of those that failed (denied: how many of them the site refused, 401 or 403)."""

    downloaded: int = 0
    adopted: int = 0
    skipped: int = 0
    failures: list[str] = dataclasses.field(default_factory=list)
    denied: int = 0

    def sentence(self) -> str:
        """The run's outcome, as the jobs' list shows it."""
        message = f"{self.downloaded} downloaded"
        if self.adopted:
            message += f", {self.adopted} already there"
        if self.skipped:
            message += f", {self.skipped} skipped (not of the job's file types, or too large)"
        if self.failures:
            message += f", {len(self.failures)} failed (tried again next run): {self.failures[0]}"
        return message


def run(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements  # the run's parts, each given by the executor
    job: Job,
    source: Source,
    library: Library,
    seen: Callable[[str], bool],
    mark_seen: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    *,
    until: float | Callable[[], float] | None = None,
    report: Report = lambda level, text: None,
    stopped: Callable[[], bool] = lambda: False,
    record: Callable[[Placed], None] = lambda placed: None,
    owners: dict[int, str] | None = None,
) -> str:
    """Downloads up to max_items new items (and stops at until, a time.monotonic(), or a
    function giving it) and returns what was done, in a sentence; report hears of each step
    (the page's progress log). stopped is asked before each file: it may wait while the run is
    paused, and True ends the run there (the rest comes next run, as at the limits). owners are
    the files the job saved before, by ID, with their items' keys."""
    limit = int(job.options.get("max_items", COMMON_OPTIONS["max_items"].default))
    tally = Tally()
    complete = True
    halted = False
    # The files of this run count too: two items of one name in a run get two files.
    owners = dict(owners or {})

    def keep(placed: Placed) -> None:
        if placed.file_id is not None:
            owners[placed.file_id] = placed.key
        record(placed)

    source.report = report
    source.remember = mark_seen
    report("info", "Looking for new files")
    try:
        for item in source.items(seen):
            if stopped():
                report("info", "Stopped: the rest comes next run")
                halted = True
                complete = False
                break
            # -1: no limit of files (the time limit still holds).
            if 0 <= limit <= tally.downloaded:
                report("info", f"{limit} files this run (Files per run): the rest comes next run")
                complete = False
                break
            if until is not None and time.monotonic() > (until() if callable(until) else until):
                report("info", "The run's time is up: the rest comes next run")
                complete = False
                break
            # A pause between two downloads, not before the first.
            pause = (lambda: sleep(source.pause)) if tally.downloaded else (lambda: None)
            try:
                if place(job, source, library, item, pause, report, keep, owners):
                    tally.downloaded += 1
                else:
                    tally.adopted += 1
            except (AuthError, ChallengeError):
                raise  # the whole site refuses: no point in the next files
            except Refused as error:
                # Not of the job's types or too large: the same next run, so it is not tried again
                # (until the job's types or sizes change: REFUSED marks it for that).
                tally.skipped += 1
                report("info", f"Skipped {item.path or item.url}: {error}")
                mark_seen(item.key)
                mark_seen(REFUSED + item.key)
                continue
            except SourceError as error:
                # One file the site will not give (an error page, a network failure, a name that
                # cannot be made): the others go on, and it is tried again next run, not marked
                # as seen.
                tally.failures.append(str(error))
                tally.denied += isinstance(error, Denied)
                report("error", f"{item.path or item.url}: {error}")
                complete = False
                if len(tally.failures) >= MAX_FAILURES and not tally.downloaded:
                    break
                continue
            mark_seen(item.key)
    except Stopped:
        # Stopped during a wait (a listing, a retry, the site asking to wait): the file it was at
        # is not marked and comes next run.
        report("info", "Stopped: the rest comes next run")
        halted = True
        complete = False
    if tally.failures and not (tally.downloaded or tally.adopted or tally.skipped):
        more = f" (and {len(tally.failures) - 1} more)" if len(tally.failures) > 1 else ""
        # Every file refused (two at least: one alone may be a file taken away): the account's
        # sign-in, not the files.
        if tally.denied == len(tally.failures) >= 2:
            raise AuthError(tally.failures[0] + more)
        raise SourceError(tally.failures[0] + more)
    if complete:
        # The whole site is known now: later runs may stop at known files (stop_at_seen).
        mark_seen(FULL_PASS)
    return f"{tally.sentence()}; stopped, the rest comes next run" if halted else tally.sentence()
