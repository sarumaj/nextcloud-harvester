"""The browser's guard in its process (harvester.browser.guard.Guard): without the app's
secrets, relaying and stopped with its browser, ended with the app, failing closed when it
crashes, and serving its own browser alone (its credentials: another browser that reaches its
port, one that took a crashed guard's, say, is refused). Its connections go to the one local
address a guard may open (rclone's exception): the stand-ins of test_guard do not reach another
process.
"""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import needs_loopback
from harvester import browser
from harvester.browser import guard
from harvester.browser.guard import Guard, Relay
from test_guard import ask, echo_server, echoes, http_server, port_of, refuses, stopped, tunnel


@contextlib.contextmanager
def serving(make):
    """A server (an asyncio one: make()) on 127.0.0.1 in a thread of its own: its port."""
    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(make())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield port_of(server)
    finally:
        loop.call_soon_threadsafe(server.close)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)


def gone(pid: int) -> bool:
    """Whether the process has ended (reaped, or a zombie its parent has yet to reap)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_the_guard_runs_in_a_process_without_the_apps_secrets(monkeypatch):
    monkeypatch.setenv("APP_SECRET", "the-apps-secret")
    threads = threading.active_count()
    proxy = Guard("http://anna:p%3Ass@proxy.example:3128")
    proxy.start()
    process = proxy._process
    assert process is not None and process.pid != os.getpid() and process.poll() is None
    assert proxy.port and proxy.connections == 0
    arguments = Path(f"/proc/{process.pid}/cmdline").read_bytes()
    assert b"p:ss" not in arguments and b"anna" not in arguments, "the proxy's address goes by the pipe"
    try:
        environment = Path(f"/proc/{process.pid}/environ").read_bytes()
    except PermissionError:
        environment = b""  # undumpable: its /proc files are root's
    assert b"the-apps-secret" not in environment
    proxy.stop()
    assert process.returncode is not None and gone(process.pid), "ended and reaped"
    assert proxy.connections == 0
    proxy.stop()  # again: nothing
    assert wait_for(lambda: threading.active_count() <= threads), "its reader ends"


def test_the_guard_ends_with_the_app():
    script = (
        "import sys, time; sys.path.insert(0, sys.argv[1])\n"
        "from harvester.browser.guard import Guard\n"
        "proxy = Guard(); proxy.start(); print(proxy._process.pid, flush=True); time.sleep(60)\n"
    )
    lib = str(Path(guard.__file__).resolve().parents[2])
    with subprocess.Popen([sys.executable, "-c", script, lib], stdout=subprocess.PIPE, text=True) as app:
        assert app.stdout is not None
        child = int(app.stdout.readline())
        assert not gone(child)
        app.send_signal(signal.SIGKILL)  # no stop(), no atexit: the app's end alone
        app.wait()
    assert wait_for(lambda: gone(child)), "the guard's process ends with its pipe"


@needs_loopback
def test_the_guards_process_relays_and_stopping_ends_it():
    with serving(echo_server) as port:
        proxy = Guard()
        proxy.allow(f"http://127.0.0.1:{port}")  # before it starts: it learns it as it starts
        proxy.start()

        async def run():
            reader, writer, answer = await tunnel(proxy.port, f"127.0.0.1:{port}", proxy.credentials)
            assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
            assert await asyncio.to_thread(lambda: proxy.connections) == 1
            _, refused, answer = await tunnel(proxy.port, f"127.0.0.1:{port + 1}", proxy.credentials)
            assert answer.startswith("HTTP/1.1 403")
            refused.close()
            await asyncio.to_thread(proxy.allow, "")
            _, refused, answer = await tunnel(proxy.port, f"127.0.0.1:{port}", proxy.credentials)
            assert answer.startswith("HTTP/1.1 403"), "in force when allow() returns"
            refused.close()
            await stopped(proxy, reader)
            writer.close()

        asyncio.run(run())
    assert refuses(proxy.port) and proxy.connections == 0
    assert list(proxy.refused) == [
        f"127.0.0.1:{port + 1}: not a public address",
        f"127.0.0.1:{port}: not a public address",
    ]


@needs_loopback
def test_a_guard_that_crashes_fails_closed(caplog):
    caplog.set_level(logging.ERROR, "harvester.guard")
    with serving(echo_server) as port, Guard() as proxy:
        proxy.allow(f"http://127.0.0.1:{port}")
        process = proxy._process
        assert process is not None and not refuses(proxy.port)
        os.kill(process.pid, signal.SIGKILL)
        assert wait_for(lambda: process.returncode is not None), "reaped by its reader"
        assert refuses(proxy.port), "its port is closed: the browser's connections fail"
        assert proxy.connections == 0
        proxy.allow("")  # nothing to tell: no error
    assert "the browser's guard ended" in caplog.text


# Its browser's alone


@needs_loopback
def test_a_request_without_the_browsers_credentials_is_refused():
    heads: list[bytes] = []
    with serving(echo_server) as port, serving(lambda: http_server(heads)) as site:
        proxy = Guard()
        proxy.allow(f"http://127.0.0.1:{port}")
        assert proxy.credentials[0] and len(proxy.credentials[1]) >= 24 and proxy.credentials != Guard().credentials
        with proxy:

            async def run():
                for credentials in (None, ("someone", "else"), (proxy.credentials[0], "wrong")):
                    reader, writer, answer = await tunnel(proxy.port, f"127.0.0.1:{port}", credentials)
                    assert answer.startswith("HTTP/1.1 407"), credentials
                    assert 'Proxy-Authenticate: Basic realm="harvester"' in answer
                    assert b"another browser's" in await asyncio.wait_for(reader.read(), 2)
                    writer.close()
                reader, writer, answer = await tunnel(proxy.port, f"127.0.0.1:{port}", proxy.credentials)
                assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
                writer.close()
                await asyncio.to_thread(proxy.allow, f"http://127.0.0.1:{site}")
                login = guard.basic(proxy.credentials)
                reader, writer, answer = await ask(
                    proxy.port,
                    f"GET http://127.0.0.1:{site}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    f"Proxy-Authorization: Basic {login}\r\n\r\n",
                )
                assert answer.startswith("HTTP/1.1 200") and await reader.read() == b"ok"
                writer.close()

            asyncio.run(run())
        assert not proxy.refused, "a request without credentials is no refusal of a destination"
    assert b"Proxy-Authorization" not in heads[0], "the browser's credentials never go on"


@needs_loopback
def test_a_crashed_guards_port_taken_by_another_refuses_its_browser():
    """Guard A's process crashed; its port is free, and a guard B took it (a relay on the same
    port here): browser A's requests, with A's credentials, are refused there."""
    with serving(echo_server) as port:
        first = Guard()
        first.allow(f"http://127.0.0.1:{port}")
        with first:
            process = first._process
            assert process is not None
            os.kill(process.pid, signal.SIGKILL)
            assert wait_for(lambda: process.returncode is not None)
            second = Relay(credentials=Guard().credentials)
            second.allow(f"http://127.0.0.1:{port}")  # B's browser may open the listener
            try:
                assert second.start(first.port) == first.port

                async def run():
                    _, writer, answer = await tunnel(first.port, f"127.0.0.1:{port}", first.credentials)
                    assert answer.startswith("HTTP/1.1 407"), "browser A at B's guard"
                    writer.close()

                asyncio.run(run())
                assert not second.refused
            finally:
                second.stop()


def opened(page, url: str) -> str:
    """What a page's navigation got: its status, or why it failed."""
    # pylint: disable-next=import-outside-toplevel
    from playwright.sync_api import Error

    try:
        response = page.goto(url, timeout=10000)
    except Error as error:
        return f"failed: {str(error).splitlines()[0]}"
    return str(response.status if response else None)


