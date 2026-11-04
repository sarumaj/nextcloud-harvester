# SPDX-License-Identifier: AGPL-3.0-or-later
"""A vault's folder in the person's files, over Nextcloud's WebDAV (nc_py_api), as vault.py's
Storage: PROPFIND (depth 1) for a folder's entries, GET (streamed into a scratch file, then read
in pieces) for a file, MKCOL for a folder, PUT for a file (one request below SINGLE_PUT, else a
chunked upload, which appears only at its final MOVE: never half there), DELETE for what a
failed write left. A path missing is FileNotFoundError, a name taken FileExistsError, as the
storage's contract wants them.

It keeps the Nextcloud IDs of the entries it lists or writes (file_id): a vault's file is known
to its job by the ID of its encrypted entry, which a move or rename inside the vault keeps (as
in Files), so that Download deleted files again finds it."""

from __future__ import annotations

import posixpath
import re
import tempfile
from collections.abc import Iterable, Iterator

from nc_py_api import NextcloudException

from .nextcloud import SINGLE_PUT, Call, NextcloudLibrary
from .vault import StorageEntry

PIECE = 1 << 20


class WebDavStorage:
    """The vault in the folder root of the person's files nc acts for."""

    def __init__(self, nc, call: Call, root: str):
        self.nc = nc
        self.call = call
        self.root = "/" + root.strip("/")
        self.ids: dict[str, int] = {}

    def full(self, path: str) -> str:
        """The path in the person's files of a path in the vault's folder."""
        return posixpath.join(self.root, path) if path else self.root

    def _call(self, awaitable, path: str):
        """The request's answer; its 404 as FileNotFoundError, a taken name's 405 as
        FileExistsError, a missing parent's 409 as FileNotFoundError."""
        try:
            return self.call(awaitable)
        except NextcloudException as error:
            if error.status_code in (404, 409):
                raise FileNotFoundError(path) from error
            if error.status_code == 405:
                raise FileExistsError(path) from error
            raise

    def list(self, path: str) -> list[StorageEntry]:
        """A folder's entries (PROPFIND, depth 1)."""
        entries = []
        for node in self._call(self.nc.files.listdir(self.full(path), depth=1), path):
            name = node.name
            entries.append(StorageEntry(name, node.is_dir, 0 if node.is_dir else int(node.info.size or 0)))
            if getattr(node.info, "fileid", 0):
                self.ids[posixpath.join(path, name) if path else name] = int(node.info.fileid)
        return entries

    def read(self, path: str) -> bytes:
        """A small file's bytes (GET)."""
        return self._call(self.nc.files.download(self.full(path)), path)

    def stream(self, path: str) -> Iterator[bytes]:
        """A file's bytes in pieces: GET streamed into a scratch file on the disk (encrypted, as it
        is in the vault), then read from it."""
        with tempfile.TemporaryFile() as scratch:
            self._call(self.nc.files.download2stream(self.full(path), scratch), path)
            scratch.seek(0)
            yield from iter(lambda: scratch.read(PIECE), b"")

    def mkdir(self, path: str) -> None:
        """Makes a folder (MKCOL)."""
        node = self._call(self.nc.files.mkdir(self.full(path)), path)
        self._keep_id(path, node)

    def write(self, path: str, chunks: Iterable[bytes]) -> None:
        """Stores a file from its pieces, gathered in a scratch file first: one PUT below
        SINGLE_PUT, else a chunked upload."""
        with tempfile.TemporaryFile() as scratch:
            for chunk in chunks:
                scratch.write(chunk)
            size = scratch.tell()
            scratch.seek(0)
            if size < SINGLE_PUT:
                node = self._call(self.nc.files.upload(self.full(path), scratch.read()), path)
            else:
                node = self._call(self.nc.files.upload_stream(self.full(path), scratch), path)
        self._keep_id(path, node)

    def remove(self, path: str) -> None:
        """Removes a file or folder (DELETE)."""
        self.ids.pop(path, None)
        self._call(self.nc.files.delete(self.full(path), not_fail=True), path)

    def _keep_id(self, path: str, node) -> None:
        """Keeps the ID an answer gave (the OC-FileId header's digits)."""
        digits = re.match(r"\d+", str(getattr(node, "file_id", "") or ""))
        if digits:
            self.ids[path] = int(digits.group())

    def file_id(self, path: str) -> int | None:
        """The Nextcloud ID of an entry of the vault's folder (asked for if not known), or None."""
        if path not in self.ids:
            try:
                node = self._call(self.nc.files.by_path(self.full(path)), path)
            except FileNotFoundError:
                return None
            if node is None or not getattr(node.info, "fileid", 0):
                return None
            self.ids[path] = int(node.info.fileid)
        return self.ids[path]

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        """The entries of the IDs that are among the person's files (one SEARCH), with "" for
        their paths: an encrypted name says nothing to a person."""
        return {file_id: "" for file_id in NextcloudLibrary(self.nc, self.call).existing(file_ids)}
