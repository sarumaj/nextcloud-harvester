"""Pausing, resuming and stopping a running job."""

# pylint: disable=missing-function-docstring,protected-access

import threading
import time

from test_runner import FakeLibrary, FakeSource

from harvester import runner
from harvester.executor import Executor
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


def test_the_controls_only_act_on_the_running_job(tmp_path):
    executor = Executor(Store(str(tmp_path / "db")))
    assert not executor.pause(1) and not executor.resume(1) and not executor.stop(1)
    executor.running = 1
    assert executor.pause(1) and executor.paused
    assert executor.resume(1) and not executor.paused
    assert not executor.pause(2)


def test_a_paused_run_waits_until_resumed_and_the_wait_does_not_count(tmp_path):
    executor = Executor(Store(str(tmp_path / "db")))
    executor.running = 1
    executor.pause(1)
    lines: list[str] = []
    threading.Timer(1.2, executor.resume, args=(1,)).start()
    began = time.monotonic()
    assert executor.checkpoint(lambda level, text: lines.append(text)) is False
    assert time.monotonic() - began >= 1.0
    assert lines == ["Paused", "Resumed"]
    assert executor._paused_for >= 1.0


def test_stopping_a_paused_run_ends_its_wait(tmp_path):
    executor = Executor(Store(str(tmp_path / "db")))
    executor.running = 1
    executor.pause(1)
    threading.Timer(0.5, executor.stop, args=(1,)).start()
    assert executor.checkpoint(lambda level, text: None) is True


def test_a_stop_ends_a_wait_at_once_and_the_file_comes_next_run(tmp_path):
    executor = Executor(Store(str(tmp_path / "db")))
    executor.running = 1

    class Waiting(FakeSource):
        """A site that asks to wait an hour before its second file."""

        def fetch(self, item, dest):
            if item.key == "b":
                executor.nap(3600)
            super().fetch(item, dest)

    seen: set[str] = set()
    lines: list[str] = []
    threading.Timer(0.5, executor.stop, args=(1,)).start()
    began = time.monotonic()
    message = runner.run(
        job(), Waiting({}, {}, None), FakeLibrary(), seen.__contains__, seen.add, executor.nap,
        report=lambda level, text: lines.append(text),
    )  # fmt: skip
    assert time.monotonic() - began < 10
    assert seen == {"a"}, "the file it waited for is not marked"
    assert "Stopped: the rest comes next run" in lines and "stopped" in message


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
