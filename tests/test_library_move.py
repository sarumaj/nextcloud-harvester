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
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from nc_py_api import NextcloudException
from test_library import CHEAP, nextcloud, set_up

from harvester import library
from harvester import librarycrypt as crypt
from harvester.library import Shelf
from harvester.librarykeys import Keyring
from harvester.librarymove import FAILED, MOVED, Moves
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
    (PROPFIND), download2stream (GET, in pieces), delete (into the trash bin), get_versions and
    the versions' DELETE, trashbin_list and trashbin_delete. fail names what fails."""

    def __init__(self, root: Path):
        self.root = root
        self.ids: dict[str, int] = {}
        self.shared: set[str] = set()
        self.trash: list[SimpleNamespace] = []
        self.versions: dict[int, list[str]] = {}
        self.fail: dict[str, str] = {}
        self.deleted_versions: list[str] = []
        self._session = SimpleNamespace(adapter_dav=SimpleNamespace(delete=self._delete_version))

    def id_of(self, path: str) -> int:
        return self.ids.setdefault("/" + path.strip("/"), len(self.ids) + 1)

    def node_of(self, path: str):
        place = self.root / path.strip("/")
        size = 0 if place.is_dir() else place.stat().st_size
        return node(path, self.id_of(path), size, place.is_dir(), "/" + path.strip("/") in self.shared)

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
        self.trash.append(SimpleNamespace(user_path=f"{place.name}.d1", info=info))
        if place.is_dir():
            shutil.rmtree(place)
        else:
            place.unlink()

    async def get_versions(self, file_object):
        file_id = file_object.info.fileid
        folder = f"versions/anna/versions/{file_id}"
        return [SimpleNamespace(full_path=folder)] + [
            SimpleNamespace(full_path=f"{folder}/{version}") for version in self.versions.get(file_id, [])
        ]

    async def _delete_version(self, path):
        if self.fail.get("VERSIONS"):
            return SimpleNamespace(status_code=500)
        self.deleted_versions.append(path)
        return SimpleNamespace(status_code=204)

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
    notes = app.dav.ids["/Docs/notes.txt"]
    assert sorted(app.dav.deleted_versions) == [
        f"/versions/anna/versions/{notes}/1700000000",
        f"/versions/anna/versions/{notes}/1700000100",
    ], "each version deleted, not the versions' folder"


def test_a_name_taken_in_the_library_is_numbered(app):
    app.shelf.put("anna", "/In/photo.jpg", io.BytesIO(b"kept"))
    move(app, "/photo.jpg", folder="/In")
    assert library_files(app) == {"/In/photo.jpg": b"kept", "/In/photo (2).jpg": b"jpeg"}


@pytest.mark.parametrize("failing", ["GET", "DELETE", "VERSIONS"])
def test_a_file_that_fails_stays_where_it_was(app, failing):
    app.dav.fail[failing] = "Docs/notes.txt" if failing != "VERSIONS" else "yes"
    app.dav.versions[app.dav.ids["/Docs/notes.txt"]] = ["1700000000"]
    done = move(app, "/Docs/notes.txt", "/photo.jpg")
    notes, photo = done.items
    assert notes.state == FAILED and notes.message
    assert (app.files / "Docs" / "notes.txt").read_bytes() == b"notes", "the original stays"
    assert "/From Files/notes.txt" not in library_files(app), "the library's copy removed again"
    if failing != "VERSIONS":
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
