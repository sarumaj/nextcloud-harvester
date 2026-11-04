# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harvester's private library: each person's hidden, encrypted space in the app's own persistent
storage (`$APP_PERSISTENT_STORAGE/library/<person>/`), not in Nextcloud. Nextcloud's Files,
Photos, search, WebDAV and the sync clients never see it; the person sees it on Harvester's page
(Library), through the app's routes (routes/library.py), which serve a person their own files
alone. Jobs save into it (Save to), the person uploads into it and moves files from Files into
it (librarymove.py).

Everything is encrypted (librarycrypt.py, keys in librarykeys.py): a file's content on the disk
(a blob named by a random id), its name, folder, size and type in the database, its labels, its
thumbnail. The database (`library_files`) holds per file and folder only its random ids, its
folder's row, its size on the disk (whole chunks), its times, the job that saved it, and a lookup
value: the HMAC of its path with the person's lookup key, by which a job finds the file it saved
while the library is locked. Whoever has the database can test a guessed path with it, not read
one. A file's ID is its row's, never used again (AUTOINCREMENT), so that a file deleted in the
library is gone for its job (Library.existing) and, with Download deleted files again, fetched
anew, as in Nextcloud.

Writing needs the public key alone (a job, an upload, a move work while the library is locked);
reading names, contents and thumbnails needs an unlocked Session's private key. What a session
decrypted is kept in its cache (by row, checked against the row's encrypted value), dropped at
lock.

Paths are checked as they come (clean): no empty, ".", ".." or hidden part, no control character
or backslash. A file is written beside its place and renamed into it (never seen half-written).

Limits (the admin's settings, LIMITS): bytes on the disk per person and in all (0: none). A file
that would pass one is not saved: LibraryFull ends the run with why.
"""

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import posixpath
import secrets
import shutil
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from . import librarycrypt as crypt
from . import thumbnails
from .l10n import said
from .librarykeys import Keyring, Locked, NotSetUp, Session
from .runner import FileGone, Library, size_text
from .sources import SourceError
from .store import Store

LOGGER = logging.getLogger("harvester")
__all__ = ["Locked", "NotSetUp"]

SCHEMA = """
-- Harvester's private library (library.py): each person's files and folders, encrypted. A row
-- holds its folder's row (parent), the HMAC of its path (lookup), its blob's random id, its
-- secret sealed to the person's public key, its name, size and type encrypted (meta), whether it
-- has a thumbnail, its size on the disk, the job that saved it (kept when the job is deleted).
CREATE TABLE IF NOT EXISTS library_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    parent INTEGER,
    dir INTEGER NOT NULL DEFAULT 0,
    lookup TEXT NOT NULL,
    blob TEXT NOT NULL,
    secret BLOB NOT NULL,
    meta BLOB NOT NULL,
    thumb INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    job INTEGER,
    at REAL NOT NULL,
    UNIQUE (user, lookup)
);
CREATE INDEX IF NOT EXISTS library_files_parent ON library_files (user, parent);
-- The labels given to and taken off a person's files, each change sealed to their public key
-- (a job labels while the library is locked); applied in order.
CREATE TABLE IF NOT EXISTS library_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    file INTEGER NOT NULL,
    change BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS library_labels_file ON library_labels (user, file);
"""
GB = 1000**3
# The admin's limits (settings.LIBRARY, LIBRARY_TOTAL), in bytes; 0: none. Read each minute.
LIMITS = {"person": 10 * GB, "total": 0}
# A name's most characters, and a path's most parts.
MAX_NAME = 255
MAX_DEPTH = 30
PIECE = 1 << 20


class BadPath(SourceError, ValueError):
    """A path the library does not keep (a part ".." or hidden, a control character, …)."""


class LibraryFull(Exception):
    """A file would pass a limit of the library: the run ends (not a failure of one file)."""


def clean(path: str) -> str:
    """The library's path of a file or folder ("/a/b.pdf"), or BadPath: no empty, ".", ".." or
    hidden part (".x"), no control character or backslash, names of MAX_NAME characters at most,
    MAX_DEPTH parts at most."""
    parts = [part for part in path.split("/") if part]
    if len(parts) > MAX_DEPTH:
        raise BadPath(f"{path}: more than {MAX_DEPTH} folders deep")
    for part in parts:
        if part.startswith(".") or len(part) > MAX_NAME or any(ord(char) < 32 or char in "\\\x7f" for char in part):
            raise BadPath(f"{path}: the library keeps no name {part!r}")
    return "/" + "/".join(parts)


def person_folder(user: str) -> str:
    """The folder of a person's files: their user ID percent-encoded (no "/", no leading ".")."""
    name = quote(user, safe="")
    return "%2E" + name[1:] if name.startswith(".") else name


def file_type(path: str) -> str:
    """A file's media type, by its name's extension."""
    return mimetypes.guess_type(posixpath.basename(path))[0] or "application/octet-stream"


def lookup_of(key: bytes, path: str) -> str:
    """A path's lookup value: its HMAC with the person's lookup key."""
    return hmac.new(key, b"path\0" + path.encode(), hashlib.sha256).hexdigest()


def _aad(kind: str, blob: bytes) -> bytes:
    return kind.encode() + b"|" + blob


def _label_aad(user: str, file_id: int) -> bytes:
    return b"label|" + user.encode() + b"|" + str(file_id).encode()


class Incoming:
    """A file being written into a person's library (Shelf.receive): its content encrypted as it
    comes (write), never whole in memory, then committed (commit) or dropped (abort). Needs the
    person's public key alone."""

    def __init__(self, shelf: "Shelf", user: str, path: str, job: int | None = None, expected: int = 0):
        self.shelf, self.user, self.path, self.job = shelf, user, clean(path), job
        if self.path == "/":
            raise BadPath("/ is a folder")
        self.public, self.lookup_key = shelf.keys.public(user)
        self.allowed = shelf.allowance(user, self.path)
        if expected and crypt.stored_size(expected) > self.allowed:
            shelf.room(user, self.path, crypt.stored_size(expected))
        self.secret = secrets.token_bytes(32)
        self.blob = secrets.token_bytes(crypt.BLOB_ID)
        self.type = file_type(self.path)
        self.place = shelf.blob_path(user, self.blob.hex())
        self.place.parent.mkdir(parents=True, exist_ok=True)
        self.partial = self.place.with_name(f".{self.blob.hex()}.part")
        self.file: BinaryIO | None = open(self.partial, "wb")  # pylint: disable=consider-using-with
        keep = thumbnails.SOURCE_MOST if thumbnails.wanted(self.type) else 0
        self.sealer = crypt.Sealer(self.file, crypt.subkey(self.secret, "content"), self.blob, keep)
        self.file_id: int | None = None

    def write(self, data: bytes) -> int:
        """Takes the file's next bytes; LibraryFull (and the file dropped) past a limit."""
        written = self.sealer.write(data)
        if crypt.stored_size(self.sealer.size) > self.allowed:
            self.abort()
            self.shelf.room(self.user, self.path, crypt.stored_size(self.sealer.size))
        return written

    @property
    def digest(self) -> str:
        """The SHA-256 of the plaintext written."""
        return self.sealer.digest.hexdigest()

    def abort(self) -> None:
        """Drops what was written."""
        if self.file is not None:
            self.file.close()
            self.file = None
        self.partial.unlink(missing_ok=True)

    def commit(self) -> int:
        """Puts the file at its path (over the person's file there, which keeps its ID and
        labels), its folders made; its ID."""
        if self.file is None:
            raise ValueError("written already, or dropped")
        try:
            size = self.sealer.finish()
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.file = None
            kept = self.sealer.kept
            picture = thumbnails.thumbnail(bytes(kept)) if kept is not None and size else None
            self.file_id = self.shelf.commit(self, size, picture)
        except BaseException:
            self.abort()
            raise
        return self.file_id

    def verify(self) -> bool:
        """Whether the blob on the disk decrypts to what was written (its SHA-256)."""
        digest = hashlib.sha256()
        try:
            size = self.sealer.size
            for piece in crypt.opened(self.place, crypt.subkey(self.secret, "content"), self.blob, size, 0, size - 1):
                digest.update(piece)
        except (crypt.Tampered, OSError):
            return False
        return hmac.compare_digest(digest.hexdigest(), self.digest)


class Shelf:  # pylint: disable=too-many-public-methods
    """The library's files on the disk under root, and their index in the store."""

    def __init__(self, store: Store, root: str | os.PathLike, keys: Keyring | None = None):
        self.store = store
        self.root = Path(root)
        self.keys = keys or Keyring(store)
        self._retire_unencrypted()
        store.tables(SCHEMA)
        self._writing = threading.Lock()
        # What a write broke off by a restart left (an upload, a move, a run): encrypted, unused.
        for partial in self.root.glob("*/*/.*.part"):
            partial.unlink(missing_ok=True)

    def _retire_unencrypted(self) -> None:
        """A development build's unencrypted library (its table `library` with paths) is not
        carried over: its tables go, its folder is set aside as <root>-unencrypted."""
        columns = [row["name"] for row in self.store.rows("PRAGMA table_info(library)")]
        if "path" not in columns:
            return
        with self.store.writing() as db:
            db.execute("DROP TABLE library")
            db.execute("DROP TABLE IF EXISTS library_locks")
        aside = self.root.with_name(self.root.name + "-unencrypted")
        if self.root.is_dir() and any(self.root.iterdir()) and not aside.exists():
            self.root.rename(aside)
            LOGGER.warning("the unencrypted library of an earlier build is set aside in %s", aside)

    def blob_path(self, user: str, blob: str) -> Path:
        """Where a blob of the person's is on the disk."""
        if len(blob) != 2 * crypt.BLOB_ID or any(char not in "0123456789abcdef" for char in blob):
            raise BadPath(blob)
        return self.root / person_folder(user) / blob[:2] / blob

    def find(self, user: str, path: str) -> int | None:
        """The ID of the person's file or folder at path, or None (also before the setup)."""
        try:
            _, key = self.keys.public(user)
        except NotSetUp:
            return None
        rows = self.store.rows(
            "SELECT id FROM library_files WHERE user = ? AND lookup = ?", (user, lookup_of(key, clean(path)))
        )
        return rows[0]["id"] if rows else None

    def usage(self, user: str | None = None) -> int:
        """The bytes on the disk of a person's files (of everyone's without one)."""
        if user is None:
            rows = self.store.rows("SELECT COALESCE(SUM(size), 0) AS used FROM library_files")
        else:
            rows = self.store.rows("SELECT COALESCE(SUM(size), 0) AS used FROM library_files WHERE user = ?", (user,))
        return int(rows[0]["used"])

    def _before(self, user: str, path: str) -> int:
        """The bytes on the disk of the person's file at path (0: none)."""
        found = self.find(user, path)
        rows = self.store.rows("SELECT size FROM library_files WHERE id = ?", (found,)) if found else []
        return int(rows[0]["size"]) if rows else 0

    def allowance(self, user: str, path: str) -> int:
        """The most bytes on the disk a file at path may take now (beside the limits)."""
        before = self._before(user, path)
        person, total = LIMITS["person"], LIMITS["total"]
        allowed = [1 << 62]
        if person:
            allowed.append(person - self.usage(user) + before)
        if total:
            allowed.append(total - self.usage() + before)
        return min(allowed)

    def room(self, user: str, path: str, stored: int) -> None:
        """LibraryFull if a file of stored bytes on the disk at path (over the one there) would
        pass a limit."""
        grows = stored - self._before(user, path)
        person, total = LIMITS["person"], LIMITS["total"]
        if person and self.usage(user) + grows > person:
            raise LibraryFull(
                said(
                    "The library is full: {path} ({size}) would pass your limit of {limit}",
                    path=path,
                    size=size_text(stored),
                    limit=size_text(person),
                )
            )
        if total and self.usage() + grows > total:
            raise LibraryFull(
                said(
                    "The library is full: {path} ({size}) would pass the server's limit of {limit} for everyone",
                    path=path,
                    size=size_text(stored),
                    limit=size_text(total),
                )
            )

    def receive(self, user: str, path: str, job: int | None = None, expected: int = 0) -> Incoming:
        """A file to write at path, encrypted as it comes (NotSetUp before the person's setup)."""
        return Incoming(self, user, path, job, expected)

    def put(self, user: str, path: str, content: BinaryIO, job: int | None = None) -> int:
        """Stores the file at path (over the person's file there), its folders made; its ID."""
        content.seek(0)
        incoming = self.receive(user, path, job)
        try:
            for piece in iter(lambda: content.read(PIECE), b""):
                incoming.write(piece)
        except BaseException:
            incoming.abort()
            raise
        return incoming.commit()

    def _row_values(self, public: bytes, name: str, parent: int | None, **meta) -> dict:
        """A new row's blob, sealed secret and encrypted meta (name, parent, size, type)."""
        secret, blob = secrets.token_bytes(32), secrets.token_bytes(crypt.BLOB_ID)
        return {
            "blob": blob.hex(),
            "secret": crypt.seal(public, secret, _aad("secret", blob)),
            "meta": self._meta(secret, blob, {"name": name, "parent": parent} | meta),
        }

    @staticmethod
    def _meta(secret: bytes, blob: bytes, meta: dict) -> bytes:
        data = json.dumps(meta, separators=(",", ":")).encode()
        return crypt.encrypt_small(crypt.subkey(secret, "meta"), data, _aad("meta", blob))

    def _folders(self, db, user: str, path: str, keys: tuple[bytes, bytes], job: int | None, now: float) -> int | None:
        """The row of path's folder (None: the top), the folders above path made where missing;
        BadPath where a file has the name of one of them."""
        public, key = keys
        parent, current = None, ""
        for part in path.strip("/").split("/")[:-1]:
            current += "/" + part
            look = lookup_of(key, current)
            row = db.execute("SELECT id, dir FROM library_files WHERE user = ? AND lookup = ?", (user, look)).fetchone()
            if row is not None:
                if not row["dir"]:
                    raise BadPath(f"{current} is a file, not a folder")
                parent = int(row["id"])
                continue
            values = self._row_values(public, part, parent, dir=True)
            parent = db.execute(
                "INSERT INTO library_files (user, parent, dir, lookup, blob, secret, meta, job, at)"
                " VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (user, parent, look, values["blob"], values["secret"], values["meta"], job, now),
            ).lastrowid
        return parent

    def mkdir(self, user: str, path: str) -> int:
        """Makes the folder at path (and those above it); its ID (one there already: its own)."""
        path = clean(path)
        if path == "/":
            raise BadPath("/ is there")
        keys = self.keys.public(user)
        with self._writing, self.store.writing() as db:
            now = time.time()
            parent = self._folders(db, user, path + "/x", keys, None, now)
        assert parent is not None
        return parent

    def commit(self, incoming: Incoming, size: int, picture: bytes | None) -> int:  # pylint: disable=too-many-locals
        """Indexes an Incoming file written in full (Incoming.commit): its blob put in place, its
        row made or the row at its path given it (the old blob removed); its ID."""
        user, path, blob, secret = incoming.user, incoming.path, incoming.blob, incoming.secret
        stored = crypt.stored_size(size)
        with self._writing:
            self.room(user, path, stored)
            if picture:
                thumb = crypt.encrypt_small(crypt.subkey(secret, "thumb"), picture, _aad("thumb", blob))
                incoming.place.with_suffix(".thumb").write_bytes(thumb)
            os.replace(incoming.partial, incoming.place)
            old = None
            try:
                with self.store.writing() as db:
                    now = time.time()
                    parent = self._folders(db, user, path, (incoming.public, incoming.lookup_key), incoming.job, now)
                    look = lookup_of(incoming.lookup_key, path)
                    meta = self._meta(
                        secret,
                        blob,
                        {"name": posixpath.basename(path), "parent": parent, "size": size, "type": incoming.type},
                    )
                    sealed = crypt.seal(incoming.public, secret, _aad("secret", blob))
                    row = db.execute(
                        "SELECT id, dir, blob FROM library_files WHERE user = ? AND lookup = ?", (user, look)
                    ).fetchone()
                    if row is not None and row["dir"]:
                        raise BadPath(f"{path} is a folder")
                    values = (blob.hex(), sealed, meta, int(bool(picture)), stored, incoming.job, now)
                    if row is None:
                        file_id = db.execute(
                            "INSERT INTO library_files (blob, secret, meta, thumb, size, job, at, user, parent, lookup)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (*values, user, parent, look),
                        ).lastrowid
                    else:
                        old, file_id = row["blob"], int(row["id"])
                        db.execute(
                            "UPDATE library_files SET blob = ?, secret = ?, meta = ?, thumb = ?, size = ?, job = ?,"
                            " at = ? WHERE id = ?",
                            (*values, file_id),
                        )
            except BaseException:
                self._remove_blob(user, blob.hex())
                raise
        if old:
            self._remove_blob(user, old)
        assert file_id is not None
        return int(file_id)

    def _remove_blob(self, user: str, blob: str) -> None:
        place = self.blob_path(user, blob)
        place.unlink(missing_ok=True)
        place.with_suffix(".thumb").unlink(missing_ok=True)

    def existing(self, user: str, ids: list[int]) -> dict[int, str]:
        """The person's files of the IDs that are there, with "" for their paths (encrypted)."""
        found: dict[int, str] = {}
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            marks = ",".join("?" * len(batch))
            for row in self.store.rows(
                f"SELECT id FROM library_files WHERE user = ? AND id IN ({marks})", (user, *batch)
            ):
                found[row["id"]] = ""
        return found

    def label(self, user: str, file_id: int, add: Sequence[str] = (), remove: Sequence[str] = ()) -> bool:
        """Gives the person's file the labels add and takes those of remove off (each change
        sealed to their public key: no unlock needed); False when there is no such file."""
        public, _ = self.keys.public(user)
        with self.store.writing() as db:
            if db.execute("SELECT 1 FROM library_files WHERE user = ? AND id = ?", (user, file_id)).fetchone() is None:
                return False
            changes = [("-", label) for label in remove] + [("+", label) for label in add]
            for change in changes:
                db.execute(
                    "INSERT INTO library_labels (user, file, change) VALUES (?, ?, ?)",
                    (user, file_id, crypt.seal(public, json.dumps(change).encode(), _label_aad(user, file_id))),
                )
        return True

    def _below(self, user: str, file_id: int) -> list:
        """The rows of a file, or of a folder and everything in it."""
        return self.store.rows(
            "WITH RECURSIVE below(id) AS (SELECT id FROM library_files WHERE user = ? AND id = ?"
            " UNION SELECT f.id FROM library_files f JOIN below b ON f.parent = b.id WHERE f.user = ?)"
            " SELECT f.id, f.blob, f.dir FROM library_files f JOIN below USING (id)",
            (user, file_id, user),
        )

    def delete(self, user: str, file_id: int) -> bool:
        """Deletes the person's file, or folder with everything in it; False when there is none."""
        with self._writing:
            rows = self._below(user, file_id)
            if not rows:
                return False
            ids = [row["id"] for row in rows]
            with self.store.writing() as db:
                for start in range(0, len(ids), 500):
                    batch = ids[start : start + 500]
                    marks = ",".join("?" * len(batch))
                    db.execute(f"DELETE FROM library_labels WHERE user = ? AND file IN ({marks})", (user, *batch))
                    db.execute(f"DELETE FROM library_files WHERE user = ? AND id IN ({marks})", (user, *batch))
            for row in rows:
                if not row["dir"]:
                    self._remove_blob(user, row["blob"])
        return True

    def reset(self, user: str) -> int:
        """Deletes the person's library entirely (the admin's reset): files, folders, labels and
        keys; the number of files and folders deleted."""
        with self._writing:
            with self.store.writing() as db:
                count = db.execute("DELETE FROM library_files WHERE user = ?", (user,)).rowcount
                db.execute("DELETE FROM library_labels WHERE user = ?", (user,))
            shutil.rmtree(self.root / person_folder(user), ignore_errors=True)
            self.keys.forget(user)
        return count

    # What an unlocked session reads (its private key): names, folders, labels, contents.

    def _opened_rows(self, session: Session) -> dict[int, tuple]:
        """The person's rows decrypted: by ID, (row, secret, meta); a row that does not open, or
        whose meta does not match its place, is left out (and logged by its ID)."""
        assert session.private is not None
        cache: dict = session.cache.setdefault("files", {})
        found = {}
        for row in self.store.rows(
            "SELECT id, parent, dir, lookup, blob, secret, meta, thumb, size, job, at FROM library_files"
            " WHERE user = ?",
            (session.user,),
        ):
            hit = cache.get(row["id"])
            if hit is None or hit[:2] != (row["blob"], bytes(row["meta"]) + bytes(row["secret"])):
                try:
                    blob = bytes.fromhex(row["blob"])
                    secret = crypt.unseal(session.private, bytes(row["secret"]), _aad("secret", blob))
                    meta = json.loads(
                        crypt.decrypt_small(crypt.subkey(secret, "meta"), bytes(row["meta"]), _aad("meta", blob))
                    )
                except (crypt.Tampered, ValueError):
                    LOGGER.warning("library: entry %s does not open (changed outside the app?)", row["id"])
                    continue
                hit = cache[row["id"]] = (row["blob"], bytes(row["meta"]) + bytes(row["secret"]), secret, meta)
            if hit[3].get("parent") != row["parent"]:
                LOGGER.warning("library: entry %s is not in its folder (changed outside the app?)", row["id"])
                continue
            found[row["id"]] = (row, hit[2], hit[3])
        return found

    def _opened_labels(self, session: Session) -> dict[int, list[str]]:
        """The labels of the person's files, their changes applied in order."""
        assert session.private is not None
        cache: dict = session.cache.setdefault("labels", {})
        labels: dict[int, list[str]] = {}
        for row in self.store.rows(
            "SELECT id, file, change FROM library_labels WHERE user = ? ORDER BY id", (session.user,)
        ):
            if row["id"] not in cache:
                try:
                    opened = crypt.unseal(session.private, bytes(row["change"]), _label_aad(session.user, row["file"]))
                    cache[row["id"]] = tuple(json.loads(opened))
                except (crypt.Tampered, ValueError):
                    LOGGER.warning("library: a label of entry %s does not open", row["file"])
                    cache[row["id"]] = ("", "")
            sign, label = cache[row["id"]]
            current = labels.setdefault(row["file"], [])
            if sign == "+" and label not in current:
                current.append(label)
            elif sign == "-" and label in current:
                current.remove(label)
        return labels

    def index(self, session: Session) -> dict[int, dict]:
        """The person's files and folders, decrypted, by ID: id, path, name, dir, size (the
        file's own), type, labels, job, at, thumb. An entry whose folder is missing, or whose
        lookup is not its path's, is left out."""
        rows = self._opened_rows(session)
        labels = self._opened_labels(session)
        _, key = self.keys.public(session.user)
        paths: dict[int, str | None] = {}

        def path_of(file_id: int, depth: int = 0) -> str | None:
            if file_id in paths:
                return paths[file_id]
            row, _, meta = rows[file_id]
            parent = row["parent"]
            above = (
                "" if parent is None else (path_of(parent, depth + 1) if parent in rows and depth < MAX_DEPTH else None)
            )
            paths[file_id] = None if above is None else f"{above}/{meta['name']}"
            return paths[file_id]

        entries = {}
        for file_id, (row, _, meta) in rows.items():
            path = path_of(file_id)
            if path is None or not hmac.compare_digest(lookup_of(key, path), row["lookup"]):
                LOGGER.warning("library: entry %s is not at its place (changed outside the app?)", file_id)
                continue
            entries[file_id] = {
                "id": file_id,
                "path": path,
                "name": meta["name"],
                "dir": bool(row["dir"]),
                "size": int(meta.get("size", 0)),
                "type": "" if row["dir"] else meta.get("type", "application/octet-stream"),
                "labels": labels.get(file_id, []),
                "job": row["job"],
                "at": row["at"],
                "thumb": bool(row["thumb"]),
            }
        return entries

    def entry(self, session: Session, file_id: int) -> dict | None:
        """One of the person's files or folders, decrypted; None for another's or none."""
        return self.index(session).get(file_id)

    def listing(self, session: Session, folder: str = "/", label: str = "", query: str = "") -> list[dict]:
        """The person's files and folders in folder (folders first, by name); with a label or a
        query (in names and labels, any case), those in it and below it that match, by path."""
        folder = clean(folder)
        entries = self.index(session).values()
        if folder != "/" and not any(entry["dir"] and entry["path"] == folder for entry in entries):
            raise BadPath(f"{folder}: no such folder")
        prefix = "/" if folder == "/" else folder + "/"
        below = [entry for entry in entries if entry["path"].startswith(prefix)]
        if label or query:
            wanted = query.casefold()
            return sorted(
                (
                    entry
                    for entry in below
                    if (not label or label in entry["labels"])
                    and (
                        not wanted
                        or wanted in entry["name"].casefold()
                        or any(wanted in name.casefold() for name in entry["labels"])
                    )
                ),
                key=lambda entry: entry["path"].casefold(),
            )
        return sorted(
            (entry for entry in below if "/" not in entry["path"][len(prefix) :]),
            key=lambda entry: (not entry["dir"], entry["name"].casefold()),
        )

    def labels(self, session: Session) -> list[str]:
        """The labels of the person's files, by name."""
        return sorted({label for entry in self.index(session).values() for label in entry["labels"]}, key=str.casefold)

    def _secret(self, session: Session, file_id: int) -> tuple[bytes, bytes]:
        """A file's secret and blob id (from the session's cache, which index filled)."""
        blob, _, secret, _ = session.cache["files"][file_id]
        return secret, bytes.fromhex(blob)

    def content(self, session: Session, file_id: int, start: int, end: int) -> Iterator[bytes]:
        """The bytes start to end (inclusive) of the person's file, decrypted (Tampered as they
        come if the blob was changed); KeyError for none."""
        entry = self.index(session)[file_id]
        if entry["dir"]:
            raise KeyError(file_id)
        secret, blob = self._secret(session, file_id)
        key = crypt.subkey(secret, "content")
        return crypt.opened(self.blob_path(session.user, blob.hex()), key, blob, entry["size"], start, end)

    def thumbnail(self, session: Session, file_id: int) -> bytes | None:
        """The JPEG thumbnail of the person's picture, decrypted; None for none."""
        entry = self.index(session).get(file_id)
        if entry is None or not entry["thumb"]:
            return None
        secret, blob = self._secret(session, file_id)
        try:
            data = self.blob_path(session.user, blob.hex()).with_suffix(".thumb").read_bytes()
            return crypt.decrypt_small(crypt.subkey(secret, "thumb"), data, _aad("thumb", blob))
        except (OSError, crypt.Tampered):
            return None

    def update(  # pylint: disable=too-many-locals
        self, session: Session, file_id: int, name: str = "", folder: str | None = None
    ) -> dict:
        """Renames the person's file or folder (name) and/or moves it into folder ("/": the top);
        the entry after. KeyError for none, BadPath for a name taken, a folder that is not there,
        or a folder moved into itself."""
        index = self.index(session)
        entry = index[file_id]
        above = clean(folder) if folder is not None else posixpath.dirname(entry["path"])
        target = clean(posixpath.join(above, name or entry["name"]))
        if target == entry["path"]:
            return entry
        if entry["dir"] and (target + "/").startswith(entry["path"] + "/"):
            raise BadPath(f"{entry['path']} cannot go into itself")
        by_path = {item["path"]: item for item in index.values()}
        if above != "/" and not by_path.get(above, {}).get("dir"):
            raise BadPath(f"{above}: no such folder")
        if target in by_path:
            raise BadPath(f"{target} is taken")
        parent = by_path[above]["id"] if above != "/" else None
        _, key = self.keys.public(session.user)
        secret, blob = self._secret(session, file_id)
        _, _, meta = self._opened_rows(session)[file_id]
        meta = meta | {"name": posixpath.basename(target), "parent": parent}
        below = [item for item in index.values() if item["path"].startswith(entry["path"] + "/")]
        with self._writing, self.store.writing() as db:
            db.execute(
                "UPDATE library_files SET parent = ?, meta = ?, lookup = ? WHERE user = ? AND id = ?",
                (parent, self._meta(secret, blob, meta), lookup_of(key, target), session.user, file_id),
            )
            for item in below:
                moved = target + item["path"][len(entry["path"]) :]
                db.execute(
                    "UPDATE library_files SET lookup = ? WHERE user = ? AND id = ?",
                    (lookup_of(key, moved), session.user, item["id"]),
                )
        return self.index(session)[file_id]


class PrivateLibrary(Library):
    """A job's files in the person's private library; tags are the files' labels there."""

    def __init__(self, shelf: Shelf, user: str, job_id: int | None):
        self.shelf = shelf
        self.user = user
        self.job_id = job_id

    def find(self, path: str) -> int | None:
        return self.shelf.find(self.user, path)

    def put(self, path: str, content: BinaryIO) -> int:
        return self.shelf.put(self.user, path, content, self.job_id)

    def tag(self, file_id: int, tags: list[str]) -> None:
        if not self.shelf.label(self.user, file_id, add=tags):
            raise FileGone(file_id)

    def untag(self, file_id: int, tags: list[str]) -> None:
        if not self.shelf.label(self.user, file_id, remove=tags):
            raise FileGone(file_id)

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        return self.shelf.existing(self.user, file_ids)
