"""What a run does with the files it cannot or should not take, names that collide, a changed
account, tags changed in the meantime and schedules in a time zone."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import contextlib
import datetime
import io
import threading

import httpx
import pytest
from test_media import CHANNEL, FakeYoutubeDL, media, video
from test_releases import release
from test_releases import source as releases_source
from test_runner import FakeFiles, FakeLibrary, FakeNextcloud, FakeSource

from harvester import executor as executor_module
from harvester import runner, schedule
from harvester.sources import FULL_PASS, REFUSED, AuthError, Denied, Item, Refused, SourceError, download
from harvester.store import Job, Store


def job(**extra) -> Job:
    return Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *", **extra)


def run(source, library=None, seen=None, **options) -> tuple[str, set[str], list[str]]:
    seen = set() if seen is None else seen
    log: list[str] = []
    message = runner.run(
        job(options=options),
        source,
        library if library is not None else FakeLibrary(),
        seen.__contains__,
        seen.add,
        lambda seconds: None,
        report=lambda level, text: log.append(text),
    )
    return message, seen, log


class Failing(FakeSource):
    """Items a, b and c; fetching those in failures raises what is given for them."""

    def __init__(self, failures: dict):
        super().__init__({}, {}, None)
        self.failures = failures

    def fetch(self, item, dest):
        if item.key in self.failures:
            raise self.failures[item.key]
        super().fetch(item, dest)


# 401 and 403: one file's, unless all are refused.


def test_a_file_the_site_refuses_fails_alone():
    message, seen, _ = run(Failing({"b": Denied("https://s.example/b answered 403")}))
    assert message.startswith("2 downloaded, 1 failed (tried again next run)")
    assert "b" not in seen and {"a", "c"} <= seen


def test_every_file_refused_is_the_accounts_trouble_and_one_alone_is_not():
    refused = {key: Denied(f"https://s.example/{key} answered 401") for key in "abc"}
    with pytest.raises(AuthError, match="answered 401"):
        run(Failing(refused))

    class One(Failing):
        """Only item a."""

        def items(self, seen):
            yield Item(key="a", path="a.txt", url="a")

    with pytest.raises(SourceError) as caught:
        run(One({"a": refused["a"]}))
    assert not isinstance(caught.value, AuthError), "one file taken away does not blame the account"


def test_a_download_answering_401_or_403_is_denied_not_an_auth_error():
    @contextlib.contextmanager
    def opener(url):
        yield download.Response(403, url, {}, iter([b""]))

    with pytest.raises(Denied) as caught:
        download.fetch(opener, "https://s.example/episode.mp3", io.BytesIO(), {}, lambda seconds: None)
    assert not isinstance(caught.value, AuthError)


# Refused by type or size: skipped for good, until the types or sizes change.


def test_a_file_of_another_type_or_too_large_is_skipped_and_not_tried_again(tmp_path):
    source = Failing({"b": Refused("https://s.example/b is not of the job's file types (image)")})
    message, seen, log = run(source)
    assert message == "2 downloaded, 1 skipped (not of the job's file types, or too large)"
    assert {"b", REFUSED + "b", FULL_PASS} <= seen
    assert "Skipped b.txt: https://s.example/b is not of the job's file types (image)" in log
    assert run(source, seen=seen)[0] == "0 downloaded", "not tried again"

    store = Store(str(tmp_path / "db"))
    job_id = store.save(job()).id or 0
    for key in seen:
        store.mark_seen(job_id, key)
    store.forget_refused(job_id)
    assert store.seen_keys(job_id) == {"a", "c", FULL_PASS}, "other types or sizes: b is tried again"


def test_the_download_checks_refuse_by_type_and_size():
    @contextlib.contextmanager
    def opener(url):
        yield download.Response(200, url, {"content-length": "3000000"}, iter([b"GIF89a" + b"x" * 100]))

    def fetch(**settings):
        download.fetch(opener, "https://s.example/a", io.BytesIO(), settings, lambda seconds: None)

    with pytest.raises(Refused, match="not of the job's file types"):
        fetch(file_type="pdf")
    with pytest.raises(Refused, match="3 MB, more than the job's largest file"):
        fetch(max_size=2)
    fetch(max_size=4)  # megabytes of 1000 × 1000 bytes, as the form shows them: 3 MB fit in 4
    with pytest.raises(SourceError, match="gave only") as caught:
        fetch(min_size=1000)
    assert not isinstance(caught.value, Refused), "a short answer is an error page, tried again"


# Names that cannot be made: that file fails, not the listing.


def test_an_item_whose_name_cannot_be_made_fails_alone():
    class Nameless(FakeSource):
        """b's name cannot be made."""

        def items(self, seen):
            for item in super().items(seen):
                if item.key == "b":
                    item = Item(key="b", path="", url="b", problem=SourceError("{year} names year, which ..."))
                yield item

    message, seen, log = run(Nameless({}, {}, None))
    assert message.startswith("2 downloaded, 1 failed")
    assert "b: {year} names year, which ..." in log and "b" not in seen


