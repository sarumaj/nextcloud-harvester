"""The browser's guard (harvester.browser.guard): what it lets through and what not, its tunnels
and plain requests, the home connection behind it, rclone's exception for one browser, its limits,
and its life with the browser (started and stopped with it).

The sites are servers on 127.0.0.1 standing in for public addresses: guard.dial, where the guard
connects, takes them for PUBLIC (what the test names resolve to, conftest) and the stand-in
proxies; anything else it refuses, so that nothing reaches the network.
"""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name

import asyncio
import base64
import dataclasses
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from conftest import PUBLIC, needs_loopback
from harvester import browser
from harvester.browser import guard
from harvester.browser.guard import Guard


@dataclasses.dataclass
class Sites:
    """The stand-ins guard.dial reaches: (host, port) to a server's port on 127.0.0.1; and where
    it connected."""

    routes: dict[tuple[str, int], int] = dataclasses.field(default_factory=dict)
    dialed: list[str] = dataclasses.field(default_factory=list)


@pytest.fixture
def sites(monkeypatch):
    real = guard.dial
    found = Sites()

    async def dial(host: str, port: int, tls: bool = False):
        found.dialed.append(f"{host}:{port}")
        if (host, port) in found.routes:
            return await real("127.0.0.1", found.routes[(host, port)])
        if host == "127.0.0.1":
            return await real(host, port, tls)  # the sign-in's exception: the real loopback
        raise ConnectionRefusedError(f"{host}:{port} is no stand-in")

    monkeypatch.setattr(guard, "dial", dial)
    return found


async def echo_server() -> asyncio.Server:
    async def echo(reader, writer):
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(echo, "127.0.0.1", 0)


async def http_server(heads: list[bytes]) -> asyncio.Server:
    """Answers one request with "ok", keeping its head."""

    async def answer(reader, writer):
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(answer, "127.0.0.1", 0)


def port_of(server: asyncio.Server) -> int:
    return server.sockets[0].getsockname()[1]


