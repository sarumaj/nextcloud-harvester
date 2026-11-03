# SPDX-License-Identifier: AGPL-3.0-or-later
"""The guard: a proxy of the app's that each browser's connections go through, every one of them.

Playwright's routes see a page's requests and its own WebSockets, not those of its workers, and
the name they check Chromium looks up once more as it connects: a page could reach the
container's own loopback that way, or, without the External Apps' network rules, the private
network. Chromium starts with its guard as its proxy for everything, loopback included
(launch_options; unless the admin switched the guard off, browser.GUARD): a worker's WebSocket,
a fetch() and a navigation alike reach the guard, which looks the name up itself, refuses it
unless every address it resolves to is public (as allowed_host), and connects to an address it
checked, never looking the name up again as it connects (the checked addresses serve the name
for NAME_TTL).

https and WebSockets come as CONNECT (a tunnel), plain http as a request with the whole address,
which goes on in origin form. With the home connection (browser.PROXY: http, https, socks5 or
socks5h), the guard checks the destination first and then asks the proxy for a tunnel to the
checked address, never the name (CONNECT for plain http too: a proxy that took the request whole
would rebuild its Host header from the address, and sites by name would not answer): the proxy is
the one address the guard connects to unchecked. A browser's guard
may also let it open one local address (allow: rclone's sign-in listener, while rclone runs).

One guard per browser, on 127.0.0.1 with a port of its own, in a process of its own (serve):
in the app's process, every chunk a page loads waited for the interpreter's lock, which the
runners, the app's event loop and Playwright hold most of the time under load. Guard is the
browser's handle on it: the recorder runs on the app's loop, the jobs' browsers in the runners'
threads (Playwright's sync API), and either starts and stops its guard with its browser. The
process gets the children's environment (child_env), the home connection's address over its
pipe (never in its arguments or environment), and is undumpable; it ends when the app does
(its pipe closes), and a guard that ends early closes its port: the browser's connections fail,
they never go around it. Each guard asks for its browser's own credentials (Proxy-Authorization,
launch_options): another guard that took a crashed one's port refuses that browser.
"""

import asyncio
import base64
import collections
import concurrent.futures
import functools
import hmac
import ipaddress
import itertools
import json
import logging
import math
import os
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlsplit

from . import LOOKUP_TIMEOUT, child_env, local_name, undumpable

LOG = logging.getLogger("harvester.guard")

# Connections a browser may hold open at once (Chromium keeps up to 32 per proxy, and its
# WebSockets apart, up to 255); one more is refused.
CONNECTIONS = 256
# A request's head (its line and headers) at most; the browser's are a few KB.
HEAD_LIMIT = 16 * 1024
# How long the browser has to send a request's head, and a connection to the site (or the
# proxy) may take (s).
HEAD_TIMEOUT = 30.0
CONNECT_TIMEOUT = 15.0
# A connection that carries nothing either way this long (s) is closed (a WebSocket that waits
# for news sends its pings well within this).
IDLE_TIMEOUT = 300.0
# What one read takes at most, and what a side of a connection holds before it stops reading
# until the other side takes it (asyncio's limit: it pauses at twice that). Large chunks move a
# download in few turns of the loop; a side that reads slowly still holds the other back.
CHUNK = 256 * 1024
LIMIT = 256 * 1024
# How long one address may try to connect before the next one starts trying too (s; RFC 8305's
# "Connection Attempt Delay").
STAGGER = 0.25
# The names looked up at once (the lookups block a thread each).
LOOKUPS = 8
# How long a name's checked addresses serve its connections (s; as long as Chromium keeps a
# lookup, which it does not make behind a proxy), a refused name's refusal, and how many names
# are kept. The guard connects only to addresses it checked, cached or not: a name that
# changes meanwhile (DNS rebinding) still gains nothing.
NAME_TTL = 60.0
REFUSAL_TTL = 5.0
NAMES = 512
# How long an address family that did not connect when the other did goes second (s): a
# container without IPv6, or a network that drops it, holds up one connection, not each one.
FAMILY_MEMORY = 600.0
# The refusals kept for the logs and the tests.
REFUSALS = 100
# How long stopping may take (s): the connections are cancelled, the loop ends.
STOP_TIMEOUT = 5.0
# How long the guard's process may take to start (an interpreter, under load), and to answer.
START_TIMEOUT = 15.0
ANSWER_TIMEOUT = 5.0
# What the guard's process runs: this module from the app's code (lib), isolated from the
# environment's and the working directory's Python paths.
LIB = str(Path(__file__).resolve().parents[2])
BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from harvester.browser.guard import serve; serve()"
# A request's headers that concern the proxy (or this hop) alone, never sent on.
HOP_HEADERS = frozenset({"proxy-connection", "proxy-authorization", "keep-alive", "connection"})
T = TypeVar("T")
PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    407: "Proxy Authentication Required",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


