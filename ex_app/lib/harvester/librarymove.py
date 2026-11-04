# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move to the private library: files and folders from the person's Files into their library
(library.py), from the Files app's action (routes/moves.py) or the page.

A move is proposed with the file IDs the action sent (propose), shown on Harvester's page, and
started there with the library folder it goes into (start: by default FOLDER), keeping the files'
folders below the one chosen. It runs in a thread of its own, as the person (their own files
only: nothing shared with them, which is not theirs to delete, and nothing in a team folder,
which belongs to everyone who shares it). Each file:

1. is streamed from WebDAV into the library, encrypted as it comes (Incoming: never whole in
   memory, never in plain on the disk), at a free name (" (2)" where one is taken);
2. is checked: its size, and the blob decrypted again to the SHA-256 of what came;
3. the original is deleted (WebDAV): Nextcloud moves its versions into the trash bin with it
   (or, without a trash bin, deletes them with it), so they stay while the delete fails;
4. once all are done, the originals (and the folders the move emptied) are purged from the trash
   bin (trashbin DAV), each by its original place.

A step that fails leaves the original where it was (the library's copy removed again) and says
why, per file. Moves are kept in memory: after a restart the page no longer shows them; the
files are either moved or still in Files.

The other way, Move to Files (export, kind OUT): files and folders chosen in the library go into a
folder of the person's Files, their folders kept below it, while the library is unlocked (the
browser's session, kept open by the move). Each file is decrypted as it streams into Files (in
chunks for a large one, never whole in memory, never in plain on Harvester's disk) at a free name,
read back from Files and compared (its SHA-256), and only then deleted from the library; a folder
all of whose files went is deleted from the library too. A file that fails stays in the library
(a copy that does not match is deleted from Files again) and the page says why."""

import dataclasses
import hashlib
import hmac
import logging
import os
import posixpath
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from nc_py_api import NextcloudException
from nc_py_api.files._files import dav_get_obj_path

from . import librarycrypt as crypt
from .l10n import said
from .library import BadPath, LibraryFull, Shelf, clean
from .librarykeys import Locked, NotSetUp, Session
from .nextcloud import NextcloudLibrary
from .runner import numbered

LOGGER = logging.getLogger("harvester")
Call = Callable[[Awaitable[Any]], Any]
# The library folder a move goes into unless the person chooses one.
FOLDER = "/From Files"
# A proposed move waits so long to be started; a finished one is shown so long.
KEPT_SECONDS = 24 * 3600
# The most files and folders one action may send, and the most moves a person has at once.
MOST = 1000
MOVES_KEPT = 20
WAITING, MOVING, MOVED, FAILED = "waiting", "moving", "moved", "failed"
# A move's way: from Files into the library, or out of it into Files.
IN, OUT = "in", "out"


@dataclasses.dataclass
class Item:
    """A file of a move: its ID and path in Files, its size, its path in the library, how it
    went (WAITING, MOVING, MOVED, FAILED) and why."""

    file_id: int
    path: str
    size: int = 0
    target: str = ""
    state: str = WAITING
    message: str = ""


@dataclasses.dataclass
class Move:  # pylint: disable=too-many-instance-attributes
    """A move: its person, the IDs and names the action sent, the library folder, its files as
    they go, whether it started and ended, and what was left in the trash bin."""

    id: str
    user: str
    selected: list[dict]
    folder: str = FOLDER
    items: list[Item] = dataclasses.field(default_factory=list)
    started: float = 0.0
    ended: float = 0.0
    created: float = dataclasses.field(default_factory=time.time)
    problem: str = ""
    trash: str = ""
    kind: str = IN

    def view(self) -> dict:
        """The move for the page."""
        return {
            "id": self.id,
            "kind": self.kind,
            "selected": self.selected,
            "folder": self.folder,
            "started": self.started,
            "ended": self.ended,
            "problem": self.problem,
            "trash": self.trash,
            "items": [dataclasses.asdict(item) for item in self.items],
            # Seconds since Files proposed it: the page opens the library for a fresh one.
            "age": int(time.time() - self.created),
        }


class Moves:
    """The people's moves (in memory) and their threads."""

    def __init__(self, shelf: Shelf, connect: Callable[[], Any], call: Call):
        self.shelf = shelf
        self.connect = connect
        self.call = call
        self._lock = threading.Lock()
        self._moves: dict[str, Move] = {}

    def propose(self, user: str, files: list[dict]) -> Move:
        """A move of the files the action sent (fileId, name, directory, fileType), not started."""
        selected = [
            {
                "fileId": int(entry["fileId"]),
                "name": str(entry.get("name", "")),
                "directory": str(entry.get("directory", "")),
                "dir": str(entry.get("fileType", "")) == "dir",
            }
            for entry in files[:MOST]
        ]
        move = Move(secrets.token_urlsafe(12), user, selected)
        with self._lock:
            now = time.time()
            self._moves = {key: kept for key, kept in self._moves.items() if now - kept.created < KEPT_SECONDS}
            # A person's oldest ended or never started moves give way to a new one.
            own = sorted((kept for kept in self._moves.values() if kept.user == user), key=lambda kept: kept.created)
            for old in own[: max(0, len(own) - MOVES_KEPT + 1)]:
                if old.ended or not old.started:
                    self._moves.pop(old.id)
            self._moves[move.id] = move
        return move

    def export(self, session: Session, token: str, ids: list[int], folder: str) -> Move:
        """Starts a move of the person's library files and folders (ids) into the folder of their
        Files, kept going by the session of the browser's token; KeyError for an ID that is none."""
        index = self.shelf.index(session)
        selected = [
            {"id": index[file_id]["id"], "name": index[file_id]["name"], "dir": index[file_id]["dir"]}
            for file_id in dict.fromkeys(ids[:MOST])
        ]
        move = Move(secrets.token_urlsafe(12), session.user, selected, folder=files_folder(folder), kind=OUT)
        move.started = time.time()
        with self._lock:
            self._moves[move.id] = move
        thread = threading.Thread(target=self.run_export, args=(move, token), name=f"export-{move.id}", daemon=True)
        thread.start()
        return move

    def run_export(self, move: Move, token: str) -> None:
        """Runs a move to Files to its end (in its thread)."""
        try:
            Exporter(self.shelf, self.connect(), self.call, move, token).run()
        except Exception as error:  # pylint: disable=broad-exception-caught
            LOGGER.warning("a move to Files ended: %s", type(error).__name__)
            move.problem = str(error) or type(error).__name__
        finally:
            move.ended = time.time()

    def of(self, user: str) -> list[Move]:
        """The person's moves, the newest first."""
        with self._lock:
            return sorted((move for move in self._moves.values() if move.user == user), key=lambda move: -move.created)

    def get(self, user: str, move_id: str) -> Move | None:
        """The person's move of this ID, or None."""
        with self._lock:
            move = self._moves.get(move_id)
        return move if move is not None and move.user == user else None

    def start(self, move: Move, folder: str = FOLDER) -> threading.Thread:
        """Starts the move into the library folder (BadPath for a folder the library keeps no
        name of; ValueError if it started already)."""
        with self._lock:
            if move.started:
                raise ValueError("started already")
            move.folder = clean(folder or FOLDER)
            move.started = time.time()
        thread = threading.Thread(target=self.run, args=(move,), name=f"move-{move.id}", daemon=True)
        thread.start()
        return thread

    def run(self, move: Move) -> None:
        """Runs the move to its end (in its thread)."""
        try:
            Mover(self.shelf, self.connect(), self.call, move).run()
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Whatever ends it, the page says so; an original not yet moved stays in Files.
            LOGGER.warning("a move to the library ended: %s", type(error).__name__)
            move.problem = str(error) or type(error).__name__
        finally:
            move.ended = time.time()


class Mover:  # pylint: disable=too-few-public-methods
    """One move's work, as its person (nc), each request run to its end by call."""

    def __init__(self, shelf: Shelf, nc, call: Call, move: Move):
        self.shelf, self.nc, self.call, self.move = shelf, nc, call, move
        self.user = move.user
        # The originals deleted and the folders the move emptied: their paths in Files.
        self.deleted: list[str] = []

    def run(self) -> None:
        """Moves each file and folder selected, then purges the trash bin of the originals."""
        self.call(self.nc.set_user(self.user))
        try:
            self.shelf.keys.public(self.user)
        except NotSetUp as error:
            self.move.problem = str(error)
            return
        for selected in self.move.selected:
            node = self._node(selected["fileId"])
            if node is None:
                self.move.items.append(
                    Item(selected["fileId"], selected["name"], state=FAILED, message=said("Not found in your files."))
                )
                continue
            if node.is_shared:
                self.move.items.append(
                    Item(
                        selected["fileId"],
                        node.user_path,
                        state=FAILED,
                        message=said("Shared with you: only its owner can move it."),
                    )
                )
                continue
            if self._in_team_folder(node):
                self.move.items.append(
                    Item(
                        selected["fileId"],
                        node.user_path,
                        state=FAILED,
                        message=said("In a team folder: it belongs to everyone who shares the folder."),
                    )
                )
                continue
            base = posixpath.dirname("/" + node.user_path.strip("/"))
            files = self._walk(node) if node.is_dir else [node]
            for file in files:
                path = "/" + file.user_path.strip("/")
                relative = path[len(base) :].lstrip("/") if base != "/" else path.lstrip("/")
                item = Item(int(file.info.fileid), path, int(file.info.size or 0))
                item.target = posixpath.join(self.move.folder, relative)
                self.move.items.append(item)
            for item, file in zip(self.move.items[-len(files) :], files):
                self._one(item, file)
            if node.is_dir:
                self._empty_folders(node)
        self._purge_trash()

    def _node(self, file_id: int):
        """The person's file or folder of the ID (a search of their own files), or None."""
        try:
            return self.call(self.nc.files.by_id(file_id))
        except NextcloudException:
            return None

    def _walk(self, folder) -> list:
        """The files in the folder and below it (a PROPFIND of depth 1 each)."""
        found = []
        for node in self.call(self.nc.files.listdir(folder.user_path, depth=1)):
            if node.is_dir and node.is_mounted and self._in_team_folder(node):
                continue  # a team folder mounted below: not the person's alone to move
            if node.is_dir:
                found += self._walk(node)
            elif not node.is_shared:
                found.append(node)
        return found

    def _in_team_folder(self, node) -> bool:
        """Whether the file or folder is in a team folder (Nextcloud's mount type group, of the
        app Team folders): the permissions say only "mounted", as for an external storage, which
        the person may move out of."""
        if not node.is_mounted:
            return False
        body = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">'
            "<d:prop><nc:mount-type/></d:prop></d:propfind>"
        )
        path = quote(dav_get_obj_path(self.user, node.user_path))
        answer = self.call(
            self.nc._session.adapter_dav.request(  # pylint: disable=protected-access
                "PROPFIND", path, content=body, headers={"Depth": "0", "Content-Type": "text/xml"}
            )
        )
        if answer.status_code != 207:
            # Unknown: kept from the move, as a team folder would be.
            return True
        found = re.search(r"<nc:mount-type>([^<]*)</nc:mount-type>", answer.text)
        return found is not None and found.group(1).strip() == "group"

    def _free(self, target: str) -> str:
        """target, or the first of target (2), (3), … not taken in the library."""
        number, path = 1, clean(target)
        while self.shelf.find(self.user, path) is not None:
            number += 1
            path = numbered(clean(target), number)
        return path

    def _one(self, item: Item, node) -> None:
        """Moves one file: into the library, checked, its original deleted (its versions go into
        the trash bin with it, purged at the end); else it stays, versions and all, and the item
        says why."""
        item.state = MOVING
        file_id = None
        try:
            item.target = self._free(item.target)
            incoming = self.shelf.receive(self.user, item.target, expected=item.size)
            try:
                self.call(self.nc.files.download2stream(node.user_path, incoming))
            except BaseException:
                incoming.abort()
                raise
            if incoming.sealer.size != item.size:
                incoming.abort()
                raise ValueError(said("It changed while it was read: {size} bytes came.", size=incoming.sealer.size))
            file_id = incoming.commit()
            if not incoming.verify():
                raise ValueError(said("The library's copy does not match: nothing was deleted."))
            self.call(self.nc.files.delete(node.user_path))
        except (NextcloudException, OSError, ValueError, BadPath, LibraryFull, NotSetUp) as error:
            if file_id is not None:
                self.shelf.delete(self.user, file_id)
            item.state = FAILED
            item.message = str(error) or type(error).__name__
            return
        self.deleted.append(item.path)
        item.state = MOVED

    def _empty_folders(self, folder) -> None:
        """Deletes the folder and those below it that the move emptied (deepest first)."""
        try:
            inner = self.call(self.nc.files.listdir(folder.user_path, depth=1))
        except NextcloudException:
            return
        for node in inner:
            if node.is_dir:
                self._empty_folders(node)
        try:
            if not self.call(self.nc.files.listdir(folder.user_path, depth=1)):
                self.call(self.nc.files.delete(folder.user_path))
                self.deleted.append("/" + folder.user_path.strip("/"))
        except NextcloudException:
            return

    def _purge_trash(self) -> None:
        """Deletes the originals the move deleted from the trash bin (by their original places,
        deleted since the move started), and with them what the trash bin kept of them."""
        if not self.deleted:
            return
        wanted = {path.strip("/") for path in self.deleted}
        try:
            trash = self.call(self.nc.files.trashbin_list())
        except NextcloudException as error:
            if error.status_code != 404:  # no trash bin: deleted is gone
                self.move.trash = said("The trash bin could not be read: empty it yourself.")
            return
        left = 0
        for node in trash:
            place = str(getattr(node.info, "trashbin_original_location", "") or "").strip("/")
            deleted_at = int(getattr(node.info, "trashbin_deletion_time", 0) or 0)
            if place not in wanted or deleted_at < self.move.started - 120:
                continue
            try:
                self.call(self.nc.files.trashbin_delete(node, not_fail=True))
            except NextcloudException:
                left += 1
        if left:
            self.move.trash = said(
                "{count} of the originals are still in the trash bin: empty it yourself.", count=left
            )