def chromium_or_skip():
    from test_browser import needs_chromium  # pylint: disable=import-outside-toplevel

    if needs_chromium.args[0]:
        pytest.skip("Chromium is not installed")


# A worker's WebSocket to the target: 'open', 'error' or 'timeout'.
WORKER_SOCKET = """target => new Promise(done => {
  const code = `const s = new WebSocket('${target}'); s.onopen = () => postMessage('open');`
    + ` s.onerror = () => postMessage('error')`
  const w = new Worker(URL.createObjectURL(new Blob([code])))
  w.onmessage = e => done(e.data)
  setTimeout(() => done('timeout'), 5000)
})"""


@needs_loopback
def test_chromium_goes_through_its_own_guard_with_its_credentials():
    """Pages, a worker's WebSocket and context.request all pass the guard's 407 with the browser's
    credentials (Playwright answers it); a browser with another guard's port and its own
    credentials is refused there."""
    chromium_or_skip()
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    heads: list[bytes] = []
    with serving(lambda: http_server(heads)) as port, serving(lambda: http_server([])) as other:
        with Guard() as proxy, Guard() as stranger, sync_playwright() as playwright:
            proxy.allow(f"http://127.0.0.1:{port}")
            stranger.allow(f"http://127.0.0.1:{port}")
            chromium = playwright.chromium.launch(**browser.launch_options(proxy.port, credentials=proxy.credentials))
            elsewhere = playwright.chromium.launch(
                **browser.launch_options(proxy.port, credentials=stranger.credentials)
            )
            try:
                context = chromium.new_context()
                page = context.new_page()
                assert opened(page, f"http://127.0.0.1:{port}/page") == "200"
                assert page.inner_text("body") == "ok"
                assert page.evaluate(WORKER_SOCKET, f"ws://127.0.0.1:{other}/socket") != "open"
                assert context.request.get(f"http://127.0.0.1:{other}/request").status == 403
                assert opened(elsewhere.new_page(), f"http://127.0.0.1:{port}/stranger") != "200"
            finally:
                chromium.close()
                elsewhere.close()
            refused = list(proxy.refused)
    # The worker's WebSocket and the context's request reached the guard (past its 407), and
    # were refused there as not public; the stranger never got past the 407.
    assert refused.count(f"127.0.0.1:{other}: not a public address") >= 2, refused
    paths = [head.split(b" ")[1] for head in heads]
    assert b"/page" in paths and b"/stranger" not in paths