class Refused(Exception):
    """A connection the guard does not make: the status it answers and why."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class Upstream:  # pylint: disable=too-few-public-methods
    """The proxy the guard connects through (browser.PROXY): its kind, address and credentials."""

    def __init__(self, url: str):
        address = urlsplit(url)
        self.scheme = "socks5" if address.scheme == "socks5h" else address.scheme
        if self.scheme not in ("http", "https", "socks5") or not address.hostname:
            raise ValueError(f"not a proxy the browser can use: {address.scheme}://{address.hostname or ''}")
        self.host = address.hostname
        self.port = address.port or {"http": 80, "https": 443, "socks5": 1080}[self.scheme]
        self.username = unquote(address.username or "")
        self.password = unquote(address.password or "")

    @property
    def authorization(self) -> str:
        """The Proxy-Authorization header's line for an http(s) proxy ("" without credentials)."""
        if not self.username:
            return ""
        pair = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Proxy-Authorization: Basic {pair}\r\n"


def host_port(address: str) -> str:
    """An address and port as a request names them (an IPv6 address in brackets)."""
    host, _, port = address.rpartition(":")
    return f"[{host}]:{port}" if ":" in host else address


def split_target(target: str) -> tuple[str, int]:
    """A CONNECT's host and port ("example.org:443", "[2001:db8::1]:443")."""
    host, separator, port = target.rpartition(":")
    if not separator or not port.isdigit():
        raise Refused(400, "no port to connect to")
    return host.strip("[]"), int(port)


def sent_headers(lines: list[str]) -> str:
    """A plain request's header lines as they go on: without those for the proxy or this hop,
    and with Connection: close. One request per connection: the browser's next one may well be
    for another site, which the guard checks on a connection of its own. A WebSocket's upgrade
    keeps its Connection: Upgrade."""
    names = [line.split(":", 1)[0].strip().lower() for line in lines]
    kept = [line for line, name in zip(lines, names) if line and name not in HOP_HEADERS]
    kept.append("Connection: Upgrade" if "upgrade" in names else "Connection: close")
    return "".join(f"{line}\r\n" for line in kept)


