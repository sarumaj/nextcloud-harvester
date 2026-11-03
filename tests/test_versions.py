# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versions and duplicates (harvester.versions, runner.place): a file of the same content at
another address is not saved again; with "Check known files for changes" a known address is asked
again, only if it changed where it gives an ETag or Last-Modified (304: no download), else by its
content's hash, and a changed file is saved over its own (Nextcloud's versions); at most once in
RECHECK_EVERY, a file its person deleted stays away, a failure of a known file fails nothing. The
sites are httpx's MockTransport."""

# pylint: disable=missing-function-docstring,protected-access

import io
import time

import httpx
from test_runner import FakeLibrary

from harvester import runner, versions
from harvester.sources import Item, Source, SOURCES
from harvester.sources import download
from harvester.sources.base import RECHECK
from harvester.store import Job, Store


class Server:
    """Files by path, each with an optional ETag and Last-Modified; the requests it saw."""

    def __init__(self, files: dict[str, bytes], etag: bool = True):
        self.files = dict(files)
        self.etag = etag
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.files.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        tag = f'"{hash(body) & 0xFFFF:x}"'
        if self.etag and request.headers.get("if-none-match") == tag:
            return httpx.Response(304)
        headers = {"etag": tag, "content-type": "application/pdf"} if self.etag else {"content-type": "application/pdf"}
        return httpx.Response(200, content=body, headers=headers)

    def asked(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]


class Files(Source):
    """The server's paths as items, over the guarded client's http opener."""

    name = "files"
    title = "Files"
    options = {"recheck": RECHECK}
    paths: tuple[str, ...] = ("/a.pdf", "/b.pdf")

    def items(self, seen):
        for path in self.paths:
            if not seen(path):
                yield Item(key=path, path=path.strip("/"), url=f"https://site.example{path}")

    def fetch(self, item, dest):
        download.fetch(download.http(self.client), item.url, dest, {}, lambda seconds: None)


def setup(tmp_path, server: Server, options: dict | None = None):
    store = Store(str(tmp_path / "db"))
    job = store.save(Job(user="u", name="n", source="files", target="/In", schedule="* * * * *"))
    client = httpx.Client(transport=httpx.MockTransport(server))
    source = Files(options or {}, {}, client)
    source.contents = versions.Contents(store, job.id or 0)
    return store, job, source


def go(store: Store, job: Job, source: Source, library: FakeLibrary, lines: list | None = None) -> str:
    job_id = job.id or 0
    placed = store.placed(job_id)
    return runner.run(
        job,
        source,
        library,
        lambda key: store.seen(job_id, key),
        lambda key: store.mark_seen(job_id, key),
        lambda seconds: None,
        report=lambda level, text: (lines if lines is not None else []).append((level, str(text))),
        record=lambda entry: store.record_placed(job_id, entry.key, entry.file_id, entry.values, entry.tags),
        owners={entry["file"]: key for key, entry in placed.items() if entry["file"] is not None},
    )


def test_a_file_of_the_same_content_at_another_address_is_a_duplicate(tmp_path):
    server = Server({"/a.pdf": b"%PDF-1 same", "/b.pdf": b"%PDF-1 same"})
    store, job, source = setup(tmp_path, server)
    library = FakeLibrary()
    assert go(store, job, source, library) == "1 downloaded, 1 duplicate (not saved again)"
    assert library.files == {"/In/a.pdf": b"%PDF-1 same"}
    assert store.seen(job.id or 0, "/b.pdf"), "a duplicate is not asked for again"
    assert (store.content(job.id or 0, "/b.pdf") or {})["path"] == "/In/a.pdf"


def test_known_files_are_not_asked_again_without_the_option(tmp_path):
    server = Server({"/a.pdf": b"%PDF-1 a", "/b.pdf": b"%PDF-1 b"})
    store, job, source = setup(tmp_path, server)
    library = FakeLibrary()
    go(store, job, source, library)
    server.files["/a.pdf"] = b"%PDF-1 a, corrected"
    assert go(store, job, source, library) == "0 downloaded"
    assert len(server.requests) == 2
    assert library.files["/In/a.pdf"] == b"%PDF-1 a"


def test_an_existing_job_has_the_option_off_a_new_job_on():
    assert Files({}, {}, None).settings["recheck"] == 0
    assert RECHECK.default == 1 and RECHECK.before == 0
    assert SOURCES["site"].options["recheck"] is RECHECK and SOURCES["feed"].options["recheck"] is RECHECK