async def ask(port: int, head: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    """Sends a request to the guard: its answer's head (the connection stays open)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode())
    answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return reader, writer, answer.decode()


async def tunnel(port: int, target: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    return await ask(port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n")


async def echoes(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    writer.write(b"hello")
    return await asyncio.wait_for(reader.readexactly(5), 5) == b"hello"


def checked(host: str, port: int = 443) -> list[str] | str:
    """The guard's check of a destination: the addresses, or why it is refused."""

    async def check():
        try:
            return await Guard().check(host, port)
        except guard.Refused as refused:
            return refused.reason

    return asyncio.run(check())


def resolving(monkeypatch, names: dict[str, list[str]], asked: list[str] | None = None) -> None:
    """The names resolve to these addresses (asked: each lookup's name)."""
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host in names:
            if asked is not None:
                asked.append(host)
            return [
                (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in names[host]
            ]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


# What it lets through


def test_only_public_addresses_pass(monkeypatch):
    resolving(monkeypatch, {"127.0.0.1.nip.io": ["127.0.0.1"], "half.example": [PUBLIC, "10.0.0.5"]})
    assert checked("news.example") == [PUBLIC]
    assert checked("93.184.215.14") == ["93.184.215.14"]
    assert checked("[2606:4700::1]") == ["2606:4700::1"]
    for host in (
        "127.0.0.1",
        "10.0.0.26",
        "192.168.178.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "localhost",
        "app.localhost",
        "printer.local",
        "metadata.internal",
        "127.0.0.1.nip.io",
        "half.example",
    ):
        assert checked(host) in ("not a public address", "a local name"), host
    assert checked("news.example", 0) == "no such port"


def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    def fail(*_args, **_kwargs):
        raise socket.gaierror("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert checked("nowhere.example") == "the name does not resolve"


@needs_loopback
def test_a_tunnel_goes_to_the_checked_address(sites):
    async def run():
        server = await echo_server()
        sites.routes[(PUBLIC, 443)] = port_of(server)
        with Guard() as proxy:
            reader, writer, answer = await tunnel(proxy.port, "news.example:443")
            assert answer.startswith("HTTP/1.1 200")
            assert await echoes(reader, writer)
            writer.close()
        server.close()

    asyncio.run(run())
    assert sites.dialed == [f"{PUBLIC}:443"]


@needs_loopback
def test_a_refused_tunnel_is_answered_and_kept(sites, monkeypatch):
    resolving(monkeypatch, {"127.0.0.1.nip.io": ["127.0.0.1"]})

    async def run(proxy: Guard):
        for target in ("127.0.0.1:9", "10.0.0.1:80", "127.0.0.1.nip.io:443", "[::1]:22"):
            _, writer, answer = await tunnel(proxy.port, target)
            assert answer.startswith("HTTP/1.1 403"), target
            writer.close()

    with Guard() as proxy:
        asyncio.run(run(proxy))
        assert list(proxy.refused) == [
            "127.0.0.1:9: not a public address",
            "10.0.0.1:80: not a public address",
            "127.0.0.1.nip.io:443: not a public address",
            "[::1]:22: not a public address",
        ]
    assert not sites.dialed, "nothing refused is connected"


@needs_loopback
def test_a_plain_request_goes_on_in_origin_form(sites):
    heads: list[bytes] = []

    async def run():
        server = await http_server(heads)
        sites.routes[(PUBLIC, 8080)] = port_of(server)
        with Guard() as proxy:
            reader, writer, answer = await ask(
                proxy.port,
                "GET http://news.example:8080/feed?page=2 HTTP/1.1\r\nHost: news.example:8080\r\n"
                "Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n",
            )
            assert answer.startswith("HTTP/1.1 200") and await reader.read() == b"ok"
            writer.close()
        server.close()

    asyncio.run(run())
    lines = heads[0].decode().split("\r\n")
    assert lines[0] == "GET /feed?page=2 HTTP/1.1"
    assert "Host: news.example:8080" in lines and "Connection: close" in lines
    assert not any(line.lower().startswith(("proxy-", "connection: keep")) for line in lines)


@needs_loopback
def test_bad_requests_are_refused():
    async def run(proxy: Guard):
        for head in (
            "GET https://news.example/ HTTP/1.1\r\n\r\n",
            "GET /relative HTTP/1.1\r\n\r\n",
            "CONNECT news.example HTTP/1.1\r\n\r\n",
            "nonsense\r\n\r\n",
        ):
            _, writer, answer = await ask(proxy.port, head)
            assert answer.startswith("HTTP/1.1 400"), head
            writer.close()

    with Guard() as proxy:
        asyncio.run(run(proxy))


# DNS rebinding: one lookup, the checked address


@needs_loopback
def test_the_name_is_looked_up_once_and_its_checked_address_connected(sites, monkeypatch):
    asked: list[str] = []
    answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])
    real = socket.getaddrinfo

    def rebinding(host, *args, **kwargs):
        if host == "rebind.test":
            asked.append(host)
            address = next(answers)[0]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)

    async def run():
        server = await echo_server()
        sites.routes[(PUBLIC, 443)] = port_of(server)
        with Guard() as proxy:
            reader, writer, answer = await tunnel(proxy.port, "rebind.test:443")
            assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
            writer.close()
            # The name's next answer is the loopback: the next connection is refused.
            _, writer, answer = await tunnel(proxy.port, "rebind.test:443")
            assert answer.startswith("HTTP/1.1 403")
            writer.close()
        server.close()

    asyncio.run(run())
    assert asked == ["rebind.test", "rebind.test"], "one lookup per connection"
    assert sites.dialed == [f"{PUBLIC}:443"], "the checked address, never the name"


