"""The guard: a proxy in the app that each browser's connections go through, every one of them.

Playwright's routes see a page's requests and its own WebSockets, not those of its workers, and
the name they check Chromium looks up once more as it connects: a page could reach the
container's own loopback that way, or, without the External Apps' network rules, the private
network. Chromium starts with its guard as its proxy for everything, loopback included
(launch_options): a worker's WebSocket, a fetch() and a navigation alike reach the guard, which
looks the name up itself, refuses it unless every address it resolves to is public (as
allowed_host), and connects to an address it checked, never looking the name up again.

https and WebSockets come as CONNECT (a tunnel), plain http as a request with the whole address,
which goes on in origin form. With the home connection (browser.PROXY: http, https, socks5 or
socks5h), the guard checks the destination first and then asks the proxy for the checked address,
never the name: the proxy is the one address the guard connects to unchecked. A browser's guard
may also let it open one local address (allow: rclone's sign-in listener, while rclone runs).

One guard per browser, on 127.0.0.1 with a port of its own, in a thread with its own event loop:
the recorder runs on the app's loop, the jobs' browsers in the runners' threads (Playwright's
sync API), and either starts and stops its guard with its browser.
"""

import asyncio
import base64
import collections
import concurrent.futures
import ipaddress
import itertools
import logging
import socket
import ssl
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar
from urllib.parse import unquote, urlsplit

