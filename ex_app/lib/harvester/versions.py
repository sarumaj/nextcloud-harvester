# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the files a job saved hold, kept per job in the store (Contents): the hash of each file's
content, and the ETag and Last-Modified its address answered with.

A file whose hash one the job saved from another address has is a duplicate: it is not saved
again (runner.place). With the option "Check known files for changes" (sources.base.RECHECK), a
run asks the addresses of the files it saved again, at most once in RECHECK_EVERY each: over
HTTP only if they changed since (If-None-Match, If-Modified-Since: 304 is no download), else the
file is downloaded and its hash compared. A file that changed is saved over its own, the same
path: Nextcloud keeps the one before among the file's versions (its versions app), so a new
version needs no new name.
"""

import hashlib
import time
from collections.abc import Callable
from typing import Any, BinaryIO

# How often a known file's address is asked again at most (s): a job that runs every quarter
# of an hour does not download its files anew each time where the site gives no ETag.
RECHECK_EVERY = 20 * 3600


def digest(content: BinaryIO) -> str:
    """The SHA-256 of the file's content (read from its start; left at its start)."""
    content.seek(0)
    value = hashlib.file_digest(content, "sha256").hexdigest()
    content.seek(0)
    return value


class Contents:
    """A job's record of what its files hold (the store's contents table) for its run, and who
    is told when a page it watches changed (tell: its address and the change's excerpt)."""

    def __init__(self, store: Any, job_id: int, tell: Callable[[str, str], None] = lambda url, excerpt: None):
        self.store = store
        self.job_id = job_id
        self.tell = tell

    def known(self, key: str) -> dict | None:
        """What the item's file held when last asked for (hash, etag, modified, path), or None."""
        return self.store.content(self.job_id, key)

    def same(self, value: str, key: str) -> dict | None:
        """The file another item gave with that hash (its path), or None."""
        return self.store.same_content(self.job_id, value, key)

    def due(self, keys: set[str]) -> set[str]:
        """The items of keys whose address is due to be asked again (not in RECHECK_EVERY)."""
        return keys - self.store.checked_since(self.job_id, time.time() - RECHECK_EVERY)

    def checked(self, key: str, known: dict) -> None:
        """The item's address answered that its file did not change since known (304)."""
        self.store.keep_content(self.job_id, key, known["hash"], known["path"], known["etag"], known["modified"])

    def keep(self, key: str, value: str, path: str, check: Any = None) -> None:
        """Records the item's file (its hash, where it is) and the validators of its answer (a
        download.Check's), as asked for now."""
        etag, modified = (check.etag_now, check.modified_now) if check is not None else ("", "")
        self.store.keep_content(self.job_id, key, value, path, etag, modified)