@needs_loopback
def test_an_address_that_hangs_holds_up_the_next_one_briefly(sites, monkeypatch):
    resolving(monkeypatch, {"dual.example": ["2001:db8:1::1", "2001:db8:1::2", PUBLIC]})
    real = guard.dial

    async def dial(host: str, port: int, tls: bool = False):
        if ":" in host:
            sites.dialed.append(host)
            await asyncio.sleep(60)  # no IPv6 route: the attempt hangs
        return await real(host, port, tls)

    monkeypatch.setattr(guard, "dial", dial)
    monkeypatch.setattr(Guard, "check", staticmethod(lambda host, port: guard_check(host)))

    async def run():
        server = await echo_server()
        sites.routes[(PUBLIC, 443)] = port_of(server)
        with Guard() as proxy:
            started = time.monotonic()
            reader, writer, answer = await tunnel(proxy.port, "dual.example:443")
            assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
            assert time.monotonic() - started < 1, "the families take turns, the next one starts soon"
            writer.close()
        server.close()

    asyncio.run(run())
    assert sites.dialed == ["2001:db8:1::1", f"{PUBLIC}:443"], "IPv4 second, before the other IPv6"


async def guard_check(host: str) -> list[str]:
    """The addresses of dual.example as the check would give them (2001:db8::/32 is not public)."""
    assert host == "dual.example"
    return ["2001:db8:1::1", "2001:db8:1::2", PUBLIC]


def test_interleaved_families():
    assert guard.interleaved(["a::1", "a::2", "1.1.1.1", "a::3", "2.2.2.2"]) == [
        "a::1",
        "1.1.1.1",
        "a::2",
        "2.2.2.2",
        "a::3",
    ]
    assert guard.interleaved(["1.1.1.1"]) == ["1.1.1.1"]


@needs_loopback
def test_a_site_that_does_not_answer_is_a_bad_gateway(sites):
    async def run(proxy: Guard):
        _, writer, answer = await tunnel(proxy.port, "news.example:443")  # no stand-in: refused
        assert answer.startswith("HTTP/1.1 502")
        writer.close()

    with Guard() as proxy:
        asyncio.run(run(proxy))
    assert sites.dialed == [f"{PUBLIC}:443"]


# The home connection


