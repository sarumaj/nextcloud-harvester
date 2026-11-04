# SPDX-License-Identifier: AGPL-3.0-or-later
"""Save to: Immich, against a fake Immich (httpx's MockTransport answering as Immich 3.3.5's API):
photos and videos uploaded (other files skipped for good), a content Immich has already adopted
and still filed, the album made once and reused, Immich's tags, a file gone from Immich uploaded
again, a refused key asking for the account again, the person's account (tested, its key never
sent back), and the upload streamed from the file in pieces."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import base64
import functools
import hashlib
import io
import json
import re
import uuid
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from test_runner import FakeNextcloud, FakeSource

from harvester import destinations, immich, unpack
from harvester import executor as executor_module
from harvester.routes import immich as immich_routes
from harvester.sources import Item
from harvester.store import Job, Store

SERVER = "https://immich.example"
KEY = "secret-api-key"


class PhotoSource(FakeSource):
    """Photos a and b, and a text c, of 2024."""

    pause = 0.0

    def items(self, seen):
        for key, name in (("a", "a.jpg"), ("b", "b.jpg"), ("c", "c.txt")):
            if not seen(key):
                yield Item(key=key, path=name, url=key, values={"letter": key, "year": 2024, "month": 5, "day": 7})


class FakeImmich:  # pylint: disable=too-many-instance-attributes
    """The parts of Immich's API Harvester uses, in memory."""

    def __init__(self):
        self.assets: dict[str, dict] = {}
        self.albums: dict[str, dict] = {}
        self.tags: dict[str, str] = {}
        self.tagged: dict[str, set[str]] = {}
        self.calls: list[str] = []
        self.status = 0

    def seed(self, content: bytes, trashed: bool = False) -> str:
        asset = str(uuid.uuid4())
        self.assets[asset] = {"checksum": sha1(content), "name": "seeded", "trashed": trashed, "created": ""}
        return asset

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        self.calls.append(f"{request.method} {path}")
        if request.headers.get("x-api-key") != KEY:
            return httpx.Response(401, json={"message": "Invalid API key"})
        if self.status:
            return httpx.Response(self.status, json={"message": "Quota has been exceeded!"})
        body = json.loads(request.content) if request.headers.get("content-type") == "application/json" else None
        route = f"{request.method} {re.sub(r'/[0-9a-f-]{36}', '/{id}', path)}"
        match = re.search(r"/([0-9a-f-]{36})", path)
        ident = match.group(1) if match else ""
        handler = getattr(self, "on_" + re.sub(r"\W+", "_", route).strip("_").lower())
        return handler(request, body, ident)

    def on_get_users_me(self, _request, _body, _ident):
        return httpx.Response(200, json={"id": "u", "email": "anna@example.org", "name": "Anna Example"})

    def on_get_server_media_types(self, _request, _body, _ident):
        return httpx.Response(200, json={"image": [".jpg", ".png"], "video": [".mp4"], "sidecar": [".xmp"]})

    def on_post_assets_bulk_upload_check(self, _request, body, _ident):
        results = []
        for entry in body["assets"]:
            same = next((key for key, asset in self.assets.items() if asset["checksum"] == entry["checksum"]), None)
            if same:
                trashed = self.assets[same]["trashed"]
                results.append(
                    {
                        "id": entry["id"],
                        "action": "reject",
                        "reason": "duplicate",
                        "assetId": same,
                        "isTrashed": trashed,
                    }
                )
            else:
                results.append({"id": entry["id"], "action": "accept"})
        return httpx.Response(200, json={"results": results})

    def on_post_assets(self, request, _body, _ident):
        content = request.read()
        given = request.headers["x-immich-checksum"]
        same = next((key for key, asset in self.assets.items() if asset["checksum"] == given), None)
        if same:
            self.assets[same]["trashed"] = False
            return httpx.Response(200, json={"id": same, "status": "duplicate"})
        name = re.search(rb'name="filename"\r\n\r\n([^\r]*)', content).group(1).decode()
        created = re.search(rb'name="fileCreatedAt"\r\n\r\n([^\r]*)', content).group(1).decode()
        asset = str(uuid.uuid4())
        self.assets[asset] = {"checksum": given, "name": name, "trashed": False, "created": created, "body": content}
        return httpx.Response(201, json={"id": asset, "status": "created"})

    def on_get_assets_id(self, _request, _body, ident):
        if ident not in self.assets:
            return httpx.Response(400, json={"message": "Not found or no asset.read access"})
        return httpx.Response(200, json={"id": ident, "isTrashed": self.assets[ident]["trashed"]})

    def on_get_albums(self, request, _body, _ident):
        name = request.url.params.get("name")
        listed = [{"id": key, "albumName": album["name"]} for key, album in self.albums.items()]
        return httpx.Response(200, json=[entry for entry in listed if name is None or entry["albumName"] == name])

    def on_post_albums(self, _request, body, _ident):
        album = str(uuid.uuid4())
        self.albums[album] = {"name": body["albumName"], "assets": list(body.get("assetIds") or [])}
        return httpx.Response(201, json={"id": album, "albumName": body["albumName"]})

    def on_put_albums_id_assets(self, _request, body, ident):
        results = []
        for asset in body["ids"]:
            there = asset in self.albums[ident]["assets"]
            if not there:
                self.albums[ident]["assets"].append(asset)
            results.append({"id": asset, "success": not there} | ({"error": "duplicate"} if there else {}))
        return httpx.Response(200, json=results)

    def on_put_tags(self, _request, body, _ident):
        made = []
        for value in body["tags"]:
            for end in range(1, value.count("/") + 2):
                self.tags.setdefault("/".join(value.split("/")[:end]), str(uuid.uuid4()))
            made.append({"id": self.tags[value], "name": value.rsplit("/", 1)[-1], "value": value})
        return httpx.Response(200, json=made)

    def on_get_tags(self, _request, _body, _ident):
        return httpx.Response(200, json=[{"id": key, "value": value} for value, key in self.tags.items()])

    def on_put_tags_id_assets(self, _request, body, ident):
        self.tagged.setdefault(ident, set()).update(body["ids"])
        return httpx.Response(200, json=[{"id": asset, "success": True} for asset in body["ids"]])

    def on_delete_tags_id_assets(self, _request, body, ident):
        self.tagged.setdefault(ident, set()).difference_update(body["ids"])
        return httpx.Response(200, json=[{"id": asset, "success": True} for asset in body["ids"]])

    def tags_of(self, asset: str) -> set[str]:
        return {value for value, key in self.tags.items() if asset in self.tagged.get(key, set())}


