# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harvester's private library on the page (library.py), and the files a job saved.

Every route serves the person their own library alone (their user ID, as AppAPI gives it, picks
the rows, the keys and the folder on the disk). Reading needs the library unlocked: setting it up,
unlocking it (the password) or recovering it (the recovery key) opens a session in the server's
memory (librarykeys.py), whose token the browser holds two ways:

- a cookie (COOKIE: HttpOnly, SameSite=Strict, its path the app's proxy path the page names in
  X-Harvester-Base), which AppAPI's proxy passes to the app (its cookie jar) and back (Set-Cookie
  among the answer's headers), so that an <img> or <video> of the library needs no token in its
  address;
- the answer's token, which the page keeps in memory and sends as X-Harvester-Library.

Whether the cookie came back is in the state (cookie); where it does not (a proxy that drops
it), the page fetches pictures with the header into blob: addresses and asks a ticket for a video,
a sound or a PDF (POST …/ticket): ?ticket= opens that one file, while the session lasts, nothing
else.

Several files at once (batch): moved into a folder, deleted, or labels given and taken off;
downloaded as one ZIP file (libraryzip.py), streamed as it is made, by a ticket of its own (a
download is a plain link). Recent lists the newest files of the whole library; a file's details
name the job that saved it and where it came from (the job's record of it, not encrypted: a job's
records never are).

A person may reset their own library (reset), locked or not: everything in it and its keys are
deleted, as the admin's occ command does, and it asks to be set up anew.