async def http_proxy(seen: list[str], sites: Sites, refuse: bool = False) -> asyncio.Server:
    """A stand-in http proxy like tinyproxy: CONNECTs to the stand-in sites; a request with the
    whole address goes on with a Host header rebuilt from that address."""

    async def serve(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        seen.append(head)
        method, target, version = head.split("\r\n", 1)[0].split(" ")
        if refuse:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        elif method == "CONNECT":
            host, port = target.rsplit(":", 1)
            site_reader, site_writer = await asyncio.open_connection("127.0.0.1", sites.routes[(host, int(port))])
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await guard.pipe(reader, writer, site_reader, site_writer)
            site_writer.close()
        else:
            address = urlsplit(target)
            site_reader, site_writer = await asyncio.open_connection(
                "127.0.0.1", sites.routes[(address.hostname, address.port or 80)]
            )
            kept = [
                line for line in head.split("\r\n")[1:] if line and not line.lower().startswith(("host:", "proxy-"))
            ]
            rebuilt = [f"{method} {address.path} {version}", f"Host: {address.netloc}", *kept]
            site_writer.write(("\r\n".join(rebuilt) + "\r\n\r\n").encode())
            await guard.pipe(reader, writer, site_reader, site_writer)
            site_writer.close()
        await writer.drain()
        writer.close()

    return await asyncio.start_server(serve, "127.0.0.1", 0)


async def named_site(heads: list[bytes]) -> asyncio.Server:
    """A site served by name (a virtual host): news.example answers "ok", any other Host 404."""

    async def answer(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        heads.append(head)
        if b"\r\nHost: news.example\r\n" in head:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 7\r\nConnection: close\r\n\r\nno site")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(answer, "127.0.0.1", 0)


@needs_loopback
def test_the_home_connection_gets_the_checked_address_with_credentials(sites):
    seen: list[str] = []
    heads: list[bytes] = []

    async def run():
        site, named, home = await echo_server(), await named_site(heads), await http_proxy(seen, sites)
        sites.routes[(PUBLIC, 443)] = port_of(site)
        sites.routes[(PUBLIC, 80)] = port_of(named)
        sites.routes[("proxy.example", 3128)] = port_of(home)
        with Guard("http://anna%40home:p%3Ass@proxy.example:3128") as proxy:
            reader, writer, answer = await tunnel(proxy.port, "news.example:443")
            assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
            writer.close()
            _, writer, answer = await tunnel(proxy.port, "192.168.178.1:80")
            assert answer.startswith("HTTP/1.1 403"), "checked before the proxy is asked"
            writer.close()
            reader, writer, answer = await ask(
                proxy.port, "GET http://news.example/feed HTTP/1.1\r\nHost: news.example\r\n\r\n"
            )
            assert answer.startswith("HTTP/1.1 200") and await reader.read() == b"ok", "the site by its name"
            writer.close()
        # The stand-in does rebuild Host: the whole address with the checked one would miss the site.
        reader, writer = await asyncio.open_connection("127.0.0.1", port_of(home))
        writer.write(f"GET http://{PUBLIC}/feed HTTP/1.1\r\nHost: news.example\r\n\r\n".encode())
        assert (await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 404")
        writer.close()
        for server in (site, named, home):
            server.close()

    asyncio.run(run())
    credentials = base64.b64encode(b"anna@home:p:ss").decode()
    assert seen[0].startswith(f"CONNECT {PUBLIC}:443 HTTP/1.1\r\n")
    assert f"Proxy-Authorization: Basic {credentials}\r\n" in seen[0]
    assert seen[1].startswith(f"CONNECT {PUBLIC}:80 HTTP/1.1\r\n"), "plain http through a tunnel too"
    assert f"Proxy-Authorization: Basic {credentials}\r\n" in seen[1]
    assert len(seen) == 3, "the refused address never reached the proxy (the third: the check above)"
    assert heads[0].startswith(b"GET /feed HTTP/1.1\r\n") and b"Connection: close" in heads[0]


@needs_loopback
def test_a_proxy_that_refuses_is_told(sites):
    async def run():
        home = await http_proxy([], sites, refuse=True)
        sites.routes[("proxy.example", 3128)] = port_of(home)
        with Guard("http://proxy.example:3128") as proxy:
            reader, writer, answer = await tunnel(proxy.port, "news.example:443")
            assert answer.startswith("HTTP/1.1 502")
            assert b"the proxy refused (407)" in await asyncio.wait_for(reader.read(), 5)
            writer.close()
        home.close()

    asyncio.run(run())


async def socks_proxy(seen: list[bytes], sites: Sites) -> asyncio.Server:
    """A stand-in SOCKS5 proxy with a username and password (RFC 1928, 1929)."""

    async def serve(reader, writer):
        _, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        assert 2 in methods
        writer.write(b"\x05\x02")
        _, length = await reader.readexactly(2)
        user = await reader.readexactly(length)
        secret = await reader.readexactly((await reader.readexactly(1))[0])
        seen.append(user + b":" + secret)
        writer.write(b"\x01\x00")
        request = await reader.readexactly(4)
        seen.append(request)
        address = await reader.readexactly({1: 4, 4: 16}[request[3]])
        port = int.from_bytes(await reader.readexactly(2), "big")
        host = socket.inet_ntop(socket.AF_INET if request[3] == 1 else socket.AF_INET6, address)
        seen.append(f"{host}:{port}".encode())
        site_reader, site_writer = await asyncio.open_connection("127.0.0.1", sites.routes[(host, port)])
        writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        await guard.pipe(reader, writer, site_reader, site_writer)
        site_writer.close()
        writer.close()

    return await asyncio.start_server(serve, "127.0.0.1", 0)


@needs_loopback
def test_a_socks_proxy_gets_the_checked_address_never_the_name(sites):
    seen: list[bytes] = []

    async def run():
        site, home = await echo_server(), await socks_proxy(seen, sites)
        sites.routes[(PUBLIC, 443)] = port_of(site)
        sites.routes[("proxy.example", 1080)] = port_of(home)
        with Guard("socks5h://anna:secret@proxy.example:1080") as proxy:
            reader, writer, answer = await tunnel(proxy.port, "news.example:443")
            assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
            writer.close()
        site.close()
        home.close()

    asyncio.run(run())
    assert seen == [b"anna:secret", b"\x05\x01\x00\x01", f"{PUBLIC}:443".encode()], "by address (ATYP 1)"


def test_an_unknown_proxy_is_refused():
    with pytest.raises(ValueError):
        Guard("ftp://proxy.example:21")


# rclone's sign-in: one local address, for one browser, while allowed


@needs_loopback
@pytest.mark.usefixtures("sites")
def test_the_sign_in_browsers_guard_alone_opens_rclones_listener():
    async def run():
        listener = await echo_server()
        port = port_of(listener)
        signing_in, recording = Guard(), Guard()
        signing_in.allow(f"http://127.0.0.1:{port}")
        with signing_in, recording:
            reader, writer, answer = await tunnel(signing_in.port, f"127.0.0.1:{port}")
            assert answer.startswith("HTTP/1.1 200") and await echoes(reader, writer)
            writer.close()
            for elsewhere in (f"127.0.0.1:{port + 1}", f"localhost:{port}", f"127.0.0.2:{port}", f"[::1]:{port}"):
                _, writer, answer = await tunnel(signing_in.port, elsewhere)
                assert answer.startswith("HTTP/1.1 403"), elsewhere
                writer.close()
            _, writer, answer = await tunnel(recording.port, f"127.0.0.1:{port}")
            assert answer.startswith("HTTP/1.1 403"), "another browser has no exception"
            writer.close()
            signing_in.allow("")
            _, writer, answer = await tunnel(signing_in.port, f"127.0.0.1:{port}")
            assert answer.startswith("HTTP/1.1 403"), "gone once rclone ends"
            writer.close()
        listener.close()

    asyncio.run(run())


def test_the_recorders_exception_reaches_its_guard():
    from harvester.browser.recorder import Recorder  # pylint: disable=import-outside-toplevel

    recorder = Recorder(lambda message: asyncio.sleep(0))
    recorder.loopback = "http://127.0.0.1:53682"  # before the browser starts, as main.sign_in does
    recorder._gate = Guard()
    recorder._gate.allow(recorder.loopback)
    assert recorder._gate._loopback == ("127.0.0.1", 53682)
    recorder.loopback = ""
    assert recorder._gate._loopback is None


# Limits


@needs_loopback
def test_a_head_too_large_is_refused():
    async def run(proxy: Guard):
        _, writer, answer = await ask(
            proxy.port, "GET http://news.example/ HTTP/1.1\r\nX-Long: " + "a" * guard.HEAD_LIMIT + "\r\n\r\n"
        )
        assert answer.startswith("HTTP/1.1 400")
        writer.close()

    with Guard() as proxy:
        asyncio.run(run(proxy))


@needs_loopback
def test_connections_beyond_the_limit_are_refused(sites, monkeypatch):
    monkeypatch.setattr(guard, "CONNECTIONS", 2)

    async def run():
        server = await echo_server()
        sites.routes[(PUBLIC, 443)] = port_of(server)
        with Guard() as proxy:
            opened = [await tunnel(proxy.port, "news.example:443") for _ in range(2)]
            assert all(answer.startswith("HTTP/1.1 200") for _, _, answer in opened)
            _, writer, answer = await tunnel(proxy.port, "news.example:443")
            assert answer.startswith("HTTP/1.1 503")
            writer.close()
            opened[0][1].close()
            await asyncio.sleep(0.2)
            _, writer, answer = await tunnel(proxy.port, "news.example:443")
            assert answer.startswith("HTTP/1.1 200"), "a closed one frees its place"
            writer.close()
            opened[1][1].close()
        server.close()

    asyncio.run(run())


@needs_loopback
def test_idle_and_silent_connections_are_closed(sites, monkeypatch):
    monkeypatch.setattr(guard, "IDLE_TIMEOUT", 0.3)
    monkeypatch.setattr(guard, "HEAD_TIMEOUT", 0.3)

    async def run():
        server = await echo_server()
        sites.routes[(PUBLIC, 443)] = port_of(server)
        with Guard() as proxy:
            reader, writer, _ = await tunnel(proxy.port, "news.example:443")
            assert await echoes(reader, writer)
            assert await asyncio.wait_for(reader.read(), 3) == b"", "idle: closed"
            writer.close()
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            assert await asyncio.wait_for(reader.read(), 3) == b"", "no request: closed"
            writer.close()
            await asyncio.sleep(0.1)
            assert proxy.connections == 0
        server.close()

    asyncio.run(run())


@needs_loopback
def test_a_slow_reader_holds_the_stream_back(sites):
    """Nothing is buffered without bound: a browser that reads nothing stops the site's sending
    after a few chunks."""
    sent = [0]

    async def run():
        async def flood(_reader, writer):
            try:
                while True:
                    writer.write(b"x" * 65536)
                    await writer.drain()
                    sent[0] += 65536
            except (ConnectionError, OSError):
                pass

        server = await asyncio.start_server(flood, "127.0.0.1", 0)
        sites.routes[(PUBLIC, 443)] = port_of(server)
        with Guard() as proxy:
            _, writer, _ = await tunnel(proxy.port, "news.example:443")
            await asyncio.sleep(1)
            writer.close()
        server.close()

    asyncio.run(run())
    # The sockets' own buffers (some MB on Linux) and the guard's few chunks: not hundreds of MB.
    assert sent[0] < 64 * 1024 * 1024


# Life with the browser


def refuses(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return False
    except OSError:
        return True


@needs_loopback
def test_stopping_frees_the_port_and_closes_the_connections(sites):
    threads = threading.active_count()

    async def run():
        server = await echo_server()
        sites.routes[(PUBLIC, 443)] = port_of(server)
        proxy = Guard()
        port = proxy.start()
        reader, writer, _ = await tunnel(port, "news.example:443")
        assert await echoes(reader, writer) and proxy.connections == 1
        started = time.monotonic()
        await asyncio.to_thread(proxy.stop)
        assert time.monotonic() - started < 2
        assert await asyncio.wait_for(reader.read(), 2) == b"", "the tunnel ends with its guard"
        writer.close()
        server.close()
        return port, proxy

    port, proxy = asyncio.run(run())
    assert refuses(port) and proxy.connections == 0
    proxy.stop()  # again: nothing
    Guard().stop()  # never started: nothing
    time.sleep(0.2)
    assert threading.active_count() <= threads, "its threads end"


@needs_loopback
def test_the_jobs_browser_stops_its_guard_on_every_path(monkeypatch):
    from test_browser import needs_chromium  # pylint: disable=import-outside-toplevel

    if needs_chromium.args[0]:
        pytest.skip("Chromium is not installed")
    from harvester.sources import site  # pylint: disable=import-outside-toplevel

    free = browser.free_browsers()
    driver = site.BrowserDriver([], {})
    try:
        assert driver._guard is not None
        port = driver._guard.port
        assert not refuses(port)
    finally:
        driver.close()
    assert refuses(port) and browser.free_browsers() == free

    ports: list[int] = []
    real = browser.launch_options

    def failing(guard_port: int, http1: bool = False):
        ports.append(guard_port)
        raise RuntimeError(f"no browser ({real(guard_port, http1)['proxy']['server']})")

    monkeypatch.setattr(browser, "launch_options", failing)
    with pytest.raises(RuntimeError):
        site.BrowserDriver([], {})
    assert refuses(ports[0]) and browser.free_browsers() == free, "a failed start frees both"


@needs_loopback
def test_the_recorders_guard_stops_with_it():
    from test_browser import needs_chromium  # pylint: disable=import-outside-toplevel

    if needs_chromium.args[0]:
        pytest.skip("Chromium is not installed")
    from harvester.browser.recorder import Recorder  # pylint: disable=import-outside-toplevel

    async def run():
        recorder = Recorder(lambda message: asyncio.sleep(0))
        recorder.loopback = "http://127.0.0.1:53682"
        try:
            await recorder.start()
            assert recorder._gate is not None and recorder._gate._loopback == ("127.0.0.1", 53682)
            port = recorder._gate.port
            assert not refuses(port)
        finally:
            await recorder.close()  # also when a check fails: no browser left to the next tests
        return port

    assert refuses(asyncio.run(run()))


# In Chromium: what Playwright's routes do not see reaches the guard


# A peer connection with a STUN server: how many candidates it gathered in two seconds.
PEER = """async server => {
  const peer = new RTCPeerConnection({iceServers: [{urls: server}]})
  peer.createDataChannel('x')
  await peer.setLocalDescription(await peer.createOffer())
  await new Promise(done => setTimeout(done, 2000))
  const found = (peer.localDescription.sdp.match(/a=candidate/g) || []).length
  peer.close()
  return found
}"""


@needs_loopback
def test_chromium_goes_through_its_guard_for_everything(sites, monkeypatch):  # pylint: disable=too-many-locals
    """A worker's WebSocket and a page's fetch to the loopback and the private network are
    refused at the guard (the routes, the second layer, are not set here); a public page loads."""
    from test_browser import needs_chromium  # pylint: disable=import-outside-toplevel

    if needs_chromium.args[0]:
        pytest.skip("Chromium is not installed")
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    resolving(monkeypatch, {"127.0.0.1.nip.io": ["127.0.0.1"]})
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    stun = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stun.bind(("127.0.0.1", 0))
    local = listener.getsockname()[1]
    heads: list[bytes] = []
    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(http_server(heads))
    threading.Thread(target=loop.run_forever, daemon=True).start()
    sites.routes[(PUBLIC, 80)] = port_of(server)
    with Guard() as proxy, sync_playwright() as playwright:
        chromium = playwright.chromium.launch(**browser.launch_options(proxy.port))
        try:
            page = chromium.new_page()
            page.goto("http://news.example/")
            assert page.inner_text("body") == "ok"
            worker = """target => new Promise(done => {
              const code = `const s = new WebSocket('${target}'); s.onopen = () => postMessage('open');`
                + ` s.onclose = () => postMessage('closed'); s.onerror = () => postMessage('error')`
              const w = new Worker(URL.createObjectURL(new Blob([code])))
              w.onmessage = e => done(e.data)
              setTimeout(() => done('timeout'), 5000)
            })"""
            for target in (f"ws://127.0.0.1:{local}/x", "ws://10.0.0.1/x"):
                assert page.evaluate(worker, target) != "open", target
            # A refused plain request is the guard's 403 (an opaque answer to no-cors), never the
            # local server's: the guard keeps each refusal.
            fetched = "url => fetch(url, {mode: 'no-cors'}).then(() => 'answered', () => 'failed')"
            for url in (f"http://127.0.0.1:{local}/", f"http://127.0.0.1.nip.io:{local}/"):
                page.evaluate(fetched, url)
            # WebRTC's STUN (UDP) would pass by any proxy: it sends nothing at all.
            stun.settimeout(3)
            assert page.evaluate(PEER, f"stun:127.0.0.1:{stun.getsockname()[1]}") == 0, "no candidates"
            with pytest.raises(OSError):
                stun.recvfrom(1024)
        finally:
            chromium.close()
        refused = list(proxy.refused)
    loop.call_soon_threadsafe(loop.stop)
    assert f"127.0.0.1:{local}: not a public address" in refused, refused
    assert "10.0.0.1:80: not a public address" in refused, refused
    assert refused.count(f"127.0.0.1:{local}: not a public address") >= 2, "the worker's and the fetch"
    assert f"127.0.0.1.nip.io:{local}: not a public address" in refused, refused
    with pytest.raises(OSError):
        listener.accept()  # nothing ever reached the local listener
    listener.close()
    stun.close()
