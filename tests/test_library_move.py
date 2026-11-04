# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move to the private library (librarymove.py, routes/moves.py) against a fake Nextcloud whose
files are a folder on the disk, with a trash bin and versions: files and folders moved with their
folders kept, encrypted, checked, their versions deleted, the originals deleted and purged from
the trash bin; a file that fails stays where it was; a file shared with the person is not
theirs to move; a large file streams; the action's answer and the admin's reset."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import io
import os
import shutil
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import HTTPException
from nc_py_api import NextcloudException
from starlette.requests import Request
from test_library import CHEAP, nextcloud, set_up

from harvester import library
from harvester import nextcloud as nextcloud_module
from harvester import librarycrypt as crypt
from harvester.library import Shelf
from harvester.librarykeys import Keyring
from harvester.librarymove import FAILED, MOVED, OUT, Moves
from harvester.routes import common
from harvester.routes import moves as routes
from harvester.store import Store


def node(path: str, file_id: int, size: int, is_dir: bool, shared: bool = False):
    """An FsNode as nc_py_api gives it, its path from the person's root."""
    return SimpleNamespace(
        user_path=path.strip("/") + ("/" if is_dir else ""),
        full_path=f"files/anna/{path.strip('/')}",
        is_dir=is_dir,
        is_shared=shared,
        info=SimpleNamespace(fileid=file_id, size=size),
    )


class Dav:  # pylint: disable=too-many-instance-attributes
    """The person's files on the disk, with a trash bin and versions: by_id (SEARCH), listdir
    (PROPFIND), download2stream (GET, in pieces), delete (into the trash bin), and
    trashbin_list and trashbin_delete; a file's versions go into the trash bin with it, as in
    Nextcloud. fail names what fails."""

    def __init__(self, root: Path):
        self.root = root
        self.ids: dict[str, int] = {}
        self.shared: set[str] = set()
        self.trash: list[SimpleNamespace] = []
        self.versions: dict[int, list[str]] = {}
        self.fail: dict[str, str] = {}
        self.mounts: dict[str, str] = {}
        self.uploads: list[str] = []
        self._session = SimpleNamespace(adapter_dav=SimpleNamespace(request=self._propfind))

    def id_of(self, path: str) -> int:
        return self.ids.setdefault("/" + path.strip("/"), len(self.ids) + 1)

    def node_of(self, path: str):
        place = self.root / path.strip("/")
        size = 0 if place.is_dir() else place.stat().st_size
        found = node(path, self.id_of(path), size, place.is_dir(), "/" + path.strip("/") in self.shared)
        mounted = any(("/" + path.strip("/") + "/").startswith(mount + "/") for mount in self.mounts)
        return SimpleNamespace(**vars(found), is_mounted=mounted)

    async def _propfind(self, method, path, **kwargs):  # pylint: disable=unused-argument
        user_path = "/" + unquote(path).split("/", 3)[-1].strip("/")
        kind = next((kind for mount, kind in self.mounts.items() if (user_path + "/").startswith(mount + "/")), "")
        text = f"<d:multistatus><nc:mount-type>{kind}</nc:mount-type></d:multistatus>"
        return SimpleNamespace(status_code=207, text=text)

    async def by_id(self, file_id):
        for path, known in self.ids.items():
            if known == file_id and (self.root / path.strip("/")).exists():
                return self.node_of(path)
        return None

    async def listdir(self, path, depth=1):  # pylint: disable=unused-argument
        place = self.root / path.strip("/")
        return [self.node_of(f"{path.strip('/')}/{child.name}") for child in sorted(place.iterdir())]

    async def download2stream(self, path, fp, **kwargs):  # pylint: disable=unused-argument
        if self.fail.get("GET") == path.strip("/"):
            fp.write(b"part of it")
            raise NextcloudException(500, "Internal Server Error")
        with open(self.root / path.strip("/"), "rb") as file:
            for piece in iter(lambda: file.read(1 << 20), b""):
                fp.write(piece)

    async def delete(self, path, not_fail=False):  # pylint: disable=unused-argument
        if self.fail.get("DELETE") == path.strip("/"):
            raise NextcloudException(423, "Locked")
        place = self.root / path.strip("/")
        info = SimpleNamespace(trashbin_original_location=path.strip("/"), trashbin_deletion_time=10**10)
        versions = self.versions.pop(self.id_of(path), [])
        self.trash.append(SimpleNamespace(user_path=f"{place.name}.d1", info=info, versions=versions))
        if place.is_dir():
            shutil.rmtree(place)
        else:
            place.unlink()

    async def by_path(self, path):
        if not (self.root / path.strip("/")).exists():
            raise NextcloudException(404, "Not Found")
        return self.node_of(path)

    async def makedirs(self, path, exist_ok=False):  # pylint: disable=unused-argument
        (self.root / path.strip("/")).mkdir(parents=True, exist_ok=True)

    async def upload(self, path, content):
        place = self.root / path.strip("/")
        place.write_bytes(content)
        self.uploads.append(path)
        return SimpleNamespace(file_id=f"{self.id_of(path):08d}ocabc")

    async def upload_stream(self, path, fp, **kwargs):  # pylint: disable=unused-argument
        with open(self.root / path.strip("/"), "wb") as file:
            for piece in iter(lambda: fp.read(1 << 20), b""):
                file.write(piece)
        if self.fail.get("PUT") == path.strip("/"):
            with open(self.root / path.strip("/"), "ab") as file:
                file.write(b"!")
        self.uploads.append(path)
        return SimpleNamespace(file_id=f"{self.id_of(path):08d}ocabc")

    async def trashbin_list(self):
        return list(self.trash)

    async def trashbin_delete(self, entry, not_fail=False):  # pylint: disable=unused-argument
        self.trash.remove(entry)


