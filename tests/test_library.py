# SPDX-License-Identifier: AGPL-3.0-or-later
"""Save to and Tag as (destinations.py), Harvester's private library (library.py) and its routes
(routes/library.py): a job's files in the app's storage per person, never another's; paths
that would leave a person's folder refused; the limits; the labels; a deleted file fetched again
as the job's option says; the password, the lock and its lapse; a file's parts by Range; the
job's files with their labels."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import io
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_runner import FakeNextcloud, FakeSource

import main
from harvester import destinations, library, places
from harvester import executor as executor_module
from harvester.library import BadPath, LibraryFull, Locks, PrivateLibrary, Shelf, clean, person_folder
from harvester.routes import common
from harvester.routes import library as routes
from harvester.store import Job, Store


class QuickSource(FakeSource):
    """The fake source without its pause between files."""

    pause = 0.0


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """The app's state as at start: a store, the library under tmp_path, the locks."""
    store = Store(str(tmp_path / "db"))
    shelf = Shelf(store, tmp_path / "library")
    monkeypatch.setitem(common.STATE, "store", store)
    monkeypatch.setitem(common.STATE, "shelf", shelf)
    monkeypatch.setitem(common.STATE, "locks", Locks(store))
    monkeypatch.setattr(places, "SHELF", [shelf])
    monkeypatch.setitem(executor_module.SOURCES, "fake", QuickSource)
    monkeypatch.setattr(library, "LIMITS", {"person": 10 * library.GB, "total": 0})
    return common.STATE


def nextcloud(user: str = "anna") -> FakeNextcloud:
    nc = FakeNextcloud()
    nc._user = user
    return nc


def library_job(store: Store, user: str = "anna", **options) -> Job:
    options = {"save_to": "library", "tag_as": "labels"} | options
    return store.save(
        Job(
            user=user,
            name="Mag",
            source="fake",
            target="/Mag",
            schedule="0 6 * * *",
            tags=["{letter}"],
            options=options,
        )
    )


def run(app, job: Job) -> Job:
    executor_module.Executor(app["store"], lambda: nextcloud(job.user)).run(job)
    return app["store"].job(job.id or 0) or job


def request(range_header: str = "") -> Request:
    headers = [(b"range", range_header.encode())] if range_header else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""})


async def body(response) -> bytes:
    return b"".join([piece async for piece in response.body_iterator])


def test_the_defaults_are_todays_and_the_destination_decides_the_tags():
    assert destinations.save_to({}) == "files" and destinations.tag_as({}) == "nextcloud"
    assert destinations.tag_as({"save_to": "library"}) == "labels", "no Nextcloud tags in the library"
    assert destinations.tag_as({"save_to": "library", "tag_as": "nextcloud"}) == "labels"
    assert destinations.tag_as({"tag_as": "none"}) == "none"
    job = Job(user="u", name="n", source="fake", target="/x", schedule="", tags=["a"], options={"tag_as": "none"})
    assert destinations.effective(job).tags == []
    plain = Job(user="u", name="n", source="fake", target="/x", schedule="", tags=["a"])
    assert destinations.effective(plain) is plain, "a job of today's kind runs as it did"


def test_the_form_is_refused_nextcloud_tags_outside_the_files():
    form = main.JobIn(name="n", source="feed", target="/Mag", options={"url": "https://e.example/feed"})
    common.checked(form)
    form.options |= {"save_to": "library", "tag_as": "nextcloud"}
    with pytest.raises(HTTPException, match="Nextcloud tags only"):
        common.checked(form)
    form.options |= {"tag_as": "labels"}
    common.checked(form)
    form.options |= {"save_to": "elsewhere"}
    with pytest.raises(HTTPException, match="Save to"):
        common.checked(form)


def test_paths_never_leave_a_persons_folder(tmp_path):
    for path in ["/../x", "/a/../../x", "/.hidden", "/a\\b", "/a\x00b", "/" + "x" * 300]:
        with pytest.raises(BadPath):
            clean(path)
    assert clean("//Mag///2024/x.pdf") == "/Mag/2024/x.pdf"
    assert person_folder("../anna") == "%2E.%2Fanna"
    assert person_folder("a/b") == "a%2Fb" and "/" not in person_folder("../../etc")
    shelf = Shelf(Store(str(tmp_path / "db")), tmp_path / "library")
    assert shelf.disk("anna", "/Mag/x.pdf") == tmp_path / "library" / "anna" / "Mag" / "x.pdf"
    with pytest.raises(BadPath):
        shelf.put("anna", "/Mag/../../ben/x", io.BytesIO(b"x"))


