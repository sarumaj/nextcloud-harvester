"""One run of a job: the source's new items, downloaded into the user's folder and tagged."""

import abc
import dataclasses
import posixpath
import tempfile
import time
from collections.abc import Callable
from typing import BinaryIO

from .patterns import render_tags
from .sources import FULL_PASS, AuthError, ChallengeError, Option, Source, SourceError
from .store import Job

# Files that failed on their own before a run without any success gives up (the site, not the
# files, is the trouble then).
MAX_FAILURES = 5

# Options every job has, whatever its source.
COMMON_OPTIONS = {
    "max_items": Option(
        "Files per run",
        10,
        "The rest comes in the next runs (a first run may find hundreds); -1: no limit (a run still ends "
        "after two hours, and goes on in the next).",
        minimum=-1,
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
        """Gives the file the collaborative tags, creating those that do not exist yet."""


def place(job: Job, source: Source, library: Library, item, pause: Callable[[], None]) -> bool:
    """Puts the item's file into the job's folder (after the pause) and tags it; False when it
    was there already (downloaded by hand or by an earlier job: kept, tagged and not downloaded
    again)."""
    if not item.path.strip("/"):
        raise SourceError(f"the file name for {item.url} came out empty (check the job's File name)")
    path = posixpath.join(job.target, item.path)
    file_id = library.find(path)
    fetched = file_id is None
    if file_id is None:
        pause()
        with tempfile.TemporaryFile() as content:
            source.fetch(item, content)
            content.seek(0)
            file_id = library.put(path, content)
    # The job's tags are templates of the link's values: {year} is the link's year.
    tags = render_tags(job.tags, item.values)
    if tags:
        library.tag(file_id, tags)
    return fetched


@dataclasses.dataclass
class Tally:
    """What a run did: files downloaded, found already there, and the errors of those that failed."""

    downloaded: int = 0
    adopted: int = 0
    failures: list[str] = dataclasses.field(default_factory=list)

    def sentence(self) -> str:
        """The run's outcome, as the jobs' list shows it."""
        message = f"{self.downloaded} downloaded"
        if self.adopted:
            message += f", {self.adopted} already there"
        if self.failures:
            message += f", {len(self.failures)} failed (tried again next run): {self.failures[0]}"
        return message


def run(
    job: Job,
    source: Source,
    library: Library,
    seen: Callable[[str], bool],
    mark_seen: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    until: float | None = None,
) -> str:
    """Downloads up to max_items new items (and stops at until, a time.monotonic()) and returns
    what was done, in a sentence."""
    limit = int(job.options.get("max_items", COMMON_OPTIONS["max_items"].default))
    tally = Tally()
    complete = True
    for item in source.items(seen):
        # -1: no limit of files (the time limit still holds).
        if (0 <= limit <= tally.downloaded) or (until is not None and time.monotonic() > until):
            complete = False
            break
        # A pause between two downloads, not before the first.
        pause = (lambda: sleep(source.pause)) if tally.downloaded else (lambda: None)
        try:
            if place(job, source, library, item, pause):
                tally.downloaded += 1
            else:
                tally.adopted += 1
        except (AuthError, ChallengeError):
            raise  # the whole site refuses: no point in the next files
        except SourceError as error:
            # One file the site will not give (an error page, the wrong type): the others go on,
            # and it is tried again next run, not marked as seen.
            tally.failures.append(str(error))
            complete = False
            if len(tally.failures) >= MAX_FAILURES and not tally.downloaded:
                break
            continue
        mark_seen(item.key)
    if tally.failures and not (tally.downloaded or tally.adopted):
        more = f" (and {len(tally.failures) - 1} more)" if len(tally.failures) > 1 else ""
        raise SourceError(tally.failures[0] + more)
    if complete:
        # The whole site is known now: later runs may stop at known files (stop_at_seen).
        mark_seen(FULL_PASS)
    return tally.sentence()