def test_a_release_without_a_date_names_what_is_missing():
    def handler(_request):
        undated = release("v1", "", ["app.AppImage"])
        return httpx.Response(200, json=[undated])

    (item,) = releases_source(handler, path="{year}/{filename}").items(lambda key: False)
    assert str(item.problem) == "{year}/{filename} names year, which this release does not give"


# Names taken by the job's other files.


class Twins(FakeSource):
    """Items x1, x2 and x3, all named Rechnung.pdf."""

    def items(self, seen):
        for key in ("x1", "x2", "x3"):
            if not seen(key):
                yield Item(key=key, path="2026/Rechnung.pdf", url=key, values={})


def test_files_of_one_name_get_numbered_names():
    library = FakeLibrary()
    message, _, _ = run(Twins({}, {}, None), library)
    assert message == "3 downloaded"
    assert library.files == {
        "/In/2026/Rechnung.pdf": b"x1",
        "/In/2026/Rechnung (2).pdf": b"x2",
        "/In/2026/Rechnung (3).pdf": b"x3",
    }


def test_a_name_taken_by_an_earlier_runs_file_is_numbered_and_one_put_there_by_hand_adopted():
    library = FakeLibrary({"/In/2026/Rechnung.pdf": b"the job's x1"})
    seen = {"x1"}
    message = runner.run(
        job(), Twins({}, {}, None), library, seen.__contains__, seen.add, lambda s: None, owners={0: "x1"}
    )
    assert message == "2 downloaded"
    assert library.files["/In/2026/Rechnung (2).pdf"] == b"x2"

    by_hand = FakeLibrary({"/In/2026/Rechnung.pdf": b"by hand"})
    assert run(Twins({}, {}, None), by_hand)[0] == "2 downloaded, 1 already there"
    assert by_hand.files["/In/2026/Rechnung.pdf"] == b"by hand"


# A changed account forgets the kept session.


def credentials_app(tmp_path, monkeypatch):
    import main  # pylint: disable=import-outside-toplevel

    store = Store(str(tmp_path / "db"))
    monkeypatch.setitem(main.STATE, "store", store)
    nc = FakeNextcloud()
    nc._user = "u"
    saved = store.save(Job(user="u", name="n", source="mail", target="/x", schedule="* * * * *"))
    return main, nc, saved


def test_a_new_credential_forgets_the_kept_session(tmp_path, monkeypatch):
    main, nc, saved = credentials_app(tmp_path, monkeypatch)
    values = nc.preferences_ex.values
    values[f"credential.job{saved.id}.refresh_token"] = "old"
    values[f"session.job{saved.id}"] = '{"refresh_token": "rotated-old"}'
    asyncio.run(main.set_credentials(saved.id or 0, {"refresh_token": "old", "username": ""}, nc))
    assert f"session.job{saved.id}" in values, "the same account: the session stays"
    asyncio.run(main.set_credentials(saved.id or 0, {"refresh_token": "new"}, nc))
    assert values[f"credential.job{saved.id}.refresh_token"] == "new"
    assert f"session.job{saved.id}" not in values, "the old account's refresh token is not used again"


