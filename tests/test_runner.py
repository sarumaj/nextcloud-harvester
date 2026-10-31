"""A run (with a fake source and a fake Nextcloud), the schedule and the database."""

# pylint: disable=missing-function-docstring

import datetime
import json
import sqlite3
import time

from nc_py_api import NextcloudExceptionNotFound

from harvester import executor as executor_module
from harvester import runner
from harvester.executor import is_due
from harvester.nextcloud import NextcloudLibrary
from harvester.sources import ChallengeError, Item, Source
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

    def find(self, path):
        return list(self.files).index(path) if path in self.files else None

    def put(self, path, content):
        self.files[path] = content.read()
        return list(self.files).index(path)

    def tag(self, file_id, tags):
        self.tags[file_id] = tags


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
    """A user's preferences in Nextcloud: a dictionary."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key, default=None):
        return self.values.get(key, default)

    def set_value(self, key, value, sensitive=None):  # pylint: disable=unused-argument
        self.values[key] = value


class FakeNextcloud:  # pylint: disable=too-few-public-methods
    """What the executor uses of NextcloudApp."""

    def __init__(self):
        self.preferences_ex = FakePreferences()
        self.sent: list[tuple[str, str, str]] = []
        self.notifications = self
        self.app_cfg = type("Config", (), {"endpoint": "https://cloud.example/"})()
        self.user = ""
        # Set by the tests that store files.
        self.files = None

    def set_user(self, user):
        self.user = user

    def create(self, subject, message="", link=""):
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
    """Nextcloud's files as nc_py_api reaches them: by_path raises for a missing path (404)."""

    def __init__(self):
        self.stored: dict[str, bytes] = {}

    def by_path(self, path):
        if path not in self.stored:
            raise NextcloudExceptionNotFound(f"<list: u, {path}>")
        return type("Node", (), {"info": type("Info", (), {"fileid": list(self.stored).index(path) + 1})()})()

    def makedirs(self, path, exist_ok=False):  # pylint: disable=unused-argument
        return None

    def upload_stream(self, path, content):
        self.stored[path] = content.read()
        return self.by_path(path)


def test_the_nextcloud_library_takes_a_missing_file_for_one_to_download():
    nc = FakeNextcloud()
    nc.files = FakeFiles()
    library = NextcloudLibrary(nc)  # type: ignore[arg-type]
    job = Job(user="u", name="n", source="fake", target="/Zeitschriften/ix", schedule="* * * * *")
    seen: set[str] = set()
    assert runner.run(job, FakeSource({}, {}, None), library, seen.__contains__, seen.add, lambda s: None) == (
        "3 downloaded"
    )
    assert library.find("/Zeitschriften/ix/a.txt") == 1 and library.find("/Zeitschriften/ix/missing") is None
