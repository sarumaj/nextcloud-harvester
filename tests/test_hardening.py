"""The guards: where the app may connect, what child processes see, how much a file may be, and
what keeps the executor going (retries, one notification per failure, a run's time limit)."""

# pylint: disable=missing-function-docstring

import io
import os
import socket

import httpx
import pytest

from harvester import browser, runner
from harvester.sources import SourceError
from harvester.sources import download
from harvester.store import Job


def test_a_redirect_to_a_private_address_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.example":
            return httpx.Response(302, headers={"location": "http://10.0.0.26/secret"})
        return httpx.Response(200, content=b"%PDF-1.7 inside")

    with download.guarded_client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError, match="10.0.0.26.*may not be reached"):
            client.get("https://files.example/report.pdf")


def test_a_public_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    def resolve(host, *_args, **_kwargs):
        address = "192.168.1.10" if host == "inside.example" else "93.184.215.14"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    browser._resolves_public.cache_clear()  # pylint: disable=protected-access
    assert not browser.reachable("https://inside.example/x")
    assert browser.reachable("https://outside.example/x")


def test_child_processes_see_no_secret(monkeypatch):
    monkeypatch.setenv("APP_SECRET", "s3cret")
    monkeypatch.setenv("HP_SHARED_KEY", "k3y")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    env = browser.child_env()
    assert "APP_SECRET" not in env and "HP_SHARED_KEY" not in env and "PATH" in env


def answering(content, headers: dict | None = None):
    """An opener whose answer is the content (an iterator: streamed, without a length)."""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content, headers=headers))
    )
    return download.http(client)


def test_a_file_larger_than_the_limit_is_refused():
    unannounced = answering(iter([b"x" * (1024 * 1024)] * 2))
    with pytest.raises(SourceError, match="larger than"):
        download.fetch(unannounced, "https://files.example/a", io.BytesIO(), {"max_size": 1}, lambda s: None)
    announced = answering(b"x", {"content-length": str(5 * 1024 * 1024)})
    with pytest.raises(SourceError, match="5 MB, more than"):
        download.fetch(announced, "https://files.example/a", io.BytesIO(), {"max_size": 1}, lambda s: None)


def test_a_dropped_connection_is_tried_again():
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadError("connection reset")
        return httpx.Response(200, content=b"%PDF-1.7 the file")

    pauses: list[float] = []
    dest = io.BytesIO()
    opener = download.http(httpx.Client(transport=httpx.MockTransport(handler)))
    download.fetch(opener, "https://files.example/a.pdf", dest, {"file_type": "pdf"}, pauses.append)
    assert dest.getvalue() == b"%PDF-1.7 the file" and pauses == [10, 20]


def test_a_run_stops_at_its_time_limit():
    class Endless(runner.Library):
        """Files in a dictionary."""

        def __init__(self):
            self.files: dict[str, int] = {}

        def find(self, path):
            return self.files.get(path)

        def put(self, path, content):
            self.files[path] = len(self.files) + 1
            return self.files[path]

        def tag(self, file_id, tags):
            return None

        def untag(self, file_id, tags):
            return None

    from test_runner import FakeSource  # pylint: disable=import-outside-toplevel

    job = Job(user="u", name="n", source="fake", target="/x", schedule="* * * * *")
    seen: set[str] = set()
    message = runner.run(job, FakeSource({}, {}, None), Endless(), seen.__contains__, seen.add, lambda s: None, until=0)
    assert message == "0 downloaded" and runner.FULL_PASS not in seen, "past the limit: nothing, and no full pass"


def test_the_recorder_opens_only_for_nextclouds_pages(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://cloud.example")
    monkeypatch.setenv("HARVESTER_ORIGINS", "https://nextcloud.example.org")
    from main import allowed_origin  # pylint: disable=import-outside-toplevel

    assert allowed_origin("https://cloud.example") and allowed_origin("https://nextcloud.example.org/")
    assert allowed_origin(""), "no origin: not a browser; HaRP authenticated it"
    assert not allowed_origin("https://evil.example") and not allowed_origin("http://cloud.example")


def test_a_repeated_failure_is_notified_once(tmp_path, monkeypatch):
    from harvester import executor as executor_module  # pylint: disable=import-outside-toplevel
    from harvester.store import Store  # pylint: disable=import-outside-toplevel
    from test_runner import FakeNextcloud, FakeSource  # pylint: disable=import-outside-toplevel

    class Down(FakeSource):
        """The site is down."""

        name = "down"

        def items(self, seen):
            raise SourceError("https://site.example answered 503")

    monkeypatch.setitem(executor_module.SOURCES, "down", Down)
    store = Store(str(tmp_path / "db"))
    job = store.save(Job(user="u", name="Site", source="down", target="/x", schedule="* * * * *"))
    nc = FakeNextcloud()
    executor = executor_module.Executor(store, lambda: nc)
    for _ in range(3):
        executor.run(store.job(job.id or 0))  # type: ignore[arg-type]
    assert len(nc.sent) == 1, "the same failure three times: told once"


def test_several_file_types():
    settings = {"file_type": "pdf,zip"}
    for content in (b"%PDF-1.7 x", b"PK\x03\x04 x"):
        dest = io.BytesIO()
        download.fetch(answering(content), "https://files.example/a", dest, settings, lambda s: None)
        assert dest.getvalue() == content
    with pytest.raises(SourceError, match=r"not of the job's file types \(pdf, zip\)"):
        download.fetch(answering(b"\x89PNG x"), "https://files.example/a", io.BytesIO(), settings, lambda s: None)
    assert download.extension(settings) == "" and download.extension({"file_type": "pdf"}) == ".pdf"
    assert download.file_types({"file_type": "pdf,any"}) == ["any"]
    assert download.problems({"file_type": "pdf,exe"}) == ["unknown file type exe"]


def test_nextcloud_requests_run_while_a_browser_is_open(tmp_path):
    # Playwright's sync API keeps an event loop running in its thread: a request run on a loop
    # there failed with "Cannot run the event loop while another loop is running".
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    from harvester import executor as executor_module  # pylint: disable=import-outside-toplevel
    from harvester.store import Store  # pylint: disable=import-outside-toplevel

    async def answer(value):
        return value

    async def refuse():
        raise SourceError("refused")

    executor = executor_module.Executor(Store(str(tmp_path / "jobs.db")))
    assert executor.call(answer(1)) == 1
    playwright = sync_playwright().start()
    try:
        assert executor.call(answer(2)) == 2
        with pytest.raises(SourceError, match="refused"):
            executor.call(refuse())
    finally:
        playwright.stop()
