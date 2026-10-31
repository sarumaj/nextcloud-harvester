"""One run of a job: the source's new items, downloaded into the user's folder and tagged."""

import abc
import posixpath
import tempfile
import time
from collections.abc import Callable
from typing import BinaryIO

from .patterns import render_tags
from .sources import FULL_PASS, Option, Source
from .store import Job

# Options every job has, whatever its source.
COMMON_OPTIONS = {
    "max_items": Option("Files per run", 10, "The rest comes in the next runs (a first run may find hundreds)."),
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


def run(
    job: Job,
    source: Source,
    library: Library,
    seen: Callable[[str], bool],
    mark_seen: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Downloads up to max_items new items and returns what was done, in a sentence."""
    limit = int(job.options.get("max_items", COMMON_OPTIONS["max_items"].default))
    downloaded = adopted = 0
    complete = True
    for item in source.items(seen):
        if downloaded >= limit:
            complete = False
            break
        # A pause between two downloads, not before the first.
        pause = (lambda: sleep(source.pause)) if downloaded else (lambda: None)
        if place(job, source, library, item, pause):
            downloaded += 1
        else:
            adopted += 1
        mark_seen(item.key)
    if complete:
        # The whole site is known now: later runs may stop at known files (stop_at_seen).
        mark_seen(FULL_PASS)
    message = f"{downloaded} downloaded"
    if adopted:
        message += f", {adopted} already there"
    return message
