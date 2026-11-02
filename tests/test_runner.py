"""A run (with a fake source and a fake Nextcloud), the schedule and the database."""

# pylint: disable=missing-function-docstring

import asyncio
import datetime
import io
import json
import sqlite3
import time

import pytest

from nc_py_api import NextcloudException, NextcloudExceptionNotFound

from harvester import executor as executor_module
from harvester import runner
from harvester.executor import is_due
from harvester.patterns import render_tags
from harvester.nextcloud import NextcloudLibrary
from harvester.sources import ChallengeError, Item, Source, SourceError
from harvester.store import Job, Store


class FakeSource(Source):
    """Items a, b and c; each gives its letter as the value {letter}."""

    name = "fake"
    title = "Fake"
    pause = 5.0

    def items(self, seen):
        for key in ("a", "b", "c"):
            if not seen(key):
                yield Item(key=key, path=f"{key}.txt", url=key, values={"letter": key})

    def fetch(self, item, dest):
        dest.write(item.key.encode())


class FakeLibrary(runner.Library):
    """Files in a dictionary, by path; tags by file ID."""

    def __init__(self, files=None):
        self.files: dict[str, bytes] = dict(files or {})
        self.tags: dict[int, list[str]] = {}
        # Files deleted by their user since: tagging them fails as in Nextcloud.
        self.gone: set[int] = set()

    def find(self, path):
        return list(self.files).index(path) if path in self.files else None

    def put(self, path, content):
        self.files[path] = content.read()
        return list(self.files).index(path)

    def tag(self, file_id, tags):
        if file_id in self.gone:
            raise runner.FileGone(file_id)
        current = self.tags.setdefault(file_id, [])
        current += [tag for tag in tags if tag not in current]

    def untag(self, file_id, tags):
        if file_id in self.gone:
            raise runner.FileGone(file_id)
        self.tags[file_id] = [tag for tag in self.tags.get(file_id, []) if tag not in tags]


def test_run_downloads_new_files_tags_them_and_keeps_existing_ones():
    job = Job(
        user="u",
        name="n",
        source="fake",
        target="/In",
        schedule="* * * * *",
        tags=["mine", "letter {letter}", "{missing}"],
    )
    library = FakeLibrary({"/In/a.txt": b"by hand"})
    seen: set[str] = set()
    sleeps: list[float] = []
    message = runner.run(job, FakeSource({}, {}, None), library, seen.__contains__, seen.add, sleeps.append)
    assert message == "2 downloaded, 1 already there"
    assert library.files == {"/In/a.txt": b"by hand", "/In/b.txt": b"b", "/In/c.txt": b"c"}
    assert library.tags[0] == ["mine", "letter a"]
    assert seen == {"a", "b", "c", runner.FULL_PASS}
    assert sleeps == [5.0]


def test_run_stops_after_max_items():
    job = Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *", options={"max_items": 1})
    seen: set[str] = set()
    library = FakeLibrary()
    assert runner.run(job, FakeSource({}, {}, None), library, seen.__contains__, seen.add) == "1 downloaded"
    assert seen == {"a"}, "a run stopped by max_items is not a full pass"


def test_schedule():
    created = datetime.datetime(2026, 10, 30, 5, 0, tzinfo=datetime.UTC).timestamp()
    job = Job(user="u", name="n", source="fake", target="/", schedule="0 6 * * *", created=created)
    assert not is_due(job, created + 59 * 60)
    assert is_due(job, created + 60 * 60)
    job.last_run = created + 60 * 60
    assert not is_due(job, created + 2 * 60 * 60)
    job.enabled = False
    assert not is_due(job, created + 48 * 60 * 60)


def test_store(tmp_path):
    store = Store(str(tmp_path / "db"))
    job = store.save(
        Job(user="u", name="n", source="web", target="/x", schedule="0 6 * * *", options={"a": 1}, tags=["t"])
    )
    assert store.job(job.id or 0) == job
    assert store.jobs("other") == []
    store.mark_seen(job.id or 0, "k")
    assert store.seen(job.id or 0, "k")
    store.finish(job.id or 0, "ok", "1 downloaded")
    assert (store.job(job.id or 0) or job).last_message == "1 downloaded"
    store.delete(job.id or 0)
    assert store.jobs() == [] and not store.seen(job.id or 0, "k")


