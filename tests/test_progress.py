# SPDX-License-Identifier: AGPL-3.0-or-later
"""The runs' progress log: what the runner reports, how the store keeps it, and a new run's start."""

# pylint: disable=missing-function-docstring

from test_runner import FakeLibrary, FakeNextcloud, FakeSource

from harvester import executor as executor_module
from harvester import runner, store as store_module
from harvester.sources import FULL_PASS, Item, SourceError
from harvester.store import Job, Store


def job(**options) -> Job:
    return Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *", options=options)


class BrokenB(FakeSource):
    """b is refused by the site."""

    def fetch(self, item, dest):
        if item.key == "b":
            raise SourceError("https://site.example/b answered 403")
        super().fetch(item, dest)


def test_the_runner_reports_each_step():
    lines: list[tuple[str, str]] = []
    library = FakeLibrary({"/In/a.txt": b"by hand"})
    seen: set[str] = set()

    def report(level: str, text: str) -> None:
        lines.append((level, text))

    runner.run(job(), BrokenB({}, {}, None), library, seen.__contains__, seen.add, lambda s: None, report=report)
    assert lines == [
        ("info", "Looking for new files"),
        ("info", "Already in the folder, tagged: /In/a.txt"),
        ("info", "Downloading /In/b.txt"),
        ("error", "b.txt: https://site.example/b answered 403"),
        ("info", "Downloading /In/c.txt"),
        ("info", "Saved /In/c.txt (1 B)"),
    ]


def test_the_runner_says_why_it_stops_early():
    lines: list[tuple[str, str]] = []
    seen: set[str] = set()
    runner.run(job(max_items=1), FakeSource({}, {}, None), FakeLibrary(), seen.__contains__, seen.add,
               report=lambda level, text: lines.append((level, text)))  # fmt: skip
    assert lines[-1] == ("info", "1 file this run (Files per run): the rest comes next run")


class Addressed(FakeSource):
    """A source with an address (its option "url"), whose listing asks seen() about a mark too."""

    address_option = "url"

    def items(self, seen):
        seen(FULL_PASS)
        yield from super().items(seen)


def test_the_run_names_its_address_and_counts_the_known_files():
    lines: list[tuple[str, str]] = []
    seen: set[str] = {"a", "b", FULL_PASS}
    source = Addressed({}, {}, None)
    outcome = runner.run(job(url="https://site.example/list"), source, FakeLibrary(), seen.__contains__, seen.add,
                         lambda s: None, report=lambda level, text: lines.append((level, text)))  # fmt: skip
    assert lines[0] == ("info", "Looking for new files at https://site.example/list")
    assert lines[-1] == ("info", "2 listed files are known already: not downloaded again")
    assert lines[-1][1].template["count"] == 2
    assert outcome == "1 downloaded, 2 known", "not the mark of a complete pass, nor the file of this run"


def test_sizes_read_as_people_say_them():
    assert [runner.size_text(n) for n in (0, 999, 1000, 5_242_880, 3 * 10**9)] == [
        "0 B",
        "999 B",
        "1.0 KB",
        "5.2 MB",
        "3.0 GB",
    ]


def test_the_log_is_kept_per_user_and_job_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "LOG_KEPT", 3)
    store = Store(str(tmp_path / "db"))
    mine = store.save(job())
    other = store.save(Job(user="v", name="theirs", source="fake", target="/x", schedule="* * * * *"))
    for number in range(5):
        store.log(mine.id or 0, "info", f"line {number}")
    store.log(other.id or 0, "info", "not yours")
    lines = store.log_lines("u")
    assert [line["message"] for line in lines] == ["line 2", "line 3", "line 4"]
    assert {line["name"] for line in lines} == {"n"}
    assert store.log_lines("u", after=lines[1]["id"])[0]["message"] == "line 4"
    assert store.log_lines("u", job_id=other.id) == []
    store.log(9999, "info", "a job deleted meanwhile")  # no error
    store.delete(mine.id or 0)
    assert store.log_lines("u") == []


def test_a_new_run_clears_the_last_message_and_logs_its_end(tmp_path, monkeypatch):
    class Failing(FakeSource):
        """A listing that breaks after its first file."""

        name = "failing"

        def items(self, seen):
            yield Item(key="x", path="x.txt", url="x")
            raise SourceError("the listing broke")

    monkeypatch.setitem(executor_module.SOURCES, "failing", Failing)
    store = Store(str(tmp_path / "db"))
    saved = store.save(Job(user="u", name="Site", source="failing", target="/x", schedule="* * * * *"))
    store.finish(saved.id or 0, "failed", "an old error")
    started: list[str | None] = []
    begin = store.begin

    def watch(job_id):
        begin(job_id)
        started.append((store.job(job_id) or saved).last_message)

    monkeypatch.setattr(store, "begin", watch)
    nc = FakeNextcloud()
    nc.files = None
    executor = executor_module.Executor(store, lambda: nc)
    monkeypatch.setattr(executor_module, "NextcloudLibrary", lambda nc, call: FakeLibrary())
    executor.run(store.job(saved.id or 0) or saved)
    assert started == [None], "the old error is gone as soon as the run starts"
    messages = [(line["level"], line["message"]) for line in store.log_lines("u")]
    assert messages[0] == ("info", "Looking for new files")
    assert messages[-1] == ("error", "the listing broke")
    assert (store.job(saved.id or 0) or saved).last_message == "the listing broke"


def test_the_runs_upload_in_chunks_nextcloud_takes_on_s3(monkeypatch):
    # nc_py_api's v2 names the first chunk 0, which Nextcloud refuses (400) where the
    # storage takes multipart uploads; Harvester's client uses v1.
    for name, value in {
        "NEXTCLOUD_URL": "https://cloud.example",
        "APP_ID": "harvester",
        "APP_VERSION": "0.0.0",
        "APP_SECRET": "s",
        "AA_VERSION": "3.0.0",
    }.items():
        monkeypatch.setenv(name, value)
    app = executor_module.connect_nextcloud()
    assert app._session.cfg.options.upload_chunk_v2 is False  # pylint: disable=protected-access