def test_a_known_file_with_an_etag_is_asked_only_if_it_changed(tmp_path):
    server = Server({"/a.pdf": b"%PDF-1 a", "/b.pdf": b"%PDF-1 b"})
    store, job, source = setup(tmp_path, server, {"recheck": 1})
    library = FakeLibrary()
    assert go(store, job, source, library) == "2 downloaded"
    assert store.content(job.id or 0, "/a.pdf")["etag"]  # type: ignore[index]
    # Due again: the record says it was asked a day ago.
    store._db.execute("UPDATE contents SET at = ?", (time.time() - versions.RECHECK_EVERY - 1,))
    server.files["/a.pdf"] = b"%PDF-1 a, corrected"
    assert go(store, job, source, library) == "0 downloaded, 1 changed (a new version), 1 known file unchanged"
    assert server.asked("/b.pdf")[-1].headers["if-none-match"]
    assert library.files == {"/In/a.pdf": b"%PDF-1 a, corrected", "/In/b.pdf": b"%PDF-1 b"}, "saved over its own"
    # Not due again within RECHECK_EVERY: nothing is asked.
    before = len(server.requests)
    assert go(store, job, source, library) == "0 downloaded"
    assert len(server.requests) == before


def test_a_known_file_without_validators_is_compared_by_its_hash(tmp_path):
    server = Server({"/a.pdf": b"%PDF-1 a", "/b.pdf": b"%PDF-1 b"}, etag=False)
    store, job, source = setup(tmp_path, server, {"recheck": 1})
    library = FakeLibrary()
    go(store, job, source, library)
    store._db.execute("UPDATE contents SET at = 0")
    server.files["/b.pdf"] = b"%PDF-1 b, second edition"
    assert go(store, job, source, library) == "0 downloaded, 1 changed (a new version), 1 known file unchanged"
    assert "if-none-match" not in server.asked("/b.pdf")[-1].headers
    assert library.files["/In/b.pdf"] == b"%PDF-1 b, second edition"


def test_a_file_saved_before_the_record_is_learnt_and_a_deleted_one_stays_away(tmp_path):
    server = Server({"/a.pdf": b"%PDF-1 a", "/b.pdf": b"%PDF-1 b"}, etag=False)
    store, job, source = setup(tmp_path, server, {"recheck": 1})
    library = FakeLibrary()
    go(store, job, source, library)
    job_id = job.id or 0
    store._db.execute("DELETE FROM contents")
    server.files["/a.pdf"] = b"%PDF-1 a, changed before the record"
    assert go(store, job, source, library) == "0 downloaded, 2 known files unchanged"
    assert library.files["/In/a.pdf"] == b"%PDF-1 a", "no record: what it holds now is the measure"
    store._db.execute("UPDATE contents SET at = 0")
    del library.files["/In/b.pdf"]
    server.files["/b.pdf"] = b"%PDF-1 b, changed after its deletion"
    go(store, job, source, library)
    assert "/In/b.pdf" not in library.files, "a file its person deleted is not brought back without deleted.py"
    assert store.content(job_id, "/b.pdf")["hash"] == versions.digest(_bytes(b"%PDF-1 b"))  # type: ignore[index]


def test_a_known_file_that_fails_fails_nothing(tmp_path):
    server = Server({"/a.pdf": b"%PDF-1 a", "/b.pdf": b"%PDF-1 b"})
    store, job, source = setup(tmp_path, server, {"recheck": 1})
    library = FakeLibrary()
    go(store, job, source, library)
    store._db.execute("UPDATE contents SET at = 0")
    del server.files["/a.pdf"]
    lines: list = []
    assert go(store, job, source, library, lines) == "0 downloaded, 1 known file unchanged"
    assert ("info", "a.pdf could not be checked for changes: https://site.example/a.pdf answered 404") in lines
    assert not [line for line in lines if line[0] == "error"]


def test_pages_the_job_saves_are_never_asked_again():
    saving = Files({"recheck": 1}, {}, None)
    setattr(saving, "keeps", True)
    assert not saving.rechecks()
    assert Files({"recheck": 1}, {}, None).rechecks()


def test_http_sends_the_conditions_only_for_the_checked_address():
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304 if request.headers.get("if-modified-since") else 200, content=b"%PDF-1")

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        check = download.Check("https://site.example/a.pdf", modified="Mon, 02 Nov 2026 10:00:00 GMT")
        with download.checking(check):
            with download.http(client)("https://site.example/other.pdf") as response:
                assert response.status == 200
            try:
                download.fetch(download.http(client), check.url, _bytes(b""), {}, lambda seconds: None)
            except download.Unchanged:
                pass
            else:
                raise AssertionError("304 is Unchanged")
    assert "if-modified-since" not in seen[0].headers
    assert seen[1].headers["if-modified-since"] == "Mon, 02 Nov 2026 10:00:00 GMT"


def _bytes(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