Writing (an upload, a new folder) needs the library set up, not unlocked. A file is served with
its type for what a browser shows safely (pictures but SVG, videos, sounds, PDFs, plain text),
else as a download, never sniffed (nosniff), and with a sandbox for whatever the browser shows of
it; a Range gives part of it (a video's seeking), only the chunks that hold it decrypted.
"""

import itertools
import logging
import posixpath
import re
import secrets
import threading
import time
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import librarycrypt as crypt
from .. import libraryzip
from ..library import LIMITS, BadPath, Incoming, LibraryFull, Shelf, clean_label
from ..librarykeys import LOCK_MINUTES, MAX_LOCK_MINUTES, MIN_PASSWORD, Keyring, Locked, NotSetUp, Session
from .common import NC, STATE, own_job, store

LOGGER = logging.getLogger("harvester")
ROUTER = APIRouter()
# The types a browser shows in the page's viewer; any other is downloaded.
SHOWN = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "application/pdf")
SHOWN_KINDS = ("video/", "audio/")
Token = Annotated[str, Header(alias="X-Harvester-Library", max_length=100)]
Base = Annotated[str, Header(alias="X-Harvester-Base", max_length=300)]
COOKIE = "harvester_library"
# The app's path behind AppAPI's proxy, as the page's OC.generateUrl makes it (a web root first,
# index.php or not): the cookie's path.
PROXY_PATH = re.compile(r"^(/[\w.~-]+)*?(/index\.php)?/apps/app_api/proxy/harvester$")
# A ticket opens one file for so long at most (and no longer than its session).
TICKET_SECONDS = 3600
# An upload's pieces, at most; an upload unfinished for so long is dropped.
UPLOAD_PIECE = 16 * 1024 * 1024
UPLOAD_IDLE = 3600
# A person's uploads under way at once, at most (each holds a file open).
UPLOADS_AT_ONCE = 8
# The most files and folders one batch (or ZIP) names, and the most labels it gives or takes.
BATCH = 1000
BATCH_LABELS = 20
# A ZIP's ticket is good for so long.
ZIP_SECONDS = 120


def shelf() -> Shelf:
    """The library's files, set at start."""
    return STATE["shelf"]


def keyring() -> Keyring:
    """The people's library keys and sessions, set at start."""
    return STATE["keyring"]


def token_of(request: Request | None, header: str) -> str:
    """The browser's session token: the header's, else the cookie's."""
    return header or (request.cookies.get(COOKIE, "") if request is not None else "")


async def opened(nc: NC, request: Request | None, header: str) -> Session:
    """The person's unlocked session of this browser; else 403."""
    user = await nc.user
    try:
        return keyring().session(user, token_of(request, header))
    except Locked as error:
        raise HTTPException(403, str(error)) from error


def with_session(data: dict, token: str, request: Request, base: str) -> JSONResponse:
    """An answer that opens a session: its token, and the cookie (where the page named the app's
    proxy path)."""
    response = JSONResponse(data | {"token": token})
    if PROXY_PATH.match(base):
        secure = not request.headers.get("origin", "").startswith("http://")
        response.set_cookie(COOKIE, token, path=base, httponly=True, samesite="strict", secure=secure)
    return response


@ROUTER.get("/api/library/state")
async def library_state(request: Request, nc: NC, x_harvester_library: Token = "") -> dict:
    """Whether the person's library is set up and open to this browser (by the cookie alone,
    too), its minutes, how much it holds (bytes on the disk) and the limits (0: none)."""
    user = await nc.user
    cookie = request.cookies.get(COOKIE, "")
    return {
        "setup": keyring().is_set_up(user),
        "unlocked": keyring().unlocked(user, token_of(request, x_harvester_library)),
        "cookie": bool(cookie) and keyring().unlocked(user, cookie),
        "minutes": keyring().minutes(user),
        "usage": shelf().usage(user),
        "limit": LIMITS["person"],
    }


@ROUTER.post("/api/library/setup/start")
async def propose_recovery(nc: NC) -> dict:
    """A recovery key for the person's setup, shown once (409 when set up already)."""
    try:
        return {"recovery": keyring().propose(await nc.user)}
    except Locked as error:
        raise HTTPException(409, str(error)) from error


class Setup(BaseModel):
    """The library's password, the recovery key typed back, the minutes it stays unlocked."""

    password: str = Field(min_length=MIN_PASSWORD, max_length=1000)
    recovery: str = Field(max_length=200)
    minutes: int = Field(LOCK_MINUTES, ge=1, le=MAX_LOCK_MINUTES)


@ROUTER.post("/api/library/setup")
async def set_up_library(body: Setup, request: Request, nc: NC, x_harvester_base: Base = "") -> Response:
    """Sets the person's library up (its keys made) and opens it to this browser; 403 when the
    recovery key typed is not the one shown."""
    try:
        token = keyring().set_up(await nc.user, body.password, body.recovery, body.minutes)
    except Locked as error:
        raise HTTPException(403, str(error)) from error
    return with_session({}, token, request, x_harvester_base)


class Unlock(BaseModel):
    """The library's password, typed."""

    password: str = Field(max_length=1000)


@ROUTER.post("/api/library/unlock")
async def unlock_library(body: Unlock, request: Request, nc: NC, x_harvester_base: Base = "") -> Response:
    """Opens the person's library to this browser; 403 for a wrong password."""
    try:
        token = keyring().unlock(await nc.user, body.password)
    except Locked as error:
        raise HTTPException(403, str(error)) from error
    return with_session({}, token, request, x_harvester_base)


class Recover(BaseModel):
    """The recovery key, and the new password."""

    recovery: str = Field(max_length=200)
    password: str = Field(min_length=MIN_PASSWORD, max_length=1000)


@ROUTER.post("/api/library/recover")
async def recover_library(body: Recover, request: Request, nc: NC, x_harvester_base: Base = "") -> Response:
    """Opens the person's library with the recovery key and sets the new password; 403 for a
    wrong recovery key."""
    try:
        token = keyring().recover(await nc.user, body.recovery, body.password)
    except Locked as error:
        raise HTTPException(403, str(error)) from error
    return with_session({}, token, request, x_harvester_base)


@ROUTER.post("/api/library/lock")
async def lock_library(
    request: Request, _nc: NC, x_harvester_library: Token = "", x_harvester_base: Base = ""
) -> Response:
    """Locks the library to this browser again (its session and private key dropped, the
    cookie too)."""
    keyring().lock(x_harvester_library)
    keyring().lock(request.cookies.get(COOKIE, ""))
    response = JSONResponse({})
    if PROXY_PATH.match(x_harvester_base):
        response.delete_cookie(COOKIE, path=x_harvester_base, httponly=True, samesite="strict")
    return response


class Password(BaseModel):
    """The password set now, a new one ("": the same) and the minutes it stays unlocked."""

    current: str = Field(max_length=1000)
    password: str = Field("", max_length=1000)
    minutes: int = Field(LOCK_MINUTES, ge=1, le=MAX_LOCK_MINUTES)


@ROUTER.put("/api/library/password")
async def set_library_password(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    body: Password, request: Request, nc: NC, x_harvester_library: Token = "", x_harvester_base: Base = ""
) -> Response:
    """Changes the person's library password and minutes (unlocked, and the current password
    given; else 403); every other session of theirs ends."""
    if body.password and len(body.password) < MIN_PASSWORD:
        raise HTTPException(422, "A library password has 8 characters at least.")
    try:
        token = keyring().change_password(
            await nc.user, token_of(request, x_harvester_library), body.current, body.password, body.minutes
        )
    except Locked as error:
        raise HTTPException(403, str(error)) from error
    return with_session({}, token, request, x_harvester_base)


@ROUTER.get("/api/library")
async def list_library(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    request: Request,
    nc: NC,
    folder: Annotated[str, Query(max_length=4000)] = "/",
    label: Annotated[str, Query(max_length=200)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    recent: bool = False,
    x_harvester_library: Token = "",
) -> dict:
    """The person's files and folders in folder (or with the label, or matching q in their names
    and labels: in it and below it; recent: the newest files of the whole library), the labels of
    all their files and the paths of all their folders."""
    session = await opened(nc, request, x_harvester_library)
    try:
        entries = shelf().recent(session) if recent else shelf().listing(session, folder, label, q.strip())
    except BadPath as error:
        raise HTTPException(422, "No such folder.") from error
    return {
        "folder": folder,
        "entries": entries,
        "labels": shelf().labels(session),
        "folders": shelf().folders(session),
    }


@ROUTER.get("/api/library/files/{file_id}/details")
async def library_details(file_id: int, request: Request, nc: NC, x_harvester_library: Token = "") -> dict:
    """One of the person's files or folders with the job that saved it (its ID and name, while
    the job is there) and where the file came from (the job's record: its link or key and its
    values)."""
    session = await opened(nc, request, x_harvester_library)
    entry = shelf().entry(session, file_id)
    if entry is None:
        raise HTTPException(404, "No such file.")
    job = store().job(entry["job"]) if entry["job"] else None
    if job is None or job.user != session.user:
        return entry | {"job_name": "", "source": None}
    return entry | {"job_name": job.name, "source": None if entry["dir"] else store().placed_file(job.id or 0, file_id)}


class Batch(BaseModel):
    """What to do with several files and folders: move them into folder, delete them, or give
    them the labels of add and take those of remove off."""

    ids: list[int] = Field(min_length=1, max_length=BATCH)
    action: str = Field(pattern="^(move|delete|label)$")
    folder: str = Field("/", max_length=4000)
    add: list[str] = Field([], max_length=BATCH_LABELS)
    remove: list[str] = Field([], max_length=BATCH_LABELS)


@ROUTER.post("/api/library/batch")
async def library_batch(body: Batch, request: Request, nc: NC, x_harvester_library: Token = "") -> dict:
    """Moves, deletes or labels the person's files and folders, each on its own: the IDs done,
    and those that failed with why (a file inside a folder deleted with it counts as done)."""
    session = await opened(nc, request, x_harvester_library)
    try:
        add = [clean_label(label) for label in body.add]
        remove = [clean_label(label) for label in body.remove]
    except BadPath as error:
        raise HTTPException(422, str(error)) from error
    done: list[int] = []
    failed: list[dict] = []
    for file_id in dict.fromkeys(body.ids):
        entry = shelf().entry(session, file_id)
        if entry is None:
            if body.action == "delete":
                done.append(file_id)  # gone already: with a folder deleted before it
            else:
                failed.append({"id": file_id, "name": "", "error": "No such file."})
            continue
        try:
            if body.action == "move":
                shelf().update(session, file_id, folder=body.folder)
            elif body.action == "delete":
                shelf().delete(session.user, file_id)
            else:
                shelf().label(session.user, file_id, add=add, remove=remove)
        except (BadPath, KeyError) as error:
            failed.append({"id": file_id, "name": entry["name"], "error": str(error)})
            continue
        done.append(file_id)
    return {"done": done, "failed": failed}


class Zip(BaseModel):
    """The files and folders a ZIP file holds."""

    ids: list[int] = Field(min_length=1, max_length=BATCH)


# ZIP files asked for: by ticket, [token, user, [(name, entry)], its name, when it lapses].
ZIPS: dict[str, list] = {}


@ROUTER.post("/api/library/zip")
async def library_zip_ticket(body: Zip, request: Request, nc: NC, x_harvester_library: Token = "") -> dict:
    """A ticket for one ZIP file of the person's files and folders (GET …/zip?ticket=), good for
    ZIP_SECONDS, once."""
    session = await opened(nc, request, x_harvester_library)
    try:
        chosen = libraryzip.members(shelf(), session, body.ids)
    except KeyError as error:
        raise HTTPException(404, "No such file.") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    # Named after the one file or folder chosen, else Library.zip.
    first = shelf().entry(session, body.ids[0])
    name = "Library.zip"
    if len(set(body.ids)) == 1 and first is not None:
        name = (first["name"] if first["dir"] else posixpath.splitext(first["name"])[0]) + ".zip"
    ticket = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _TICKETS_LOCK:
        for lapsed in [key for key, value in ZIPS.items() if value[4] < now]:
            ZIPS.pop(lapsed)
        ZIPS[ticket] = [token_of(request, x_harvester_library), session.user, chosen, name, now + ZIP_SECONDS]
    return {"ticket": ticket, "name": name, "count": len(chosen)}


@ROUTER.get("/api/library/zip")
async def library_zip(nc: NC, ticket: Annotated[str, Query(max_length=100)] = "") -> Response:
    """The ZIP file of a ticket (once), streamed as it is made."""
    user = await nc.user
    with _TICKETS_LOCK:
        held = ZIPS.pop(ticket, None)
    if held is None or held[1] != user or held[4] < time.monotonic():
        raise HTTPException(403, "The download has lapsed: choose Download again.")
    try:
        session = keyring().session(user, held[0])
    except Locked as error:
        raise HTTPException(403, str(error)) from error
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Disposition": disposition(held[3], True),
    }
    pieces = libraryzip.stream(shelf(), session, held[2])
    return StreamingResponse(guarded(pieces, 0), media_type="application/zip", headers=headers)


class Folder(BaseModel):
    """A new folder's path."""

    path: str = Field(min_length=1, max_length=4000)


@ROUTER.post("/api/library/folders")
async def make_library_folder(body: Folder, request: Request, nc: NC, x_harvester_library: Token = "") -> dict:
    """Makes a folder in the person's library (unlocked); its ID."""
    session = await opened(nc, request, x_harvester_library)
    try:
        return {"id": shelf().mkdir(session.user, body.path)}
    except BadPath as error:
        raise HTTPException(422, str(error)) from error


def byte_range(header: str, size: int) -> tuple[int, int] | None:
    """The first and last byte a Range header asks for (one range: "bytes=0-99", "bytes=100-",
    "bytes=-100"), None for none or one not understood (the whole file); ValueError for one
    outside the file."""
    if not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[len("bytes=") :].strip().partition("-")
    if not (first.isdigit() or not first) or not (last.isdigit() or not last) or not (first or last):
        return None
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    else:
        start, end = max(0, size - int(last)), size - 1 if int(last) else -1
    if start > end or start >= size:
        raise ValueError(header)
    return start, end


def disposition(name: str, attached: bool) -> str:
    """Content-Disposition with the file's name (RFC 6266: an ASCII stand-in, and UTF-8)."""
    plain = "".join(char if 32 <= ord(char) < 127 and char not in '"\\' else "_" for char in name)
    return f"{'attachment' if attached else 'inline'}; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; sandbox",
}