def files_folder(folder: str) -> str:
    """A folder of the person's Files ("/Documents"), or BadPath for one with a "." or ".." part,
    a control character or a backslash."""
    parts = [part for part in folder.split("/") if part]
    if any(part in (".", "..") or any(ord(char) < 32 or char in "\\\x7f" for char in part) for part in parts):
        raise BadPath(f"{folder}: no such folder name")
    return "/" + "/".join(parts)


class Decrypted:
    """A library file read as a file (what an upload reads): decrypted as it is read, its
    SHA-256 taken; seek only to its start (read again) or its end (its size)."""

    def __init__(self, shelf: Shelf, session: Session, entry: dict):
        self.shelf, self.session, self.entry = shelf, session, entry
        self.size = int(entry["size"])
        self._restart()

    def _restart(self) -> None:
        size = self.size
        self._pieces = self.shelf.content(self.session, self.entry["id"], 0, size - 1) if size else iter(())
        self._buffer = b""
        self._at = 0
        self.digest = hashlib.sha256()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """To the start (read again) or the end (to tell the size); nowhere else."""
        if offset == 0 and whence == os.SEEK_SET:
            self._restart()
        elif offset == 0 and whence == os.SEEK_END:
            self._at = self.size
        else:
            raise OSError("a decrypted file is read from its start")
        return self._at

    def tell(self) -> int:
        """Where the reading is (the size after a seek to the end)."""
        return self._at

    def read(self, count: int = -1) -> bytes:
        """The next count bytes (all that is left for -1)."""
        while count < 0 or len(self._buffer) < count:
            piece = next(self._pieces, None)
            if piece is None:
                break
            self._buffer += piece
        data, self._buffer = (self._buffer, b"") if count < 0 else (self._buffer[:count], self._buffer[count:])
        self.digest.update(data)
        self._at += len(data)
        return data