class FakePreferences:
    """A user's preferences in Nextcloud: a dictionary (async, as nc_py_api's client)."""

    def __init__(self):
        self.values: dict[str, str] = {}

    async def get_value(self, key, default=None):
        return self.values.get(key, default)

    async def set_value(self, key, value, sensitive=None):  # pylint: disable=unused-argument
        self.values[key] = value

    async def delete(self, keys, not_fail=True):  # pylint: disable=unused-argument
        for key in [keys] if isinstance(keys, str) else keys:
            self.values.pop(key, None)


class FakeNextcloud:  # pylint: disable=too-few-public-methods
    """What the executor uses of AsyncNextcloudApp."""

    def __init__(self):
        self.preferences_ex = FakePreferences()
        self.sent: list[tuple[str, str, str]] = []
        self.notifications = self
        self.app_cfg = type("Config", (), {"endpoint": "https://cloud.example/"})()
        self._user = ""
        # Set by the tests that store files.
        self.files = None

    @property
    async def user(self):
        return self._user

    async def set_user(self, user):
        self._user = user

    async def create(self, subject, message="", link=""):
        self.sent.append((subject, message, link))


class AskingSource(FakeSource):
    """A source whose site asks for a person, after setting a cookie."""

    name = "asking"

    def items(self, seen):
        raise ChallengeError("https://site.example/list", "reCAPTCHA")
        yield  # pylint: disable=unreachable

    def session(self):
        return {"cookies": [{"name": "sid", "value": "1"}], "origins": []}


def test_a_challenge_waits_for_the_user(tmp_path, monkeypatch):
    monkeypatch.setitem(executor_module.SOURCES, "asking", AskingSource)
    store = Store(str(tmp_path / "db"))
    job = store.save(Job(user="u", name="Site", source="asking", target="/x", schedule="* * * * *"))
    nc = FakeNextcloud()
    executor_module.Executor(store, lambda: nc).run(job)

    stored = store.job(job.id or 0)
    assert stored is not None and stored.last_status == "challenge"
    assert stored.challenge_url == "https://site.example/list"
    assert nc.sent == [
        (
            "Harvester: Site needs you",
            "https://site.example/list asks for a person (reCAPTCHA).",
            "https://cloud.example/index.php/apps/app_api/embedded/harvester/harvester",
        )
    ]
    assert json.loads(nc.preferences_ex.values[f"session.job{job.id}"])["cookies"][0]["name"] == "sid"
    assert not is_due(stored, time.time() + 3600), "a job waiting for its user is not run by its schedule"


def test_an_older_database_gets_the_new_column(tmp_path):
    path = str(tmp_path / "db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, user TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL,"
        " options TEXT NOT NULL, target TEXT NOT NULL, tags TEXT NOT NULL, schedule TEXT NOT NULL,"
        " enabled INTEGER NOT NULL, created REAL NOT NULL, last_run REAL, last_status TEXT, last_message TEXT);"
        "INSERT INTO jobs VALUES (1, 'u', 'n', 'site', '{}', '/x', '[]', '* * * * *', 1, 0, NULL, NULL, NULL);"
    )
    old.commit()
    old.close()
    job = Store(path).job(1)
    assert job is not None and job.challenge_url is None