def served(entry: dict, download: bool) -> tuple[str, dict[str, str]]:
    """The type a file is served as (its own where the browser shows it safely, else a download)
    and the headers that go with it."""
    kind = entry["type"]
    shown = not download and (kind in SHOWN or kind.startswith(SHOWN_KINDS) or kind == "text/plain")
    headers = SAFE_HEADERS | {"Accept-Ranges": "bytes", "Content-Disposition": disposition(entry["name"], not shown)}
    if kind == "application/pdf":
        # A PDF viewer does not work in a sandbox; anything else shown runs no script.
        del headers["Content-Security-Policy"]
    return (kind if shown else "application/octet-stream") + (
        "; charset=utf-8" if kind == "text/plain" else ""
    ), headers


# Tickets that open one file each (where the cookie does not come back): by ticket, [token,
# user, file ID, when it lapses].
TICKETS: dict[str, list] = {}
_TICKETS_LOCK = threading.Lock()


def ticket_session(ticket: str, user: str, file_id: int) -> Session:
    """The session a ticket of this person's file stands for; else 403."""
    now = time.monotonic()
    with _TICKETS_LOCK:
        for lapsed in [key for key, value in TICKETS.items() if value[3] < now]:
            TICKETS.pop(lapsed)
        held = TICKETS.get(ticket)
    if held is None or held[1] != user or held[2] != file_id:
        raise HTTPException(403, "The library is locked.")
    try:
        return keyring().session(user, held[0])
    except Locked as error:
        raise HTTPException(403, str(error)) from error


