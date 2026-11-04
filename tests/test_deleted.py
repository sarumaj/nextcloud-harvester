# SPDX-License-Identifier: AGPL-3.0-or-later
"""Files deleted in Nextcloud (harvester.deleted, the option Download deleted files again): a
known item whose file is gone (deleted, in the trash) is downloaded again and the log says so,
one moved or renamed is kept; the files looked for by their IDs, those of a page's items in one
request, each once a run, only those the listing gives; a file without an ID by its recorded
path, its ID recorded then; a lookup that fails downloads nothing; a duplicate's record does
not make the file anew a duplicate of itself; a known file due for a check of changes that is
gone is downloaded anew; an unpacked ZIP is kept while its folder is; an address sent to an
on-demand job brings its file back whatever the option, else says where it is; and nc_py_api's
SEARCH of several IDs."""

# pylint: disable=missing-function-docstring,protected-access

import io
from xml.etree import ElementTree
import zipfile

import pytest
from nc_py_api import NextcloudException
from nc_py_api.files._files import build_search_req
from test_on_demand import Pages
from test_runner import FakeLibrary, FakeNextcloud, library_of

from harvester import deleted as deleted_module
from harvester import executor as executor_module
from harvester import places, runner, versions
from harvester.deleted import Deleted
from harvester.executor import Executor
from harvester.sources import Item, Source
from harvester.sources.base import RECHECK
from harvester.store import Job, Store


class Drive(FakeLibrary):
    """Files whose IDs stay theirs when other files go or move (as Nextcloud's); existing looks
    them up by ID, each call a request (asked: the IDs of each)."""

    def __init__(self, files=None):
        super().__init__()
        self.ids: dict[str, int] = {}
        self.next_id = 1
        self.asked: list[list[int]] = []
        self.broken = False
        for path, content in (files or {}).items():
            self.put(path, io.BytesIO(content))

    def find(self, path):
        if path in self.ids:
            return self.ids[path]
        inside = [file_id for name, file_id in self.ids.items() if name.startswith(path + "/")]
        return 10000 + inside[0] if inside else None  # a folder of files

    def put(self, path, content):
        self.files[path] = content.read()
        if path not in self.ids:
            self.ids[path] = self.next_id
            self.next_id += 1
        return self.ids[path]

    def existing(self, file_ids):
        self.asked.append(list(file_ids))
        if self.broken:
            raise runner.NotChecked("503 Service Unavailable")
        return {file_id: path for path, file_id in self.ids.items() if file_id in file_ids}

    def delete(self, path):
        """Deleted, or in the trash: not among the user's files either way."""
        del self.files[path]
        del self.ids[path]

    def move(self, path, to):
        self.files[to] = self.files.pop(path)
        self.ids[to] = self.ids.pop(path)


class Listing(Source):
    """Items by key (their file is the key's text), told ahead all at once as a listing's page."""

    name = "listing"
    title = "Listing"
    options = {"recheck": RECHECK}
    keys: list[str] = ["a", "b"]
    # A key's file content and name, where they are not its own (key.txt, the key's text).
    body: dict[str, bytes] = {}
    names: dict[str, str] = {}

    def items(self, seen):
        self.ahead(list(self.keys))
        for key in self.keys:
            if not seen(key):
                yield Item(key=key, path=self.names.get(key, f"{key}.txt"), url=f"https://site.example/{key}")

    def fetch(self, item, dest):
        dest.write(self.body.get(item.key, item.key.encode()))


def setup(tmp_path, options: dict | None = None, keys=("a", "b")):
    store = Store(str(tmp_path / "db"))
    job = store.save(
        Job(user="u", name="n", source="listing", target="/In", schedule="* * * * *", options=options or {})
    )
    Listing.keys = list(keys)
    Listing.body = {}
    Listing.names = {}
    return store, job


def go(store: Store, job: Job, library: Drive, lines: list | None = None, explicit: bool = False) -> str:
    """A run as the executor makes it, with its record of the job's files and Deleted."""
    job_id = job.id or 0
    placed = store.placed(job_id)
    source = Listing(job.options, {}, None)
    source.contents = versions.Contents(store, job_id)
    log = lines if lines is not None else []

    def report(level, text):
        log.append((level, str(text)))

    gone = Deleted(store, job, placed, library, report, explicit=explicit)
    return runner.run(
        job,
        source,
        library,
        lambda key: gone.seen(key, store.seen(job_id, key)),
        lambda key: store.mark_seen(job_id, key),
        lambda seconds: None,
        report=report,
        record=lambda entry: store.record_placed(job_id, entry.key, entry.file_id, entry.values, entry.tags),
        owners={entry["file"]: key for key, entry in placed.items() if entry["file"] is not None},
        deleted=gone,
    )