class FakeNc:  # pylint: disable=too-few-public-methods
    """nc as the move uses it."""

    def __init__(self, files: Dav):
        self.files = files
        self._session = files._session
        self.user = ""

    async def set_user(self, user):
        self.user = user


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """A library with anna's set up, her files in tmp_path/files, the moves."""
    monkeypatch.setattr(crypt, "SCRYPT", CHEAP)
    monkeypatch.setattr(library, "LIMITS", {"person": 0, "total": 0})
    store = Store(str(tmp_path / "db"))
    keys = Keyring(store)
    shelf = Shelf(store, tmp_path / "library", keys)
    token = set_up(keys, "anna", "anna's password")
    files = tmp_path / "files"
    (files / "Docs" / "Tax").mkdir(parents=True)
    (files / "Docs" / "Tax" / "return.pdf").write_bytes(b"%PDF tax")
    (files / "Docs" / "notes.txt").write_bytes(b"notes")
    (files / "photo.jpg").write_bytes(b"jpeg")
    dav = Dav(files)
    for path in ("/Docs", "/Docs/Tax", "/Docs/Tax/return.pdf", "/Docs/notes.txt", "/photo.jpg"):
        dav.id_of(path)
    moves = Moves(shelf, lambda: FakeNc(dav), asyncio.run)
    monkeypatch.setitem(common.STATE, "shelf", shelf)
    monkeypatch.setitem(common.STATE, "moves", moves)
    monkeypatch.setitem(common.STATE, "keyring", keys)
    return SimpleNamespace(shelf=shelf, keys=keys, token=token, dav=dav, moves=moves, files=files)


def move(app, *paths: str, folder: str = "/From Files"):
    """Proposes and runs a move of the paths (as the Files action sends them); the move."""
    selected = [{"fileId": app.dav.ids[path], "name": path.rsplit("/", 1)[-1], "fileType": "file"} for path in paths]
    proposed = app.moves.propose("anna", selected)
    app.moves.start(proposed, folder).join()
    return proposed


def library_files(app) -> dict[str, bytes]:
    opened = app.keys.session("anna", app.token)
    return {
        entry["path"]: b"".join(app.shelf.content(opened, entry["id"], 0, entry["size"] - 1))
        for entry in app.shelf.index(opened).values()
        if not entry["dir"]
    }


def test_files_and_folders_move_with_their_folders_and_leave_no_trace(app):
    app.dav.versions[app.dav.ids["/Docs/notes.txt"]] = ["1700000000", "1700000100"]
    done = move(app, "/Docs", "/photo.jpg")
    assert done.problem == "" and done.ended
    assert [(item.path, item.state) for item in done.items] == [
        ("/Docs/Tax/return.pdf", MOVED),
        ("/Docs/notes.txt", MOVED),
        ("/photo.jpg", MOVED),
    ]
    assert library_files(app) == {
        "/From Files/Docs/Tax/return.pdf": b"%PDF tax",
        "/From Files/Docs/notes.txt": b"notes",
        "/From Files/photo.jpg": b"jpeg",
    }
    assert not any(app.files.iterdir()), "the originals and the folders the move emptied are gone"
    assert app.dav.trash == [], "and purged from the trash bin"
    assert app.dav.versions == {}, "the versions went into the trash bin with it, and were purged"


def test_a_name_taken_in_the_library_is_numbered(app):
    app.shelf.put("anna", "/In/photo.jpg", io.BytesIO(b"kept"))
    move(app, "/photo.jpg", folder="/In")
    assert library_files(app) == {"/In/photo.jpg": b"kept", "/In/photo (2).jpg": b"jpeg"}


