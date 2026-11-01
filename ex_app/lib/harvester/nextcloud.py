"""The user's files and tags in Nextcloud, through the app's access as that user.

nc_py_api's client is asynchronous; the runner and the sources are not (Playwright's sync API,
httpx). The executor passes call, which runs each Nextcloud request to its end on an event loop
in a thread of its own (Executor.call), between the sources' work.
"""

import posixpath
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from nc_py_api import AsyncNextcloudApp, NextcloudException

from .runner import Library

Call = Callable[[Awaitable[Any]], Any]


class NextcloudLibrary(Library):
    """The files of the user nc acts for."""

    def __init__(self, nc: AsyncNextcloudApp, call: Call):
        self.nc = nc
        self.call = call
        self._tags: dict[str, int] = {}

    def find(self, path: str) -> int | None:
        # by_path raises for a path that does not exist (WebDAV's 404), rather than returning None:
        # nc_py_api 0.30.3 raises a plain NextcloudException with status 404 there (its
        # check_error), not NextcloudExceptionNotFound, so the status decides; any other error
        # stops the run.
        try:
            node = self.call(self.nc.files.by_path(path))
        except NextcloudException as error:
            if error.status_code != 404:
                raise
            return None
        return node.info.fileid if node else None

    def put(self, path: str, content: BinaryIO) -> int:
        self.call(self.nc.files.makedirs(posixpath.dirname(path), exist_ok=True))
        return self.call(self.nc.files.upload_stream(path, content)).info.fileid

    def _tag_ids(self) -> dict[str, int]:
        return {tag.display_name: tag.tag_id for tag in self.call(self.nc.files.list_tags())}

    def tag(self, file_id: int, tags: list[str]) -> None:
        # Nextcloud answers 409 Conflict to a tag already on the file (a file tagged before a run
        # broke off, then found again) and to a new tag whose name a tag the user cannot see has:
        # neither stops the run. A tag the user cannot see or assign is left out.
        if any(name not in self._tags for name in tags):
            self._tags = self._tag_ids()
            missing = [name for name in tags if name not in self._tags]
            for name in missing:
                try:
                    self.call(self.nc.files.create_tag(name))
                except NextcloudException as error:
                    if error.status_code != 409:
                        raise
            if missing:
                self._tags = self._tag_ids()
        for name in tags:
            tag_id = self._tags.get(name)
            if tag_id is None:
                continue
            try:
                self.call(self.nc.files.assign_tag(file_id, tag_id))
            except NextcloudException as error:
                if error.status_code != 409:
                    raise