class FakeFiles:
    """Nextcloud's files as nc_py_api's async client reaches them: by_path raises for a missing
    path, as 0.30.3 does, a plain NextcloudException with status 404 (check_error, not its
    NextcloudExceptionNotFound); assigning a tag twice and creating an existing one answer 409
    Conflict; "hidden" exists but is not visible to the user. upload_stream's node has no id
    (0), as nc_py_api's, and tagging a file that does not exist answers 404."""

    def __init__(self):
        self.stored: dict[str, bytes] = {}
        self.tags: dict[str, int] = {}
        self.assigned: set[tuple[int, int]] = set()

    async def by_path(self, path):
        if path not in self.stored:
            raise NextcloudException(404, "Not Found", info=f"list: u, {path}, [...]")
        return type("Node", (), {"info": type("Info", (), {"fileid": list(self.stored).index(path) + 1})()})()

    async def makedirs(self, path, exist_ok=False):  # pylint: disable=unused-argument
        return None

    async def upload_stream(self, path, content):
        # As nc_py_api's: the node's info.fileid is 0, only by_path gives the id.
        self.stored[path] = content.read()
        return type("Node", (), {"info": type("Info", (), {"fileid": 0})()})()

    async def list_tags(self):
        return [type("Tag", (), {"display_name": name, "tag_id": i})() for name, i in self.tags.items()]

    async def create_tag(self, name):
        if name in self.tags or name == "hidden":
            raise NextcloudException(409, "Conflict")
        self.tags[name] = len(self.tags) + 1

    async def assign_tag(self, file_id, tag_id):
        if file_id not in range(1, len(self.stored) + 1):
            raise NextcloudException(404, "Not Found", info=f"(Adding `{tag_id}` to {file_id})")
        if (file_id, tag_id) in self.assigned:
            raise NextcloudException(409, "Conflict")
        self.assigned.add((file_id, tag_id))

    async def unassign_tag(self, file_id, tag_id):
        # Nextcloud answers 404 for a file that is gone and for a tag the file does not have.
        if (file_id, tag_id) not in self.assigned:
            raise NextcloudException(404, "Not Found")
        self.assigned.discard((file_id, tag_id))

    async def by_id(self, file_id):
        return object() if file_id in range(1, len(self.stored) + 1) else None


def library_of(nc) -> NextcloudLibrary:
    """The Nextcloud library over the fake, its requests run to their end as the executor does."""
    return NextcloudLibrary(nc, asyncio.new_event_loop().run_until_complete)  # type: ignore[arg-type]


def test_the_nextcloud_library_takes_a_missing_file_for_one_to_download():
    nc = FakeNextcloud()
    nc.files = FakeFiles()
    library = library_of(nc)
    job = Job(user="u", name="n", source="fake", target="/Zeitschriften/ix", schedule="* * * * *")
    seen: set[str] = set()
    assert runner.run(job, FakeSource({}, {}, None), library, seen.__contains__, seen.add, lambda s: None) == (
        "3 downloaded"
    )
    assert library.find("/Zeitschriften/ix/a.txt") == 1 and library.find("/Zeitschriften/ix/missing") is None


def test_a_missing_file_is_any_404_and_other_errors_stop_the_run():
    """The status decides: nc_py_api's NotFound (should a later version raise it) is missing too,
    and an error that is not 404 stops the run instead of downloading the file again."""

    class Raising(FakeFiles):
        """by_path always fails with the given error."""

        def __init__(self, error: Exception):
            super().__init__()
            self.error = error

        async def by_path(self, path):
            raise self.error

    nc = FakeNextcloud()
    nc.files = Raising(NextcloudExceptionNotFound("<list: u, /x>"))
    assert library_of(nc).find("/x") is None
    nc.files = Raising(NextcloudException(500, "Internal Server Error"))
    with pytest.raises(NextcloudException):
        library_of(nc).find("/x")


def test_tagging_survives_tags_already_there_and_hidden_ones():
    nc = FakeNextcloud()
    nc.files = FakeFiles()
    library = library_of(nc)
    job = Job(user="u", name="n", source="fake", target="/x", schedule="* * * * *", tags=["mine", "hidden"])
    seen: set[str] = set()
    runner.run(job, FakeSource({}, {}, None), library, seen.__contains__, seen.add, lambda s: None)
    assert nc.files.assigned == {(1, 1), (2, 1), (3, 1)}, "'mine' on each file, 'hidden' left out"
    # A run that broke off after a file was tagged: it is found again and tagged again.
    library.tag(1, ["mine"])
    assert library_of(nc).find("/x/a.txt") == 1