@needs_loopback
def test_chromium_whose_guard_crashed_connects_nowhere():
    """Its guard's process killed, and its port taken by another browser's guard (a relay on the
    same port that would let its own browser open the listener): this browser gets nowhere."""
    chromium_or_skip()
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    heads: list[bytes] = []
    with serving(lambda: http_server(heads)) as port, Guard() as proxy, sync_playwright() as playwright:
        proxy.allow(f"http://127.0.0.1:{port}")
        chromium = playwright.chromium.launch(**browser.launch_options(proxy.port, credentials=proxy.credentials))
        try:
            page = chromium.new_page()
            assert opened(page, f"http://127.0.0.1:{port}/") == "200"
            assert page.inner_text("body") == "ok"
            process = proxy._process
            assert process is not None
            os.kill(process.pid, signal.SIGKILL)
            assert wait_for(lambda: process.returncode is not None)
            assert opened(page, f"http://127.0.0.1:{port}/again").startswith("failed")
            taker = Relay(credentials=Guard().credentials)
            taker.allow(f"http://127.0.0.1:{port}")
            try:
                taker.start(proxy.port)
                assert opened(page, f"http://127.0.0.1:{port}/taken") != "200"
            finally:
                taker.stop()
        finally:
            chromium.close()
    paths = [head.split(b" ")[1] for head in heads]
    assert b"/again" not in paths and b"/taken" not in paths, "nothing went around the guard"
