"""A run (with a fake source and a fake Nextcloud), the schedule and the database."""

# pylint: disable=missing-function-docstring

import datetime

from harvester import runner
from harvester.executor import is_due
from harvester.sources import Item, Source
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
