# SPDX-License-Identifier: AGPL-3.0-or-later
"""The destinations beside the person's files and the library: a Cryptomator vault's inbox (its
marker exactly as the vault app's contract has it, never over another vault's, never in a
folder shared with the person, nothing re-downloaded once the vault app moved it), a vault
written directly over WebDAV (its keys kept, never the password; a file the sister app's code
reads; a deleted one fetched again), and Tag as (labels or none: no Nextcloud tag), against a
fake Nextcloud whose files are a folder on the disk."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import base64
import json
import posixpath
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from nc_py_api import NextcloudException
from test_library import QuickSource
from test_runner import FakeFiles, FakeNextcloud
from test_vault_js import DATA, NODE, SISTER, password, run_js

import main
from harvester import destinations, places
from harvester import executor as executor_module
from harvester.inbox import MARKER
from harvester.routes import common
from harvester.routes import vault as vault_routes
from harvester.store import Job, Store
from harvester.vault import FolderStorage, Vault
from harvester.vaultlib import key_name


def node(full: str, file_id: int, size: int = 0, shared: bool = False):
    """An FsNode as nc_py_api's listing gives it (a folder's path ends with /)."""
    info = SimpleNamespace(fileid=file_id, size=size, content_length=size)
    return SimpleNamespace(
        full_path=full,
        name=full.rstrip("/").rsplit("/", 1)[-1],
        is_dir=full.endswith("/"),
        info=info,
        is_shared=shared,
        file_id=f"{file_id:08d}ocinstance",
        user_path=full.strip("/"),
    )


class DavFiles:
    """The person's files as nc_py_api's async client reaches them, kept in a folder on the disk:
    PROPFIND (listdir, by_path), GET (download, download2stream), MKCOL (405 for a name taken,
    409 without the parent), PUT (upload, upload_stream), DELETE, SEARCH by IDs (find), tags not
    at all (Tag as labels or none never asks). IDs by path, kept across a move (moved)."""

    def __init__(self, root: Path):
        self.root = root
        self.ids: dict[str, int] = {}
        self.shared: set[str] = set()
        self.requests: list[str] = []

    def disk(self, path: str) -> Path:
        return self.root / path.strip("/")

    def id_of(self, path: str) -> int:
        path = "/" + path.strip("/")
        return self.ids.setdefault(path, len(self.ids) + 1)

    def node_of(self, path: str):
        place = self.disk(path)
        full = "files/anna/" + path.strip("/") + ("/" if place.is_dir() else "")
        return node(full, self.id_of(path), 0 if place.is_dir() else place.stat().st_size, path in self.shared)

    async def listdir(self, path, depth=1, exclude_self=True):  # pylint: disable=unused-argument
        self.requests.append("PROPFIND")
        place = self.disk(path)
        if not place.is_dir():
            raise NextcloudException(404, "Not Found")
        return [self.node_of(posixpath.join(path, child.name)) for child in sorted(place.iterdir())]

    async def by_path(self, path):
        if not self.disk(path).exists():
            raise NextcloudException(404, "Not Found")
        return self.node_of(path)

    async def download(self, path):
        self.requests.append("GET")
        if not self.disk(path).is_file():
            raise NextcloudException(404, "Not Found")
        return self.disk(path).read_bytes()

    async def download2stream(self, path, fp, **kwargs):  # pylint: disable=unused-argument
        fp.write(await self.download(path))

    async def mkdir(self, path):
        self.requests.append("MKCOL")
        place = self.disk(path)
        if place.exists():
            raise NextcloudException(405, "Method Not Allowed")
        if not place.parent.is_dir():
            raise NextcloudException(409, "Conflict")
        place.mkdir()
        return node("files/anna/" + path.strip("/") + "/", self.id_of(path))

    async def makedirs(self, path, exist_ok=False):  # pylint: disable=unused-argument
        self.disk(path).mkdir(parents=True, exist_ok=True)

    async def upload(self, path, content):
        self.requests.append("PUT")
        place = self.disk(path)
        if not place.parent.is_dir():
            raise NextcloudException(409, "Conflict")
        place.write_bytes(content)
        return node("files/anna/" + path.strip("/"), self.id_of(path))

    async def upload_stream(self, path, fp):
        self.requests.append("chunked")
        return await self.upload(path, fp.read())

    async def delete(self, path, not_fail=False):  # pylint: disable=unused-argument
        place = self.disk(path)
        if place.is_dir():
            shutil.rmtree(place)
        else:
            place.unlink(missing_ok=True)
        self.ids.pop("/" + path.strip("/"), None)

    async def find(self, req, path=""):  # pylint: disable=unused-argument
        wanted = {value for value in req if isinstance(value, int)}
        return [self.node_of(found) for found, file_id in list(self.ids.items()) if file_id in wanted]


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """A store, the person's files in tmp_path/files with a copy of a fixture vault at
    /Vaults/V, and a Nextcloud whose every client is this one."""
    store = Store(str(tmp_path / "db"))
    monkeypatch.setitem(common.STATE, "store", store)
    monkeypatch.setitem(executor_module.SOURCES, "fake", QuickSource)
    files_root = tmp_path / "files"
    shutil.copytree(DATA / "vault-gcm", files_root / "Vaults" / "V")
    nc = FakeNextcloud()
    nc._user = "anna"
    nc.files = DavFiles(files_root)
    return SimpleNamespace(store=store, nc=nc, files=nc.files, root=files_root)


def saved(app, **options) -> Job:
    target = options.pop("target", "/Mag")
    return app.store.save(
        Job(
            user="anna",
            name="Mag",
            source="fake",
            target=target,
            schedule="0 6 * * *",
            tags=["{letter}"],
            options=options,
        )
    )


def run(app, job: Job) -> Job:
    executor_module.Executor(app.store, lambda: app.nc).run(job)
    return app.store.job(job.id or 0) or job


def unlock(app, job: Job, typed: str):
    return asyncio.run(vault_routes.unlock_vault(job.id or 0, vault_routes.VaultPassword(password=typed), app.nc))


def test_an_inbox_gets_the_contracts_marker_and_the_files_whole(app):
    job = run(app, saved(app, save_to="inbox", vault="Vaults/V/", vault_target="Magazines", target="/Mag inbox"))
    assert job.last_status == "ok", job.last_message
    marker = json.loads((app.root / "Mag inbox" / MARKER).read_text(encoding="utf-8"))
    assert marker == {
        "version": 1,
        "vault": "/Vaults/V",
        "target": "/Magazines",
        "from": 'Harvester job "Mag"',
        "harvester_job": job.id,
    }
    assert sorted(path.name for path in (app.root / "Mag inbox").iterdir()) == [MARKER, "a.txt", "b.txt", "c.txt"]
    assert "chunked" not in app.files.requests, "small files in one PUT each"
    lines = json.dumps(app.store.log_lines("anna", job.id))
    assert "Saved in the inbox of the vault /Vaults/V: /Mag inbox/a.txt (1 B)" in lines


def test_files_the_vault_app_moved_out_are_never_fetched_again(app):
    job = run(app, saved(app, save_to="inbox", vault="/Vaults/V", target="/Mag inbox"))
    for name in ("a.txt", "b.txt", "c.txt"):
        (app.root / "Mag inbox" / name).unlink()
    run(app, app.store.job(job.id or 0))
    assert sorted(path.name for path in (app.root / "Mag inbox").iterdir()) == [MARKER]
    assert (
        destinations.effective(job).options["again_deleted"] == 0
        and destinations.effective(job).options["recheck"] == 0
    )


def test_an_inbox_is_never_written_over_another_vaults_or_in_a_shared_folder(app):
    (app.root / "Other").mkdir()
    (app.root / "Other" / MARKER).write_text('{"version": 1, "vault": "/Vaults/W"}', encoding="utf-8")
    job = run(app, saved(app, save_to="inbox", vault="/Vaults/V", target="/Other"))
    assert job.last_status == "failed" and "inbox of another vault (/Vaults/W)" in (job.last_message or "")
    assert json.loads((app.root / "Other" / MARKER).read_text(encoding="utf-8"))["vault"] == "/Vaults/W"
    (app.root / "Same").mkdir()
    (app.root / "Same" / MARKER).write_text('{"version": 1, "vault": "/Vaults/V/", "target": "/x"}', encoding="utf-8")
    assert run(app, saved(app, save_to="inbox", vault="/Vaults/V", target="/Same")).last_status == "ok"
    assert json.loads((app.root / "Same" / MARKER).read_text(encoding="utf-8"))["target"] == "/x", "left as it is"
    (app.root / "Shared").mkdir()
    app.files.shared.add("/Shared")
    job = run(app, saved(app, save_to="inbox", vault="/Vaults/V", target="/Shared"))
    assert job.last_status == "failed" and "shared with you" in (job.last_message or "")
    assert not (app.root / "Shared" / MARKER).exists()


def test_an_inbox_marker_follows_the_jobs_folder_in_the_vault(app):
    # The job's marker of its first run, before its Folder in the vault was set; and back to /.
    for folder, wanted in (("One", "/Elite"), ("Two", "/")):
        job = saved(app, save_to="inbox", vault="/Vaults/V", vault_target=wanted, target=f"/{folder}")
        (app.root / folder).mkdir()
        old = {
            "version": 1,
            "vault": "/Vaults/V",
            "target": "/x",
            "from": 'Harvester job "Mag"',
            "harvester_job": job.id,
        }
        (app.root / folder / MARKER).write_text(json.dumps(old), encoding="utf-8")
        assert run(app, job).last_status == "ok"
        marker = json.loads((app.root / folder / MARKER).read_text(encoding="utf-8"))
        assert marker == old | {"target": wanted}, "the job's own marker follows it"
    (app.root / "Made").mkdir()
    (app.root / "Made" / MARKER).write_text(
        '{"version": 1, "vault": "/Vaults/V", "target": "/", "from": "me", "mine": 1}', encoding="utf-8"
    )
    assert (
        run(app, saved(app, save_to="inbox", vault="/Vaults/V", vault_target="Elite", target="/Made")).last_status
        == "ok"
    )
    made = json.loads((app.root / "Made" / MARKER).read_text(encoding="utf-8"))
    assert made == {
        "version": 1,
        "vault": "/Vaults/V",
        "target": "/Elite",
        "from": "me",
        "mine": 1,
    }, "a folder named wins"


def test_the_form_checks_an_inbox_and_a_vault():
    def form(**options):
        return main.JobIn(
            name="n",
            source="feed",
            target=options.pop("target", "/In"),
            options={"url": "https://e.example/f"} | options,
        )

    common.checked(form(save_to="inbox", vault="/Vaults/V"))
    for options, refusal in [
        ({"save_to": "inbox"}, "Vault: choose"),
        ({"save_to": "inbox", "vault": "/Vaults/V", "target": "/Vaults/V/In"}, "inbox is a folder of its own"),
        ({"save_to": "inbox", "vault": "/Vaults/V", "target": "/Vaults"}, "inbox is a folder of its own"),
        ({"save_to": "inbox", "vault": "/Vaults/V", "vault_target": "/../x"}, "Folder in the vault"),
        ({"save_to": "vault", "vault": "/a/../b"}, "Vault: no"),
        ({"save_to": "vault", "vault": "/V", "tag_as": "nextcloud"}, "Nextcloud tags only"),
    ]:
        with pytest.raises(HTTPException, match=refusal):
            common.checked(form(**options))


def test_a_vault_job_keeps_the_keys_never_the_password_and_writes_what_cryptomator_reads(app):
    job = saved(app, save_to="vault", vault="/Vaults/V", target="/Mag")
    with pytest.raises(HTTPException, match="not the vault's"):
        unlock(app, job, "wrong")
    assert unlock(app, job, password("vault-gcm")) == {}
    kept = app.nc.preferences_ex.values[key_name(job)]
    assert password("vault-gcm") not in json.dumps(app.nc.preferences_ex.values)
    assert len(base64.b64decode(kept)) == 64
    assert asyncio.run(vault_routes.vault_state(job.id or 0, app.nc)) == {"key": True}
    job = run(app, job)
    assert job.last_status == "ok", job.last_message
    vault = Vault.unlock(FolderStorage(app.root / "Vaults" / "V"), password("vault-gcm"))
    assert [node.name for node in vault.listdir("Mag")] == ["a.txt", "b.txt", "c.txt"]
    assert vault.read_bytes("Mag/b.txt") == b"b"
    lines = json.dumps(app.store.log_lines("anna", job.id))
    assert "Saved in the vault /Vaults/V: /Mag/b.txt (1 B)" in lines, "the log names the vault"
    assert password("vault-gcm") not in lines and kept not in lines
    if NODE and (SISTER / "app" / "cryptomator_vaults" / "js" / "vault.js").is_file():
        dumped = run_js("dump", str(app.root / "Vaults" / "V"), password("vault-gcm"))
        assert dumped["entries"]["Mag/a.txt"]["size"] == 1 and dumped["health"] == []


def test_a_file_deleted_in_the_vault_comes_again_and_a_name_taken_is_numbered(app):
    job = saved(app, save_to="vault", vault="/Vaults/V", target="/Mag")
    unlock(app, job, password("vault-gcm"))
    run(app, job)
    vault = Vault.unlock(FolderStorage(app.root / "Vaults" / "V"), password("vault-gcm"))
    entry = vault.find("Mag/a.txt")
    asyncio.run(app.files.delete(posixpath.join("/Vaults/V", entry.path)))
    assert run(app, app.store.job(job.id or 0)).last_status == "ok"
    vault = Vault.unlock(FolderStorage(app.root / "Vaults" / "V"), password("vault-gcm"))
    assert vault.read_bytes("Mag/a.txt") == b"a", "fetched again, as after a delete in Files"
    library = places.library_for(job, app.nc, asyncio.new_event_loop().run_until_complete)
    assert library.find("/Mag/b.txt") is not None and library.find("/Mag/none.txt") is None


def test_a_vault_without_its_keys_asks_for_the_password_as_a_refused_account(app):
    job = run(app, saved(app, save_to="vault", vault="/Vaults/V", target="/Mag"))
    assert job.last_status == "auth" and "password is needed" in (job.last_message or "")
    app.nc.preferences_ex.values[key_name(job)] = base64.b64encode(b"x" * 64).decode()
    job = run(app, job)
    assert job.last_status == "auth" and "do not open" in (job.last_message or "")
    other = saved(app, save_to="files")
    with pytest.raises(HTTPException) as refused:
        unlock(app, other, "x")
    assert refused.value.status_code == 409


def test_a_deleted_job_and_another_vault_take_the_keys_along(app, monkeypatch):
    monkeypatch.setitem(common.STATE, "executor", executor_module.Executor(app.store, lambda: app.nc))
    job = saved(app, save_to="vault", vault="/Vaults/V", target="/Mag")
    unlock(app, job, password("vault-gcm"))
    form = main.JobIn(
        name="Mag",
        source="feed",
        target="/Mag",
        options={"url": "https://e.example/f", "save_to": "vault", "vault": "/Vaults/W"},
    )
    app.store.save(Job(**(vars(job) | {"source": "feed", "options": dict(form.options) | {"vault": "/Vaults/V"}})))
    asyncio.run(main.update_job(job.id or 0, form, app.nc))
    assert key_name(job) not in app.nc.preferences_ex.values
    unlock_job = saved(app, save_to="vault", vault="/Vaults/V", target="/Mag")
    unlock(app, unlock_job, password("vault-gcm"))
    asyncio.run(main.delete_job(unlock_job.id or 0, app.nc))
    assert key_name(unlock_job) not in app.nc.preferences_ex.values


def test_labels_and_none_give_no_nextcloud_tag(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "db"))
    monkeypatch.setitem(executor_module.SOURCES, "fake", QuickSource)
    nc = FakeNextcloud()
    nc._user = "anna"
    nc.files = FakeFiles()
    for tag_as, recorded in (("labels", ["a"]), ("none", [])):
        job = store.save(
            Job(
                user="anna",
                name=tag_as,
                source="fake",
                target=f"/{tag_as}",
                schedule="* * * * *",
                tags=["{letter}"],
                options={"tag_as": tag_as},
            )
        )
        executor_module.Executor(store, lambda: nc).run(job)
        assert (store.job(job.id or 0) or job).last_status == "ok"
        assert store.placed(job.id or 0)["a"]["tags"] == recorded
    assert not nc.files.tags and not nc.files.assigned, "no Nextcloud tag made or given"
    job = store.save(Job(user="anna", name="t", source="fake", target="/t", schedule="* * * * *", tags=["{letter}"]))
    executor_module.Executor(store, lambda: nc).run(job)
    assert set(nc.files.tags) == {"a", "b", "c"}, "Nextcloud's tags as before"


def test_the_webdav_storage_streams_a_large_file_in_chunks_and_reads_it_back(app):
    from harvester.davstorage import WebDavStorage  # pylint: disable=import-outside-toplevel

    storage = WebDavStorage(app.nc, asyncio.new_event_loop().run_until_complete, "/Vaults/V")
    data = bytes(range(256)) * 25_000
    storage.write("big.bin", iter([data[:3_000_000], data[3_000_000:]]))
    assert "chunked" in app.files.requests and (app.root / "Vaults" / "V" / "big.bin").read_bytes() == data
    assert b"".join(storage.stream("big.bin")) == data
    assert storage.file_id("big.bin") and storage.file_id("none") is None
    with pytest.raises(FileExistsError):
        storage.mkdir("d")
    with pytest.raises(FileNotFoundError):
        storage.list("missing")
    with pytest.raises(FileNotFoundError):
        storage.mkdir("missing/x")