def test_a_run_saves_into_the_library_with_labels_never_into_nextcloud(app):
    job = run(app, library_job(app["store"]))
    assert job.last_status == "ok", job.last_message
    shelf: Shelf = app["shelf"]
    assert (shelf.root / "anna" / "Mag" / "a.txt").read_bytes() == b"a"
    entries = shelf.listing("anna", "/Mag")
    assert [(entry["name"], entry["labels"]) for entry in entries] == [
        ("a.txt", ["a"]),
        ("b.txt", ["b"]),
        ("c.txt", ["c"]),
    ]
    assert shelf.listing("anna", "/") == [shelf.entry("anna", shelf.find("anna", "/Mag"))], "the folder, at the top"
    assert shelf.listing("anna", "/", "b")[0]["path"] == "/Mag/b.txt", "a label's files, below the folder"
    assert shelf.labels("anna") == ["a", "b", "c"]
    assert shelf.listing("ben", "/") == [] and shelf.usage("ben") == 0, "nothing of anna's is ben's"


def test_a_file_deleted_in_the_library_comes_again_as_the_option_says(app):
    store, shelf = app["store"], app["shelf"]
    job = run(app, library_job(store))
    shelf.delete("anna", shelf.find("anna", "/Mag/a.txt"))
    assert not (shelf.root / "anna" / "Mag" / "a.txt").exists()
    run(app, job)
    assert (shelf.root / "anna" / "Mag" / "a.txt").exists(), "downloaded again (the option's default)"
    shelf.delete("anna", shelf.find("anna", "/Mag/a.txt"))
    job = store.save(Job(**(vars(job) | {"options": job.options | {"again_deleted": 0}})))
    run(app, job)
    assert shelf.find("anna", "/Mag/a.txt") is None, "the option off: a deleted file stays deleted"


def test_a_folder_deleted_takes_everything_in_it(app):
    shelf = app["shelf"]
    run(app, library_job(app["store"]))
    assert not shelf.delete("ben", shelf.find("anna", "/Mag")), "not another person's"
    assert shelf.delete("anna", shelf.find("anna", "/Mag"))
    assert shelf.listing("anna", "/") == [] and not (shelf.root / "anna" / "Mag").exists()


def test_a_run_past_the_limit_stops_and_says_why(app, monkeypatch):
    monkeypatch.setitem(library.LIMITS, "person", 2)
    job = run(app, library_job(app["store"]))
    assert job.last_status == "failed"
    assert job.last_message == "The library is full: /Mag/c.txt (1 B) would pass your limit of 2 B"
    monkeypatch.setitem(library.LIMITS, "person", 0)
    monkeypatch.setitem(library.LIMITS, "total", 1)
    with pytest.raises(LibraryFull, match="server's limit"):
        app["shelf"].put("ben", "/x", io.BytesIO(b"xx"))


def test_a_file_saved_over_keeps_its_id_and_a_file_is_no_folder(app):
    shelf = app["shelf"]
    first = shelf.put("anna", "/a/b.txt", io.BytesIO(b"1"))
    assert shelf.put("anna", "/a/b.txt", io.BytesIO(b"22")) == first and shelf.usage("anna") == 2
    with pytest.raises(BadPath):
        shelf.put("anna", "/a/b.txt/c", io.BytesIO(b"x"))
    with pytest.raises(BadPath):
        shelf.put("anna", "/a", io.BytesIO(b"x"))
    library_ = PrivateLibrary(shelf, "anna", None)
    library_.tag(first, ["x"])
    assert shelf.entry("anna", first)["labels"] == ["x"]
    assert library_.existing([first, 999]) == {first: "/a/b.txt"}
    assert PrivateLibrary(shelf, "ben", None).existing([first]) == {}, "not another person's"


def test_the_routes_serve_a_person_their_own_files_alone(app):
    run(app, library_job(app["store"]))
    file_id = app["shelf"].find("anna", "/Mag/a.txt")
    listed = asyncio.run(routes.list_library(nextcloud(), folder="/Mag"))
    assert [entry["name"] for entry in listed["entries"]] == ["a.txt", "b.txt", "c.txt"]
    assert listed["labels"] == ["a", "b", "c"]
    with pytest.raises(HTTPException) as refused:
        asyncio.run(routes.library_file(file_id, request(), nextcloud("ben")))
    assert refused.value.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(routes.delete_library_file(file_id, nextcloud("ben")))
    assert asyncio.run(routes.list_library(nextcloud("ben")))["entries"] == []
    with pytest.raises(HTTPException) as bad:
        asyncio.run(routes.list_library(nextcloud(), folder="/../ben"))
    assert bad.value.status_code == 422