class PartlyBroken(FakeSource):
    """Items a, b and c; b's site answers an error page."""

    def fetch(self, item, dest):
        if item.key == "b":
            raise SourceError("https://site.example/b answered 500")
        dest.write(item.key.encode())


def test_a_file_that_fails_does_not_block_the_others():
    job = Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *")
    seen: set[str] = set()
    library = FakeLibrary()
    message = runner.run(job, PartlyBroken({}, {}, None), library, seen.__contains__, seen.add, lambda s: None)
    assert message == "2 downloaded, 1 failed (tried again next run): https://site.example/b answered 500"
    assert seen == {"a", "c"}, "b is tried again next run; no complete pass yet"


def test_a_run_where_every_file_fails_fails():
    class Broken(FakeSource):
        """Every file's site answers an error page."""

        def fetch(self, item, dest):
            raise SourceError(f"{item.key} answered 500")

    job = Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *")
    with pytest.raises(SourceError, match="a answered 500 \\(and 2 more\\)"):
        runner.run(job, Broken({}, {}, None), FakeLibrary(), lambda key: False, lambda key: None, lambda s: None)


def test_an_empty_file_name_is_not_the_jobs_folder():
    class Nameless(FakeSource):
        """An item whose name came out empty."""

        def items(self, seen):
            yield Item(key="x", path="", url="https://site.example/x")

    job = Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *")
    with pytest.raises(SourceError, match="came out empty"):
        runner.run(job, Nameless({}, {}, None), FakeLibrary(), lambda key: False, lambda key: None, lambda s: None)


def test_a_job_deleted_while_it_runs_ends_quietly(tmp_path):
    store = Store(str(tmp_path / "db"))
    job = store.save(Job(user="u", name="Gone", source="asking", target="/x", schedule="* * * * *"))

    class DeletedMeanwhile(FakeSource):
        """Its job is deleted while it lists."""

        name = "deleted"

        def items(self, seen):
            store.delete(job.id or 0)
            yield Item(key="a", path="a.txt", url="a")

        def session(self):
            return {"cookies": [{"name": "sid", "value": "1"}], "origins": []}

    job.source = "deleted"
    executor_module.SOURCES["deleted"] = DeletedMeanwhile
    try:
        nc = FakeNextcloud()
        nc.files = FakeFiles()
        executor_module.Executor(store, lambda: nc).run(job)
    finally:
        del executor_module.SOURCES["deleted"]
    assert not nc.sent, "no notification about a job that is gone"
    assert f"session.job{job.id}" not in nc.preferences_ex.values, "no session left behind"
    store.mark_seen(job.id or 0, "late")  # a late record of a deleted job is no error


def test_notifications_and_tags_come_out_as_they_are():
    assert executor_module.plain("{year}/x names issue, see /login?forward=%2Fselect") == (
        "(year)/x names issue, see /login?forward=％2Fselect"
    )
    assert len(render_tags(["{subject}"], {"subject": "Re: " * 30})[0]) <= 64


def test_minus_one_files_per_run_means_no_limit():
    job = Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *", options={"max_items": -1})
    seen: set[str] = set()
    assert runner.run(job, FakeSource({}, {}, None), FakeLibrary(), seen.__contains__, seen.add, lambda s: None) == (
        "3 downloaded"
    )
    assert runner.FULL_PASS in seen


def test_a_run_records_its_files_and_their_tags():
    job = Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *", tags=["letter {letter}"])
    recorded: list[runner.Placed] = []
    seen: set[str] = set()
    runner.run(job, FakeSource({}, {}, None), FakeLibrary(), seen.__contains__, seen.add, record=recorded.append)
    assert recorded == [
        runner.Placed("a", 0, {"letter": "a"}, ["letter a"]),
        runner.Placed("b", 1, {"letter": "b"}, ["letter b"]),
        runner.Placed("c", 2, {"letter": "c"}, ["letter c"]),
    ]