def test_a_deleted_file_is_downloaded_again_and_a_moved_one_is_kept(tmp_path):
    store, job = setup(tmp_path)
    library = Drive()
    assert go(store, job, library) == "2 downloaded"
    library.delete("/In/a.txt")
    library.move("/In/b.txt", "/Elsewhere/b renamed.txt")
    lines: list = []
    assert go(store, job, library, lines) == "1 downloaded, 1 known"
    assert library.files["/In/a.txt"] == b"a" and "/In/b.txt" not in library.files
    assert ("info", "Deleted in Nextcloud, downloaded again: /In/a.txt") in lines
    assert library.asked[-1] == [1, 2], "both looked for in one request"
    assert store.placed(job.id or 0)["a"]["file"] == library.ids["/In/a.txt"], "its new ID recorded"
    assert go(store, job, library) == "0 downloaded, 2 known", "there again: skipped"


def test_without_the_option_nothing_is_looked_for(tmp_path):
    store, job = setup(tmp_path, {runner.AGAIN: 0})
    library = Drive()
    go(store, job, library)
    library.delete("/In/a.txt")
    assert go(store, job, library) == "0 downloaded, 2 known"
    assert not library.asked


def test_the_option_is_on_for_new_jobs_and_those_saved_before():
    option = runner.COMMON_OPTIONS[runner.AGAIN]
    assert option.default == 1 and option.before == 1 and option.kind == "flag"
    job = Job(user="u", name="n", source="listing", target="/In", schedule="", options={"max_items": 3})
    assert Deleted(None, job, {}, Drive(), lambda level, text: None).active


def test_lookups_go_by_hundreds_once_a_run_and_only_for_what_the_listing_gives(tmp_path):
    store, job = setup(tmp_path, {"max_items": -1}, keys=[f"k{number}" for number in range(250)])
    library = Drive()
    go(store, job, library)
    store.mark_seen(job.id or 0, "elsewhere")
    store.record_placed(job.id or 0, "elsewhere", 999, {}, [])  # not listed: never looked for
    library.asked.clear()
    assert go(store, job, library) == "0 downloaded, 250 known"
    assert [len(ids) for ids in library.asked] == [100, 100, 50]
    assert 999 not in {file_id for ids in library.asked for file_id in ids}


def test_without_the_listing_telling_ahead_each_key_is_looked_for_once(tmp_path):
    store, job = setup(tmp_path)
    library = Drive()
    go(store, job, library)
    gone = Deleted(store, job, store.placed(job.id or 0), library, lambda level, text: None)
    assert not gone.gone("a") and not gone.gone("a") and not gone.gone("b")
    assert library.asked == [[1], [2]]
    assert not gone.gone("unknown") and not gone.gone(runner.FULL_PASS)


def test_a_lookup_that_fails_downloads_nothing_and_says_so(tmp_path):
    store, job = setup(tmp_path)
    library = Drive()
    go(store, job, library)
    library.delete("/In/a.txt")
    library.broken = True
    lines: list = []
    assert go(store, job, library, lines) == "0 downloaded, 2 known"
    assert (
        "info",
        "Could not ask Nextcloud whether 2 files are still there (503 Service Unavailable): not downloaded again",
    ) in lines
    assert len(library.asked) == 1, "once a run"


def test_a_file_without_an_id_is_looked_for_at_its_recorded_path(tmp_path):
    store, job = setup(tmp_path)
    library = Drive()
    go(store, job, library)
    job_id = job.id or 0
    for key in ("a", "b"):
        store.record_placed(job_id, key, None, {}, [])
    library.delete("/In/b.txt")
    assert go(store, job, library) == "1 downloaded, 1 known"
    assert store.placed(job_id)["a"]["file"] == library.ids["/In/a.txt"], "found there: its ID recorded"
    assert library.files["/In/b.txt"] == b"b"


def test_a_file_downloaded_again_is_no_duplicate_of_itself(tmp_path):
    store, job = setup(tmp_path)
    Listing.body = {"a": b"same", "b": b"same"}
    library = Drive()
    assert go(store, job, library) == "1 downloaded, 1 duplicate (not saved again)"
    library.delete("/In/a.txt")
    assert go(store, job, library) == "1 downloaded, 1 known"
    assert library.files["/In/a.txt"] == b"same"
    assert store.content(job.id or 0, "b") is None, "the duplicate's record of the gone file goes with it"


