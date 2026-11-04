# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harvester's private library: a job's files kept in the app's own persistent storage
(`$APP_PERSISTENT_STORAGE/library/<person>/<path>`), not in Nextcloud. Nextcloud's Files, Photos,
search, WebDAV and the sync clients never see them; the person sees them on Harvester's page
(Library), through the app's routes (routes/library.py), which serve a person their own files
alone.

The index is a table of the app's database (`library`): each file and folder of a person by its
path, with its size, type and labels (Tag as: Harvester's labels). A file's ID is its row's, never
used again (AUTOINCREMENT), so that a file deleted in the library is gone for its job
(Library.existing) and, with Download deleted files again, fetched anew, as in Nextcloud.

Paths are checked as they come (clean): no empty, ".", ".." or hidden part, no control character
or backslash; a person's folder is their user ID percent-encoded (quote), so no ID reaches
another's. A file is written beside its place and renamed into it (never seen half-written).

Limits (the admin's settings, LIMITS): bytes per person and in all (0: none). A file that would
pass one is not saved: LibraryFull ends the run with why.

A person may lock their library with a password (Locks): its hash (scrypt) in the database, the
library unlocked for a browser's session with a token that lapses after the person's minutes
without use. The files are not encrypted on the disk: the lock keeps out whoever uses the
person's open Nextcloud, not the server's admin.
"""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import secrets
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from .l10n import said
from .runner import FileGone, Library, size_text
from .sources import SourceError
from .store import Store

SCHEMA = """
-- Harvester's private library (library.py): each person's files and folders by path, their
-- size, type and labels, and the job that saved them (kept when the job is deleted).
CREATE TABLE IF NOT EXISTS library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    path TEXT NOT NULL,
    dir INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '[]',
    job INTEGER,
    at REAL NOT NULL,
    UNIQUE (user, path)
);
-- A person's library password (its scrypt hash) and the minutes it stays unlocked unused.
CREATE TABLE IF NOT EXISTS library_locks (
    user TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    minutes INTEGER NOT NULL
);
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


class Shelf:
    """The library's files on the disk under root, and their index in the store."""

    def __init__(self, store: Store, root: str | os.PathLike):
        self.store = store
        self.root = Path(root)
        store.tables(SCHEMA)
        self._writing = threading.Lock()

    def disk(self, user: str, path: str) -> Path:
        """Where a file of the person's is on the disk (path checked)."""
        own = self.root / person_folder(user)
        found = own.joinpath(*clean(path).strip("/").split("/")) if clean(path) != "/" else own
        if own != found and own not in found.parents:
            raise BadPath(path)
        return found

    def entry(self, user: str, file_id: int) -> dict | None:
        """One of the person's files or folders: id, path, name, dir, size, type, labels, job,
        at; None for another's or none."""
        rows = self.store.rows("SELECT * FROM library WHERE user = ? AND id = ?", (user, file_id))
        return self._entry(rows[0]) if rows else None

    @staticmethod
    def _entry(row) -> dict:
        return {
            "id": row["id"],
            "path": row["path"],
            "name": posixpath.basename(row["path"]),
            "dir": bool(row["dir"]),
            "size": row["size"],
            "type": row["type"],
            "labels": json.loads(row["labels"]),
            "job": row["job"],
            "at": row["at"],
        }

    def find(self, user: str, path: str) -> int | None:
        """The ID of the person's file or folder at path, or None."""
        rows = self.store.rows("SELECT id FROM library WHERE user = ? AND path = ?", (user, clean(path)))
        return rows[0]["id"] if rows else None

    def usage(self, user: str | None = None) -> int:
        """The bytes of a person's files (of everyone's without one)."""
        if user is None:
            rows = self.store.rows("SELECT COALESCE(SUM(size), 0) AS used FROM library")
        else:
            rows = self.store.rows("SELECT COALESCE(SUM(size), 0) AS used FROM library WHERE user = ?", (user,))
        return int(rows[0]["used"])

    def room(self, user: str, path: str, size: int) -> None:
        """LibraryFull if a file of size at path (over the one there) would pass a limit."""
        before = self.store.rows("SELECT size FROM library WHERE user = ? AND path = ?", (user, path))
        grows = size - (before[0]["size"] if before else 0)
        person, total = LIMITS["person"], LIMITS["total"]
        if person and self.usage(user) + grows > person:
            raise LibraryFull(
                said(
                    "The library is full: {path} ({size}) would pass your limit of {limit}",
                    path=path,
                    size=size_text(size),
                    limit=size_text(person),
                )
            )
        if total and self.usage() + grows > total:
            raise LibraryFull(
                said(
                    "The library is full: {path} ({size}) would pass the server's limit of {limit} for everyone",
                    path=path,
                    size=size_text(size),
                    limit=size_text(total),
                )
            )

    def put(self, user: str, path: str, content: BinaryIO, job: int | None = None) -> int:
        """Stores the file at path (over the person's file there), its folders made; its ID."""
        path = clean(path)
        content.seek(0, os.SEEK_END)
        size = content.tell()
        content.seek(0)
        with self._writing:
            self.room(user, path, size)
            folders = self._folders(user, path)
            place = self.disk(user, path)
            place.parent.mkdir(parents=True, exist_ok=True)
            partial = place.with_name(f".{uuid.uuid4().hex}.part")
            try:
                with open(partial, "wb") as file:
                    for piece in iter(lambda: content.read(PIECE), b""):
                        file.write(piece)
                os.replace(partial, place)
            finally:
                partial.unlink(missing_ok=True)
            now = time.time()
            with self.store.writing() as db:
                for folder in folders:
                    db.execute(
                        "INSERT OR IGNORE INTO library (user, path, dir, job, at) VALUES (?, ?, 1, ?, ?)",
                        (user, folder, job, now),
                    )
                db.execute(
                    "INSERT INTO library (user, path, size, type, job, at) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (user, path) DO UPDATE SET size = excluded.size, type = excluded.type,"
                    " job = excluded.job, at = excluded.at",
                    (user, path, size, file_type(path), job, now),
                )
                row = db.execute("SELECT id FROM library WHERE user = ? AND path = ?", (user, path)).fetchone()
        return int(row["id"])

    def _folders(self, user: str, path: str) -> list[str]:
        """The folders above path, the top's first (not "/"); BadPath where a file has the name of
        one of them, or a folder path's."""
        folders = [posixpath.dirname(path)]
        while folders[-1] != "/":
            folders.append(posixpath.dirname(folders[-1]))
        folders = folders[-2::-1]
        for folder in folders:
            taken = self.store.rows("SELECT dir FROM library WHERE user = ? AND path = ?", (user, folder))
            if taken and not taken[0]["dir"]:
                raise BadPath(f"{folder} is a file, not a folder")
        existing = self.store.rows("SELECT dir FROM library WHERE user = ? AND path = ?", (user, path))
        if existing and existing[0]["dir"]:
            raise BadPath(f"{path} is a folder")
        return folders

    def existing(self, user: str, ids: list[int]) -> dict[int, str]:
        """The person's files of the IDs that are there, with their paths."""
        found: dict[int, str] = {}
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            marks = ",".join("?" * len(batch))
            for row in self.store.rows(
                f"SELECT id, path FROM library WHERE user = ? AND id IN ({marks})", (user, *batch)
            ):
                found[row["id"]] = row["path"]
        return found

    def label(self, user: str, file_id: int, add: Sequence[str] = (), remove: Sequence[str] = ()) -> bool:
        """Gives the person's file the labels add and takes those of remove off; False when there
        is no such file."""
        with self.store.writing() as db:
            row = db.execute("SELECT labels FROM library WHERE user = ? AND id = ?", (user, file_id)).fetchone()
            if row is None:
                return False
            labels = [label for label in json.loads(row["labels"]) if label not in remove]
            labels += [label for label in add if label not in labels]
            db.execute("UPDATE library SET labels = ? WHERE id = ?", (json.dumps(labels), file_id))
        return True

    def listing(self, user: str, folder: str = "/", label: str = "") -> list[dict]:
        """The person's files and folders in folder (folders first, by name); with a label, the
        files of that label in it and below it, by path."""
        folder = clean(folder)
        prefix = "/" if folder == "/" else folder + "/"
        rows = self.store.rows(
            "SELECT * FROM library WHERE user = ? AND substr(path, 1, ?) = ? ORDER BY dir DESC, path",
            (user, len(prefix), prefix),
        )
        entries = [self._entry(row) for row in rows]
        if label:
            return [entry for entry in entries if not entry["dir"] and label in entry["labels"]]
        return [entry for entry in entries if "/" not in entry["path"][len(prefix) :]]

    def labels(self, user: str) -> list[str]:
        """The labels of the person's files, by name."""
        rows = self.store.rows("SELECT DISTINCT labels FROM library WHERE user = ? AND labels != '[]'", (user,))
        return sorted({label for row in rows for label in json.loads(row["labels"])}, key=str.casefold)

    def delete(self, user: str, file_id: int) -> bool:
        """Deletes the person's file, or folder with everything in it; False when there is none."""
        entry = self.entry(user, file_id)
        if entry is None:
            return False
        with self._writing:
            place = self.disk(user, entry["path"])
            if entry["dir"]:
                below = entry["path"] + "/"
                with self.store.writing() as db:
                    db.execute(
                        "DELETE FROM library WHERE user = ? AND (id = ? OR substr(path, 1, ?) = ?)",
                        (user, file_id, len(below), below),
                    )
                for inner in sorted(place.rglob("*"), key=lambda found: len(found.parts), reverse=True):
                    if inner.is_dir() and not inner.is_symlink():
                        inner.rmdir()
                    else:
                        inner.unlink(missing_ok=True)
                if place.is_dir():
                    place.rmdir()
            else:
                with self.store.writing() as db:
                    db.execute("DELETE FROM library WHERE user = ? AND id = ?", (user, file_id))
                place.unlink(missing_ok=True)
        return True


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


# A library's password's hash: scrypt with these costs (some 32 MB, a tenth of a second).
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
# Unlocked for so many minutes without use, by default; the person's choice from 1 to a day.
LOCK_MINUTES = 15
MAX_LOCK_MINUTES = 24 * 60
# Wrong passwords in a row before the next try waits FAILED_WAIT seconds.
MAX_FAILED = 5
FAILED_WAIT = 60


def hashed(password: str) -> str:
    """The password's hash as kept: scrypt$<n>$<r>$<p>$<salt>$<hash>, base64."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=128 * 1024**2, dklen=32, **SCRYPT)
    encoded = [base64.b64encode(part).decode() for part in (salt, digest)]
    return "$".join(["scrypt", str(SCRYPT["n"]), str(SCRYPT["r"]), str(SCRYPT["p"]), *encoded])


def matches(password: str, kept: str) -> bool:
    """Whether the password is the one of the hash kept."""
    try:
        kind, n, r, p, salt, digest = kept.split("$")
        if kind != "scrypt":
            return False
        again = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=128 * 1024**2, dklen=32
        )
    except ValueError:
        return False
    return hmac.compare_digest(again, base64.b64decode(digest))


class Locked(Exception):
    """The person's library is locked (or a wrong password was given): the routes answer 403."""


class Locks:
    """The people's library passwords and the browsers' unlocked sessions (in memory: a restart
    locks every library again)."""

    def __init__(self, store: Store):
        self.store = store
        store.tables(SCHEMA)
        self._lock = threading.Lock()
        # By token: [user, the time it lapses, its minutes].
        self._sessions: dict[str, list] = {}
        # By user: [wrong passwords in a row, the time of the last].
        self._failed: dict[str, list] = {}

    def setting(self, user: str) -> dict | None:
        """The person's lock (hash, minutes), or None: no password."""
        rows = self.store.rows("SELECT hash, minutes FROM library_locks WHERE user = ?", (user,))
        return {"hash": rows[0]["hash"], "minutes": rows[0]["minutes"]} if rows else None

    def unlocked(self, user: str, token: str) -> bool:
        """Whether the person's library is open to this browser: it has no password, or the token
        is the person's and has not lapsed (each use counts as use)."""
        setting = self.setting(user)
        if setting is None:
            return True
        now = time.monotonic()
        with self._lock:
            session = self._sessions.get(token or "")
            if session is None or session[0] != user or session[1] < now:
                self._sessions.pop(token or "", None)
                return False
            session[1] = now + 60 * session[2]
        return True

    def check(self, user: str, token: str) -> None:
        """Locked unless the library is open to this browser."""
        if not self.unlocked(user, token):
            raise Locked(said("The library is locked."))

    def _verify(self, user: str, password: str, kept: str) -> None:
        """Locked unless the password is the person's; wrong ones in a row make the next wait."""
        now = time.monotonic()
        with self._lock:
            count, last = self._failed.get(user, [0, 0.0])
            if count >= MAX_FAILED and now - last < FAILED_WAIT:
                raise Locked(said("Too many wrong passwords: try again in a minute."))
        if matches(password, kept):
            with self._lock:
                self._failed.pop(user, None)
            return
        with self._lock:
            self._failed[user] = [count + 1, now]
        raise Locked(said("Wrong password."))

    def unlock(self, user: str, password: str) -> str:
        """A token that opens the person's library to the browser that holds it ("" if it has no
        password); Locked if the password is wrong."""
        setting = self.setting(user)
        if setting is None:
            return ""
        self._verify(user, password, setting["hash"])
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._sessions = {key: value for key, value in self._sessions.items() if value[1] >= now}
            self._sessions[token] = [user, now + 60 * setting["minutes"], setting["minutes"]]
        return token

    def lock(self, token: str) -> None:
        """Ends the browser's session (the library is locked to it again)."""
        with self._lock:
            self._sessions.pop(token or "", None)

    def set_password(self, user: str, current: str, password: str, minutes: int) -> None:
        """Sets, changes (current given) or (password "") removes the person's password, and the
        minutes it stays unlocked; Locked if current is not the password set. Every session of
        theirs ends."""
        setting = self.setting(user)
        if setting is not None:
            self._verify(user, current, setting["hash"])
        minutes = min(max(1, int(minutes)), MAX_LOCK_MINUTES)
        with self.store.writing() as db:
            if password:
                db.execute(
                    "INSERT OR REPLACE INTO library_locks (user, hash, minutes) VALUES (?, ?, ?)",
                    (user, hashed(password), minutes),
                )
            else:
                db.execute("DELETE FROM library_locks WHERE user = ?", (user,))
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items() if value[0] != user}
