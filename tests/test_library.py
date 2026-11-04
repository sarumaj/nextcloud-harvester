# SPDX-License-Identifier: AGPL-3.0-or-later
"""Save to and Tag as (destinations.py), Harvester's private library (library.py) and its routes
(routes/library.py): a job's files encrypted in the app's storage per person, never another's;
paths refused that the library keeps no name of; the limits; the labels; a deleted file fetched
again as the job's option says; the setup, the lock and its lapse; a file's parts by Range;
uploads, folders, renames and moves; the job's files with their labels."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import io
import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_runner import FakeNextcloud, FakeSource

import main
from harvester import destinations, library, places
from harvester import executor as executor_module
from harvester import librarycrypt, librarykeys
from harvester.library import BadPath, LibraryFull, PrivateLibrary, Shelf, clean, person_folder
from harvester.librarykeys import Keyring
from harvester.routes import common
from harvester.routes import library as routes
from harvester.store import Job, Store


class QuickSource(FakeSource):
    """The fake source without its pause between files."""

    pause = 0.0


# A library password's key derivation, cheap for the tests (the costs are stored with the key).
CHEAP = {"n": 2**4, "r": 8, "p": 1}


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """The app's state as at start: a store, the keys, the library under tmp_path; anna's library
    set up (her token in TOKEN), ben's not."""
    monkeypatch.setattr(librarycrypt, "SCRYPT", CHEAP)
    store = Store(str(tmp_path / "db"))
    keys = Keyring(store)
    shelf = Shelf(store, tmp_path / "library", keys)
    monkeypatch.setitem(common.STATE, "store", store)
    monkeypatch.setitem(common.STATE, "shelf", shelf)
    monkeypatch.setitem(common.STATE, "keyring", keys)
    monkeypatch.setattr(places, "SHELF", [shelf])
    monkeypatch.setitem(executor_module.SOURCES, "fake", QuickSource)
    monkeypatch.setattr(library, "LIMITS", {"person": 10 * library.GB, "total": 0})
    common.STATE["token"] = set_up(keys, "anna", "anna's password")
    return common.STATE


def set_up(keys: Keyring, user: str, password: str) -> str:
    """Sets the person's library up (the recovery key typed back) and unlocks it: the token."""
    return keys.set_up(user, password, keys.propose(user), 15)


def session(app, user: str = "anna"):
    return app["keyring"].session(user, app["token"] if user == "anna" else app[f"token-{user}"])


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


def request(range_header: str = "", origin: str = "", cookie: str = "") -> Request:
    headers = [(b"range", range_header.encode())] if range_header else []
    headers += [(b"origin", origin.encode())] if origin else []
    headers += [(b"cookie", f"{routes.COOKIE}={cookie}".encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""})