async def file_session(nc: NC, request: Request, header: str, ticket: str, file_id: int) -> Session:
    """The session that opens the person's file: the browser's, or the file's ticket's."""
    if ticket:
        return ticket_session(ticket, await nc.user, file_id)
    return await opened(nc, request, header)


@ROUTER.post("/api/library/files/{file_id}/ticket")
async def file_ticket(file_id: int, request: Request, nc: NC, x_harvester_library: Token = "") -> dict:
    """A ticket that opens this one file (?ticket=) while the session lasts, an hour at most."""
    session = await opened(nc, request, x_harvester_library)
    if shelf().entry(session, file_id) is None:
        raise HTTPException(404, "No such file.")
    ticket = secrets.token_urlsafe(24)
    with _TICKETS_LOCK:
        TICKETS[ticket] = [
            token_of(request, x_harvester_library),
            session.user,
            file_id,
            time.monotonic() + TICKET_SECONDS,
        ]
    return {"ticket": ticket}


def primed(pieces: Iterator[bytes]) -> Iterator[bytes]:
    """The pieces, the first read now: a blob that is not the file's (changed, cut off) is
    found before the answer starts (Tampered)."""
    first = next(pieces, b"")
    return itertools.chain([first], pieces)


def guarded(pieces: Iterator[bytes], file_id: int) -> Iterator[bytes]:
    """The pieces; a chunk that does not open ends the answer, logged by the file's ID."""
    try:
        yield from pieces
    except crypt.Tampered:
        LOGGER.warning("library: file %s is damaged (changed outside the app?)", file_id)


