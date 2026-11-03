# SPDX-License-Identifier: AGPL-3.0-or-later
"""The user's files and tags in Nextcloud, through the app's access as that user.

nc_py_api's client is asynchronous; the runner and the sources are not (Playwright's sync API,
httpx). The executor passes call, which runs each Nextcloud request to its end on an event loop
in a thread of its own (Executor.call), between the sources' work.
"""

import os
import posixpath
import re
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from nc_py_api import AsyncNextcloudApp, NextcloudException

from .runner import FileGone, Library, NotChecked

Call = Callable[[Awaitable[Any]], Any]

# Below this size a file is uploaded in one PUT (a request instead of upload_stream's MKCOL, PUT,
# MOVE and DELETE); from it on in chunks of this size, nc_py_api's (connect_nextcloud).
SINGLE_PUT = 5 * 1024 * 1024


class NextcloudLibrary(Library):
    """The files of the user nc acts for."""

    def __init__(self, nc: AsyncNextcloudApp, call: Call):
        self.nc = nc
        self.call = call
        self._tags: dict[str, int] = {}
        # The folders made (or found there) this run: no MKCOLs for them again.
        self._folders: set[str] = set()

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
        # The file's folder is made once a run (this library is the run's): each makedirs asks a
        # MKCOL of every part of the path, answered 405 where it exists. A folder deleted since
        # answers the upload 409, and is made again. A file under SINGLE_PUT goes in one PUT; a
        # larger one in chunks (upload_stream, connect_nextcloud's first kind), so that no file is
        # held in memory whole.
        folder = posixpath.dirname(path)
        if folder not in self._folders:
            self._make(folder)
        try:
            node = self._upload(path, content)
        except NextcloudException as error:
            if error.status_code != 409:
                raise
            self._folders.clear()
            self._make(folder)
            node = self._upload(path, content)
        # The upload's node has its id only as the OC-FileId header's text (nc_py_api 0.30.3: its
        # file_id, "00000123oc…": the id padded to 8 digits, then the instance's id), its
        # info.fileid stays 0; without it, the file is looked up (tagging id 0 answers 404).
        digits = re.match(r"\d+", str(getattr(node, "file_id", "") or ""))
        file_id = int(digits.group()) if digits else self.find(path)
        if not file_id:
            raise NextcloudException(404, "Not Found", info=f"{path} after its upload")
        return file_id

    def _upload(self, path: str, content: BinaryIO) -> Any:
        """The file's content (from its start) stored at path, in one PUT or in chunks."""
        content.seek(0, os.SEEK_END)
        size = content.tell()
        content.seek(0)
        if size < SINGLE_PUT:
            return self.call(self.nc.files.upload(path, content.read()))
        return self.call(self.nc.files.upload_stream(path, content))

    def _make(self, folder: str) -> None:
        """Makes the folder and those above it (those there already are no matter), and
        remembers them as there."""
        self.call(self.nc.files.makedirs(folder, exist_ok=True))
        while folder not in self._folders:
            self._folders.add(folder)
            folder = posixpath.dirname(folder)

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        # One SEARCH for them all (nc_py_api's find, an "eq" of each ID joined by "or"s, as its
        # by_id asks for one) in the user's files, of every storage they see: a file in the trash
        # is not among them, one moved or renamed is, at its path now. Each ID is looked for once.
        if not file_ids:
            return {}
        conditions: list = []
        for file_id in file_ids[:-1]:
            conditions += ["or", "eq", "fileid", file_id]
        conditions += ["eq", "fileid", file_ids[-1]]
        try:
            nodes = self.call(self.nc.files.find(conditions))
        except (NextcloudException, OSError) as error:
            raise NotChecked(str(error) or type(error).__name__) from error
        return {int(node.info.fileid): posixpath.join("/", node.user_path.rstrip("/")) for node in nodes}

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
                if error.status_code == 404:
                    raise FileGone(file_id) from error
                if error.status_code != 409:
                    raise

    def untag(self, file_id: int, tags: list[str]) -> None:
        # A tag that is not on the file (or not one the user sees) is no matter: Nextcloud answers
        # 404 for a file that is gone, and for a relation that is not there; the file decides.
        if any(name not in self._tags for name in tags):
            self._tags = self._tag_ids()
        for name in tags:
            tag_id = self._tags.get(name)
            if tag_id is None:
                continue
            try:
                self.call(self.nc.files.unassign_tag(file_id, tag_id))
            except NextcloudException as error:
                if error.status_code != 404:
                    raise
                if not self._exists(file_id):
                    raise FileGone(file_id) from error

    def _exists(self, file_id: int) -> bool:
        try:
            return bool(self.call(self.nc.files.by_id(file_id)))
        except NextcloudException as error:
            if error.status_code != 404:
                raise
            return False