def test_a_file_is_served_whole_or_in_part_and_never_as_a_page(app):
    shelf = app["shelf"]
    picture = shelf.put("anna", "/p.png", io.BytesIO(b"0123456789"))
    page = shelf.put("anna", "/x.html", io.BytesIO(b"<script>alert(1)</script>"))
    whole = asyncio.run(routes.library_file(picture, request(), nextcloud()))
    assert whole.status_code == 200 and asyncio.run(body(whole)) == b"0123456789"
    assert whole.media_type == "image/png" and whole.headers["content-disposition"].startswith("inline")
    assert (
        "sandbox" in whole.headers["content-security-policy"] and whole.headers["x-content-type-options"] == "nosniff"
    )
    part = asyncio.run(routes.library_file(picture, request("bytes=2-4"), nextcloud()))
    assert part.status_code == 206 and asyncio.run(body(part)) == b"234"
    assert part.headers["content-range"] == "bytes 2-4/10"
    end = asyncio.run(routes.library_file(picture, request("bytes=-3"), nextcloud()))
    assert asyncio.run(body(end)) == b"789"
    assert asyncio.run(routes.library_file(picture, request("bytes=20-"), nextcloud())).status_code == 416
    html = asyncio.run(routes.library_file(page, request(), nextcloud()))
    assert html.media_type == "application/octet-stream" and html.headers["content-disposition"].startswith(
        "attachment"
    )
    saved = asyncio.run(routes.library_file(picture, request(), nextcloud(), download=True))
    assert saved.headers["content-disposition"].startswith("attachment")


def test_a_password_locks_the_library_until_unlocked_and_after_its_minutes(app, monkeypatch):
    asyncio.run(routes.set_library_password(routes.Password(password="correct horse", minutes=5), nextcloud()))
    state = asyncio.run(routes.library_state(nextcloud()))
    assert state["password"] and not state["unlocked"]
    for call in (
        lambda: routes.list_library(nextcloud()),
        lambda: routes.library_file(1, request(), nextcloud()),
        lambda: routes.delete_library_file(1, nextcloud()),
    ):
        with pytest.raises(HTTPException) as locked:
            asyncio.run(call())
        assert locked.value.status_code == 403
    with pytest.raises(HTTPException, match="Wrong password"):
        asyncio.run(routes.unlock_library(routes.Unlock(password="wrong"), nextcloud()))
    token = asyncio.run(routes.unlock_library(routes.Unlock(password="correct horse"), nextcloud()))["token"]
    assert asyncio.run(routes.list_library(nextcloud(), x_harvester_library=token))["entries"] == []
    assert asyncio.run(routes.list_library(nextcloud("ben")))["entries"] == [], "ben has no password"
    now = time.monotonic()
    monkeypatch.setattr(library.time, "monotonic", lambda: now + 5 * 60 + 1)
    with pytest.raises(HTTPException) as lapsed:
        asyncio.run(routes.list_library(nextcloud(), x_harvester_library=token))
    assert lapsed.value.status_code == 403


def test_a_tokens_person_is_checked_and_wrong_passwords_wait(app):
    locks: Locks = app["locks"]
    locks.set_password("anna", "", "anna's password", 15)
    locks.set_password("ben", "", "ben's password", 15)
    token = locks.unlock("anna", "anna's password")
    assert locks.unlocked("anna", token) and not locks.unlocked("ben", token), "anna's token is not ben's"
    with pytest.raises(library.Locked):
        locks.set_password("anna", "wrong", "", 15)
    for _ in range(library.MAX_FAILED):
        with pytest.raises(library.Locked):
            locks.unlock("ben", "wrong")
    with pytest.raises(library.Locked, match="Too many"):
        locks.unlock("ben", "ben's password")
    locks.set_password("anna", "anna's password", "", 15)
    assert locks.setting("anna") is None and locks.unlocked("anna", ""), "no password: open"
    assert "anna's password" not in str(app["store"].rows("SELECT * FROM library_locks"))


def test_the_jobs_files_with_their_labels(app):
    job = run(app, library_job(app["store"]))
    listed = asyncio.run(routes.job_files(job.id or 0, nextcloud()))
    assert sorted(entry["path"] for entry in listed["files"]) == ["/Mag/a.txt", "/Mag/b.txt", "/Mag/c.txt"]
    assert listed["labels"] == ["a", "b", "c"]
    assert [entry["path"] for entry in asyncio.run(routes.job_files(job.id or 0, nextcloud(), label="b"))["files"]] == [
        "/Mag/b.txt"
    ]
    with pytest.raises(HTTPException):
        asyncio.run(routes.job_files(job.id or 0, nextcloud("ben")))


def test_another_save_to_forgets_where_the_files_are_not_that_they_were_fetched(app):
    store = app["store"]
    job = run(app, library_job(store))
    assert store.placed(job.id or 0)
    saved = Job(**(vars(job) | {"options": {"save_to": "files"}}))
    main.forget_changed(job, saved)
    assert store.placed(job.id or 0) == {} and store.seen(job.id or 0, "a")
    same = Job(**(vars(job) | {"options": dict(job.options)}))
    store.record_placed(job.id or 0, "a", 1, {}, [])
    main.forget_changed(job, same)
    assert store.placed(job.id or 0), "the same Save to keeps them"


def test_recipe_files_never_carry_where_the_files_go():
    from harvester import recipe_file  # pylint: disable=import-outside-toplevel

    known = recipe_file.known_options("feed")
    assert "tag_as" in known and not {"save_to", "vault", "vault_target", "vault_verify"} & known