def refusal(status: int, reason: str) -> bytes:
    """The answer to a refused request: the status, and why in a line."""
    body = f"{reason}\n".encode()
    challenge = 'Proxy-Authenticate: Basic realm="harvester"\r\n' if status == 407 else ""
    head = (
        f"HTTP/1.1 {status} {PHRASES.get(status, 'Error')}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"{challenge}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


def basic(credentials: tuple[str, str]) -> str:
    """The Basic authorization's token for a username and password."""
    return base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()


def authorized(lines: list[str], token: str) -> bool:
    """Whether a request's header lines carry the proxy's Basic credentials (its token)."""
    for line in lines:
        name, _, value = line.partition(":")
        scheme, _, given = value.strip().partition(" ")
        if name.strip().lower() == "proxy-authorization" and scheme.lower() == "basic":
            return hmac.compare_digest(given.strip().encode(), token.encode())
    return False


class Guard:  # pylint: disable=too-many-instance-attributes
    """A browser's guard: start() gives the port to launch the browser with, stop() ends it and
    every connection it holds (also as a context manager). Its Relay runs in a process of its
    own (serve), which this talks to over a pipe."""

    def __init__(self, upstream: str = ""):
        # Checked here: a proxy the browser cannot use fails before any process starts.
        self.upstream = Upstream(upstream) if upstream else None
        self._upstream = upstream
        self.port = 0
        # The browser's own credentials for its guard (launch_options): a browser that reaches
        # another guard's port (one that took a crashed guard's port, say) is refused there.
        # Never logged.
        self.credentials = (secrets.token_urlsafe(12), secrets.token_urlsafe(24))
        # Why the guard refused, latest last ("host:port: reason").
        self.refused: collections.deque[str] = collections.deque(maxlen=REFUSALS)
        self._loopback: tuple[str, int] | None = None
        self._origin = ""
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        # The questions asked of the process, by number (0: its port), until it answers.
        self._answers: dict[int, concurrent.futures.Future] = {}
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()
        self._stopping = False

    def allow(self, origin: str) -> None:
        """Lets this browser open the one local address of the origin (scheme://host:port; its
        host and port, whichever the scheme), or none (""): in force when this returns."""
        self._origin = origin
        self._loopback = loopback_of(origin)
        try:
            self._ask({"allow": origin})
        except ConnectionError:
            pass  # no process: it learns the origin as it starts, or nothing passes anyway

    @property
    def connections(self) -> int:
        """How many connections the guard holds now."""
        try:
            return int(self._ask({"connections": True}))
        except ConnectionError:
            return 0

    def start(self) -> int:
        """Starts the guard's process, listening on 127.0.0.1, and gives its port."""
        self._stopping = False
        # pylint: disable-next=consider-using-with
        process = self._process = subprocess.Popen(
            [sys.executable, "-I", "-c", BOOT, LIB], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=child_env()
        )
        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._answers[0] = ready
        self._reader = threading.Thread(target=self._read, args=(process,), name=f"guard-{process.pid}", daemon=True)
        self._reader.start()
        try:
            self._send(process, {"upstream": self._upstream, "allow": self._origin, "credentials": self.credentials})
            self.port = ready.result(START_TIMEOUT)
        except BaseException:
            self.stop()
            raise
        return self.port

    def stop(self) -> None:
        """Stops the guard: its port is free and its connections closed when this returns (its
        process has ended). Again, or before start(), nothing."""
        process, reader = self._process, self._reader
        if process is None:
            return
        self._stopping = True
        with self._lock:
            try:
                assert process.stdin is not None
                process.stdin.close()  # the process ends as its pipe does
            except OSError:
                pass  # it has ended already
        try:
            process.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if reader:
            reader.join(STOP_TIMEOUT)
        self._process = self._reader = None

    def __enter__(self) -> "Guard":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _ask(self, question: dict) -> object:
        """The process's answer to a question; ConnectionError without a process."""
        process = self._process
        if process is None or process.poll() is not None:
            raise ConnectionError("the guard is not running")
        answer: concurrent.futures.Future = concurrent.futures.Future()
        number = next(self._numbers)
        self._answers[number] = answer
        try:
            self._send(process, {"id": number, **question})
            return answer.result(ANSWER_TIMEOUT)
        finally:
            self._answers.pop(number, None)

    def _send(self, process: subprocess.Popen, message: dict) -> None:
        with self._lock:
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(message).encode() + b"\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                raise ConnectionError("the guard's process has ended") from error

    def _read(self, process: subprocess.Popen) -> None:
        """What the process says: its answers, its refusals and its log, until it ends (and is
        reaped here, however it ended)."""
        assert process.stdout is not None
        for line in process.stdout:
            message = json.loads(line)
            if "refused" in message:
                self.refused.append(message["refused"])
            elif "log" in message:
                LOG.log(message["log"], "%s", message["message"])
            elif "port" in message or "failed" in message:
                ready = self._answers.pop(0, None)
                if ready and "port" in message:
                    ready.set_result(message["port"])
                elif ready:
                    ready.set_exception(OSError(f"the guard could not listen: {message['failed']}"))
            elif (answer := self._answers.get(message.get("id", -1))) is not None:
                answer.set_result(message.get("value"))
        process.stdout.close()
        code = process.wait()
        for answer in list(self._answers.values()):
            if not answer.done():
                answer.set_exception(ConnectionError("the guard's process has ended"))
        if not self._stopping:
            LOG.error("the browser's guard ended (%s): its browser's connections fail", code)


def loopback_of(origin: str) -> tuple[str, int] | None:
    """The host and port of an origin a browser may open although local (allow)."""
    if not origin:
        return None
    address = urlsplit(origin)
    return (address.hostname or "", address.port or 80)


def serve() -> None:
    """The guard's process (Guard.start): a Relay with the home connection the first line on
    stdin names, and the questions on the next lines answered on stdout, as are its refusals
    and its log; when stdin ends (the app stopped the guard, or ended), the relay ends too."""
    undumpable()
    # Ctrl-C in a terminal is the app's to handle: the guard ends with its pipe.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    answers = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # whatever else is printed goes to stderr, never among the answers
    lock = threading.Lock()

    def send(message: dict) -> None:
        with lock:
            answers.write(json.dumps(message).encode() + b"\n")
            answers.flush()

    class Forward(logging.Handler):
        """The relay's log, to the app's."""

        def emit(self, record: logging.LogRecord) -> None:
            send({"log": record.levelno, "message": self.format(record)})

    LOG.addHandler(Forward())
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    first = sys.stdin.readline()
    if not first:
        return
    settings = json.loads(first)
    credentials = tuple(settings.get("credentials") or ()) or None
    relay = Relay(settings.get("upstream", ""), refused=lambda line: send({"refused": line}), credentials=credentials)
    relay.allow(settings.get("allow", ""))
    try:
        send({"port": relay.start()})
    except OSError as error:
        send({"failed": str(error)})
        return
    try:
        for line in sys.stdin:
            question = json.loads(line)
            if "allow" in question:
                relay.allow(question["allow"])
            send({"id": question["id"], "value": True if "allow" in question else relay.connections})
    finally:
        relay.stop()


class Relay:  # pylint: disable=too-many-instance-attributes
    """What a guard does, in the process it runs in: a listener on 127.0.0.1 in a thread with its
    own event loop, which checks and relays the browser's connections. start() gives its port,
    stop() ends it and every connection it holds (also as a context manager)."""

    def __init__(
        self,
        upstream: str = "",
        refused: Callable[[str], None] | None = None,
        credentials: tuple[str, str] | None = None,
    ):
        self.upstream = Upstream(upstream) if upstream else None
        self.port = 0
        # The Basic token every request must carry (Proxy-Authorization), if any: 407 otherwise.
        self._token = basic(credentials) if credentials else ""
        # Why the guard refused, latest last ("host:port: reason"), and who else learns it.
        self.refused: collections.deque[str] = collections.deque(maxlen=REFUSALS)
        self._on_refused = refused
        self._loopback: tuple[str, int] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.Server | None = None
        self._lookups: concurrent.futures.ThreadPoolExecutor | None = None
        self._connections: set[asyncio.Task] = set()
        # The names' lookups: [until when it serves, the lookup (its addresses, or Refused)].
        self._names: collections.OrderedDict[str, list] = collections.OrderedDict()
        # The families (IPv6: True) that did not connect when the other did, and until when.
        self._behind: dict[bool, float] = {}

    def allow(self, origin: str) -> None:
        """Lets this browser open the one local address of the origin (scheme://host:port; its
        host and port, whichever the scheme), or none ("")."""
        self._loopback = loopback_of(origin)

    @property
    def connections(self) -> int:
        """How many connections the guard holds now."""
        return len(self._connections)

    def start(self, port: int = 0) -> int:
        """Starts the guard on 127.0.0.1 (on the port, or any free one) and gives its port."""
        ready = concurrent.futures.Future()  # type: ignore[var-annotated]
        self._thread = threading.Thread(target=self._run, args=(ready, port), name=f"guard-{id(self):x}", daemon=True)
        self._thread.start()
        self.port = ready.result(timeout=STOP_TIMEOUT)
        return self.port

    def stop(self) -> None:
        """Stops the guard: its port is free and its connections closed when this returns. Again,
        or before start(), nothing."""
        loop, thread = self._loop, self._thread
        if loop and thread and thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(STOP_TIMEOUT)
            except (concurrent.futures.TimeoutError, RuntimeError):
                pass  # the loop ends below in any case
            loop.call_soon_threadsafe(loop.stop)
            thread.join(STOP_TIMEOUT)
        self._thread = None

    def __enter__(self) -> "Relay":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _run(self, ready: concurrent.futures.Future, port: int) -> None:
        loop = self._loop = asyncio.new_event_loop()
        # The lookups in threads of the guard's own, a few at a time (getaddrinfo blocks).
        self._lookups = concurrent.futures.ThreadPoolExecutor(LOOKUPS, thread_name_prefix="guard-lookup")
        loop.set_default_executor(self._lookups)
        try:
            self._server = loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", port, limit=LIMIT))
        except OSError as error:
            ready.set_exception(error)
            loop.close()
            return
        ready.set_result(self._server.sockets[0].getsockname()[1])
        try:
            loop.run_forever()
        finally:
            # A lookup that still runs ends by itself; nobody waits for it.
            self._lookups.shutdown(wait=False, cancel_futures=True)
            loop.close()

    async def _shutdown(self) -> None:
        if self._server:
            self._server.close()
        for task in [*self._connections, *(entry[1] for entry in self._names.values())]:
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            if len(self._connections) > CONNECTIONS:
                raise Refused(503, "too many connections")
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            except (asyncio.LimitOverrunError, ValueError) as error:
                raise Refused(400, "the request's head is too large") from error
            except (asyncio.IncompleteReadError, TimeoutError):
                return  # the browser went away, or said nothing
            # The reader's limit is the stream's (LIMIT), the head's is smaller.
            if len(head) > HEAD_LIMIT:
                raise Refused(400, "the request's head is too large")
            server_reader, upstream = await self._forward(head, writer)
            await pipe(reader, writer, server_reader, upstream)
        except Refused as refused:
            writer.write(refusal(refused.status, refused.reason))
            try:
                await asyncio.wait_for(writer.drain(), HEAD_TIMEOUT)
            except (OSError, TimeoutError):
                pass  # the browser went away
        except OSError:
            pass  # a connection reset: closed below
        except Exception:  # pylint: disable=broad-exception-caught
            LOG.exception("the browser's guard failed on a connection")
        finally:
            for side in (upstream, writer):
                if side:
                    side.close()
            self._connections.discard(task)

    async def _forward(
        self, head: bytes, writer: asyncio.StreamWriter
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Checks the request and connects where it goes: a tunnel the browser is told of, or a
        plain request sent on (the rest of the browser's bytes follow by pipe)."""
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            raise Refused(400, "not a request")
        if self._token and not authorized(lines[1:], self._token):
            raise Refused(407, "this guard is another browser's")
        if parts[0] != "CONNECT":
            return await self._plain(parts, lines[1:])
        server = await self._connect(*split_target(parts[1]))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        return server

    async def _plain(self, parts: list[str], lines: list[str]) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Sends a plain http request on (method, address, version; its header lines) to the
        checked address in origin form, with the site's Host header (through the proxy's tunnel,
        if any)."""
        method, target, version = parts
        try:
            address = urlsplit(target)
            host, port = address.hostname or "", address.port or 80
        except ValueError as error:
            raise Refused(400, "not an address") from error
        if address.scheme != "http" or not host:
            raise Refused(400, "only http:// addresses (https comes as CONNECT)")
        path = (address.path or "/") + (f"?{address.query}" if address.query else "")
        reader, writer = await self._connect(host, port)
        writer.write((f"{method} {path} {version}\r\n" + sent_headers(lines) + "\r\n").encode("latin-1"))
        return reader, writer

    async def _connect(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A connection to the host's checked address (through the upstream proxy if any); else
        Refused."""
        try:
            addresses = await self.check(host, port)
        except Refused as refused:
            line = f"{host_port(f'{host}:{port}')}: {refused.reason}"
            self.refused.append(line)
            if self._on_refused:
                self._on_refused(line)
            LOG.info("the browser's guard refused %s:%s: %s", host, port, refused.reason)
            raise

        async def attempt(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            if self.upstream and (host, port) != self._loopback:
                return await asyncio.wait_for(self._via_upstream(address, port), CONNECT_TIMEOUT)
            return await asyncio.wait_for(dial(address, port), CONNECT_TIMEOUT)

        # A family that did not connect lately goes second (a stable sort: the order otherwise).
        now = time.monotonic()
        behind = {family for family, until in self._behind.items() if until > now}
        ordered = interleaved(sorted(addresses, key=lambda address: (":" in address) in behind))
        try:
            (reader, writer), address = await race(ordered, attempt)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
            raise Refused(502, f"{host} did not answer ({type(error).__name__})") from error
        if (":" in address) != (":" in ordered[0]):
            # The first family failed or was slow where the other connected (no route, say).
            self._behind[":" in ordered[0]] = now + FAMILY_MEMORY
        return reader, writer

    async def check(self, host: str, port: int) -> list[str]:
        """The addresses the browser may reach the host at (each one public), looked up once for
        NAME_TTL; Refused otherwise. The allowed local address alone is itself."""
        host = host.strip("[]").lower()
        if not 0 < port < 65536:
            raise Refused(400, "no such port")
        if (host, port) == self._loopback:
            return [host]
        if local_name(host):
            raise Refused(403, "a local name")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return await self._resolve(host)
        if not address.is_global:
            raise Refused(403, "not a public address")
        return [str(address)]

    async def _resolve(self, host: str) -> list[str]:
        """A name's checked addresses: one lookup for the connections that ask meanwhile (a page
        opens several to a site at once) and for NAME_TTL after, a refusal for REFUSAL_TTL."""
        entry = self._names.get(host)
        if entry is None or entry[0] <= time.monotonic():
            lookup = asyncio.ensure_future(look_up(host))
            entry = self._names[host] = [math.inf, lookup]
            self._names.move_to_end(host)
            lookup.add_done_callback(functools.partial(expire, entry))
            while len(self._names) > NAMES:
                self._names.popitem(last=False)
        # Shielded: a connection that ends does not end the lookup the others wait for.
        return await asyncio.shield(entry[1])

    async def _via_upstream(self, address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A tunnel to the address through the upstream proxy (CONNECT, SOCKS5), whatever goes
        through it."""
        upstream = self.upstream
        assert upstream is not None
        reader, writer = await dial(upstream.host, upstream.port, tls=upstream.scheme == "https")
        try:
            if upstream.scheme == "socks5":
                await socks5(reader, writer, address, port, upstream.username, upstream.password)
            else:
                target = host_port(f"{address}:{port}")
                writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{upstream.authorization}\r\n".encode())
                answer = await reader.readuntil(b"\r\n\r\n")
                status = answer.split(b" ", 2)[1:2]
                if status != [b"200"]:
                    raise Refused(502, f"the proxy refused ({(status or [b'?'])[0].decode('latin-1')})")
        except BaseException:
            writer.close()
            raise
        return reader, writer


async def look_up(host: str) -> list[str]:
    """The name's addresses, if every one is public; Refused otherwise."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), LOOKUP_TIMEOUT)
    except (OSError, TimeoutError) as error:
        raise Refused(502, "the name does not resolve") from error
    found = list(dict.fromkeys(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos))
    if found and all(address.is_unspecified for address in found):
        # A filter's answer (AdGuard Home's, Pi-hole's): said as such, not as a private address.
        raise Refused(403, f"blocked by the DNS server (it answered {', '.join(map(str, found))})")
    if not found or not all(address.is_global for address in found):
        raise Refused(403, "not a public address")
    return [str(address) for address in found]


def expire(entry: list, lookup: asyncio.Future) -> None:
    """Sets until when a name's finished lookup serves: its addresses NAME_TTL, a refusal
    REFUSAL_TTL, a cancelled one not at all."""
    if lookup.cancelled():
        entry[0] = 0.0
    else:
        entry[0] = time.monotonic() + (REFUSAL_TTL if lookup.exception() else NAME_TTL)


def interleaved(addresses: list[str]) -> list[str]:
    """The addresses with their families taking turns, the first one's first (RFC 8305): a
    family that does not connect (no IPv6 route) holds up only one attempt."""
    first = ":" in addresses[0]
    same = [address for address in addresses if (":" in address) == first]
    other = [address for address in addresses if (":" in address) != first]
    return [address for pair in itertools.zip_longest(same, other) for address in pair if address]


async def race(addresses: list[str], attempt: Callable[[str], Awaitable[T]]) -> tuple[T, str]:
    """The first connection that succeeds, and its address (Happy Eyeballs, RFC 8305): an attempt
    every STAGGER seconds, or as soon as the one before fails, all of them racing; the others are
    cancelled, a second one that connected meanwhile closed. The last failure if none does."""
    queue, attempts = list(addresses), {}
    failure: BaseException | None = None
    try:
        while queue or attempts:
            if queue:
                address = queue.pop(0)
                attempts[asyncio.ensure_future(attempt(address))] = address
            done, _ = await asyncio.wait(
                attempts, timeout=STAGGER if queue else None, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                address = attempts.pop(task)
                if task.exception() is None:
                    return task.result(), address
                failure = task.exception()
    finally:
        for task in attempts:
            task.cancel()
            task.add_done_callback(close_connected)
    assert failure is not None
    raise failure


def close_connected(task: asyncio.Task) -> None:
    """Closes the connection of an attempt that lost the race but connected all the same."""
    if not task.cancelled() and task.exception() is None:
        task.result()[1].close()


async def dial(host: str, port: int, tls: bool = False) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to the host (a checked address, or the upstream proxy; tls: verified for its
    name): where the guard connects, the one place (the tests stand in for it)."""
    context = ssl.create_default_context() if tls else None
    return await asyncio.open_connection(
        host, port, ssl=context, server_hostname=host if context else None, limit=LIMIT
    )


async def socks5(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, address: str, port: int, username: str, password: str
) -> None:
    """Asks a SOCKS5 proxy for a connection to the address (RFC 1928; with credentials, RFC 1929):
    by address, never by name, so that the proxy does not look the name up again."""
    methods = b"\x00\x02" if username else b"\x00"
    writer.write(b"\x05" + bytes([len(methods)]) + methods)
    version, method = await reader.readexactly(2)
    if version != 5 or method not in methods:
        raise Refused(502, "the proxy refused (SOCKS)")
    if method == 2:
        user, secret = username.encode()[:255], password.encode()[:255]
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        if (await reader.readexactly(2))[1] != 0:
            raise Refused(502, "the proxy refused its credentials")
    checked = ipaddress.ip_address(address)
    writer.write(
        b"\x05\x01\x00" + (b"\x01" if checked.version == 4 else b"\x04") + checked.packed + port.to_bytes(2, "big")
    )
    reply = await reader.readexactly(4)
    if reply[0] != 5 or reply[1] != 0:
        raise Refused(502, f"the proxy could not connect (SOCKS {reply[1]})")
    # The proxy's own address for the connection, which nobody needs.
    length = {1: 4, 4: 16}.get(reply[3]) or (await reader.readexactly(1))[0]
    await reader.readexactly(length + 2)


async def pipe(
    browser_reader: asyncio.StreamReader,
    browser_writer: asyncio.StreamWriter,
    server_reader: asyncio.StreamReader,
    server_writer: asyncio.StreamWriter,
) -> None:
    """Streams both ways, a chunk at a time (a side that reads slowly holds the other back), until
    both sides end, one fails, or neither carries anything for IDLE_TIMEOUT: one watch for both
    ways, not a timer for every chunk."""
    last = [time.monotonic()]

    async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(CHUNK):
            last[0] = time.monotonic()
            writer.write(data)
            await writer.drain()
        # The other side learns that this one has said all (it may still answer).
        if writer.can_write_eof():
            writer.write_eof()

    async def watch() -> None:
        while (left := last[0] + IDLE_TIMEOUT - time.monotonic()) > 0:
            await asyncio.sleep(left)

    copies = [
        asyncio.ensure_future(copy(browser_reader, server_writer)),
        asyncio.ensure_future(copy(server_reader, browser_writer)),
    ]
    idle = asyncio.ensure_future(watch())
    try:
        while not all(task.done() for task in copies):
            await asyncio.wait(
                [idle, *(task for task in copies if not task.done())], return_when=asyncio.FIRST_COMPLETED
            )
            if idle.done() or any(task.done() and task.exception() for task in copies):
                break
    finally:
        for task in (*copies, idle):
            task.cancel()
        await asyncio.gather(*copies, idle, return_exceptions=True)