def sha1(content: bytes) -> str:
    return base64.b64encode(hashlib.sha1(content, usedforsecurity=False).digest()).decode()


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """A store, a fake Immich as the admin's server, and anna's account with her key."""
    store = Store(str(tmp_path / "db"))
    immich.prepare(store)
    monkeypatch.setattr(immich, "STORES", [store])
    monkeypatch.setitem(immich.SETTINGS, "url", SERVER)
    monkeypatch.setitem(immich.SETTINGS, "http", False)
    fake = FakeImmich()
    monkeypatch.setattr(immich, "Client", functools.partial(immich.Client, transport=httpx.MockTransport(fake)))
    monkeypatch.setitem(executor_module.SOURCES, "photos", PhotoSource)
    nc = FakeNextcloud()
    nc._user = "anna"  # pylint: disable=protected-access
    nc.preferences_ex.values[immich.API_KEY] = KEY
    return SimpleNamespace(store=store, nc=nc, immich=fake)


def saved(app, tags=("{letter}", "Photos/{year}"), **options) -> Job:
    options = {"save_to": "immich", "tag_as": "immich", "immich_album": "Harvest {year}"} | options
    return app.store.save(
        Job(user="anna", name="P", source="photos", target="/P", schedule="0 6 * * *", tags=list(tags), options=options)
    )


def run(app, job: Job) -> Job:
    executor_module.Executor(app.store, lambda: app.nc).run(job)
    return app.store.job(job.id or 0) or job


def by_name(app) -> dict[str, str]:
    return {asset["name"]: key for key, asset in app.immich.assets.items()}


def test_photos_go_into_one_album_with_immichs_tags_and_other_files_are_skipped(app):
    job = run(app, saved(app))
    assert job.last_status == "ok", job.last_message
    assets = by_name(app)
    assert sorted(assets) == ["a.jpg", "b.jpg"], "the text file is not uploaded"
    assert "skipped" in (job.last_message or "")
    assert app.immich.assets[assets["a.jpg"]]["created"] == "2024-05-07T12:00:00.000Z", "the item's date"
    assert b"a" in app.immich.assets[assets["a.jpg"]]["body"]
    [album] = app.immich.albums.values()
    assert album["name"] == "Harvest 2024" and sorted(album["assets"]) == sorted(assets.values())
    assert app.immich.calls.count("POST /albums") == 1 and app.immich.calls.count("GET /albums") == 1, "made once"
    assert app.immich.tags_of(assets["a.jpg"]) == {"a", "Photos/2024"}
    assert "Photos" in app.immich.tags, "a/b nested as Immich does"


def test_a_content_immich_has_is_adopted_and_still_filed_and_tagged(app):
    seeded = app.immich.seed(b"a")
    job = run(app, saved(app))
    assert job.last_status == "ok", job.last_message
    assert "1 already there" in (job.last_message or "") and "1 downloaded" in (job.last_message or "")
    assert app.immich.calls.count("POST /assets") == 1, "only b uploaded"
    [album] = app.immich.albums.values()
    assert seeded in album["assets"] and app.immich.tags_of(seeded) == {"a", "Photos/2024"}


def test_an_existing_album_is_reused_and_labels_give_no_immich_tag(app):
    app.immich.albums["0" * 8 + "-0000-4000-8000-" + "0" * 12] = {"name": "Harvest 2024", "assets": []}
    job = run(app, saved(app, tag_as="labels"))
    assert job.last_status == "ok", job.last_message
    assert "POST /albums" not in app.immich.calls and len(app.immich.albums) == 1
    assert len(next(iter(app.immich.albums.values()))["assets"]) == 2
    assert not app.immich.tags, "labels are Harvester's"
    assert app.store.placed(job.id or 0)["a"]["tags"] == ["a", "Photos/2024"]