class Hashed:  # pylint: disable=too-few-public-methods
    """What a download writes into: its SHA-256 and size."""

    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        """Takes the next bytes of the download."""
        self.digest.update(data)
        self.size += len(data)
        return len(data)


class Exporter:  # pylint: disable=too-few-public-methods
    """One move to Files: the library's files decrypted into the person's Files (nc acting as
    them), each checked before the library's copy goes."""

    def __init__(
        self, shelf: Shelf, nc, call: Call, move: Move, token: str
    ):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self.shelf, self.nc, self.call, self.move, self.token = shelf, nc, call, move, token
        self.user = move.user
        self.files = NextcloudLibrary(nc, call)

    def _session(self) -> Session:
        """The browser's session (each use keeps it open longer); Locked when it ended."""
        return self.shelf.keys.session(self.user, self.token)

    def run(self) -> None:
        """Moves each file and folder chosen; a folder all of whose files went goes too."""
        self.call(self.nc.set_user(self.user))
        for selected in self.move.selected:
            try:
                entries = self.shelf.below(self._session(), selected["id"])
            except Locked as error:
                self.move.problem = str(error)
                return
            except KeyError:
                self.move.items.append(
                    Item(selected["id"], selected["name"], state=FAILED, message=said("Not in your library."))
                )
                continue
            top = posixpath.dirname(entries[0]["path"])
            items = [
                Item(
                    entry["id"],
                    entry["path"],
                    entry["size"],
                    target=posixpath.join(self.move.folder, entry["path"][len(top) :].lstrip("/")),
                )
                for entry in entries
                if not entry["dir"]
            ]
            self.move.items += items
            for item, entry in zip(items, [entry for entry in entries if not entry["dir"]]):
                self._one(item, entry)
            if entries[0]["dir"] and all(item.state == MOVED for item in items):
                self.shelf.delete(self.user, entries[0]["id"])

    def _free(self, target: str) -> str:
        """target, or the first of target (2), (3), … not taken in Files."""
        number, path = 1, target
        while self.files.find(path) is not None:
            number += 1
            path = numbered(target, number)
        return path

    def _one(self, item: Item, entry: dict) -> None:
        """Moves one file into Files: uploaded as it is decrypted, read back and compared, then
        deleted from the library; else it stays there and the item says why."""
        item.state = MOVING
        placed = ""
        try:
            reader = Decrypted(self.shelf, self._session(), entry)
            item.target = placed = self._free(item.target)
            self.files.put(placed, reader)
            check = Hashed()
            self.call(self.nc.files.download2stream(placed, check))
            if check.size != item.size or not hmac.compare_digest(check.digest.digest(), reader.digest.digest()):
                raise ValueError(said("The copy in Files does not match: it stays in the library."))
            self.shelf.delete(self.user, item.file_id)
        except (NextcloudException, OSError, ValueError, KeyError, Locked, crypt.Tampered) as error:
            if placed and not isinstance(error, Locked) and self.files.find(placed) is not None:
                try:
                    self.call(self.nc.files.delete(placed))
                except NextcloudException:
                    pass
            item.state = FAILED
            item.message = str(error) or type(error).__name__
            return
        item.state = MOVED