def test_a_known_file_due_for_a_check_of_changes_that_is_gone_is_downloaded_anew(tmp_path):
    store, job = setup(tmp_path, {"recheck": 1})
    library = Drive()
    go(store, job, library)
    store._db.execute("UPDATE contents SET at = 0")
    library.delete("/In/a.txt")
    lines: list = []
    assert go(store, job, library, lines) == "1 downloaded, 1 known file unchanged"
    assert library.files["/In/a.txt"] == b"a"
    assert ("info", "Deleted in Nextcloud, downloaded again: /In/a.txt") in lines


def test_an_unpacked_zip_counts_as_kept_while_its_folder_is_there(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as made:
        made.writestr("one.txt", b"1")
    store, job = setup(tmp_path, {"unpack": 1}, keys=["r"])
    Listing.body = {"r": data.getvalue()}
    Listing.names = {"r": "r.zip"}
    library = Drive()
    assert go(store, job, library) == "1 downloaded"
    assert "/In/r/one.txt" in library.files
    library.delete("/In/r.zip")
    assert go(store, job, library) == "0 downloaded, 1 known", "its files are there"
    library.delete("/In/r/one.txt")
    assert go(store, job, library) == "1 downloaded", "archive and folder gone"
    assert "/In/r.zip" in library.files and "/In/r/one.txt" in library.files


def test_an_explicit_request_brings_the_file_back_whatever_the_option_or_says_where_it_is(tmp_path):
    store, job = setup(tmp_path, {runner.AGAIN: 0})
    library = Drive()
    go(store, job, library)
    library.delete("/In/a.txt")
    library.move("/In/b.txt", "/Moved/b.txt")
    lines: list = []
    assert go(store, job, library, lines, explicit=True) == "1 downloaded, 1 known"
    assert ("info", "Fetched before, in your files at /Moved/b.txt") in lines
    assert ("info", "Deleted in Nextcloud, downloaded again: /In/a.txt") in lines


def test_a_fetch_of_the_executor_brings_a_deleted_file_back(tmp_path, monkeypatch):
    library = Drive()
    monkeypatch.setitem(executor_module.SOURCES, "pages", Pages)
    monkeypatch.setattr(places, "NextcloudLibrary", lambda nc, call: library)
    store = Store(str(tmp_path / "h.db"))
    nc = FakeNextcloud()
    executor = Executor(store, lambda: nc, runners=1)
    job = store.save(Job(user="anna", name="Clips", source="pages", target="/Clips", schedule=""))
    job_id = job.id or 0
    for _ in range(2):
        fetch, _ = executor.fetch(job_id, "https://v.example/1")
        executor.run(store.job(job_id) or job, wanted=False, fetch=fetch)
        if "/Clips/a.txt" in library.files:
            library.delete("/Clips/a.txt")
    lines = [line["message"] for line in store.log_lines("anna", job_id)]
    assert "Deleted in Nextcloud, downloaded again: /Clips/a.txt" in lines
    assert "Fetched before, in your files at /Clips/b.txt" in lines
    assert lines[-1] == "https://v.example/1: 1 downloaded, 1 known"


def test_the_nextcloud_library_asks_for_several_ids_in_one_search():
    class Searched:  # pylint: disable=too-few-public-methods
        """nc_py_api's files: find answers the IDs 2 and 3, moved; asked: its conditions."""

        def __init__(self, error: Exception | None = None):
            self.asked: list[list] = []
            self.error = error

        async def find(self, req, path=""):  # pylint: disable=unused-argument
            self.asked.append(list(req))
            if self.error:
                raise self.error
            return [
                type("Node", (), {"info": type("Info", (), {"fileid": file_id})(), "user_path": path_})()
                for file_id, path_ in ((2, "Moved/b.txt"), (3, "Folder/"))
            ]

    nc = FakeNextcloud()
    nc.files = Searched()
    assert library_of(nc).existing([1, 2, 3]) == {2: "/Moved/b.txt", 3: "/Folder"}
    where = ElementTree.Element("d:where")
    build_search_req(where, list(nc.files.asked[0]))
    literals = [element.text for element in where.iter("d:literal")]
    assert literals == ["1", "2", "3"] and len(list(where.iter("d:or"))) == 2
    assert library_of(nc).existing([]) == {} and len(nc.files.asked) == 1
    nc.files = Searched(NextcloudException(503, "Service Unavailable"))
    with pytest.raises(runner.NotChecked):
        library_of(nc).existing([1])


def test_a_library_that_cannot_tell_checks_nothing(tmp_path):
    store, job = setup(tmp_path)
    library = FakeLibrary()
    gone = Deleted(store, job, {"a": {"file": 1, "vals": {}, "tags": []}}, library, lambda level, text: None)
    assert not gone.gone("a"), "kept: skipped as without the option"
    assert deleted_module.BATCH == 100
