# SPDX-License-Identifier: AGPL-3.0-or-later
"""The library a job's run saves into (runner.Library), by its Save to and Tag as
(destinations.py): the person's files in Nextcloud, or Harvester's private library; with
Harvester's labels in place of Nextcloud's tags (Labelled), where the files are not in the
library (which keeps the labels itself)."""

from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from nc_py_api import AsyncNextcloudApp

from . import destinations
from .library import PrivateLibrary, Shelf
from .nextcloud import NextcloudLibrary
from .runner import Library
from .sources import SourceError
from .store import Job

Call = Callable[[Awaitable[Any]], Any]


class Labelled(Library):
    """A library whose files get no Nextcloud tags: the tags a run gives are Harvester's labels,
    which the job's records keep (store.placed), nothing more."""

    def __init__(self, inner: Library):
        self.inner = inner

    def find(self, path: str) -> int | None:
        return self.inner.find(path)

    def put(self, path: str, content: BinaryIO) -> int:
        return self.inner.put(path, content)

    def tag(self, file_id: int, tags: list[str]) -> None:
        return None

    def untag(self, file_id: int, tags: list[str]) -> None:
        return None

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        return self.inner.existing(file_ids)


# Harvester's private library on the app's storage, set at start (main.lifespan).
SHELF: list[Shelf] = []


def library_for(job: Job, nc: AsyncNextcloudApp, call: Call) -> Library:
    """The library the job's run saves into."""
    where = destinations.save_to(job.options)
    if where == destinations.LIBRARY:
        if not SHELF:
            raise SourceError("Harvester's private library is not set up on this server")
        return PrivateLibrary(SHELF[0], job.user, job.id)
    library: Library = NextcloudLibrary(nc, call)
    return Labelled(library) if destinations.tag_as(job.options) == destinations.LABELS else library
