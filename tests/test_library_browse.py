# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browsing the private library (routes/library.py, libraryzip.py): Recent, the folders offered,
a file's details (the job and where the file came from), several files moved, deleted and
labelled at once, and a ZIP file of files and folders streamed as it is made."""

# pylint: disable=missing-function-docstring,consider-using-with

import asyncio
import io
import time
import zipfile

import pytest
from fastapi import HTTPException
from test_library import fixture_app, library_job, nextcloud, request  # pylint: disable=unused-import

from harvester import library, libraryzip
from harvester.routes import library as routes


def put(app, path: str, data: bytes, at: float | None = None, job: int | None = None) -> int:
    file_id = app["shelf"].put("anna", path, io.BytesIO(data), job)
    if at is not None:
        with app["store"].writing() as db:
            db.execute("UPDATE library_files SET at = ? WHERE id = ?", (at, file_id))
    return file_id


def listing(app, **query) -> dict:
    return asyncio.run(routes.list_library(request(), nextcloud(), x_harvester_library=app["token"], **query))


def batch(app, **body) -> dict:
    return asyncio.run(routes.library_batch(routes.Batch(**body), request(), nextcloud(), app["token"]))


def test_recent_lists_the_newest_files_of_the_whole_library_and_the_folders_are_offered(app):
    put(app, "/Old/a.txt", b"a", at=1000)
    put(app, "/New/deep/b.txt", b"b", at=3000)
    put(app, "/c.txt", b"c", at=2000)
    recent = listing(app, recent=True)
    assert [entry["path"] for entry in recent["entries"]] == ["/New/deep/b.txt", "/c.txt", "/Old/a.txt"]
    assert recent["folders"] == ["/New", "/New/deep", "/Old"]
    assert [entry["name"] for entry in listing(app)["entries"]] == ["New", "Old", "c.txt"]


def test_a_files_details_name_its_job_and_where_it_came_from(app):
    job = library_job(app["store"])
    file_id = put(app, "/Mag/a.txt", b"a", job=job.id)
    with app["store"].writing() as db:
        db.execute(
            "INSERT INTO placed (job, key, file, vals, tags) VALUES (?, ?, ?, ?, ?)",
            (job.id, "https://example.org/a.txt", file_id, '{"year": "2026"}', "[]"),
        )
    details = asyncio.run(routes.library_details(file_id, request(), nextcloud(), app["token"]))
    assert details["path"] == "/Mag/a.txt" and details["job_name"] == "Mag"
    assert details["source"] == {"key": "https://example.org/a.txt", "values": {"year": "2026"}}
    own = put(app, "/mine.txt", b"m")
    assert asyncio.run(routes.library_details(own, request(), nextcloud(), app["token"]))["source"] is None
    with pytest.raises(HTTPException) as missing:
        asyncio.run(routes.library_details(9999, request(), nextcloud(), app["token"]))
    assert missing.value.status_code == 404


def test_several_files_are_moved_labelled_and_deleted_at_once(app):
    one, two = put(app, "/a.txt", b"a"), put(app, "/b.txt", b"b")
    folder = app["shelf"].mkdir("anna", "/Box")
    inner = put(app, "/Box/c.txt", b"c")
    assert put(app, "/Into/x.txt", b"x")
    moved = batch(app, ids=[one, two, 9999], action="move", folder="/Into")
    assert moved["done"] == [one, two] and [failed["id"] for failed in moved["failed"]] == [9999]
    assert [entry["name"] for entry in listing(app, folder="/Into")["entries"]] == ["a.txt", "b.txt", "x.txt"]
    taken = batch(app, ids=[put(app, "/a.txt", b"again")], action="move", folder="/Into")
    assert "taken" in taken["failed"][0]["error"]
    batch(app, ids=[one, two], action="label", add=[" trip ", "sea"])
    batch(app, ids=[two], action="label", remove=["sea"])
    labels = {entry["name"]: entry["labels"] for entry in listing(app, folder="/Into")["entries"]}
    assert labels == {"a.txt": ["trip", "sea"], "b.txt": ["trip"], "x.txt": []}
    with pytest.raises(HTTPException) as bad:
        batch(app, ids=[one], action="label", add=["a\nb"])
    assert bad.value.status_code == 422
    deleted = batch(app, ids=[folder, inner, one], action="delete")
    assert deleted == {"done": [folder, inner, one], "failed": []}, "the folder's file went with it"
    assert sorted(entry["path"] for entry in listing(app, recent=True)["entries"]) == [
        "/Into/b.txt",
        "/Into/x.txt",
        "/a.txt",
    ]


def zipped(app, ids: list[int]) -> tuple[str, zipfile.ZipFile]:
    asked = asyncio.run(routes.library_zip_ticket(routes.Zip(ids=ids), request(), nextcloud(), app["token"]))
    answer = asyncio.run(routes.library_zip(nextcloud(), ticket=asked["ticket"]))

    async def body() -> bytes:
        return b"".join([piece async for piece in answer.body_iterator])

    assert answer.media_type == "application/zip"
    return asked["name"], zipfile.ZipFile(io.BytesIO(asyncio.run(body())))


def test_a_zip_holds_files_and_folders_below_their_names_streamed_once(app):
    big = bytes(range(256)) * (3 * library.PIECE // 256 + 7)
    trip = app["shelf"].mkdir("anna", "/Trip")
    put(app, "/Trip/a.jpg", big)
    put(app, "/Trip/day 2/b.jpg", b"b")
    app["shelf"].mkdir("anna", "/Trip/empty")
    loose = put(app, "/Trip.txt", b"t")
    other = put(app, "/Other/a.jpg", b"other a")
    name, archive = zipped(app, [trip])
    assert name == "Trip.zip"
    assert sorted(archive.namelist()) == ["Trip/a.jpg", "Trip/day 2/b.jpg", "Trip/empty/"]
    assert archive.read("Trip/a.jpg") == big and archive.testzip() is None
    name, archive = zipped(app, [loose])
    assert name == "Trip.zip" and archive.read("Trip.txt") == b"t"
    name, archive = zipped(app, [app["shelf"].find("anna", "/Trip/a.jpg") or 0, other])
    assert name == "Library.zip"
    assert sorted(archive.namelist()) == ["a (2).jpg", "a.jpg"], "a name taken twice is numbered"
    asked = asyncio.run(routes.library_zip_ticket(routes.Zip(ids=[loose]), request(), nextcloud(), app["token"]))
    asyncio.run(routes.library_zip(nextcloud(), ticket=asked["ticket"]))
    with pytest.raises(HTTPException) as again:
        asyncio.run(routes.library_zip(nextcloud(), ticket=asked["ticket"]))
    assert again.value.status_code == 403, "a ticket opens one download"


def test_a_zip_is_anna_s_alone_and_lapses(app, monkeypatch):
    file_id = put(app, "/a.txt", b"a")
    asked = asyncio.run(routes.library_zip_ticket(routes.Zip(ids=[file_id]), request(), nextcloud(), app["token"]))
    with pytest.raises(HTTPException):
        asyncio.run(routes.library_zip(nextcloud("ben"), ticket=asked["ticket"]))
    with pytest.raises(HTTPException) as missing:
        asyncio.run(routes.library_zip_ticket(routes.Zip(ids=[9999]), request(), nextcloud(), app["token"]))
    assert missing.value.status_code == 404
    asked = asyncio.run(routes.library_zip_ticket(routes.Zip(ids=[file_id]), request(), nextcloud(), app["token"]))
    monkeypatch.setattr(time, "monotonic", lambda: 10**12)
    with pytest.raises(HTTPException):
        asyncio.run(routes.library_zip(nextcloud(), ticket=asked["ticket"]))
    assert libraryzip.MOST >= 1000
