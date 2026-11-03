# SPDX-License-Identifier: AGPL-3.0-or-later
"""One run of a job: the source's new items, downloaded into the user's folder and tagged."""

import abc
import dataclasses
import posixpath
import tempfile
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, BinaryIO

from . import unpack, versions
from .l10n import Said, listed, said, said_n
from .patterns import render_tags, stable_url
from .sources import download
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
    WebPage,
)
from .store import Job

if TYPE_CHECKING:
    from .deleted import Deleted

# Files that failed on their own before a run without any success gives up (the site, not the
# files, is the trouble then).
MAX_FAILURES = 5
# Files that failed one after another, after successes, before the run gives up for this time (a
# site that broke down, a session that ended): the rest comes next run, not one failure each
# until the time limit.
MAX_IN_A_ROW = 10
# Files of other items at the path an item's name gives: it takes the name with " (2)", " (3)", ...
MAX_TWINS = 100

# The option to download a file again that its person deleted in Nextcloud (deleted.py).
AGAIN = "again_deleted"

# Options every job has, whatever its source.
COMMON_OPTIONS = {
    "unpack": Option(
        "Unpack ZIP files",
        0,
        "A downloaded .zip file's files go into a folder of its name beside it (not an archive in it again). "
        "Unpacked only when nothing in it is suspect (a path out of the folder, a link, a zip bomb, more than 1000 "
        "files, more than the largest file in all): else it is saved as it is.",
        kind="flag",
        group="files",
    ),
    "unpack_keep": Option(
        "Keep the ZIP files",
        1,
        "With Unpack ZIP files: the archive is kept beside its folder. Off: only its files are saved, the folder "
        "standing for it.",
        kind="flag",
        group="files",
    ),
    # On for every job, also for those saved before it existed (before): a file deleted in the
    # folder comes back with the next run whose listing finds it.
    AGAIN: Option(
        "Download deleted files again",
        1,
        "Before a run skips a file it saved already, it asks Nextcloud whether the file is still there: one "
        "deleted (or in the trash) is downloaded again. A file moved or renamed counts as there, and is not "
        "downloaded again. Off: a file once saved is never downloaded again.",
        kind="flag",
        before=1,
        group="runs",
    ),
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
        group="browser",
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

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        """The files of the IDs that are among the user's files (not in the trash), with their
        paths now; NotChecked when that cannot be found out (by default: a library that cannot
        tell)."""
        raise NotChecked(f"{type(self).__name__} cannot look for {len(file_ids)} files by their IDs")


class FileGone(Exception):
    """The file a job saved is not there any more (deleted or moved away by its user)."""


class NotChecked(Exception):
    """Nextcloud could not tell which files are there (Library.existing)."""


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
# When the files a run left are tried again (Tally.later): a scheduled job's next run.
NEXT_RUN = said("next run")
# A fetch's (an address sent to an on-demand job): when the address comes again.
SENT_AGAIN = said("when the address is sent again")


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
    # A key recorded before keys were made stable (with a session in its address, say) is the
    # same item's.
    while file_id is not None and stable_url(owners.get(file_id, item.key)) != stable_url(item.key):
        number += 1
        if number > MAX_TWINS:
            raise SourceError(f"{path} and {MAX_TWINS - 1} numbered names are taken by other files of the job")
        path = numbered(posixpath.join(job.target, item.path), number)
        file_id = library.find(path)
    return path, file_id


# What place did with an item (the run's Tally counts each by its name): downloaded, found in the
# folder already (adopted), a duplicate of another of the job's files (not saved), and for a known
# file asked again (recheck): saved over its own as a new version (updated), or unchanged.
DOWNLOADED, ADOPTED, DUPLICATE, UPDATED, UNCHANGED = "downloaded", "adopted", "duplicates", "updated", "unchanged"


def _tagged(job: Job, library: Library, item, file_id: int, record: Callable[[Placed], None]) -> None:
    """Gives the item's file the job's tags (templates of the link's values: {year} is the
    link's year) and records it."""
    tags = render_tags(job.tags, item.values)
    if tags:
        library.tag(file_id, tags)
    record(Placed(item.key, file_id, dict(item.values), tags))


def _put(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    job: Job, source: Source, library: Library, item, path: str, content: BinaryIO, report: Report, fresh: bool
) -> tuple[int, str, bool]:
    """The downloaded file put at path, or a ZIP the job unpacks (unpack.py) into a folder of its
    name; the ID the job records of it, its path (a new archive's numbered where its folder's name
    is taken; fresh: it is new, not a known one's new version), and whether the file itself was
    saved (not an archive unpacked without it)."""
    if not unpack.wanted(job.options, path, content):
        return library.put(path, content), path, True
    if fresh:
        try:
            path = unpack.free(path, lambda taken: library.find(taken) is not None, numbered)
        except unpack.NotUnpacked as error:
            report("info", said("{path} is not unpacked: {error}", path=path, error=str(error)))
            return library.put(path, content), path, True
    file_id, kept = unpack.saved(library, job, item.values, path, content, download.largest(source.settings), report)
    return file_id, path, kept


def place(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    job: Job,
    source: Source,
    library: Library,
    item,
    pause: Callable[[], None],
    report: Report,
    record: Callable[[Placed], None] = lambda placed: None,
    owners: dict[int, str] | None = None,
    recheck: bool = False,
    fetched: Callable[[], None] = lambda: None,
) -> str:
    """Puts the item's file into the job's folder (after the pause) and tags it; returns what it
    did (DOWNLOADED, ADOPTED: it was there already, downloaded by hand or by an earlier job, kept,
    tagged and not downloaded again; DUPLICATE: its content is another of the job's files', with
    the job's record of contents, source.contents). record keeps which file it is and its tags,
    for a later change of the job's tags; owners are the files the job saved, by ID, with their
    items' keys (free_path). An item whose name comes with its file (pending) is downloaded
    first, and then put where that name says. recheck: the item's file is known, its address is
    asked again (_recheck). fetched hears when the site's answer has come (or failed): the next
    pause counts from there."""
    if item.problem is not None:
        raise item.problem
    if not item.path.strip("/"):
        raise SourceError(f"the file name for {item.url} came out empty (check the job's File name)")
    owners = owners or {}
    if recheck and source.contents is not None:
        return _recheck(job, source, library, item, pause, report, record, owners, fetched)
    path, file_id = (
        (posixpath.join(job.target, item.path), None) if item.pending else free_path(job, library, item, owners)
    )
    if file_id is not None:
        report("info", said("Already in the folder, tagged: {path}", path=path))
        _tagged(job, library, item, file_id, record)
        return ADOPTED
    pause()
    report("info", said("Downloading {file}", file=item.url if item.pending else path))
    with tempfile.TemporaryFile() as content:
        try:
            with download.checking(download.Check(item.url)) as check:
                named = source.fetch(item, content)
        finally:
            fetched()
        if item.pending:
            item = named or item
            path, file_id = free_path(job, library, item, owners)
        if file_id is not None:
            report("info", said("Already in the folder, tagged: {path}", path=path))
            _tagged(job, library, item, file_id, record)
            return ADOPTED
        size = content.tell()
        value = versions.digest(content)
        twin = source.contents.same(value, item.key) if source.contents is not None else None
        if twin is not None:
            report("info", said("A duplicate of {path}, not saved again: {file}", path=twin["path"], file=item.url))
            source.contents.keep(item.key, value, twin["path"], check)
            return DUPLICATE
        file_id, path, kept = _put(job, source, library, item, path, content, report, fresh=True)
        if kept:
            report("info", said("Saved {path} ({size})", path=path, size=size_text(size)))
        if source.contents is not None:
            source.contents.keep(item.key, value, path, check)
    _tagged(job, library, item, file_id, record)
    return DOWNLOADED


def _recheck(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    job: Job,
    source: Source,
    library: Library,
    item,
    pause: Callable[[], None],
    report: Report,
    record: Callable[[Placed], None],
    owners: dict[int, str],
    fetched: Callable[[], None],
) -> str:
    """A known file's address asked again: only if it changed since (its ETag, Last-Modified),
    else downloaded and compared by its hash. A file that changed is saved over its own (UPDATED:
    Nextcloud keeps the one before as a version); one without a record (saved before the job
    kept them) is the measure from now on (UNCHANGED); one not at its path stays away (one
    deleted in Nextcloud comes here only where the run does not download those again:
    deleted.py; one moved is no new version's place)."""
    contents = source.contents
    known = contents.known(item.key)
    pause()
    with tempfile.TemporaryFile() as content:
        check = download.Check(item.url, known["etag"], known["modified"]) if known else download.Check(item.url)
        try:
            with download.checking(check):
                named = source.fetch(item, content)
        except download.Unchanged:
            contents.checked(item.key, known)
            return UNCHANGED
        finally:
            fetched()
        item = (named or item) if item.pending else item
        path = known["path"] if known else free_path(job, library, item, owners)[0]
        size = content.tell()
        value = versions.digest(content)
        file_id = library.find(path)
        if file_id is None and unpack.unpacks(job.options, path):
            file_id = library.find(unpack.folder_of(path))  # unpacked without the archive
        if file_id is None or known is None or known["hash"] == value:
            # Not at its path (moved by its person, or deleted where the job does not download those
            # again): not brought back; asked again all the same.
            contents.keep(item.key, value if file_id is not None or known is None else known["hash"], path, check)
            return UNCHANGED
        file_id, path, _ = _put(job, source, library, item, path, content, report, fresh=False)
        report("info", said("Changed: {path} saved over as its new version ({size})", path=path, size=size_text(size)))
        contents.keep(item.key, value, path, check)
    _tagged(job, library, item, file_id, record)
    return UPDATED


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
            report("info", said("Tagged: {path}", path=posixpath.join(job.target, item.path)))
            tagged += 1
    except Stopped:
        return tagged, False
    return tagged, True


def earlier_tags(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    job: Job,
    source: Source,
    library: Library,
    earlier: set[str],
    previous: list[str],
    changed: int,
    report: Report,
    record: Callable[[Placed], None],
    stopped: Callable[[], bool],
) -> tuple[Said, bool]:
    """A pass for the tags after their change: those of the files saved before the job
    recorded them (earlier, their keys), found by a listing without downloads; changed
    recorded files were brought up to date already. Returns what it did, and whether it went
    through."""
    tagged, whole = 0, True
    if earlier:
        report(
            "info",
            said_n(
                "Looking for %n file saved before its tags were kept (nothing is downloaded)",
                "Looking for %n files saved before their tags were kept (nothing is downloaded)",
                len(earlier),
            ),
        )
        tagged, whole = retag_earlier(
            job, source, library, earlier.__contains__, record, previous=previous, report=report, stopped=stopped
        )
    count = changed + tagged
    if whole:
        return said_n("Tags brought up to date on %n file", "Tags brought up to date on %n files", count), True
    stopped_at = said_n(
        "Tags brought up to date on %n file; stopped, the others get them at the job's next turn",
        "Tags brought up to date on %n files; stopped, the others get them at the job's next turn",
        count,
    )
    return stopped_at, False


def signed_in_again(source: Source, error: WebPage, report: Report) -> bool:
    """Signs the source in anew after a web page came instead of a file (the session ended);
    False when it has no sign-in to repeat, or that failed (said in the log: the file's own
    failure follows)."""
    try:
        if not source.sign_in_again():
            return False
    except (AuthError, ChallengeError):
        raise
    except SourceError as failed:
        report("error", said("Signing in anew failed: {error}", error=str(failed)))
        return False
    report("info", said("A web page came instead of a file ({error}): signed in anew", error=str(error)))
    return True


@dataclasses.dataclass
class Tally:
    """What a run did: files downloaded, found already there, skipped (refused by type or size),
    duplicates of the job's other files, known files saved over as a new version (updated) or
    asked again and found unchanged, and the errors of those that failed (denied: how many of them
    the site refused, 401 or 403)."""

    downloaded: int = 0
    adopted: int = 0
    skipped: int = 0
    duplicates: int = 0
    updated: int = 0
    unchanged: int = 0
    failures: list[str] = dataclasses.field(default_factory=list)
    denied: int = 0
    # When what is left is tried again: the next run, or (a fetch's) the address sent again.
    later: str = NEXT_RUN

    def sentence(self) -> Said:
        """The run's outcome, as the jobs' list shows it: its figures, each a whole phrase."""
        figures = [said_n("%n downloaded", "%n downloaded", self.downloaded)]
        if self.updated:
            figures.append(said_n("%n changed (a new version)", "%n changed (new versions)", self.updated))
        if self.adopted:
            figures.append(said_n("%n already there", "%n already there", self.adopted))
        if self.duplicates:
            figures.append(said_n("%n duplicate (not saved again)", "%n duplicates (not saved again)", self.duplicates))
        if self.unchanged:
            figures.append(said_n("%n known file unchanged", "%n known files unchanged", self.unchanged))
        if self.skipped:
            figures.append(
                said_n(
                    "%n skipped (not of the job's file types, or too large)",
                    "%n skipped (not of the job's file types, or too large)",
                    self.skipped,
                )
            )
        if self.failures:
            figures.append(
                said_n(
                    "%n failed (tried again {later}): {error}",
                    "%n failed (tried again {later}): {error}",
                    len(self.failures),
                    later=self.later,
                    error=self.failures[0],
                )
            )
        return listed(figures, "unit")


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
    later: str = NEXT_RUN,
    deleted: "Deleted | None" = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Downloads up to max_items new items (and stops at until, a time.monotonic(), or a
    function giving it) and returns what was done, in a sentence; report hears of each step
    (the page's progress log). stopped is asked before each file: it may wait while the run is
    paused, and True ends the run there (the rest comes next run, as at the limits). owners are
    the files the job saved before, by ID, with their items' keys; later says when the rest
    comes (a fetch's: when its address is sent again). deleted (deleted.py) tells which known
    files are gone from Nextcloud: seen() skips them no more, and a known file due to be asked
    for changes that is gone is downloaded as a new one. clock (time.monotonic) times the pauses
    between downloads."""
    limit = int(job.options.get("max_items", COMMON_OPTIONS["max_items"].default))
    tally = Tally(later=later)
    complete = True
    halted = False
    # The files of this run count too: two items of one name in a run get two files.
    owners = dict(owners or {})

    def keep(placed: Placed) -> None:
        if placed.file_id is not None:
            owners[placed.file_id] = placed.key
        record(placed)

    # The pause between two downloads counts from the end of the site's last answer (a file, an
    # ask whether it changed, a sign-in anew: last), not from the end of saving and tagging the
    # file in Nextcloud: the site sees at least source.pause between them, and no more than that.
    last: list[float] = []

    def answered() -> None:
        last[:] = [clock()]

    def wait() -> None:
        left = source.pause - (clock() - last[0]) if last else source.pause
        if left > 0:
            sleep(left)

    # The sign-in is done anew once a run (resigned); failures one after another end it.
    resigned = False
    in_a_row = 0
    source.report = report
    source.remember = mark_seen
    if deleted is not None:
        source.ahead = deleted.ahead
    # The files the job saved whose addresses are asked again (Check known files for changes):
    # the listing gives them as if new, place asks whether they changed.
    again = source.contents.due(set(owners.values())) if source.contents is not None and source.rechecks() else set()
    listed_seen = (lambda key: key not in again and seen(key)) if again else seen
    report("info", said("Looking for new files"))
    try:
        for item in source.items(listed_seen):
            if stopped():
                report("info", said("Stopped: the rest comes {later}", later=later))
                halted = True
                complete = False
                break
            # -1: no limit of files (the time limit still holds).
            if 0 <= limit <= tally.downloaded + tally.updated:
                report(
                    "info",
                    said_n(
                        "%n file this run (Files per run): the rest comes {later}",
                        "%n files this run (Files per run): the rest comes {later}",
                        limit,
                        later=later,
                    ),
                )
                complete = False
                break
            if until is not None and time.monotonic() > (until() if callable(until) else until):
                report("info", said("The run's time is up: the rest comes {later}", later=later))
                complete = False
                break
            # A pause between two downloads (or asks of a known file), not before the first.
            asked = tally.downloaded + tally.updated + tally.unchanged + tally.duplicates
            pause = wait if asked else (lambda: None)
            rechecked = item.key in again and not (deleted is not None and deleted.gone(item.key))
            try:
                try:
                    done = place(job, source, library, item, pause, report, keep, owners, rechecked, answered)
                except WebPage as error:
                    # The session ended in the middle of the run (files came before): signed in
                    # anew once, and the file asked for again.
                    if resigned or not asked or not signed_in_again(source, error, report):
                        raise
                    resigned = True
                    answered()
                    done = place(job, source, library, item, pause, report, keep, owners, rechecked, answered)
                setattr(tally, done, getattr(tally, done) + 1)
                if deleted is not None:
                    deleted.placed(item.key, done, posixpath.join(job.target, item.path))
                in_a_row = 0
            except (AuthError, ChallengeError):
                raise  # the whole site refuses: no point in the next files
            except Refused as error:
                # Not of the job's types or too large: the same next run, so it is not tried again
                # (until the job's types or sizes change: REFUSED marks it for that).
                tally.skipped += 1
                report("info", said("Skipped {file}: {error}", file=item.path or item.url, error=str(error)))
                mark_seen(item.key)
                mark_seen(REFUSED + item.key)
                continue
            except SourceError as error:
                if rechecked:
                    # A known file that could not be asked again (gone from the site, a failure):
                    # it stays as it is, asked again next time; no failure of the run.
                    report(
                        "info",
                        said("{file} could not be checked for changes: {error}", file=item.path, error=str(error)),
                    )
                    continue
                # One file the site will not give (an error page, a network failure, a name that
                # cannot be made): the others go on, and it is tried again next run, not marked
                # as seen.
                tally.failures.append(str(error))
                tally.denied += isinstance(error, Denied)
                report("error", f"{item.path or item.url}: {error}")
                complete = False
                in_a_row += 1
                if len(tally.failures) >= MAX_FAILURES and not tally.downloaded:
                    break
                if in_a_row >= MAX_IN_A_ROW:
                    report(
                        "info",
                        said_n(
                            "%n file in a row failed: the rest comes {later}",
                            "%n files in a row failed: the rest comes {later}",
                            in_a_row,
                            later=later,
                        ),
                    )
                    break
                continue
            mark_seen(item.key)
    except Stopped:
        # Stopped during a wait (a listing, a retry, the site asking to wait): the file it was at
        # is not marked and comes next run.
        report("info", said("Stopped: the rest comes {later}", later=later))
        halted = True
        complete = False
    if tally.failures and not (tally.downloaded or tally.adopted or tally.skipped or tally.duplicates or tally.updated):
        more = f" (and {len(tally.failures) - 1} more)" if len(tally.failures) > 1 else ""
        # Every file refused (two at least: one alone may be a file taken away): the account's
        # sign-in, not the files.
        if tally.denied == len(tally.failures) >= 2:
            raise AuthError(tally.failures[0] + more)
        raise SourceError(tally.failures[0] + more)
    if complete and source.gaps:
        # Pages that should have listed files gave none (signed out, an error): those files may
        # be missing, and known files must not end the next runs before they are found.
        report(
            "info",
            said_n(
                "%n listing page gave nothing: the run does not count as a complete pass",
                "%n listing pages gave nothing: the run does not count as a complete pass",
                source.gaps,
            ),
        )
    elif complete:
        # The whole site is known now: later runs may stop at known files (stop_at_seen).
        mark_seen(FULL_PASS)
    if halted:
        return said("{outcome}; stopped, the rest comes {later}", outcome=tally.sentence(), later=later)
    # A watched page's run saves no file: its outcome is what it found.
    return source.note if source.note is not None else tally.sentence()
