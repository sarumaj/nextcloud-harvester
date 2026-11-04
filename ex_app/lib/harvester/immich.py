# SPDX-License-Identifier: AGPL-3.0-or-later
"""Save to: Immich. A job's photos and videos uploaded into the person's Immich account, into an
album the job may name, tagged with Immich's tags (or Harvester's labels, or none).

The account is the person's, not a job's: the server's address (the admin's preset, SETTINGS,
unless the person types another) and their API key, a sensitive preference of theirs (encrypted
by Nextcloud), never logged nor sent back to the page (routes/immich.py). The admin's server is
reached directly, as Nextcloud is (it may be on the private network); a server the person types is
reached as the web is, through the guard's check (download.guarded_client: public addresses
only). Either over https, plain http only where the admin allows it. No redirect is followed: the
key goes to the server named, nowhere else.

The API is Immich's of version 3 (checked against 3.3.5's OpenAPI): the key as x-api-key;
GET /api/users/me (the account's test), GET /api/server/media-types (what it keeps),
POST /api/assets/bulk-upload-check (a content there already, by its SHA-1: adopted, not uploaded
again), POST /api/assets (multipart, the file streamed from the run's temporary file), GET and
POST /api/albums and PUT /api/albums/{id}/assets, PUT /api/tags (made by name, "a/b" as b inside
a), PUT and DELETE /api/tags/{id}/assets, GET /api/assets/{id} (whether it is still there). The
key needs the permissions user.read, asset.upload, asset.read, album.read, album.create,
albumAsset.create, tag.read, tag.create and tag.asset; asset.delete only to restore a photo from
Immich's trash (POST /api/trash/restore/assets) that a job fetches again.

A job knows its files by numbers (runner.Library): each asset it placed is a row of immich_assets
(the job, the path its file had, the asset's id); one gone from Immich (deleted, or in its trash)
loses its row, so that Download deleted files again uploads it anew. A file Immich does not keep
(not a photo or video) is Refused: skipped for good, the log saying why."""

import base64
import datetime
import hashlib
import mimetypes
import os
import posixpath
import time
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO
from urllib.parse import urlparse

import httpx

from . import destinations
from .l10n import said
from .patterns import render
from .runner import FileGone, Library, NotChecked
from .sources import AuthError, Refused, SourceError
from .sources.download import USER_AGENT, guarded_client
from .store import Job, Store

Call = Callable[[Awaitable[Any]], Any]
# The person's preferences: the server's address (empty: the admin's) and the API key (sensitive).
URL_KEY, API_KEY = "immich.url", "immich.key"
# The admin's preset server and whether plain http is allowed (settings.py, read each minute by
# the executor); HARVESTER_IMMICH_URL and HARVESTER_IMMICH_HTTP are their defaults.
DEFAULT_URL = os.environ.get("HARVESTER_IMMICH_URL", "").strip()
DEFAULT_HTTP = os.environ.get("HARVESTER_IMMICH_HTTP", "").strip().lower() in ("1", "true", "on", "yes")
SETTINGS: dict[str, Any] = {"url": DEFAULT_URL, "http": DEFAULT_HTTP}
# The store the jobs' assets are recorded in (set at start: main.lifespan, prepare).
STORES: list[Store] = []
CHUNK = 1 << 20
TIMEOUT = 300
# What Immich 3.3.5 keeps (its media types), when the server does not say.
MEDIA = {
    *".3fr .ari .arw .avif .bmp .cap .cin .cr2 .cr3 .crw .dcr .dng .erf .fff .gif .heic .heif .hif .iiq .insp"
    " .jp2 .jpe .jpeg .jpg .jxl .k25 .kdc .mrw .nef .nrw .orf .ori .pef .png .psd .raf .raw .rw2 .rwl .sr2 .srf"
    " .srw .svg .tif .tiff .webp .x3f".split(),
    *".3gp .3gpp .avi .flv .insv .m2t .m2ts .m4v .mkv .mov .mp4 .mpe .mpeg .mpg .mts .vob .webm .wmv".split(),
}
SCHEMA = """
-- The assets the jobs placed in Immich (immich.py): a job's number for each (id), the path its
-- file had, the asset's id in the person's Immich.
CREATE TABLE IF NOT EXISTS immich_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    job INTEGER NOT NULL,
    path TEXT NOT NULL,
    asset TEXT NOT NULL,
    at REAL NOT NULL,
    UNIQUE (job, path)
);
"""


class ImmichError(SourceError):
    """Immich refused a request, or could not be reached."""


class ImmichRefused(AuthError):
    """Immich refused the person's API key, or there is none: they set it again (Your Immich
    account), as a refused account's password."""


