"""Pausing, resuming and stopping a running job."""

# pylint: disable=missing-function-docstring,protected-access

import contextlib
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

    def slow_run(_self, ran, _wanted=True, _fetch=None):
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
    # The browser goes to its guard alone, which goes on through the proxy (test_guard).
    assert browser.launch_options(4321)["proxy"] == {"server": "http://127.0.0.1:4321", "bypass": "<-loopback>"}
    media = Media({"url": "https://video.example/c"}, {}, None)
    assert "proxy" not in media.base()
    media.home = True
    assert media.base()["proxy"] == "http://192.168.178.33:3128"


def test_the_browsers_limit_changes_while_they_are_in_use():
    from harvester.browser import Slots  # pylint: disable=import-outside-toplevel

    slots = Slots(2)
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    slots.limit = 1
    slots.release()
    assert not slots.acquire(blocking=False), "lowered: one is still open, which is the limit"
    slots.limit = 3
    assert slots.acquire(blocking=False) and slots.free() == 1
    waiting = threading.Thread(target=slots.acquire)
    slots.acquire()
    waiting.start()
    time.sleep(0.1)
    assert waiting.is_alive(), "all taken: it waits"
    slots.limit = 4
    waiting.join(2)
    assert not waiting.is_alive(), "raised: it gets one"


def test_the_admins_settings_change_the_runners_and_browsers(tmp_path, monkeypatch):
    from harvester import browser, settings  # pylint: disable=import-outside-toplevel

    class Record:  # pylint: disable=too-few-public-methods
        """A stored value, as nc_py_api's CfgRecord."""

        def __init__(self, key, value):
            self.key, self.value = key, value

    class Config:  # pylint: disable=too-few-public-methods
        """The app's stored values (appconfig_ex)."""

        values: dict[str, str] = {}

        async def get_values(self, keys):
            return [Record(key, self.values[key]) for key in keys if key in self.values]

    class Nc:  # pylint: disable=too-few-public-methods
        """The app's client, with its stored values only."""

        appconfig_ex = Config()

    monkeypatch.setattr(browser, "SLOTS", browser.Slots(3))
    monkeypatch.setattr(browser, "PROXY", "")
    monkeypatch.setattr(browser, "PROXY_NAME", "the proxy")
    monkeypatch.setattr(settings, "DEFAULT_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setattr(browser, "GUARD", True)
    monkeypatch.setattr(settings, "DEFAULT_GUARD", True)
    executor = Executor(Store(str(tmp_path / "db")), connect=Nc, runners=2)
    executor.read_settings()
    assert (executor.runners, browser.SLOTS.limit) == (2, browser.BROWSERS), "unset: the environment's"
    assert browser.PROXY == "http://10.0.0.1:3128", "unset: the environment's proxy"
    Config.values = {
        settings.RUNNERS: "4",
        settings.BROWSERS: "1",
        settings.PROXY: "http://u:p@proxy.example:8080",
        settings.PROXY_NAME: "the home connection",
    }
    executor.read_settings()
    assert (executor.runners, browser.SLOTS.limit) == (4, 1)
    assert (browser.PROXY, browser.PROXY_NAME) == ("http://u:p@proxy.example:8080", "the home connection")
    Config.values = {settings.PROXY: "", settings.PROXY_NAME: ""}
    executor.read_settings()
    assert (browser.PROXY, browser.PROXY_NAME) == ("", settings.DEFAULT_PROXY_NAME), "emptied: none"
    Config.values = {settings.RUNNERS: "99", settings.BROWSERS: "x"}
    executor.read_settings()
    assert (executor.runners, browser.SLOTS.limit) == (settings.MAX_RUNNERS, browser.BROWSERS)
    # The browsers' guard: for the browsers started afterwards.
    assert browser.GUARD is True, "unset: the environment's (on)"
    for word, guarded in (("Off", False), ("On", True)):
        Config.values = {settings.GUARD: word}
        executor.read_settings()
        assert browser.GUARD is guarded
    monkeypatch.setattr(settings, "DEFAULT_GUARD", False)
    Config.values = {}
    executor.read_settings()
    assert browser.GUARD is False, "HARVESTER_GUARD=off and no setting"


def test_a_runner_beyond_the_setting_waits(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "db"))
    ran: list[int] = []
    monkeypatch.setattr(Executor, "run", lambda _self, job, _wanted=True, _fetch=None: ran.append(job.id))
    executor = Executor(store, runners=1)
    threading.Thread(target=executor._runner, args=(1,), daemon=True).start()
    queued = store.save(Job(user="u", name="a", source="feed", target="/a", schedule="", options={}))
    executor.request(queued.id or 0)
    time.sleep(1)
    assert not ran, "runner 1 is beyond one runner"
    executor.runners = 2
    began = time.monotonic()
    while not ran and time.monotonic() - began < 10:
        time.sleep(0.1)
    assert ran == [queued.id]