@ROUTER.get("/api/library/files/{file_id}")
async def library_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    file_id: int,
    request: Request,
    nc: NC,
    download: bool = False,
    ticket: Annotated[str, Query(max_length=100)] = "",
    x_harvester_library: Token = "",
) -> Response:
    """One of the person's files, decrypted: shown (a type the browser shows safely) or
    downloaded, the part a Range asks for (206) if it asks for one."""
    session = await file_session(nc, request, x_harvester_library, ticket, file_id)
    entry = shelf().entry(session, file_id)
    if entry is None or entry["dir"]:
        raise HTTPException(404, "No such file.")
    size = entry["size"]
    media, headers = served(entry, download)
    try:
        part = byte_range(request.headers.get("range", ""), size)
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = part if part is not None else (0, size - 1)
    try:
        pieces = primed(shelf().content(session, file_id, start, end))
    except (crypt.Tampered, OSError) as error:
        LOGGER.warning("library: file %s is damaged or missing (%s)", file_id, type(error).__name__)
        raise HTTPException(500, "The file is damaged: it does not decrypt.") from error
    headers["Content-Length"] = str(end - start + 1)
    if part is None:
        return StreamingResponse(guarded(pieces, file_id), media_type=media, headers=headers)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(guarded(pieces, file_id), status_code=206, media_type=media, headers=headers)