@pytest.mark.parametrize("failing", ["GET", "DELETE"])
def test_a_file_that_fails_stays_where_it_was(app, failing):
    app.dav.fail[failing] = "Docs/notes.txt"
    notes_id = app.dav.ids["/Docs/notes.txt"]
    app.dav.versions[notes_id] = ["1700000000"]
    done = move(app, "/Docs/notes.txt", "/photo.jpg")
    notes, photo = done.items
    assert notes.state == FAILED and notes.message
    assert (app.files / "Docs" / "notes.txt").read_bytes() == b"notes", "the original stays"
    assert app.dav.versions[notes_id] == ["1700000000"], "and its versions"
    assert "/From Files/notes.txt" not in library_files(app), "the library's copy removed again"
    assert photo.state == MOVED and not (app.files / "photo.jpg").exists()
    assert not list(app.shelf.root.rglob("*.part")), "nothing half-written left"


def test_a_file_shared_with_the_person_or_gone_is_not_moved(app):
    app.dav.shared.add("/photo.jpg")
    proposed = app.moves.propose("anna", [{"fileId": app.dav.ids["/photo.jpg"]}, {"fileId": 999}])
    app.moves.start(proposed).join()
    assert [(item.state, item.message) for item in proposed.items] == [
        (FAILED, "Shared with you: only its owner can move it."),
        (FAILED, "Not found in your files."),
    ]
    assert (app.files / "photo.jpg").exists()


def test_a_team_folder_is_not_moved_an_external_storage_is(app):
    (app.files / "Team").mkdir()
    (app.files / "Team" / "plan.txt").write_bytes(b"ours")
    (app.files / "Docs" / "Team2").mkdir()
    (app.files / "Docs" / "Team2" / "shared.txt").write_bytes(b"ours too")
    (app.files / "USB").mkdir()
    (app.files / "USB" / "scan.pdf").write_bytes(b"%PDF mine")
    app.dav.mounts = {"/Team": "group", "/Docs/Team2": "group", "/USB": "external"}
    for path in ("/Team", "/Team/plan.txt", "/Docs/Team2/shared.txt", "/USB", "/USB/scan.pdf"):
        app.dav.id_of(path)
    done = move(app, "/Team", "/Docs", "/USB")
    states = {item.path: (item.state, item.message) for item in done.items}
    assert states["Team/"] == (FAILED, "In a team folder: it belongs to everyone who shares the folder.")
    assert "/Docs/Team2/shared.txt" not in states, "a team folder below a folder moved is left out"
    assert states["/USB/scan.pdf"][0] == MOVED and states["/Docs/notes.txt"][0] == MOVED
    assert (app.files / "Team" / "plan.txt").exists() and (app.files / "Docs" / "Team2" / "shared.txt").exists()


def test_a_move_before_the_setup_says_so_and_moves_nothing(app):
    proposed = app.moves.propose("ben", [{"fileId": app.dav.ids["/photo.jpg"]}])
    app.moves.start(proposed).join()
    assert "Set up your library" in proposed.problem and (app.files / "photo.jpg").exists()