def test_new_tags_reach_the_recorded_files_and_the_old_ones_go():
    job = Job(user="u", name="n", source="fake", target="/In", schedule="", tags=["kept", "year {letter}"])
    library = FakeLibrary()
    library.tags = {0: ["kept", "old", "by hand"], 1: ["kept", "old"], 2: ["kept"]}
    library.gone = {2}
    placed = [
        runner.Placed("a", 0, {"letter": "a"}, ["kept", "old"]),
        runner.Placed("b", 1, {"letter": "b"}, ["kept", "old"]),
        runner.Placed("c", 2, {"letter": "c"}, ["kept", "old"]),
        runner.Placed("d", None, {"letter": "d"}, []),
    ]
    recorded: list[runner.Placed] = []
    assert runner.retag_placed(job, placed, library, recorded.append) == 2
    assert library.tags[0] == ["kept", "by hand", "year a"], "the user's own tag stays, the job's old one goes"
    assert library.tags[1] == ["kept", "year b"]
    assert recorded == [
        runner.Placed("a", 0, {"letter": "a"}, ["kept", "year a"]),
        runner.Placed("b", 1, {"letter": "b"}, ["kept", "year b"]),
        runner.Placed("c", None, {"letter": "c"}, []),
    ], "a deleted file is recorded as gone"
    assert runner.retag_placed(job, [r for r in recorded if r.file_id is not None], library, recorded.append) == 0


def test_files_saved_before_they_were_recorded_are_found_without_downloads():
    job = Job(user="u", name="n", source="fake", target="/In", schedule="", tags=["new {letter}"])
    library = FakeLibrary({"/In/a.txt": b"a", "/In/b.txt": b"b"})
    library.tags = {0: ["old a", "by hand"], 1: ["old b"]}
    recorded: list[runner.Placed] = []
    tagged, whole = runner.retag_earlier(
        job, FakeSource({}, {}, None), library, {"a", "c"}.__contains__, recorded.append, previous=["old {letter}"]
    )
    assert (tagged, whole) == (1, True)
    assert library.tags[0] == ["by hand", "new a"], "the previous tags' file values, the user's own stays"
    assert library.tags[1] == ["old b"], "b was recorded already: not this pass's"
    assert library.files == {"/In/a.txt": b"a", "/In/b.txt": b"b"}, "nothing downloaded, c not fetched"
    assert recorded == [
        runner.Placed("a", 0, {"letter": "a"}, ["new a"]),
        runner.Placed("c", None, {"letter": "c"}, []),
    ]


def test_the_store_keeps_the_files_and_the_executor_the_first_previous_tags(tmp_path):
    store = Store(str(tmp_path / "db"))
    job = store.save(Job(user="u", name="n", source="fake", target="/In", schedule=""))
    store.record_placed(job.id or 0, "a", 7, {"year": 2026}, ["x"])
    store.record_placed(job.id or 0, "a", 7, {"year": 2026}, ["y"])
    store.record_placed(job.id or 0, "b", None, {}, [])
    assert store.placed(job.id or 0) == {
        "a": {"file": 7, "vals": {"year": 2026}, "tags": ["y"]},
        "b": {"file": None, "vals": {}, "tags": []},
    }
    store.mark_seen(job.id or 0, "a")
    assert store.seen_keys(job.id or 0) == {"a"}
    executor = executor_module.Executor(store)
    executor.retag(job.id or 0, ["first"])
    executor.retag(job.id or 0, ["second"])
    pending = executor._retags  # pylint: disable=protected-access
    assert pending == {job.id: ["first"]}, "the tags the files have are the first change's"
    store.delete(job.id or 0)
    assert not store.placed(job.id or 0), "deleted with its job"


def test_nextcloud_takes_tags_off_and_tells_a_gone_file_from_a_missing_tag():
    nc = FakeNextcloud()
    nc.files = FakeFiles()
    library = library_of(nc)
    file_id = library.put("/In/a.pdf", io.BytesIO(b"a"))
    library.tag(file_id, ["x", "y"])
    library.untag(file_id, ["x", "not on it"])
    assert nc.files.assigned == {(file_id, nc.files.tags["y"])}
    library.untag(file_id, ["x"])  # not on it any more: no matter
    with pytest.raises(runner.FileGone):
        library.untag(99, ["y"])
    with pytest.raises(runner.FileGone):
        library.tag(99, ["y"])