def test_a_run_whose_account_changed_meanwhile_keeps_no_session(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "db"))
    saved = store.save(Job(user="u", name="n", source="changing", target="/x", schedule="* * * * *"))
    nc = FakeNextcloud()
    nc.files = FakeFiles()

    class Changing(FakeSource):
        """Its account changes while it lists; it has a session to keep."""

        name = "changing"
        credentials = ("cookies",)

        def items(self, seen):
            nc.preferences_ex.values[f"credential.job{saved.id}.cookies"] = "sid=new"
            yield from super().items(seen)

        def session(self):
            return {"cookies": [{"name": "sid", "value": "old"}]}

    monkeypatch.setitem(executor_module.SOURCES, "changing", Changing)
    executor_module.Executor(store, lambda: nc).run(saved)
    assert f"session.job{saved.id}" not in nc.preferences_ex.values


# Option changes: only those of the listing forget what it knew.


def test_only_a_change_of_the_listing_forgets_the_complete_pass(tmp_path, monkeypatch):
    import main  # pylint: disable=import-outside-toplevel

    store = Store(str(tmp_path / "db"))
    monkeypatch.setitem(main.STATE, "store", store)
    before = store.save(
        Job(user="u", name="n", source="site", target="/x", schedule="", options={"recipe": "{}", "max_items": 10})
    )
    job_id = before.id or 0
    for key in (FULL_PASS, "a", REFUSED + "a"):
        store.mark_seen(job_id, key)

    def change(**options):
        after = Job(**{**vars(before), "options": before.options | options})
        main.forget_changed(before, after)
        return store.seen_keys(job_id)

    assert change(max_items=50, path="{text}{ext}") == {FULL_PASS, "a", REFUSED + "a"}, "files per run, names"
    assert change(file_type="pdf") == {FULL_PASS}, "other types: the refused file is tried again"
    assert change(recipe='{"start": "https://other.example/"}') == set(), "another recipe lists other files"


# Tags changed: the pass is kept until it goes through, and a run asked for still comes.


def tag_store(tmp_path, monkeypatch):
    monkeypatch.setitem(executor_module.SOURCES, "fake", FakeSource)
    store = Store(str(tmp_path / "db"))
    saved = store.save(job(tags=["old"]))
    nc = FakeNextcloud()
    nc.files = FakeFiles()
    executor = executor_module.Executor(store, lambda: nc)
    executor.run(saved)
    saved.tags = ["new"]
    store.save(saved)
    return store, saved, executor, nc


def test_a_pending_pass_for_the_tags_survives_a_restart(tmp_path, monkeypatch):
    store, saved, executor, nc = tag_store(tmp_path, monkeypatch)
    job_id = saved.id or 0
    store.mark_seen(job_id, "unrecorded")  # saved before files were recorded: a listing is needed
    executor.retag(job_id, ["old"])
    last_run = (store.job(job_id) or saved).last_run

    # The app stops before the pass: the next one queues it at its start.
    later = executor_module.Executor(store, lambda: nc)
    with pytest.MonkeyPatch.context() as threads:
        threads.setattr(threading.Thread, "start", lambda self: None)
        later.start()
    assert later.requests.get_nowait() == job_id and not later._wanted, "queued for the tags alone"

    class Broken(FakeSource):
        """Its listing fails."""

        def items(self, seen):
            raise SourceError("the site is down")
            yield  # pylint: disable=unreachable

    monkeypatch.setitem(executor_module.SOURCES, "fake", Broken)
    later.run(saved, wanted=False)
    assert store.retag_from(job_id) == ["old"], "a failed pass stays pending"
    monkeypatch.setitem(executor_module.SOURCES, "fake", FakeSource)
    later.run(saved, wanted=False)
    assert store.retag_from(job_id) is None, "done once it went through"
    assert (store.job(job_id) or saved).last_run == last_run, "a pass for the tags is no run"
    names = {tag_id: name for name, tag_id in nc.files.tags.items()}
    assert sorted(names[tag] for _, tag in nc.files.assigned) == ["new"] * 3, "the recorded files: new, not old"


def test_run_now_and_a_change_of_tags_meanwhile_do_both(tmp_path, monkeypatch):
    store, saved, executor, _ = tag_store(tmp_path, monkeypatch)
    job_id = saved.id or 0
    executor.request(job_id)
    executor.retag(job_id, ["old"])
    assert executor._wanted == {job_id}, "the run asked for is kept"
    last_run = (store.job(job_id) or saved).last_run
    store.forget(job_id, "c")  # c again for the run: its file, recorded as c's, is there
    executor.run(saved, wanted=True)
    lines = [line["message"] for line in store.log_lines([job_id])]
    assert "Tags brought up to date on 3 files" in lines
    assert lines[-1] == "0 downloaded, 1 already there", "and then the run"
    assert store.retag_from(job_id) is None
    assert ((store.job(job_id) or saved).last_run or 0) > (last_run or 0)