@ROUTER.get("/api/library/files/{file_id}/thumbnail")
async def library_thumbnail(
    file_id: int,
    request: Request,
    nc: NC,
    ticket: Annotated[str, Query(max_length=100)] = "",
    x_harvester_library: Token = "",
) -> Response:
    """The thumbnail of one of the person's pictures (a small JPEG), decrypted; 404 for none."""
    session = await file_session(nc, request, x_harvester_library, ticket, file_id)
    picture = shelf().thumbnail(session, file_id)
    if picture is None:
        raise HTTPException(404, "No thumbnail.")
    return Response(picture, media_type="image/jpeg", headers=SAFE_HEADERS)


class Change(BaseModel):
    """A file's or folder's new name, and/or the folder it goes into."""

    name: str = Field("", max_length=255)
    folder: str | None = Field(None, max_length=4000)


@ROUTER.put("/api/library/files/{file_id}")
async def change_library_file(
    file_id: int, body: Change, request: Request, nc: NC, x_harvester_library: Token = ""
) -> dict:
    """Renames or moves one of the person's files or folders; the entry after."""
    session = await opened(nc, request, x_harvester_library)
    if "/" in body.name:
        raise HTTPException(422, "A name has no /.")
    try:
        return shelf().update(session, file_id, body.name, body.folder)
    except KeyError as error:
        raise HTTPException(404, "No such file.") from error
    except BadPath as error:
        raise HTTPException(422, str(error)) from error


@ROUTER.delete("/api/library/files/{file_id}")
async def delete_library_file(file_id: int, request: Request, nc: NC, x_harvester_library: Token = "") -> dict:
    """Deletes one of the person's files, or a folder with everything in it. A job whose file it
    was fetches it again if its Download deleted files again is on, as after a delete in Files."""
    session = await opened(nc, request, x_harvester_library)
    if not shelf().delete(session.user, file_id):
        raise HTTPException(404, "No such file.")
    return {}


class Reset(BaseModel):
    """The person's own reset of their library: confirm is the word RESET_WORD."""

    confirm: str = Field(max_length=20)


# What a reset must be sent with: no request deletes a library by mistake.
RESET_WORD = "reset"


@ROUTER.post("/api/library/reset")
async def reset_library(body: Reset, nc: NC, x_harvester_base: Base = "") -> Response:
    """Deletes the person's library entirely, as before its setup: files, folders, labels and keys
    (every session of theirs ends), locked or not, so that someone who lost the password and the
    recovery key can start again; 422 without the confirmation. The number of files and folders
    deleted."""
    if body.confirm != RESET_WORD:
        raise HTTPException(422, "Confirm the reset: everything in the library is deleted, for good.")
    user = await nc.user
    with _UPLOADS_LOCK:
        dropped = [UPLOADS.pop(key) for key, held in list(UPLOADS.items()) if held[0] == user]
    for upload in dropped:
        upload[1].abort()
    count = shelf().reset(user)
    LOGGER.info("library: a person reset their library (%s files and folders)", count)
    response = JSONResponse({"deleted": count})
    if PROXY_PATH.match(x_harvester_base):
        response.delete_cookie(COOKIE, path=x_harvester_base, httponly=True, samesite="strict")
    return response


# Uploads under way: by ID, [user, Incoming, bytes received, size announced, last use].
UPLOADS: dict[str, list] = {}
_UPLOADS_LOCK = threading.Lock()


def _sweep_uploads() -> None:
    """Drops the uploads unused for UPLOAD_IDLE seconds."""
    now = time.monotonic()
    with _UPLOADS_LOCK:
        stale = [key for key, value in UPLOADS.items() if now - value[4] > UPLOAD_IDLE]
        dropped = [UPLOADS.pop(key) for key in stale]
    for upload in dropped:
        upload[1].abort()