def streamed(data: bytes) -> Request:
    """A request whose body is data, in two pieces."""
    pieces = [data[: len(data) // 2], data[len(data) // 2 :]]

    async def receive():
        piece = pieces.pop(0) if pieces else b""
        return {"type": "http.request", "body": piece, "more_body": bool(pieces)}

    return Request({"type": "http", "method": "PUT", "path": "/", "headers": [], "query_string": b""}, receive)


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


def test_paths_never_leave_a_persons_folder(app):
    for path in ["/../x", "/a/../../x", "/.hidden", "/a\\b", "/a\x00b", "/" + "x" * 300]:
        with pytest.raises(BadPath):
            clean(path)
    assert clean("//Mag///2024/x.pdf") == "/Mag/2024/x.pdf"
    assert person_folder("../anna") == "%2E.%2Fanna"
    assert person_folder("a/b") == "a%2Fb" and "/" not in person_folder("../../etc")
    shelf: Shelf = app["shelf"]
    with pytest.raises(BadPath):
        shelf.put("anna", "/Mag/../../ben/x", io.BytesIO(b"x"))
    with pytest.raises(BadPath):
        shelf.blob_path("anna", "../" + "0" * 29)


def test_nothing_of_a_name_is_on_the_disk_or_in_the_database(app, tmp_path):
    shelf: Shelf = app["shelf"]
    file_id = shelf.put("anna", "/Secret plans/Invoice 2024.pdf", io.BytesIO(b"confidential content " * 100))
    shelf.label("anna", file_id, add=["tax-office"])
    database = (tmp_path / "db").read_bytes()
    disk = b"".join(path.read_bytes() for path in (tmp_path / "library").rglob("*") if path.is_file())
    names = [str(path) for path in (tmp_path / "library").rglob("*")]
    for secret in (b"Secret plans", b"Invoice", b"tax-office", b"confidential", b"application/pdf"):
        assert secret not in database and secret not in disk, secret
        assert not any(secret.decode() in name for name in names)
    row = app["store"].rows("SELECT size FROM library_files WHERE id = ?", (file_id,))[0]
    assert row["size"] == librarycrypt.stored_size(2100) and row["size"] % (librarycrypt.CHUNK + 16) == 20
    assert shelf.entry(session(app), file_id)["size"] == 2100, "its own size, encrypted beside it"


def test_a_run_saves_into_the_library_with_labels_never_into_nextcloud(app):
    job = run(app, library_job(app["store"]))
    assert job.last_status == "ok", job.last_message
    shelf: Shelf = app["shelf"]
    opened = session(app)
    entries = shelf.listing(opened, "/Mag")
    assert [(entry["name"], entry["labels"]) for entry in entries] == [
        ("a.txt", ["a"]),
        ("b.txt", ["b"]),
        ("c.txt", ["c"]),
    ]
    assert b"".join(shelf.content(opened, entries[0]["id"], 0, 0)) == b"a"
    assert shelf.listing(opened, "/") == [shelf.entry(opened, shelf.find("anna", "/Mag"))], "the folder, at the top"
    assert shelf.listing(opened, "/", "b")[0]["path"] == "/Mag/b.txt", "a label's files, below the folder"
    assert shelf.labels(opened) == ["a", "b", "c"]
    assert shelf.usage("ben") == 0 and shelf.find("ben", "/Mag") is None, "nothing of anna's is ben's"


def test_a_job_writes_while_the_library_is_locked_and_fails_before_the_setup(app):
    app["keyring"].lock(app["token"])
    job = run(app, library_job(app["store"]))
    assert job.last_status == "ok", "the public key is enough to write"
    assert app["shelf"].find("anna", "/Mag/c.txt") is not None
    refused = run(app, library_job(app["store"], user="ben"))
    assert refused.last_status == "failed" and "Set up your library" in (refused.last_message or "")


def test_a_file_deleted_in_the_library_comes_again_as_the_option_says(app):
    store, shelf = app["store"], app["shelf"]
    job = run(app, library_job(store))
    shelf.delete("anna", shelf.find("anna", "/Mag/a.txt"))
    assert shelf.find("anna", "/Mag/a.txt") is None
    run(app, job)
    assert shelf.find("anna", "/Mag/a.txt") is not None, "downloaded again (the option's default)"
    shelf.delete("anna", shelf.find("anna", "/Mag/a.txt"))
    job = store.save(Job(**(vars(job) | {"options": job.options | {"again_deleted": 0}})))
    run(app, job)
    assert shelf.find("anna", "/Mag/a.txt") is None, "the option off: a deleted file stays deleted"


def test_a_folder_deleted_takes_everything_in_it(app, tmp_path):
    shelf = app["shelf"]
    run(app, library_job(app["store"]))
    assert not shelf.delete("ben", shelf.find("anna", "/Mag")), "not another person's"
    assert shelf.delete("anna", shelf.find("anna", "/Mag"))
    assert shelf.listing(session(app), "/") == []
    assert not [path for path in (tmp_path / "library").rglob("*") if path.is_file()], "the blobs are gone"
    assert not app["store"].rows("SELECT * FROM library_labels")


def test_a_run_past_the_limit_stops_and_says_why(app, monkeypatch):
    monkeypatch.setitem(library.LIMITS, "person", 2 * librarycrypt.stored_size(1))
    job = run(app, library_job(app["store"]))
    assert job.last_status == "failed"
    assert job.last_message == "The library is full: /Mag/c.txt (65.6 KB) would pass your limit of 131.1 KB"
    monkeypatch.setitem(library.LIMITS, "person", 0)
    monkeypatch.setitem(library.LIMITS, "total", 1)
    with pytest.raises(LibraryFull, match="server's limit"):
        app["shelf"].put("anna", "/x", io.BytesIO(b"xx"))


def test_a_file_saved_over_keeps_its_id_and_a_file_is_no_folder(app):
    shelf = app["shelf"]
    first = shelf.put("anna", "/a/b.txt", io.BytesIO(b"1"))
    library_ = PrivateLibrary(shelf, "anna", None)
    library_.tag(first, ["x"])
    assert shelf.put("anna", "/a/b.txt", io.BytesIO(b"22")) == first
    assert len(list((shelf.root / "anna").rglob("*"))) == 3, "the old blob is gone (a folder of two, the blob)"
    with pytest.raises(BadPath):
        shelf.put("anna", "/a/b.txt/c", io.BytesIO(b"x"))
    with pytest.raises(BadPath):
        shelf.put("anna", "/a", io.BytesIO(b"x"))
    opened = session(app)
    assert shelf.entry(opened, first)["labels"] == ["x"] and shelf.entry(opened, first)["size"] == 2
    assert b"".join(shelf.content(opened, first, 0, 1)) == b"22"
    library_.untag(first, ["x"])
    assert shelf.entry(opened, first)["labels"] == []
    assert library_.existing([first, 999]) == {first: ""}
    assert PrivateLibrary(shelf, "ben", None).existing([first]) == {}, "not another person's"
    with pytest.raises(library.FileGone):
        PrivateLibrary(shelf, "anna", None).tag(999, ["x"])


def test_folders_renames_moves_and_search(app):
    shelf: Shelf = app["shelf"]
    opened = session(app)
    pdf = shelf.put("anna", "/In/Tax/Return.pdf", io.BytesIO(b"%PDF"))
    shelf.label("anna", pdf, add=["Finance"])
    archive = shelf.mkdir("anna", "/Archive/2024")
    assert shelf.mkdir("anna", "/Archive/2024") == archive, "there already: its own"
    tax = shelf.find("anna", "/In/Tax")
    moved = shelf.update(opened, tax, folder="/Archive/2024")
    assert moved["path"] == "/Archive/2024/Tax"
    assert shelf.find("anna", "/Archive/2024/Tax/Return.pdf") == pdf, "the files below found at their new place"
    assert shelf.find("anna", "/In/Tax/Return.pdf") is None
    assert shelf.update(opened, pdf, name="Return 2024.pdf")["path"] == "/Archive/2024/Tax/Return 2024.pdf"
    with pytest.raises(BadPath):
        shelf.update(opened, shelf.find("anna", "/Archive"), folder="/Archive/2024/Tax")
    with pytest.raises(BadPath):
        shelf.update(opened, pdf, folder="/Nowhere")
    shelf.put("anna", "/In/x.pdf", io.BytesIO(b"x"))
    with pytest.raises(BadPath, match="taken"):
        shelf.update(opened, shelf.find("anna", "/In/x.pdf"), name="Tax", folder="/Archive/2024")
    assert [entry["path"] for entry in shelf.listing(opened, "/", query="return")] == [
        "/Archive/2024/Tax/Return 2024.pdf"
    ]
    assert [entry["path"] for entry in shelf.listing(opened, "/", query="finance")] == [
        "/Archive/2024/Tax/Return 2024.pdf"
    ], "a label matches too"
    assert shelf.listing(opened, "/In", query="return") == []
    with pytest.raises(BadPath):
        shelf.listing(opened, "/Nowhere")


def test_the_routes_serve_a_person_their_own_files_alone(app):
    run(app, library_job(app["store"]))
    app["token-ben"] = set_up(app["keyring"], "ben", "ben's password")
    file_id = app["shelf"].find("anna", "/Mag/a.txt")
    anna = app["token"]
    listed = asyncio.run(routes.list_library(request(), nextcloud(), folder="/Mag", x_harvester_library=anna))
    assert [entry["name"] for entry in listed["entries"]] == ["a.txt", "b.txt", "c.txt"]
    assert listed["labels"] == ["a", "b", "c"]
    with pytest.raises(HTTPException) as refused:
        asyncio.run(routes.library_file(file_id, request(), nextcloud("ben"), x_harvester_library=anna))
    assert refused.value.status_code == 403, "anna's token is not ben's"
    with pytest.raises(HTTPException) as refused:
        asyncio.run(routes.library_file(file_id, request(), nextcloud("ben"), x_harvester_library=app["token-ben"]))
    assert refused.value.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(routes.delete_library_file(file_id, request(), nextcloud("ben"), app["token-ben"]))
    assert app["shelf"].find("anna", "/Mag/a.txt") == file_id
    ben = asyncio.run(routes.list_library(request(), nextcloud("ben"), x_harvester_library=app["token-ben"]))
    assert ben["entries"] == []
    with pytest.raises(HTTPException) as bad:
        asyncio.run(routes.list_library(request(), nextcloud(), folder="/../ben", x_harvester_library=anna))
    assert bad.value.status_code == 422


def test_a_file_is_served_whole_or_in_part_and_never_as_a_page(app):
    shelf, anna = app["shelf"], app["token"]
    picture = shelf.put("anna", "/p.png", io.BytesIO(b"0123456789"))
    page = shelf.put("anna", "/x.html", io.BytesIO(b"<script>alert(1)</script>"))

    def get(file_id, range_header="", **query):
        return asyncio.run(
            routes.library_file(file_id, request(range_header), nextcloud(), x_harvester_library=anna, **query)
        )

    whole = get(picture)
    assert whole.status_code == 200 and asyncio.run(body(whole)) == b"0123456789"
    assert whole.media_type == "image/png" and whole.headers["content-disposition"].startswith("inline")
    assert (
        "sandbox" in whole.headers["content-security-policy"] and whole.headers["x-content-type-options"] == "nosniff"
    )
    part = get(picture, "bytes=2-4")
    assert part.status_code == 206 and asyncio.run(body(part)) == b"234"
    assert part.headers["content-range"] == "bytes 2-4/10"
    assert asyncio.run(body(get(picture, "bytes=-3"))) == b"789"
    assert get(picture, "bytes=20-").status_code == 416
    html = get(page)
    assert html.media_type == "application/octet-stream" and html.headers["content-disposition"].startswith(
        "attachment"
    )
    assert get(picture, download=True).headers["content-disposition"].startswith("attachment")


def test_a_damaged_blob_is_refused_not_served(app):
    shelf, anna = app["shelf"], app["token"]
    file_id = shelf.put("anna", "/p.txt", io.BytesIO(b"x" * 100))
    blob = app["store"].rows("SELECT blob FROM library_files WHERE id = ?", (file_id,))[0]["blob"]
    place = shelf.blob_path("anna", blob)
    data = bytearray(place.read_bytes())
    data[30] ^= 1
    place.write_bytes(bytes(data))
    with pytest.raises(HTTPException) as damaged:
        asyncio.run(routes.library_file(file_id, request(), nextcloud(), x_harvester_library=anna))
    assert damaged.value.status_code == 500


@pytest.mark.usefixtures("app")
def test_setup_shows_a_recovery_key_once_and_wants_it_back():
    ben = nextcloud("ben")
    assert not asyncio.run(routes.library_state(request(), ben))["setup"]
    recovery = asyncio.run(routes.propose_recovery(ben))["recovery"]
    assert len(recovery.replace("-", "")) == 32
    for typed in ("", "AAAA-" * 7 + "AAAA", "not a key"):
        with pytest.raises(HTTPException) as wrong:
            asyncio.run(routes.set_up_library(routes.Setup(password="ben's password", recovery=typed), request(), ben))
        assert wrong.value.status_code == 403
    answer = asyncio.run(
        routes.set_up_library(
            routes.Setup(password="ben's password", recovery=recovery.lower().replace("-", " ")),
            request(origin="https://cloud.example"),
            ben,
            "/index.php/apps/app_api/proxy/harvester",
        )
    )
    cookie = answer.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    assert "Path=/index.php/apps/app_api/proxy/harvester" in cookie
    with pytest.raises(HTTPException) as again:
        asyncio.run(routes.propose_recovery(ben))
    assert again.value.status_code == 409
    no_cookie = asyncio.run(
        routes.unlock_library(routes.Unlock(password="ben's password"), request(), ben, "/elsewhere")
    )
    assert "set-cookie" not in no_cookie.headers, "a path that is not the app's proxy's gets no cookie"


def test_the_cookie_opens_the_library_and_a_ticket_one_file(app):
    file_id = app["shelf"].put("anna", "/v.mp4", io.BytesIO(b"video"))
    other = app["shelf"].put("anna", "/w.mp4", io.BytesIO(b"other"))
    cookie = request(cookie=app["token"])
    state = asyncio.run(routes.library_state(cookie, nextcloud()))
    assert state["unlocked"] and state["cookie"]
    assert asyncio.run(body(asyncio.run(routes.library_file(file_id, cookie, nextcloud())))) == b"video"
    ticket = asyncio.run(routes.file_ticket(file_id, request(), nextcloud(), app["token"]))["ticket"]
    shown = asyncio.run(routes.library_file(file_id, request(), nextcloud(), ticket=ticket))
    assert asyncio.run(body(shown)) == b"video"
    for call in (
        lambda: routes.library_file(other, request(), nextcloud(), ticket=ticket),
        lambda: routes.library_file(file_id, request(), nextcloud("ben"), ticket=ticket),
    ):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(call())
        assert refused.value.status_code == 403
    asyncio.run(routes.lock_library(cookie, nextcloud()))
    with pytest.raises(HTTPException):
        asyncio.run(routes.library_file(file_id, request(), nextcloud(), ticket=ticket))


def test_the_library_locks_on_request_and_after_its_minutes(app, monkeypatch):
    keys: Keyring = app["keyring"]
    token = app["token"]
    held = keys.session("anna", token)
    assert held.private is not None
    asyncio.run(routes.list_library(request(), nextcloud(), x_harvester_library=token))
    assert held.cache["files"] == {}, "an empty library: nothing decrypted yet"
    app["shelf"].put("anna", "/x.txt", io.BytesIO(b"x"))
    asyncio.run(routes.list_library(request(), nextcloud(), x_harvester_library=token))
    assert held.cache["files"]
    asyncio.run(routes.lock_library(request(), nextcloud(), token))
    assert held.private is None and held.cache == {}, "the private key and what it decrypted dropped"
    for call in (
        lambda: routes.list_library(request(), nextcloud(), x_harvester_library=token),
        lambda: routes.library_file(1, request(), nextcloud(), x_harvester_library=token),
        lambda: routes.delete_library_file(1, request(), nextcloud(), token),
    ):
        with pytest.raises(HTTPException) as locked:
            asyncio.run(call())
        assert locked.value.status_code == 403
    with pytest.raises(HTTPException, match="Wrong password"):
        asyncio.run(routes.unlock_library(routes.Unlock(password="wrong"), request(), nextcloud()))
    token = asyncio.run(routes.unlock_library(routes.Unlock(password="anna's password"), request(), nextcloud()))
    token = json.loads(token.body)["token"]
    held = keys.session("anna", token)
    now = time.monotonic()
    monkeypatch.setattr(librarykeys.time, "monotonic", lambda: now + 15 * 60 + 1)
    with pytest.raises(HTTPException) as lapsed:
        asyncio.run(routes.list_library(request(), nextcloud(), x_harvester_library=token))
    assert lapsed.value.status_code == 403 and held.private is None


def test_wrong_passwords_wait_and_the_recovery_key_sets_a_new_one(app):
    keys: Keyring = app["keyring"]
    recovery = keys.propose("ben")
    keys.set_up("ben", "ben's password", recovery, 15)
    file_id = app["shelf"].put("ben", "/x.txt", io.BytesIO(b"kept"))
    for _ in range(librarykeys.MAX_FAILED):
        with pytest.raises(librarykeys.Locked):
            keys.unlock("ben", "wrong")
    with pytest.raises(librarykeys.Locked, match="Too many"):
        keys.unlock("ben", "ben's password")
    keys._failed.clear()
    with pytest.raises(librarykeys.Locked, match="Wrong"):
        keys.recover("ben", librarycrypt.new_recovery_key(), "new password")
    token = keys.recover("ben", recovery, "new password")
    assert b"".join(app["shelf"].content(keys.session("ben", token), file_id, 0, 3)) == b"kept"
    with pytest.raises(librarykeys.Locked):
        keys.unlock("ben", "ben's password")
    token = keys.unlock("ben", "new password")
    with pytest.raises(librarykeys.Locked):
        keys.change_password("ben", token, "wrong", "third password", 5)
    token = keys.change_password("ben", token, "new password", "third password", 5)
    assert keys.minutes("ben") == 5 and keys.unlock("ben", "third password")
    stored = str([dict(row) for row in app["store"].rows("SELECT * FROM library_keys")])
    assert "third password" not in stored and recovery not in stored


def test_uploads_come_in_pieces_and_writes_need_no_unlock(app):
    anna = nextcloud()
    asyncio.run(routes.lock_library(request(), anna, app["token"]))
    upload = asyncio.run(routes.start_upload(routes.Upload(folder="/Up", name="big.bin", size=200_000), anna))["upload"]
    data = bytes(range(256)) * 782
    data = data[:200_000]
    with pytest.raises(HTTPException) as gap:
        asyncio.run(routes.upload_piece(upload, streamed(data[:10]), anna, offset=5))
    assert gap.value.status_code == 409
    for start in range(0, len(data), 70_000):
        asyncio.run(routes.upload_piece(upload, streamed(data[start : start + 70_000]), anna, offset=start))
    with pytest.raises(HTTPException):
        asyncio.run(routes.finish_upload(upload, nextcloud("ben")))
    file_id = asyncio.run(routes.finish_upload(upload, anna))["id"]
    token = app["keyring"].unlock("anna", "anna's password")
    assert b"".join(app["shelf"].content(app["keyring"].session("anna", token), file_id, 0, 199_999)) == data
    with pytest.raises(HTTPException) as unset:
        asyncio.run(routes.start_upload(routes.Upload(name="x", size=1), nextcloud("ben")))
    assert unset.value.status_code == 409 and "Set up" in unset.value.detail
    too_much = asyncio.run(routes.start_upload(routes.Upload(name="y", size=3), anna))["upload"]
    with pytest.raises(HTTPException) as large:
        asyncio.run(routes.upload_piece(too_much, streamed(b"1234"), anna))
    assert large.value.status_code == 413


def test_an_unencrypted_library_of_an_earlier_build_is_set_aside_and_broken_writes_go(tmp_path):
    store = Store(str(tmp_path / "db"))
    with store.writing() as db:
        db.execute("CREATE TABLE library (id INTEGER PRIMARY KEY, user TEXT, path TEXT)")
        db.execute("CREATE TABLE library_locks (user TEXT PRIMARY KEY, hash TEXT, minutes INTEGER)")
    (tmp_path / "library" / "anna").mkdir(parents=True)
    (tmp_path / "library" / "anna" / "old.pdf").write_bytes(b"old")
    Shelf(store, tmp_path / "library")
    assert (tmp_path / "library-unencrypted" / "anna" / "old.pdf").read_bytes() == b"old"
    left = tmp_path / "library" / "anna" / "ab" / ".abcd.part"
    left.parent.mkdir(parents=True)
    left.write_bytes(b"broken off")
    assert not store.rows("SELECT name FROM sqlite_master WHERE name IN ('library', 'library_locks')")
    Shelf(store, tmp_path / "library")
    assert not left.exists(), "what a write broke off by a restart left is removed"


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
