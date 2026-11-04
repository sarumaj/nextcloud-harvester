# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harvester's private library on the page (library.py), and the files a job saved.

Every route serves the person their own files alone (their user ID, as AppAPI gives it, picks
the rows and the folder on the disk). A library with a password answers 403 until the browser
unlocks it: POST /api/library/unlock gives a token, which the page sends as the header
X-Harvester-Library (or, where a browser cannot send a header, an <img>'s or <video>'s address,
as ?unlock=); it lapses after the person's minutes without use.

A file is served with its type for what a browser shows safely (pictures but SVG, videos,
sounds, PDFs, plain text), else as a download, never sniffed (nosniff), and with a sandbox for
whatever the browser shows of it; a Range gives part of it (a video's seeking).
"""

import os
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..library import LIMITS, LOCK_MINUTES, MAX_LOCK_MINUTES, BadPath, Locked, Locks, Shelf
from .common import NC, STATE, own_job, store

ROUTER = APIRouter()
# The types a browser shows in the page's viewer; any other is downloaded.
SHOWN = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "application/pdf")
SHOWN_KINDS = ("video/", "audio/")
PIECE = 1 << 20
Token = Annotated[str, Header(alias="X-Harvester-Library", max_length=100)]


def shelf() -> Shelf:
    """The library's files, set at start."""
    return STATE["shelf"]


def locks() -> Locks:
    """The people's library passwords, set at start."""
    return STATE["locks"]


async def opened(nc: NC, token: str) -> str:
    """The person, whose library is open to this browser; else 403."""
    user = await nc.user
    try:
        locks().check(user, token)
    except Locked as error:
        raise HTTPException(403, str(error)) from error
    return user


@ROUTER.get("/api/library/state")
async def library_state(nc: NC, x_harvester_library: Token = "") -> dict:
    """Whether the person's library has a password and is open to this browser, its minutes,
    how much it holds and the limits (bytes; 0: none). Answered locked or not."""
    user = await nc.user
    setting = locks().setting(user)
    return {
        "password": setting is not None,
        "unlocked": locks().unlocked(user, x_harvester_library),
        "minutes": setting["minutes"] if setting else LOCK_MINUTES,
        "usage": shelf().usage(user),
        "limit": LIMITS["person"],
    }


class Unlock(BaseModel):
    """The library's password, typed."""

    password: str = Field(max_length=1000)


@ROUTER.post("/api/library/unlock")
async def unlock_library(body: Unlock, nc: NC) -> dict:
    """Opens the person's library to this browser: its token ("" without a password), or 403."""
    try:
        return {"token": locks().unlock(await nc.user, body.password)}
    except Locked as error:
        raise HTTPException(403, str(error)) from error


@ROUTER.post("/api/library/lock")
async def lock_library(_nc: NC, x_harvester_library: Token = "") -> dict:
    """Locks the library to this browser again."""
    locks().lock(x_harvester_library)
    return {}


class Password(BaseModel):
    """A new password ("" removes it), the one set now, and the minutes it stays unlocked."""

    current: str = Field("", max_length=1000)
    password: str = Field("", max_length=1000)
    minutes: int = Field(LOCK_MINUTES, ge=1, le=MAX_LOCK_MINUTES)


@ROUTER.put("/api/library/password")
async def set_library_password(body: Password, nc: NC) -> dict:
    """Sets, changes or removes the person's library password (the current one given, if one is
    set: else 403); every browser's session of theirs ends."""
    if body.password and len(body.password) < 8:
        raise HTTPException(422, "A library password has 8 characters at least.")
    try:
        locks().set_password(await nc.user, body.current, body.password, body.minutes)
    except Locked as error:
        raise HTTPException(403, str(error)) from error
    return {}


@ROUTER.get("/api/library")
async def list_library(
    nc: NC,
    folder: Annotated[str, Query(max_length=4000)] = "/",
    label: Annotated[str, Query(max_length=200)] = "",
    x_harvester_library: Token = "",
) -> dict:
    """The person's files and folders in folder (or with the label, in it and below it), and
    the labels of all their files."""
    user = await opened(nc, x_harvester_library)
    try:
        entries = shelf().listing(user, folder, label)
    except BadPath as error:
        raise HTTPException(422, "No such folder.") from error
    return {"folder": folder, "entries": entries, "labels": shelf().labels(user)}


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


def pieces(path: str, start: int, length: int) -> Iterator[bytes]:
    """length bytes of the file from start."""
    with open(path, "rb") as file:
        file.seek(start)
        while length > 0:
            piece = file.read(min(PIECE, length))
            if not piece:
                return
            length -= len(piece)
            yield piece


def disposition(name: str, attached: bool) -> str:
    """Content-Disposition with the file's name (RFC 6266: an ASCII stand-in, and UTF-8)."""
    plain = "".join(char if 32 <= ord(char) < 127 and char not in '"\\' else "_" for char in name)
    return f"{'attachment' if attached else 'inline'}; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


def on_disk(user: str, file_id: int) -> tuple[dict, str, int]:
    """One of the person's files: its entry, where it is on the disk and its size; else 404."""
    entry = shelf().entry(user, file_id)
    if entry is None or entry["dir"]:
        raise HTTPException(404, "No such file.")
    try:
        path = str(shelf().disk(user, entry["path"]))
        return entry, path, os.stat(path).st_size
    except (BadPath, OSError) as error:
        raise HTTPException(404, "No such file.") from error


def served(entry: dict, download: bool) -> tuple[str, dict[str, str]]:
    """The type a file is served as (its own where the browser shows it safely, else a download)
    and the headers that go with it."""
    kind = entry["type"]
    shown = not download and (kind in SHOWN or kind.startswith(SHOWN_KINDS) or kind == "text/plain")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": disposition(entry["name"], not shown),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if kind != "application/pdf":
        # A PDF viewer does not work in a sandbox; anything else shown runs no script.
        headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; media-src 'self'; sandbox"
    return (kind if shown else "application/octet-stream") + (
        "; charset=utf-8" if kind == "text/plain" else ""
    ), headers


@ROUTER.get("/api/library/files/{file_id}")
async def library_file(  # pylint: disable=too-many-arguments
    file_id: int,
    request: Request,
    nc: NC,
    download: bool = False,
    unlock: Annotated[str, Query(max_length=100)] = "",
    x_harvester_library: Token = "",
) -> Response:
    """One of the person's files: shown (a type the browser shows safely) or downloaded, the
    part a Range asks for (206) if it asks for one."""
    user = await opened(nc, x_harvester_library or unlock)
    entry, path, size = on_disk(user, file_id)
    media, headers = served(entry, download)
    try:
        part = byte_range(request.headers.get("range", ""), size)
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if part is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(pieces(path, 0, size), media_type=media, headers=headers)
    start, end = part
    headers |= {"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)}
    return StreamingResponse(pieces(path, start, end - start + 1), status_code=206, media_type=media, headers=headers)


@ROUTER.delete("/api/library/files/{file_id}")
async def delete_library_file(file_id: int, nc: NC, x_harvester_library: Token = "") -> dict:
    """Deletes one of the person's files, or a folder with everything in it. A job whose file it
    was fetches it again if its Download deleted files again is on, as after a delete in Files."""
    user = await opened(nc, x_harvester_library)
    if not shelf().delete(user, file_id):
        raise HTTPException(404, "No such file.")
    return {}


@ROUTER.get("/api/jobs/{job_id}/files")
async def job_files(job_id: int, nc: NC, label: Annotated[str, Query(max_length=200)] = "") -> dict:
    """The files the person's job saved (the newest 1000): their paths, IDs and tags or labels
    (with a label, those that have it), and the labels among them."""
    job = await own_job(job_id, nc)
    files = store().job_files(job.id or 0)
    labels = sorted({tag for entry in files for tag in entry["tags"]}, key=str.casefold)
    return {"files": [entry for entry in files if not label or label in entry["tags"]], "labels": labels}