# Time zones.


def test_a_schedule_runs_in_the_jobs_time_zone():
    created = datetime.datetime(2026, 7, 1, 0, 0, tzinfo=datetime.UTC).timestamp()
    berlin = Job(user="u", name="n", source="fake", target="/", schedule="0 6 * * *", created=created)
    berlin.timezone = "Europe/Berlin"
    hour = 3600
    assert not executor_module.is_due(berlin, created + 4 * hour - 60)
    assert executor_module.is_due(berlin, created + 4 * hour), "06:00 in Berlin's summer is 04:00 UTC"
    winter = datetime.datetime(2026, 12, 1, 0, 0, tzinfo=datetime.UTC).timestamp()
    berlin.created = winter
    assert executor_module.is_due(berlin, winter + 5 * hour) and not executor_module.is_due(berlin, winter + 4 * hour)
    utc = Job(user="u", name="n", source="fake", target="/", schedule="0 6 * * *", created=created)
    assert not executor_module.is_due(utc, created + 5 * hour), "a job without a zone stays in UTC"


def test_the_form_checks_the_zone_and_explains_the_schedule_in_it():
    import main  # pylint: disable=import-outside-toplevel

    body = {"name": "n", "source": "feed", "target": "/x", "schedule": "0 6 * * *"}
    assert main.JobIn(**body).timezone == "UTC"
    assert main.JobIn(**body, timezone="Europe/Warsaw").timezone == "Europe/Warsaw"
    for wrong in ("Mars/Olympus", "../../etc/passwd", ""):
        with pytest.raises(ValueError, match="time zone"):
            main.JobIn(**body, timezone=wrong)
    explained = main.explain_schedule("0 6 * * *", "Europe/Warsaw")
    assert explained["description"] == "every day at 06:00"
    first = datetime.datetime.fromtimestamp(explained["next"][0], schedule.zone("Europe/Warsaw"))
    assert (first.hour, first.minute) == (6, 0)
    assert "error" in main.explain_schedule("0 6 * * *", "Mars/Olympus")


# Releases: the newest only.


def test_the_newest_releases_only():
    asked: list[str] = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(
            200,
            json=[
                release("v3", "2026-10-03T00:00:00Z", ["a3.zip"]),
                release("v3-rc", "2026-10-02T00:00:00Z", ["a3rc.zip"], prerelease=True),
                release("v2", "2026-10-01T00:00:00Z", ["a2.zip"]),
            ],
        )

    newest = releases_source(handler, max_releases=1)
    assert [item.values["tag"] for item in newest.items(lambda key: False)] == ["v3"]
    every = releases_source(handler)
    assert [item.values["tag"] for item in every.items(lambda key: False)] == ["v3", "v2"], "0: all, as before"


# Media: what is left out says so, and a large file is not downloaded.


def test_an_entry_that_cannot_be_had_is_logged_and_a_large_one_refused():
    flat = {
        "extractor_key": "ExampleTab",
        "entries": [
            {"id": "v1", "ie_key": "Example", "url": video(1)["webpage_url"]},
            {"id": "v9", "ie_key": "Example", "url": "https://video.example/watch?v=9"},
        ],
    }
    made = media({CHANNEL: flat, video(1)["webpage_url"]: video(1, filesize=5_000_000)}, max_size=4)
    log: list[str] = []
    made.report = lambda level, text: log.append(text)
    (item,) = made.items(lambda key: False)
    assert log == ["Skipped https://video.example/watch?v=9: yt-dlp: Unsupported URL: https://video.example/watch?v=9"]
    with pytest.raises(Refused, match="5 MB, more than the job's largest file"):
        made.fetch(item, io.BytesIO())
    assert FakeYoutubeDL.calls[-1][1]["max_filesize"] == 4_000_000


def test_the_listings_options_are_the_sources_own():
    from harvester.sources import SOURCES  # pylint: disable=import-outside-toplevel

    for source in SOURCES.values():
        assert isinstance(source.listing_options, tuple), source.name
        assert set(source.listing_options) <= set(source.options), source.name