def prepare(store: Store) -> None:
    """The store the assets are recorded in, its table made."""
    store.tables(SCHEMA)
    STORES[:] = [store]


def configure(url: str | None, http: bool) -> None:
    """The admin's settings (settings.immich): the preset server, plain http allowed."""
    SETTINGS["url"] = DEFAULT_URL if url is None else url.strip()
    SETTINGS["http"] = http


def base(url: str) -> str:
    """A server's address as Harvester asks it: without a "/" or "/api" at its end."""
    url = url.strip().rstrip("/")
    return url[: -len("/api")] if url.endswith("/api") else url


def preset(url: str) -> bool:
    """Whether the address is the admin's server (reached directly, not through the guard)."""
    return bool(SETTINGS["url"]) and base(url) == base(str(SETTINGS["url"]))


def problem(url: str) -> str:
    """What is wrong with a server's address ("" if nothing), translated: https (http only where
    the admin allows it, or for their own server), a host, no password, query or fragment."""
    parsed = urlparse(base(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return said("The Immich server's address: https:// and its host, e.g. https://photos.example.org.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return said("The Immich server's address: no name, password, ? or # in it.")
    if parsed.scheme == "http" and not (SETTINGS["http"] or preset(url)):
        return said("The Immich server's address: https:// only (the admin allows no plain http).")
    return ""


class Client:
    """Immich's API at a server, with the person's key. transport: a test's."""

    def __init__(self, url: str, key: str, transport: httpx.BaseTransport | None = None):
        self.url = base(url)
        wrong = problem(self.url)
        if wrong:
            raise ImmichError(wrong)
        settings: dict[str, Any] = {
            "base_url": self.url + "/api",
            "headers": {"x-api-key": key, "Accept": "application/json", "User-Agent": USER_AGENT},
            "follow_redirects": False,
            "timeout": TIMEOUT,
        }
        if transport is not None:
            settings["transport"] = transport
        self.http = httpx.Client(**settings) if preset(self.url) else guarded_client(**settings)

    def close(self) -> None:
        """Closes the connections."""
        self.http.close()

    def request(self, method: str, path: str, allow: tuple[int, ...] = (), **options) -> httpx.Response:
        """The answer to a request; Immich's refusal as ImmichRefused (401) or ImmichError with
        what it said (a file too large, the quota full, a permission the key lacks); statuses in
        allow are answers."""
        try:
            answer = self.http.request(method, path, **options)
        except httpx.HTTPError as error:
            raise ImmichError(
                said("Immich could not be reached: {error}", error=str(error) or type(error).__name__)
            ) from error
        if answer.is_success or answer.status_code in allow:
            return answer
        if answer.status_code == 401:
            raise ImmichRefused(said("Immich refused your API key: set it again under Your Immich account"))
        message = _message(answer)
        if answer.status_code == 413:
            raise ImmichError(said("The file is too large for the Immich server (413)"))
        if "quota" in message.lower():
            raise ImmichError(said("Your Immich storage quota is full: {message}", message=message))
        if answer.status_code == 403:
            raise ImmichError(said("The Immich API key lacks a permission: {message}", message=message))
        if 300 <= answer.status_code < 400:
            raise ImmichError(said("The Immich server's address answers with a redirect: enter the address it names"))
        raise ImmichError(said("Immich answered {status}: {message}", status=answer.status_code, message=message))

    def json(self, method: str, path: str, **options) -> Any:
        """The answer's JSON."""
        return self.request(method, path, **options).json()

    def me(self) -> dict:
        """The account's email and name (the account's test)."""
        answer = self.json("GET", "/users/me")
        return {"email": str(answer.get("email") or ""), "name": str(answer.get("name") or "")}


def _message(answer: httpx.Response) -> str:
    """What Immich said with a refusal (its JSON's message), else the status's reason."""
    try:
        message = answer.json().get("message")
    except (ValueError, AttributeError):
        message = None
    if isinstance(message, list):
        message = "; ".join(str(part) for part in message)
    return str(message or answer.reason_phrase or answer.status_code)[:300]


def checksum(content: BinaryIO) -> str:
    """The file's SHA-1 as Immich compares contents (base64), read in pieces from its start."""
    content.seek(0)
    digest = hashlib.sha1(usedforsecurity=False)
    while piece := content.read(CHUNK):
        digest.update(piece)
    content.seek(0)
    return base64.b64encode(digest.digest()).decode("ascii")


def taken_at(values: dict) -> str:
    """When the file was made, as Immich takes it (fileCreatedAt): its item's date ({date}, or
    {year}, {month} and {day}), at noon UTC; else now. Immich prefers the photo's own (EXIF)."""
    try:
        if values.get("date"):
            day = datetime.date.fromisoformat(str(values["date"])[:10])
        else:
            day = datetime.date(int(values["year"]), int(values.get("month") or 1), int(values.get("day") or 1))
        return f"{day.isoformat()}T12:00:00.000Z"
    except (KeyError, TypeError, ValueError):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ImmichLibrary(Library):  # pylint: disable=too-many-instance-attributes
    """A job's files in the person's Immich: client is their account's, album the job's template
    of an album's name ("": none)."""

    def __init__(self, client: Client, store: Store, job: Job, album: str = ""):
        self.client = client
        self.store = store
        self.user = job.user
        self.job = job.id or 0
        self.album = album.strip()
        self.values: dict = {}
        self._media: set[str] | None = None
        self._albums: dict[str, str] = {}
        self._tags: dict[str, str] = {}
        self._found: set[int] = set()

    def item(self, values: dict) -> None:
        self.values = values

    def found(self, file_id: int) -> bool:
        return file_id in self._found

    def find(self, path: str) -> int | None:
        # An archive unpacked without it (unpack.saved): its folder stands for it, its first file.
        rows = self.store.rows(
            "SELECT id FROM immich_assets WHERE job = ? AND (path = ? OR substr(path, 1, ?) = ?) ORDER BY path != ?, id"
            " LIMIT 1",
            (self.job, path, len(path) + 1, path.rstrip("/") + "/", path),
        )
        return int(rows[0]["id"]) if rows else None

    def _asset(self, file_id: int) -> str | None:
        rows = self.store.rows("SELECT asset FROM immich_assets WHERE id = ? AND job = ?", (file_id, self.job))
        return str(rows[0]["asset"]) if rows else None

    def media(self) -> set[str]:
        """The extensions Immich keeps (its media types: images and videos), asked once."""
        if self._media is None:
            try:
                kinds = self.client.json("GET", "/server/media-types")
                self._media = {str(ext).lower() for kind in ("image", "video") for ext in kinds.get(kind) or []}
            except (ImmichError, ValueError, AttributeError):
                self._media = set()
            self._media = self._media or set(MEDIA)
        return self._media

    def put(self, path: str, content: BinaryIO) -> int:
        name = posixpath.basename(path)
        extension = posixpath.splitext(name)[1].lower()
        if extension not in self.media():
            raise Refused(said("Immich keeps photos and videos only, not {kind} files", kind=extension or name))
        value = checksum(content)
        check = self.client.json(
            "POST", "/assets/bulk-upload-check", json={"assets": [{"id": name, "checksum": value}]}
        )
        result = (check.get("results") or [{}])[0]
        if result.get("reason") == "unsupported-format":
            raise Refused(said("Immich keeps photos and videos only, not {kind} files", kind=extension or name))
        asset = result.get("assetId") if result.get("reason") == "duplicate" else None
        if asset and result.get("isTrashed"):
            self._restore(str(asset))
        if not asset:
            asset, there = self._upload(name, content, value)
        else:
            there = True
        self._file(str(asset))
        file_id = self._record(path, str(asset))
        if there:
            self._found.add(file_id)
        return file_id

    def _restore(self, asset: str) -> None:
        """A content in Immich's trash taken out of it (an upload would not: Immich answers it as a
        duplicate, in the trash still); a key without asset.delete cannot, and the file is skipped
        for good, rather than fetched each run."""
        try:
            self.client.request("POST", "/trash/restore/assets", json={"ids": [asset]})
        except ImmichError as error:
            if isinstance(error, ImmichRefused) or "permission" not in str(error):
                raise
            raise Refused(
                said("It is in Immich's trash: restore it there (or give the API key asset.delete to let Harvester)")
            ) from error

    def _upload(self, name: str, content: BinaryIO, value: str) -> tuple[str, bool]:
        """The file uploaded (streamed from its start: httpx reads it in pieces), and whether
        Immich had its content already."""
        content.seek(0)
        when = taken_at(self.values)
        answer = self.client.json(
            "POST",
            "/assets",
            headers={"x-immich-checksum": value},
            data={"fileCreatedAt": when, "fileModifiedAt": when, "filename": name},
            files={"assetData": (name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")},
        )
        return str(answer["id"]), answer.get("status") == "duplicate"

    def _record(self, path: str, asset: str) -> int:
        with self.store.writing() as db:
            db.execute(
                "INSERT INTO immich_assets (user, job, path, asset, at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (job, path) DO UPDATE SET asset = excluded.asset, at = excluded.at",
                (self.user, self.job, path, asset, time.time()),
            )
            row = db.execute("SELECT id FROM immich_assets WHERE job = ? AND path = ?", (self.job, path)).fetchone()
        return int(row["id"])

    def _file(self, asset: str) -> None:
        """The asset put into the job's album (made if there is none of its name): its name the
        template filled in with the item's values; one naming a value the item lacks, none."""
        try:
            name = render(self.album, self.values).strip()[:200] if self.album else ""
        except (KeyError, ValueError, IndexError):
            name = ""
        if not name:
            return
        album = self._albums.get(name)
        if album is None:
            listed = self.client.json("GET", "/albums", params={"name": name})
            album = next((str(entry["id"]) for entry in listed if entry.get("albumName") == name), None)
            if album is None:
                made = self.client.json("POST", "/albums", json={"albumName": name, "assetIds": [asset]})
                self._albums[name] = str(made["id"])
                return
            self._albums[name] = album
        self._bulk(self.client.json("PUT", f"/albums/{album}/assets", json={"ids": [asset]}))

    @staticmethod
    def _bulk(results: Any) -> None:
        """A bulk answer's errors: a file gone is FileGone; one there already no matter."""
        for result in results or []:
            error = result.get("error")
            if result.get("success") is False and error not in (None, "duplicate"):
                if error == "not_found":
                    raise FileGone(result.get("id"))
                raise ImmichError(said("Immich refused: {error}", error=str(error)))

    def _tag_ids(self, tags: list[str]) -> dict[str, str]:
        """The tags' ids, those that do not exist made (PUT /api/tags makes "a/b" as b in a)."""
        missing = [name for name in tags if name not in self._tags]
        if missing:
            for tag in self.client.json("PUT", "/tags", json={"tags": missing}):
                self._tags[str(tag.get("value") or tag.get("name"))] = str(tag["id"])
        return {name: self._tags[name] for name in tags if name in self._tags}

    def tag(self, file_id: int, tags: list[str]) -> None:
        asset = self._asset(file_id)
        if asset is None:
            raise FileGone(file_id)
        for tag in self._tag_ids(tags).values():
            self._bulk(self.client.json("PUT", f"/tags/{tag}/assets", json={"ids": [asset]}))

    def untag(self, file_id: int, tags: list[str]) -> None:
        asset = self._asset(file_id)
        if asset is None:
            raise FileGone(file_id)
        if any(name not in self._tags for name in tags):
            for tag in self.client.json("GET", "/tags"):
                self._tags[str(tag.get("value") or tag.get("name"))] = str(tag["id"])
        for name in tags:
            if name in self._tags:
                self.client.request("DELETE", f"/tags/{self._tags[name]}/assets", json={"ids": [asset]})

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        # Each asset asked for (Immich answers 400 or 404 for one gone, isTrashed for one in its
        # trash); a gone one's row forgotten, so that its file is uploaded anew.
        if not file_ids:
            return {}
        marks = ", ".join("?" * len(file_ids))
        rows = self.store.rows(
            f"SELECT id, path, asset FROM immich_assets WHERE job = ? AND id IN ({marks})", (self.job, *file_ids)
        )
        there: dict[int, str] = {}
        gone: list[int] = []
        try:
            for row in rows:
                answer = self.client.request("GET", f"/assets/{row['asset']}", allow=(400, 404))
                if answer.is_success and not answer.json().get("isTrashed"):
                    there[int(row["id"])] = str(row["path"])
                else:
                    gone.append(int(row["id"]))
        except (ImmichError, ValueError) as error:
            raise NotChecked(str(error)) from error
        if gone:
            with self.store.writing() as db:
                db.execute(f"DELETE FROM immich_assets WHERE id IN ({', '.join('?' * len(gone))})", tuple(gone))
        return there


def account(nc, call: Call) -> tuple[str, str]:
    """The person's server (theirs, else the admin's) and API key ("" for none)."""
    url = call(nc.preferences_ex.get_value(URL_KEY)) or str(SETTINGS["url"])
    return str(url), str(call(nc.preferences_ex.get_value(API_KEY)) or "")


def opened(job: Job, nc, call: Call) -> ImmichLibrary:
    """The job's library in the person's Immich (places.Destination's make): ImmichRefused
    without an account."""
    url, key = account(nc, call)
    if not key or not url:
        raise ImmichRefused(
            said("Your Immich account is needed: set its address and API key under Your Immich account")
        )
    if not STORES:
        raise SourceError("Immich is not set up on this server")
    return ImmichLibrary(Client(url, key), STORES[0], job, str(job.options.get(destinations.ALBUM) or ""))