def test_the_admins_form_offers_the_environments_values():
    from harvester import settings  # pylint: disable=import-outside-toplevel

    fields = {field["id"]: field for field in settings.form(2, 3).to_dict()["fields"]}
    assert fields[settings.RUNNERS]["default"] == "2" and fields[settings.BROWSERS]["default"] == "3"
    assert fields[settings.RUNNERS]["options"] == [str(n) for n in range(1, settings.MAX_RUNNERS + 1)], "plain strings"
    assert settings.form(2, 3).section_type == "admin"
    assert settings.number(None, 2, 8) == 2 and settings.number("0", 2, 8) == 1


def test_the_proxy_setting_takes_only_http_addresses_and_hides_its_password():
    from harvester import settings  # pylint: disable=import-outside-toplevel

    assert settings.proxy(None, "http://env:1") == "http://env:1"
    assert settings.proxy(" ", "http://env:1") == ""
    assert settings.proxy("socks5://x:1", "http://env:1") == "socks5://x:1"
    assert settings.proxy("socks5h://x:1", "") == "socks5h://x:1"
    assert settings.proxy("socks5://u:p@x:1", "") == "", "the browser cannot send a SOCKS password"
    assert settings.proxy("socks4://x:1", "") == ""
    assert settings.proxy(None, "socks5://u:p@x:1") == "", "the environment's is checked too"
    assert settings.proxy("proxy.example:3128", "") == "", "an address needs its scheme"
    assert settings.shown("http://u:secret@p.example:3128") == "http://u:***@p.example:3128"


def test_the_proxys_credentials_go_to_the_guard_not_the_browser(monkeypatch):
    from harvester import browser  # pylint: disable=import-outside-toplevel
    from harvester.browser.guard import Guard  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(browser, "PROXY", "http://anna%40home:p%3Ass@proxy.example:3128")
    upstream = Guard(browser.PROXY).upstream
    assert upstream and (upstream.host, upstream.port, upstream.username, upstream.password) == (
        "proxy.example",
        3128,
        "anna@home",
        "p:ss",
    )
    assert "anna" not in str(browser.launch_options(4321)), "Chromium never sees them"


def test_the_jobs_form_names_the_proxy_as_the_admin_does(monkeypatch):
    import main  # pylint: disable=import-outside-toplevel

    from harvester import browser  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(browser, "PROXY", "http://proxy.example:3128")
    monkeypatch.setattr(browser, "PROXY_NAME", "the home connection")
    options = main.list_sources()[0]["options"]
    assert options["home"]["title"] == "Through the home connection"
    monkeypatch.setattr(browser, "PROXY", "")
    assert "home" not in main.list_sources()[0]["options"], "no proxy: not offered"


def test_a_socks_proxy_goes_to_the_browser_and_the_downloads(monkeypatch):
    from harvester import browser  # pylint: disable=import-outside-toplevel
    from harvester.browser.guard import Guard  # pylint: disable=import-outside-toplevel
    from harvester.sources.download import guarded_client  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(browser, "PROXY", "socks5h://proxy.example:1080")
    upstream = Guard(browser.PROXY).upstream
    assert upstream and (upstream.scheme, upstream.host, upstream.port) == ("socks5", "proxy.example", 1080)
    guarded_client(proxy=browser.PROXY).close()  # socksio is installed: httpx takes it