def test_a_large_file_moves_in_bounded_memory(app):
    size = 50 * 1000**2
    with open(app.files / "video.mp4", "wb") as out:
        for _ in range(size // 1000**2):
            out.write(os.urandom(1000**2))
    app.dav.id_of("/video.mp4")
    tracemalloc.start()
    done = move(app, "/video.mp4")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert done.items[0].state == MOVED, done.items[0].message
    assert peak < 12 * 1000**2, f"{peak} bytes at most in memory"
    entry = next(entry for entry in app.shelf.index(app.keys.session("anna", app.token)).values() if entry["size"])
    assert entry["path"] == "/From Files/video.mp4" and entry["size"] == size


def test_the_action_proposes_and_the_page_starts_a_persons_own_move(app):
    answer = asyncio.run(
        routes.from_files(
            routes.Action(files=[routes.Node(fileId=app.dav.ids["/photo.jpg"], name="photo.jpg")]), nextcloud()
        )
    )
    assert answer == {"redirect_handler": "harvester"}
    proposed = asyncio.run(routes.list_moves(nextcloud()))
    assert len(proposed) == 1 and not proposed[0]["started"]
    assert asyncio.run(routes.list_moves(nextcloud("ben"))) == [], "not ben's"
    with pytest.raises(HTTPException) as other:
        asyncio.run(routes.start_move(proposed[0]["id"], routes.Start(), nextcloud("ben")))
    assert other.value.status_code == 404
    with pytest.raises(HTTPException) as bad:
        asyncio.run(routes.start_move(proposed[0]["id"], routes.Start(folder="/../x"), nextcloud()))
    assert bad.value.status_code == 422
    started = asyncio.run(routes.start_move(proposed[0]["id"], routes.Start(folder="/Pictures"), nextcloud()))
    assert started["folder"] == "/Pictures" and started["started"]
    with pytest.raises(HTTPException) as again:
        asyncio.run(routes.start_move(proposed[0]["id"], routes.Start(), nextcloud()))
    assert again.value.status_code == 409


def test_the_admins_reset_deletes_a_library_entirely_and_only_with_confirm(app):
    app.shelf.put("anna", "/x.txt", io.BytesIO(b"x"))
    asked = routes.Occ(occ=routes.OccInput(arguments={"user": "anna"}, options={"confirm": False}))
    said = asyncio.run(routes.reset_library(asked, nextcloud("")))
    assert b"--confirm" in said.body and app.shelf.find("anna", "/x.txt") is not None
    with pytest.raises(HTTPException) as person:
        asyncio.run(routes.reset_library(asked, nextcloud("anna")))
    assert person.value.status_code == 403, "an occ command, not a person's request"
    asked.occ.options = {"confirm": True}
    asyncio.run(routes.reset_library(asked, nextcloud("")))
    assert not app.keys.is_set_up("anna") and app.shelf.usage("anna") == 0
    assert not (app.shelf.root / "anna").exists()
    assert routes.occ_command()["callback_url"].startswith("/occ/"), "outside /api: no proxy route reaches it"


def to_files(app, *paths: str, folder: str = "/Back"):
    """Moves the library's files and folders at paths into the folder of Files; the move."""
    opened = app.keys.session("anna", app.token)
    by_path = {entry["path"]: entry["id"] for entry in app.shelf.index(opened).values()}
    moved = app.moves.export(opened, app.token, [by_path[path] for path in paths], folder)
    for _ in range(500):
        if moved.ended:
            break
        time.sleep(0.01)
    return moved


def test_files_and_folders_move_to_files_checked_and_leave_the_library(app):
    app.shelf.put("anna", "/Trip/a.jpg", io.BytesIO(b"jpeg a"))
    app.shelf.put("anna", "/Trip/day 2/b.jpg", io.BytesIO(b"jpeg b"))
    app.shelf.put("anna", "/notes.txt", io.BytesIO(b"notes"))
    (app.files / "Back").mkdir()
    (app.files / "Back" / "notes.txt").write_bytes(b"other")
    done = to_files(app, "/Trip", "/notes.txt")
    assert done.kind == OUT and done.problem == "" and done.ended
    assert [(item.path, item.target, item.state) for item in done.items] == [
        ("/Trip/a.jpg", "/Back/Trip/a.jpg", MOVED),
        ("/Trip/day 2/b.jpg", "/Back/Trip/day 2/b.jpg", MOVED),
        ("/notes.txt", "/Back/notes (2).txt", MOVED),
    ]
    assert (app.files / "Back" / "Trip" / "day 2" / "b.jpg").read_bytes() == b"jpeg b"
    assert (app.files / "Back" / "notes.txt").read_bytes() == b"other", "a name taken is numbered"
    assert (app.files / "Back" / "notes (2).txt").read_bytes() == b"notes"
    assert app.shelf.index(app.keys.session("anna", app.token)) == {}, "the files and the emptied folder are gone"


def test_a_large_file_streams_to_files_and_a_copy_that_differs_stays_in_the_library(app, monkeypatch):
    monkeypatch.setattr(nextcloud_module, "SINGLE_PUT", 1000)
    data = os.urandom(3 * library.PIECE + 5)
    app.shelf.put("anna", "/big.bin", io.BytesIO(data))
    app.shelf.put("anna", "/odd.bin", io.BytesIO(b"x" * 2000))
    app.dav.fail["PUT"] = "odd.bin"
    done = to_files(app, "/big.bin", "/odd.bin", folder="/")
    big, odd = done.items
    assert big.state == MOVED and (app.files / "big.bin").read_bytes() == data
    assert odd.state == FAILED and "does not match" in odd.message
    assert not (app.files / "odd.bin").exists(), "the copy that differs is deleted from Files"
    assert library_files(app) == {"/odd.bin": b"x" * 2000}, "and the file stays in the library"


def test_a_move_to_files_needs_the_library_unlocked_and_a_sound_folder(app):
    app.shelf.put("anna", "/a.txt", io.BytesIO(b"a"))
    opened = app.keys.session("anna", app.token)
    with pytest.raises(library.BadPath):
        app.moves.export(opened, app.token, [next(iter(app.shelf.index(opened)))], "/../etc")
    app.keys.lock(app.token)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            routes.to_files(
                routes.ToFiles(ids=[1], folder="/"), Request({"type": "http", "headers": []}), nextcloud(), app.token
            )
        )
    assert refused.value.status_code == 403