from . import LOOKUP_TIMEOUT, local_name

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
# What one read takes at most: the connections stream, each holds a few chunks at most.
CHUNK = 64 * 1024
# How long one address may try to connect before the next one starts trying too (s; RFC 8305's
# "Connection Attempt Delay").
STAGGER = 0.25
# The names looked up at once (the lookups block a thread each).
LOOKUPS = 8
# The refusals kept for the logs and the tests.
REFUSALS = 100
# How long stopping may take (s): the connections are cancelled, the loop ends.
STOP_TIMEOUT = 5.0
# A request's headers that concern the proxy (or this hop) alone, never sent on.
HOP_HEADERS = frozenset({"proxy-connection", "proxy-authorization", "keep-alive", "connection"})
T = TypeVar("T")
PHRASES = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway", 503: "Service Unavailable"}


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
    head = (
        f"HTTP/1.1 {status} {PHRASES.get(status, 'Error')}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


class Guard:  # pylint: disable=too-many-instance-attributes
    """A browser's guard: start() gives the port to launch the browser with, stop() ends it and
    every connection it holds (also as a context manager)."""

    def __init__(self, upstream: str = ""):
        self.upstream = Upstream(upstream) if upstream else None
        self.port = 0
        # Why the guard refused, latest last ("host:port: reason").
        self.refused: collections.deque[str] = collections.deque(maxlen=REFUSALS)
        self._loopback: tuple[str, int] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.Server | None = None
        self._lookups: concurrent.futures.ThreadPoolExecutor | None = None
        self._connections: set[asyncio.Task] = set()

    def allow(self, origin: str) -> None:
        """Lets this browser open the one local address of the origin (scheme://host:port; its
        host and port, whichever the scheme), or none ("")."""
        if not origin:
            self._loopback = None
            return
        address = urlsplit(origin)
        self._loopback = (address.hostname or "", address.port or 80)

    @property
    def connections(self) -> int:
        """How many connections the guard holds now."""
        return len(self._connections)

    def start(self) -> int:
        """Starts the guard on 127.0.0.1 and gives its port."""
        ready = concurrent.futures.Future()  # type: ignore[var-annotated]
        self._thread = threading.Thread(target=self._run, args=(ready,), name=f"guard-{id(self):x}", daemon=True)
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

    def __enter__(self) -> "Guard":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _run(self, ready: concurrent.futures.Future) -> None:
        loop = self._loop = asyncio.new_event_loop()
        # The lookups in threads of the guard's own, a few at a time (getaddrinfo blocks).
        self._lookups = concurrent.futures.ThreadPoolExecutor(LOOKUPS, thread_name_prefix="guard-lookup")
        loop.set_default_executor(self._lookups)
        try:
            self._server = loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0, limit=HEAD_LIMIT))
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
        for task in list(self._connections):
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
        if parts[0] != "CONNECT":
            return await self._plain(parts, lines[1:])
        server = await self._connect(*split_target(parts[1]))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        return server[0], server[1]

    async def _plain(self, parts: list[str], lines: list[str]) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Sends a plain http request on (method, address, version; its header lines) to the
        checked address, in origin form (or to an http proxy, with the checked address)."""
        method, target, version = parts
        try:
            address = urlsplit(target)
            host, port = address.hostname or "", address.port or 80
        except ValueError as error:
            raise Refused(400, "not an address") from error
        if address.scheme != "http" or not host:
            raise Refused(400, "only http:// addresses (https comes as CONNECT)")
        path = (address.path or "/") + (f"?{address.query}" if address.query else "")
        reader, writer, checked = await self._connect(host, port, plain=True)
        if self.upstream and self.upstream.scheme in ("http", "https"):
            # An http proxy takes the whole address: the checked one, with the site's Host header.
            line = f"{method} http://{checked}{path} {version}\r\n{self.upstream.authorization}"
        else:
            line = f"{method} {path} {version}\r\n"
        writer.write((line + sent_headers(lines) + "\r\n").encode("latin-1"))
        return reader, writer

    async def _connect(
        self, host: str, port: int, plain: bool = False
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        """A connection to the host's checked address (through the upstream proxy if any), with
        the address and port it went to; else Refused. plain: an http request that an http proxy
        takes as it is (no tunnel)."""
        try:
            addresses = await self.check(host, port)
        except Refused as refused:
            self.refused.append(f"{host_port(f'{host}:{port}')}: {refused.reason}")
            LOG.info("the browser's guard refused %s:%s: %s", host, port, refused.reason)
            raise

        async def attempt(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            if self.upstream and (host, port) != self._loopback:
                return await asyncio.wait_for(self._via_upstream(address, port, plain), CONNECT_TIMEOUT)
            return await asyncio.wait_for(dial(address, port), CONNECT_TIMEOUT)

        try:
            (reader, writer), address = await race(interleaved(addresses), attempt)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
            raise Refused(502, f"{host} did not answer ({type(error).__name__})") from error
        return reader, writer, host_port(f"{address}:{port}")

    async def check(self, host: str, port: int) -> list[str]:
        """The addresses the browser may reach the host at (each one public), looked up once;
        Refused otherwise. The allowed local address alone is itself."""
        host = host.strip("[]").lower()
        if not 0 < port < 65536:
            raise Refused(400, "no such port")
        if (host, port) == self._loopback:
            return [host]
        if local_name(host):
            raise Refused(403, "a local name")
        try:
            found = [ipaddress.ip_address(host)]
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), LOOKUP_TIMEOUT)
            except (OSError, TimeoutError) as error:
                raise Refused(502, "the name does not resolve") from error
            found = list(dict.fromkeys(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos))
        if not found or not all(address.is_global for address in found):
            raise Refused(403, "not a public address")
        return [str(address) for address in found]

    async def _via_upstream(
        self, address: str, port: int, plain: bool
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A connection to the address through the upstream proxy: a tunnel (CONNECT, SOCKS5),
        or for a plain request to an http proxy the proxy's connection itself."""
        upstream = self.upstream
        assert upstream is not None
        reader, writer = await dial(upstream.host, upstream.port, tls=upstream.scheme == "https")
        try:
            if upstream.scheme == "socks5":
                await socks5(reader, writer, address, port, upstream.username, upstream.password)
            elif not plain:
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
        host, port, ssl=context, server_hostname=host if context else None, limit=CHUNK
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
    both sides end, one fails, or neither carries anything for IDLE_TIMEOUT."""
    last = [time.monotonic()]

    async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(CHUNK), IDLE_TIMEOUT)
            except TimeoutError:
                if time.monotonic() - last[0] < IDLE_TIMEOUT:
                    continue  # the other way carries something
                return False
            if not data:
                break
            last[0] = time.monotonic()
            writer.write(data)
            await writer.drain()
        # The other side learns that this one has said all (it may still answer).
        if writer.can_write_eof():
            writer.write_eof()
        return True

    copies = [
        asyncio.ensure_future(copy(browser_reader, server_writer)),
        asyncio.ensure_future(copy(server_reader, browser_writer)),
    ]
    try:
        pending: set = set(copies)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(task.exception() or not task.result() for task in done):
                break
    finally:
        for task in copies:
            task.cancel()
        await asyncio.gather(*copies, return_exceptions=True)