def test_the_address_sites_see_is_read_from_cloudflares_trace(monkeypatch):
    import httpx  # pylint: disable=import-outside-toplevel

    from harvester import browser  # pylint: disable=import-outside-toplevel
    from harvester.executor import through_proxy  # pylint: disable=import-outside-toplevel

    assert browser.seen_address("fl=1\nip=203.0.113.10\nloc=DE\n") == {"ip": "203.0.113.10", "country": "DE"}
    assert not browser.seen_address("<html>")
    monkeypatch.setattr(browser, "PROXY_NAME", "the home connection")
    answer = httpx.MockTransport(lambda request: httpx.Response(200, text="ip=203.0.113.10\nloc=DE\n"))
    with httpx.Client(transport=answer) as client:
        assert through_proxy(client) == "Through the home connection: sites see 203.0.113.10 (DE)"

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        assert "the check of the address failed (refused)" in through_proxy(client)


def test_the_recorder_tells_the_page_which_address_sites_see(monkeypatch):
    import asyncio  # pylint: disable=import-outside-toplevel

    from harvester import browser  # pylint: disable=import-outside-toplevel
    from harvester.browser.recorder import Recorder  # pylint: disable=import-outside-toplevel

    class Answer:  # pylint: disable=too-few-public-methods
        """Cloudflare's trace."""

        async def text(self):
            return "ip=203.0.113.10\nloc=DE\n"

    made: list[dict] = []
    disposed: list[bool] = []

    class Requests:
        """A request context of its own (not the browser context's, which shares its cookies)."""

        async def get(self, url, timeout):
            assert url == browser.TRACE_URL and timeout
            return Answer()

        async def dispose(self):
            disposed.append(True)

    class Factory:  # pylint: disable=too-few-public-methods
        """Playwright's request API."""

        async def new_context(self, proxy=None):
            made.append({"proxy": proxy})
            return Requests()

    class Playwright:  # pylint: disable=too-few-public-methods
        """Playwright, as far as the recorder asks it."""

        request = Factory()

    class Context:  # pylint: disable=too-few-public-methods
        """The browser's context: asking through it would hand Cloudflare's cookie to the browser."""

        @property
        def request(self):
            raise AssertionError("the browser's own request API shares its cookies")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(browser, "PROXY_NAME", "the home connection")
    recorder = Recorder(send)
    recorder._context = Context()  # type: ignore[assignment]
    recorder._playwright = Playwright()  # type: ignore[assignment]
    recorder._proxy = {"server": "http://127.0.0.1:4711", "username": "u", "password": "p"}
    asyncio.run(recorder._tell_address(True))
    asyncio.run(recorder._tell_address(False))
    assert made == [{"proxy": recorder._proxy}] * 2, "the browser's way out: its guard, with its credentials"
    assert disposed == [True, True]
    assert sent == [
        {"type": "network", "via": "the home connection", "proxied": True, "ip": "203.0.113.10", "country": "DE"},
        {"type": "network", "via": "directly", "proxied": False, "ip": "203.0.113.10", "country": "DE"},
    ]


def test_a_stop_ends_a_download_in_progress():
    import io  # pylint: disable=import-outside-toplevel

    import pytest  # pylint: disable=import-outside-toplevel

    from harvester.sources import Stopped, download  # pylint: disable=import-outside-toplevel
    from harvester.sources.base import watch_stop  # pylint: disable=import-outside-toplevel

    stop = threading.Event()
    sent: list[int] = []

    def chunks():
        for number in range(100):
            sent.append(number)
            if number == 2:
                stop.set()  # the person presses Stop while the file comes
            yield b"x" * 1000

    @contextlib.contextmanager
    def opener(url):
        yield download.Response(200, url, {"content-type": "application/pdf"}, chunks())

    watch_stop(stop)
    try:
        with pytest.raises(Stopped):
            download.fetch(opener, "https://files.example/a.pdf", io.BytesIO(), {}, lambda seconds: None)
    finally:
        watch_stop(None)
    assert len(sent) == 3, "it ended at the next chunk, not at the end of the file"


