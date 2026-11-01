"""Pausing, resuming and stopping a running job."""

# pylint: disable=missing-function-docstring,protected-access

import threading
import time

from test_runner import FakeLibrary, FakeSource

from harvester import runner
from harvester.executor import Control, Executor, site_of
from harvester.store import Job, Store


def job() -> Job:
    return Job(user="u", name="n", source="fake", target="/In", schedule="* * * * *", options={})


def test_a_stopped_run_keeps_what_it_saved_and_says_so():
    seen: set[str] = set()
    lines: list[str] = []
    asked = iter([False, True])
    message = runner.run(
        job(), FakeSource({}, {}, None), FakeLibrary(), seen.__contains__, seen.add, lambda s: None,
        report=lambda level, text: lines.append(text), stopped=lambda: next(asked),
    )  # fmt: skip
    assert len(seen) == 1, "the first file was saved and is known"
    assert "Stopped: the rest comes next run" in lines
    assert message.endswith("; stopped, the rest comes next run")


def test_the_controls_only_act_on_a_running_job(tmp_path):
    executor = Executor(Store(str(tmp_path / "db")))
    assert not executor.pause(1) and not executor.resume(1) and not executor.stop(1)
    executor.controls[1] = Control()
    assert executor.pause(1) and executor.paused(1)
    assert executor.resume(1) and not executor.paused(1)
    assert not executor.pause(2)


def test_a_paused_run_waits_until_resumed_and_the_wait_does_not_count():
    control = Control()
    control.paused.set()
    lines: list[str] = []
    threading.Timer(1.2, control.paused.clear).start()
    began = time.monotonic()
    assert control.checkpoint(lambda level, text: lines.append(text)) is False
    assert time.monotonic() - began >= 1.0
    assert lines == ["Paused", "Resumed"]
    assert control.paused_for >= 1.0


def test_stopping_a_paused_run_ends_its_wait():
    control = Control()
    control.paused.set()
    threading.Timer(0.5, control.stopped.set).start()
    assert control.checkpoint(lambda level, text: None) is True


def test_a_stop_ends_a_wait_at_once_and_the_file_comes_next_run():
    control = Control()

    class Waiting(FakeSource):
        """A site that asks to wait an hour before its second file."""

        def fetch(self, item, dest):
            if item.key == "b":
                control.nap(3600)
            super().fetch(item, dest)

    seen: set[str] = set()
    lines: list[str] = []
    threading.Timer(0.5, control.stopped.set).start()
    began = time.monotonic()
    message = runner.run(
        job(), Waiting({}, {}, None), FakeLibrary(), seen.__contains__, seen.add, control.nap,
        report=lambda level, text: lines.append(text),
    )  # fmt: skip
    assert time.monotonic() - began < 10
    assert seen == {"a"}, "the file it waited for is not marked"
    assert "Stopped: the rest comes next run" in lines and "stopped" in message


def test_the_site_of_a_job_is_its_addresses_host():
    assert (
        site_of(
            Job(
                user="u",
                name="n",
                source="feed",
                target="/x",
                schedule="",
                options={"url": "https://www.Podcast.example/feed"},
            )
        )
        == "podcast.example"
    )
    recipe = '{"start": "https://shop.example/archiv", "steps": []}'
    assert (
        site_of(Job(user="u", name="n", source="site", target="/x", schedule="", options={"recipe": recipe}))
        == "shop.example"
    )
    assert (
        site_of(
            Job(id=7, user="u", name="n", source="mail", target="/x", schedule="", options={"host": "imap.example"})
        )
        == "job:7"
    )


def test_runners_run_jobs_side_by_side_but_one_per_site(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "db"))
    started: list[tuple[int, float]] = []
    lock = threading.Lock()

    def slow_run(_self, ran):
        with lock:
            started.append((ran.id, time.monotonic()))
        time.sleep(1.0)

    monkeypatch.setattr(Executor, "run", slow_run)
    executor = Executor(store, runners=2)
    a = store.save(
        Job(user="u", name="a", source="feed", target="/a", schedule="", options={"url": "https://one.example/a"})
    )
    b = store.save(
        Job(user="u", name="b", source="feed", target="/b", schedule="", options={"url": "https://two.example/b"})
    )
    c = store.save(
        Job(user="u", name="c", source="feed", target="/c", schedule="", options={"url": "https://one.example/c"})
    )
    for _ in range(2):
        threading.Thread(target=executor._runner, daemon=True).start()
    began = time.monotonic()
    for queued in (a, b, c, a):
        executor.request(queued.id or 0)
    while len(started) < 3 and time.monotonic() - began < 15:
        time.sleep(0.1)
    times = {job_id: at - began for job_id, at in started}
    assert times[a.id] < 0.5 and times[b.id] < 0.5, "two sites at once"
    assert times[c.id] >= 0.9, "one.example's second job waits for its first"
    assert [job_id for job_id, _ in started].count(a.id) == 1, "asked twice while queued: run once"


def test_the_browsers_are_a_few_slots(monkeypatch):
    import importlib  # pylint: disable=import-outside-toplevel

    from harvester import browser  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("HARVESTER_BROWSERS", "2")
    fresh = importlib.reload(browser)
    try:
        assert fresh.SLOTS.acquire(blocking=False) and fresh.SLOTS.acquire(blocking=False)
        assert not fresh.SLOTS.acquire(blocking=False), "a third waits"
        fresh.SLOTS.release()
        fresh.SLOTS.release()
        assert fresh.free_browsers() == 2
    finally:
        monkeypatch.delenv("HARVESTER_BROWSERS")
        importlib.reload(browser)


def test_the_home_connection_goes_to_the_browser_and_the_downloads(monkeypatch):
    from harvester import browser  # pylint: disable=import-outside-toplevel
    from harvester.sources.media import Media  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(browser, "PROXY", "http://192.168.178.33:3128")
    assert browser.launch_options(home=True)["proxy"] == {"server": "http://192.168.178.33:3128"}
    assert "proxy" not in browser.launch_options()
    media = Media({"url": "https://video.example/c"}, {}, None)
    assert "proxy" not in media.base()
    media.home = True
    assert media.base()["proxy"] == "http://192.168.178.33:3128"
    monkeypatch.setattr(browser, "PROXY", "")
    assert "proxy" not in browser.launch_options(home=True), "no home connection set up: none"
