# SPDX-License-Identifier: AGPL-3.0-or-later
"""Files a job saved that their person deleted in Nextcloud since: with the option "Download
deleted files again" (runner.AGAIN, every job's), and for an address sent to an on-demand job whatever
the option, an item the listing finds again whose file is gone is not skipped but downloaded
again.

A file is looked for by its ID (the store's placed table, recorded as it was saved), in the
person's files: one moved or renamed is there still (at its path now) and counts as kept, one in
the trash or deleted for good is gone. A file the job has no ID of (one a pass for its tags did
not find) is looked for at the path its content was recorded at (versions.Contents), and its ID
recorded when it is there. An unpacked ZIP (unpack.py) counts as kept while its archive or its
folder is there. Only the items the listing gives are looked for: their keys as the source sees
them (seen), the IDs of up to BATCH at once in one request (the source tells the keys of a
listing's page ahead, Source.ahead), each once a run. A lookup that fails keeps the files as
kept (skipped, as without the option), with a line in the log: never downloaded blindly.

Files that are no item of their own are not looked for: a duplicate (its content is another
item's file), the files captured from a page or a mail's attachments once their page or message
is done (it is not opened again), the files of jobs saved before 0.8.1 the job has no record of.
"""

from collections.abc import Callable, Iterable
from typing import Any

from nc_py_api import NextcloudException

from . import unpack
from .l10n import said, said_n
from .runner import AGAIN, COMMON_OPTIONS, DOWNLOADED, Library, NotChecked
from .sources import MARK
from .store import Job

# The files looked for in one request at most.
BATCH = 100


class Deleted:  # pylint: disable=too-many-instance-attributes
    """A run's view of which of the job's files are still in Nextcloud (a cache: each file looked
    for once). files are the job's records of its files (store.placed: by key, file their ID);
    explicit: an address sent to the job, looked for whatever the option, and a file kept told
    where it is."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        store: Any,
        job: Job,
        files: dict[str, dict],
        library: Library,
        report: Callable[[str, str], None],
        *,
        explicit: bool = False,
    ):
        self.store = store
        self.job = job
        self.library = library
        self.report = report
        self.explicit = explicit
        self.active = explicit or bool(int(job.options.get(AGAIN, COMMON_OPTIONS[AGAIN].before) or 0))
        self.files = {key: entry for key, entry in files.items() if not key.startswith(MARK)}
        # By key: the file's path now (kept), "" (kept, where unknown: not found out), None (gone).
        self.found: dict[str, str | None] = {}
        # The keys told ahead, to look for with the next one asked (in their order).
        self.pending: dict[str, None] = {}
        self.told: set[str] = set()
        # The keys found gone and downloaded again (each said once in the log).
        self.again: set[str] = set()

    def ahead(self, keys: Iterable[str]) -> None:
        """Keys the listing asks about next (a page's items): looked for with the next one asked."""
        if self.active:
            self.pending.update((key, None) for key in keys if key in self.files and key not in self.found)

    def gone(self, key: str) -> bool:
        """Whether the item's file is gone from the person's files (deleted, or in the trash)."""
        if not self.active or key not in self.files:
            return False
        if key not in self.found:
            self._look([key] + [other for other in self.pending if other != key and other not in self.found])
        return self.found.get(key, "") is None

    def seen(self, key: str, known: bool) -> bool:
        """Whether the listing skips the item: known (the job fetched it) and its file not gone. An
        explicit request's file that is there is said where it is, once."""
        if not known or self.gone(key):
            return False
        if self.explicit and self.found.get(key) and key not in self.told:
            self.told.add(key)
            self.report("info", said("Fetched before, in your files at {path}", path=self.found[key]))
        return True

    def placed(self, key: str, done: str, path: str) -> None:
        """The item was placed (done: what place did): a file found gone and downloaded again is
        said in the log; it is there now."""
        if self.found.get(key, "") is None and done == DOWNLOADED and key not in self.again:
            self.again.add(key)
            self.report("info", said("Deleted in Nextcloud, downloaded again: {path}", path=path))
        if key in self.found:
            self.found[key] = path

    def _look(self, keys: list[str]) -> None:
        """Looks for the keys' files, BATCH at most (the first always): their IDs in one request,
        those without an ID at their recorded path."""
        batch = keys[:BATCH]
        for key in batch:
            self.pending.pop(key, None)
        by_id = {key: self.files[key]["file"] for key in batch if self.files[key]["file"] is not None}
        try:
            there = self.library.existing(list(dict.fromkeys(by_id.values()))) if by_id else {}
            for key in batch:
                self.found[key] = self._where(key, by_id.get(key), there)
        except (NotChecked, NextcloudException, OSError) as error:
            left = [key for key in batch if key not in self.found]
            self.found.update((key, "") for key in left)
            self.report(
                "info",
                said_n(
                    "Could not ask Nextcloud whether %n file is still there ({error}): not downloaded again",
                    "Could not ask Nextcloud whether %n files are still there ({error}): not downloaded again",
                    len(left),
                    error=str(error) or type(error).__name__,
                ),
            )

    def _where(self, key: str, file_id: int | None, there: dict[int, str]) -> str | None:
        """Where the key's file is now ("" where it cannot be found out), or None: gone. A file
        gone is forgotten in the job's record of contents (with the duplicates that name it), so
        that its download is no duplicate of itself."""
        if file_id is not None and file_id in there:
            return there[file_id]
        known = self.store.content(self.job.id or 0, key)
        path = known["path"] if known else ""
        if file_id is None:
            if not path:
                return ""  # neither an ID nor a path: nothing to look for
            found = self.library.find(path)
            if found is not None:
                self.store.set_placed_file(self.job.id or 0, key, found)
                return path
        if path and unpack.unpacks(self.job.options, path) and self.library.find(unpack.folder_of(path)) is not None:
            return unpack.folder_of(path)  # the archive's files, unpacked, are there
        self.store.forget_content(self.job.id or 0, key)
        return None
