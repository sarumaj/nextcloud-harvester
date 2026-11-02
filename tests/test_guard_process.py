"""The browser's guard in its process (harvester.browser.guard.Guard): without the app's
secrets, relaying and stopped with its browser, ended with the app, and failing closed when it
crashes. Its connections go to the one local address a guard may open (rclone's exception): the
stand-ins of test_guard do not reach another process.
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
from harvester.browser.guard import Guard
from test_guard import echo_server, echoes, http_server, port_of, refuses, stopped, tunnel


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
            reader, writer, answer = await tunnel(proxy.port, f"127.0.0.1:{port}")
            assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
            assert await asyncio.to_thread(lambda: proxy.connections) == 1
            _, refused, answer = await tunnel(proxy.port, f"127.0.0.1:{port + 1}")
            assert answer.startswith("HTTP/1.1 403")
            refused.close()
            await asyncio.to_thread(proxy.allow, "")
            _, refused, answer = await tunnel(proxy.port, f"127.0.0.1:{port}")
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


@needs_loopback
def test_chromium_whose_guard_crashed_connects_nowhere():
    from test_browser import needs_chromium  # pylint: disable=import-outside-toplevel

    if needs_chromium.args[0]:
        pytest.skip("Chromium is not installed")
    # pylint: disable-next=import-outside-toplevel
    from playwright.sync_api import Error, sync_playwright

    heads: list[bytes] = []
    with serving(lambda: http_server(heads)) as port, Guard() as proxy, sync_playwright() as playwright:
        proxy.allow(f"http://127.0.0.1:{port}")
        chromium = playwright.chromium.launch(**browser.launch_options(proxy.port))
        try:
            page = chromium.new_page()
            page.goto(f"http://127.0.0.1:{port}/")
            assert page.inner_text("body") == "ok"
            process = proxy._process
            assert process is not None
            os.kill(process.pid, signal.SIGKILL)
            assert wait_for(lambda: process.returncode is not None)
            with pytest.raises(Error):
                page.goto(f"http://127.0.0.1:{port}/again", timeout=10000)
        finally:
            chromium.close()
    assert not [head for head in heads if head.startswith(b"GET /again")], "nothing went around the guard"