def test_a_stop_kills_a_helper_program_at_once():
    import pytest  # pylint: disable=import-outside-toplevel

    from harvester.sources import Stopped  # pylint: disable=import-outside-toplevel
    from harvester.sources.base import stoppable_run, watch_stop  # pylint: disable=import-outside-toplevel

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    watch_stop(stop)
    began = time.monotonic()
    try:
        with pytest.raises(Stopped):
            stoppable_run(["sleep", "30"], timeout=60, capture_output=True, text=True)
    finally:
        watch_stop(None)
    assert time.monotonic() - began < 3, "killed, not waited for"
    assert (
        stoppable_run(["echo", "hi"], timeout=5, capture_output=True, text=True).stdout == "hi\n"
    ), "unwatched: as run"


def test_yt_dlps_progress_cancels_a_stopped_download():
    import pytest  # pylint: disable=import-outside-toplevel
    import yt_dlp  # pylint: disable=import-outside-toplevel

    from harvester.sources.base import watch_stop  # pylint: disable=import-outside-toplevel
    from harvester.sources.media import cancel_when_stopped  # pylint: disable=import-outside-toplevel

    stop = threading.Event()
    watch_stop(stop)
    try:
        cancel_when_stopped({"status": "downloading"})
        stop.set()
        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            cancel_when_stopped({"status": "downloading"})
    finally:
        watch_stop(None)


def test_a_job_asked_for_shows_queued_until_a_runner_takes_it(tmp_path, monkeypatch):
    import asyncio  # pylint: disable=import-outside-toplevel

    import main  # pylint: disable=import-outside-toplevel

    class Nextcloud:  # pylint: disable=too-few-public-methods
        """The page's user, as AppAPI tells the app."""

        @property
        async def user(self) -> str:
            return "u"

    store = Store(str(tmp_path / "db"))
    executor = Executor(store)
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", executor)
    waiting = store.save(job())
    other = store.save(Job(user="v", name="n", source="fake", target="/In", schedule="* * * * *", options={}))
    executor.request(waiting.id or 0)
    executor.request(other.id or 0)
    assert executor.queued == sorted([waiting.id, other.id])
    listed = asyncio.run(main.list_jobs(Nextcloud()))
    assert [(item["queued"], item["running"]) for item in listed] == [(True, False)]
    log = asyncio.run(main.progress_log(Nextcloud()))
    assert log["queued"] == [waiting.id] and log["running"] == [], "another user's job is not listed"
    assert executor._claim(waiting)
    assert executor.queued == [other.id]
    listed = asyncio.run(main.list_jobs(Nextcloud()))
    assert [(item["queued"], item["running"]) for item in listed] == [(False, True)]


def test_a_persons_pattern_that_takes_too_long_fails_its_job_not_the_app():
    import pytest  # pylint: disable=import-outside-toplevel

    from harvester.sources import SourceError  # pylint: disable=import-outside-toplevel
    from harvester.sources.base import PATTERN_TIMEOUT, pattern_search  # pylint: disable=import-outside-toplevel

    assert pattern_search(r"\.pdf$", "a.pdf") and not pattern_search(r"\.pdf$", "a.txt")
    assert pattern_search(r"(?P<year>\d{4})", "x 2026").group("year") == "2026", "Python's syntax"
    ticks: list[float] = []
    running = threading.Event()

    def tick():
        while not running.is_set():
            ticks.append(time.monotonic())
            time.sleep(0.05)

    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    began = time.monotonic()
    with pytest.raises(SourceError, match="takes too long"):
        pattern_search(r"(a|aa)+$", "a" * 60 + "b")
    took = time.monotonic() - began
    running.set()
    assert took < PATTERN_TIMEOUT + 2, "ended at the limit"
    assert sum(1 for at in ticks if began < at < began + took) >= 5, "the other threads went on meanwhile"
