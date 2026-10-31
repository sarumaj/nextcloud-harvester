"""The user's files and tags in Nextcloud, through the app's access as that user."""

import posixpath
from typing import BinaryIO

from nc_py_api import NextcloudApp, NextcloudExceptionNotFound

from .runner import Library


class NextcloudLibrary(Library):
    """The files of the user nc acts for."""

    def __init__(self, nc: NextcloudApp):
        self.nc = nc
        self._tags: dict[str, int] = {}

    def find(self, path: str) -> int | None:
        # by_path raises for a path that does not exist (WebDAV's 404), rather than returning None.
        try:
            node = self.nc.files.by_path(path)
        except NextcloudExceptionNotFound:
            return None
        return node.info.fileid if node else None

    def put(self, path: str, content: BinaryIO) -> int:
        self.nc.files.makedirs(posixpath.dirname(path), exist_ok=True)
        return self.nc.files.upload_stream(path, content).info.fileid

    def _tag_ids(self) -> dict[str, int]:
        return {tag.display_name: tag.tag_id for tag in self.nc.files.list_tags()}

    def tag(self, file_id: int, tags: list[str]) -> None:
        if any(name not in self._tags for name in tags):
            self._tags = self._tag_ids()
            missing = [name for name in tags if name not in self._tags]
            for name in missing:
                self.nc.files.create_tag(name)
            if missing:
                self._tags = self._tag_ids()
        for name in tags:
            self.nc.files.assign_tag(file_id, self._tags[name])