def test_a_photo_deleted_in_immich_is_uploaded_again(app):
    job = run(app, saved(app))
    app.immich.assets.pop(by_name(app)["a.jpg"])
    app.immich.assets[by_name(app)["b.jpg"]]["trashed"] = True
    job = run(app, app.store.job(job.id or 0))
    assert job.last_status == "ok", job.last_message
    assert app.immich.calls.count("POST /assets") == 4, "a uploaded anew, b's upload restores it"
    assert not any(asset["trashed"] for asset in app.immich.assets.values())


def test_a_refused_key_or_none_asks_for_the_account_again(app):
    app.nc.preferences_ex.values[immich.API_KEY] = "wrong"
    job = run(app, saved(app))
    assert job.last_status == "auth" and "API key" in (job.last_message or "")
    del app.nc.preferences_ex.values[immich.API_KEY]
    job = run(app, job)
    assert job.last_status == "auth" and "Immich account" in (job.last_message or "")


def test_a_full_quota_or_a_large_file_says_so(app):
    client = immich.Client(SERVER, KEY)
    app.immich.status = 400
    with pytest.raises(immich.ImmichError, match="quota is full"):
        client.me()
    app.immich.status = 413
    with pytest.raises(immich.ImmichError, match="too large"):
        client.me()


class Counted(io.BytesIO):
    """A file that records how much each read asked for."""

    def __init__(self, content: bytes):
        super().__init__(content)
        self.sizes: list[int] = []

    def read(self, size=-1, /):
        self.sizes.append(size if size is not None else -1)
        return super().read(size)


def test_the_upload_reads_the_file_in_pieces(app):
    job = saved(app)
    library = immich.opened(job, app.nc, asyncio.new_event_loop().run_until_complete)
    content = Counted(b"x" * (5 * immich.CHUNK + 3))
    file_id = library.put("/P/big.mp4", content)
    assert content.sizes and all(0 < size <= immich.CHUNK for size in content.sizes), "never the whole file"
    assert library.find("/P/big.mp4") == file_id and not library.found(file_id)
    assert library.existing([file_id]) == {file_id: "/P/big.mp4"}


def test_an_unpacked_zip_gives_its_photos_alone(app):
    job = saved(app)
    library = immich.opened(job, app.nc, asyncio.new_event_loop().run_until_complete)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("one.jpg", b"1")
        packed.writestr("notes.txt", b"2")
    lines: list[str] = []
    assert unpack.unpack(library, archive, "/P/set.zip", 10**9, [], lambda level, text: lines.append(text)) == 1
    assert list(by_name(app)) == ["one.jpg"] and any("notes.txt" in line for line in lines)
    assert library.find("/P/set") is not None, "the folder stands for the archive"
    assert (
        destinations.effective(job).options["unpack_keep"] == 0 and destinations.effective(job).options["recheck"] == 0
    )


@pytest.mark.usefixtures("app")
def test_the_account_is_tested_and_its_key_never_sent_back():
    nc = FakeNextcloud()
    answer = asyncio.run(immich_routes.set_immich_account(immich_routes.Account(key=KEY), nc))
    assert answer == {"email": "anna@example.org", "name": "Anna Example"}
    assert nc.preferences_ex.values == {immich.API_KEY: KEY}, "the admin's server is not kept as the person's"
    shown = asyncio.run(immich_routes.immich_account(nc))
    assert shown == {"url": "", "preset": SERVER, "key": True, "http": False} and KEY not in json.dumps(shown)
    assert asyncio.run(immich_routes.test_immich_account(nc))["email"] == "anna@example.org"
    for url, key, why in (
        ("", "wrong", "refused"),
        ("http://photos.example.org", KEY, "https:// only"),
        ("https://user:pw@photos.example.org", KEY, "no name"),
        ("https://127.0.0.1", KEY, "may not be reached"),
    ):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(immich_routes.set_immich_account(immich_routes.Account(url=url, key=key), nc))
        assert refused.value.status_code == 422 and why in str(refused.value.detail), url
    assert nc.preferences_ex.values == {immich.API_KEY: KEY}, "nothing kept of a refused one"
    asyncio.run(immich_routes.forget_immich_account(nc))
    assert not nc.preferences_ex.values


def test_tag_as_and_the_form_follow_the_place():
    assert destinations.tag_as({"save_to": "immich"}) == "immich"
    assert destinations.tag_as({"save_to": "immich", "tag_as": "nextcloud"}) == "immich"
    assert destinations.tag_as({"save_to": "files", "tag_as": "immich"}) == "nextcloud"
    assert "Immich's tags only" in destinations.problem({"save_to": "files", "tag_as": "immich"}, "/x")
    assert destinations.problem({"save_to": "immich", "tag_as": "immich"}, "/x") == ""