class Upload(BaseModel):
    """A file to upload: the folder it goes into, its name and size."""

    folder: str = Field("/", max_length=4000)
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)


def write_problem(error: Exception) -> HTTPException:
    """The answer to a write the library refuses."""
    if isinstance(error, NotSetUp):
        return HTTPException(409, str(error))
    return HTTPException(422, str(error))


@ROUTER.post("/api/library/uploads")
async def start_upload(body: Upload, nc: NC) -> dict:
    """Starts an upload into the person's library (set up; locked or not): its ID. The pieces
    come in order (PUT …?offset=), encrypted as they come; done puts the file in place."""
    _sweep_uploads()
    if "/" in body.name:
        raise HTTPException(422, "A name has no /.")
    user = await nc.user
    with _UPLOADS_LOCK:
        if sum(1 for held in UPLOADS.values() if held[0] == user) >= UPLOADS_AT_ONCE:
            raise HTTPException(429, "Too many uploads at once.")
    try:
        incoming = shelf().receive(user, f"{body.folder.rstrip('/')}/{body.name}", expected=body.size)
    except (NotSetUp, BadPath, LibraryFull) as error:
        raise write_problem(error) from error
    upload = secrets.token_urlsafe(18)
    with _UPLOADS_LOCK:
        UPLOADS[upload] = [user, incoming, 0, body.size, time.monotonic()]
    return {"upload": upload}


def _upload(upload: str, user: str) -> list:
    with _UPLOADS_LOCK:
        held = UPLOADS.get(upload)
    if held is None or held[0] != user:
        raise HTTPException(404, "No such upload.")
    return held


@ROUTER.put("/api/library/uploads/{upload}")
async def upload_piece(upload: str, request: Request, nc: NC, offset: int = 0) -> dict:
    """A piece of an upload, at its offset (the bytes received so far; else 409)."""
    held = _upload(upload, await nc.user)
    if offset != held[2]:
        raise HTTPException(409, f"The upload has {held[2]} bytes.")
    incoming: Incoming = held[1]
    count = 0
    try:
        async for piece in request.stream():
            count += len(piece)
            if count > UPLOAD_PIECE or held[2] + count > held[3]:
                raise HTTPException(413, "More than the upload announced.")
            incoming.write(piece)
    except (LibraryFull, HTTPException) as error:
        with _UPLOADS_LOCK:
            UPLOADS.pop(upload, None)
        incoming.abort()
        raise error if isinstance(error, HTTPException) else write_problem(error) from error
    held[2] += count
    held[4] = time.monotonic()
    return {"received": held[2]}


@ROUTER.post("/api/library/uploads/{upload}/done")
async def finish_upload(upload: str, nc: NC) -> dict:
    """Puts the uploaded file in place (over a file of its name); its ID."""
    held = _upload(upload, await nc.user)
    with _UPLOADS_LOCK:
        UPLOADS.pop(upload, None)
    if held[2] != held[3]:
        held[1].abort()
        raise HTTPException(409, "The upload is not complete.")
    try:
        return {"id": held[1].commit()}
    except (BadPath, LibraryFull) as error:
        raise write_problem(error) from error


@ROUTER.delete("/api/library/uploads/{upload}")
async def cancel_upload(upload: str, nc: NC) -> dict:
    """Drops an upload under way."""
    held = _upload(upload, await nc.user)
    with _UPLOADS_LOCK:
        UPLOADS.pop(upload, None)
    held[1].abort()
    return {}


@ROUTER.get("/api/jobs/{job_id}/files")
async def job_files(job_id: int, nc: NC, label: Annotated[str, Query(max_length=200)] = "") -> dict:
    """The files the person's job saved (the newest 1000): their paths, IDs and tags or labels
    (with a label, those that have it), and the labels among them."""
    job = await own_job(job_id, nc)
    files = store().job_files(job.id or 0)
    labels = sorted({tag for entry in files for tag in entry["tags"]}, key=str.casefold)
    return {"files": [entry for entry in files if not label or label in entry["tags"]], "labels": labels}
