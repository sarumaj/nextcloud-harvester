# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin's DNS servers (harvester.dns): which entries are servers, dnsproxy's command line,
the container's resolv.conf, the status, and a name the servers block told as such (the
messages, the guard). dnsproxy itself runs in the image (checked there, not here)."""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name,unused-argument

import asyncio
import socket
import struct

import pytest

from harvester import browser, dns, settings
from harvester.browser import guard

DOCKERS = "nameserver 127.0.0.11\nsearch corp.example\noptions edns0 trust-ad ndots:0\n"


@pytest.mark.parametrize(
    "entry",
    [
        "9.9.9.9",
        "9.9.9.9:5353",
        "2620:fe::fe",
        "[2620:fe::fe]:53",
        "tls://dns.quad9.net",
        "tls://home-proxy.guardian.example:853",
        "https://guardian.example/dns-query/harvester",
        "https://dns.example:8443/dns-query",
    ],
)
def test_servers_are_addresses_tls_or_https(entry):
    assert dns.problem(entry) == ""


@pytest.mark.parametrize(
    "entry",
    [
        "tls://x;rm",
        "$(id)",
        "-u",
        "--upstream=1.1.1.1",
        "/etc/passwd",
        "[/example.org/]1.1.1.1",
        "udp://9.9.9.9",
        "quic://dns.example",
        "tls://dns.example/path",
        "https://dns.example",
        "https://user:secret@dns.example/dns-query",
        "https://dns.example/dns-query?x=1",
        "https://dns.example/dns query",
        "tls://dns.example:0",
        "tls://dns.example:99999",
        "tls://under_score.example",
        "0.0.0.0",
        "127.0.0.1",
        "::1",
        "9.9.9.9:0",
        "dns.example",
    ],
)
def test_anything_else_is_refused(entry):
    assert dns.problem(entry)


def test_the_setting_takes_commas_spaces_and_lines_and_refuses_all_for_one_wrong():
    assert dns.parsed(" tls://a.example, 9.9.9.9\nhttps://b.example/q  9.9.9.9") == (
        ["tls://a.example", "9.9.9.9", "https://b.example/q"],
        [],
    )
    servers, problems = dns.parsed("tls://a.example, tls://x;y")
    assert not servers and problems == ["tls://x;y: not an address, tls:// or https:// address"]
    assert dns.parsed(",".join(f"9.9.9.{n}" for n in range(1, 10)))[1] == ["more than 8 servers"]
    assert dns.parsed("") == ([], []) and dns.parsed(None) == ([], [])


def test_dnsproxys_command_line_is_a_list_of_checked_arguments():
    line = dns.command(["tls://dns.example", "9.9.9.9"], ["127.0.0.11"], False, ["cloud.example", "appapi-harp", "x;y"])
    assert line == [
        "dnsproxy",
        "--listen=127.0.0.1",
        "--port=53",
        "--cache",
        "--upstream=tls://dns.example",
        "--upstream=9.9.9.9",
        "--bootstrap=127.0.0.11",
        "--upstream=[/cloud.example/appapi-harp/]127.0.0.11",
    ]
    with_fallback = dns.command(["tls://dns.example"], ["127.0.0.11", "1.1.1.1"], True, [])
    assert "--fallback=127.0.0.11" in with_fallback and "--fallback=1.1.1.1" in with_fallback
    assert not any(word.startswith("--upstream=[") for word in with_fallback)


def test_the_names_that_stay_dockers(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://Cloud.Example/")
    monkeypatch.setenv("HP_FRP_ADDRESS", "appapi-harp:8782")
    assert dns.own_names("http://10.0.0.5:3128", "http://proxy.example:3128", "") == [
        "cloud.example",
        "appapi-harp",
        "proxy.example",
    ]


def test_the_containers_file_names_the_proxy_with_dockers_search_and_options():
    assert dns.nameservers(DOCKERS + "nameserver fe80::1%eth0\nnameserver 1.1.1.1\n") == ["127.0.0.11", "1.1.1.1"]
    assert dns.search_domains(DOCKERS + "search .\n") == ["corp.example"]
    own = dns.own_file(DOCKERS)
    assert own.startswith(dns.MARK)
    assert own.splitlines()[1:] == ["nameserver 127.0.0.1", "search corp.example", "options edns0 trust-ad ndots:0"]


def answer(code: int, *addresses: str) -> bytes:
    """A DNS answer to example.com's A: its question, and each address with a pointer to it."""
    head = struct.pack(">HHHHHH", 7, 0x8180 | code, 1, len(addresses), 0, 0)
    question = b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    records = b"".join(
        b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(address) for address in addresses
    )
    return head + question + records


def test_an_answer_is_read_and_a_filters_answer_told():
    assert dns.answer_of(answer(0, "93.184.215.14", "0.0.0.0")) == (0, ["93.184.215.14", "0.0.0.0"])
    assert dns.answer_of(answer(3)) == (3, [])
    assert dns.blocked(["0.0.0.0"]) and dns.blocked(["0.0.0.0", "::"])
    assert not dns.blocked([]) and not dns.blocked(["0.0.0.0", "93.184.215.14"])


class Started(dns.Proxy):
    """The proxy without dnsproxy: started and listening as told, its lookup's answer given."""

    def __init__(self, tmp_path, listens: bool = True, answer_: tuple = (0, ["93.184.215.14"])):
        super().__init__(tmp_path / "resolv.conf", tmp_path / "resolv.conf.docker")
        self.resolv.write_text(DOCKERS, encoding="utf-8")
        self.dockers.write_text(DOCKERS, encoding="utf-8")
        self.lines: list[list[str]] = []
        self._listens, self._answer = listens, answer_

    def start(self, line):
        self.lines.append(line)
        self.line = line

    def listens(self):
        return self._listens


@pytest.fixture
def looked_up(monkeypatch):
    """The status's lookup, answered as the test says (Started's answer)."""
    current: list = []
    monkeypatch.setattr(dns, "query", lambda name: current[0])
    return current


def test_servers_set_point_the_container_at_the_proxy_and_back(tmp_path, looked_up):
    proxy = Started(tmp_path)
    looked_up.append((0, ["93.184.215.14"]))
    assert proxy.apply("tls://dns.example", False, ["cloud.example"]) == dns.WORKS and proxy.active
    assert proxy.resolv.read_text(encoding="utf-8") == dns.own_file(DOCKERS)
    assert "--upstream=[/cloud.example/corp.example/]127.0.0.11" in proxy.lines[0]
    # The same setting a minute later: dnsproxy is not started again (its line is the same).
    proxy.running = type("Running", (), {"poll": lambda self: None, "terminate": lambda self: None})()
    proxy.apply("tls://dns.example", False, ["cloud.example"])
    assert len(proxy.lines) == 1
    proxy.running = None
    assert proxy.apply("", False, []) == dns.UNSET and not proxy.active
    assert proxy.resolv.read_text(encoding="utf-8") == DOCKERS


def test_a_failing_server_keeps_the_proxy_so_that_filtering_is_not_bypassed(tmp_path, looked_up):
    proxy = Started(tmp_path)
    looked_up.append((2, []))
    assert proxy.apply("9.9.9.9", False, []) == dns.FAILS and proxy.active
    assert proxy.resolv.read_text(encoding="utf-8").startswith(dns.MARK)
    looked_up[0] = (0, ["0.0.0.0"])
    assert proxy.apply("9.9.9.9", False, []) == dns.FAILS


def test_a_proxy_that_does_not_start_leaves_dockers_dns(tmp_path, looked_up):
    proxy = Started(tmp_path, listens=False)
    proxy.resolv.write_text(dns.own_file(DOCKERS), encoding="utf-8")
    looked_up.append((0, ["93.184.215.14"]))
    assert proxy.apply("9.9.9.9", False, []) == dns.NOT_RUNNING and not proxy.active
    assert proxy.resolv.read_text(encoding="utf-8") == DOCKERS


def test_a_wrong_entry_or_a_file_not_the_apps_leaves_dockers_dns(tmp_path, looked_up, monkeypatch):
    proxy = Started(tmp_path)
    looked_up.append((0, ["93.184.215.14"]))
    assert proxy.apply("9.9.9.9, tls://x;y", False, []) == dns.INVALID and not proxy.lines
    monkeypatch.setattr(dns.os, "access", lambda path, mode: False)
    assert proxy.apply("9.9.9.9", False, []) == dns.NOT_WRITABLE and not proxy.lines
    assert proxy.resolv.read_text(encoding="utf-8") == DOCKERS


def test_the_status_is_the_dns_forms_description():
    form = settings.dns_form("tls://dns.example", True, dns.WORKS)
    assert form.id == "harvester_dns" and form.description == dns.WORKS
    assert [field.id for field in form.fields] == [settings.DNS, settings.DNS_FALLBACK]
    assert form.fields[0].default == "tls://dns.example" and form.fields[1].default == settings.ON


@pytest.fixture
def filtered(monkeypatch):
    """ads.example resolves as a filter answers (0.0.0.0, ::); gone.example not at all."""
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host == "ads.example":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("0.0.0.0", 0)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::", 0, 0, 0)),
            ]
        if host == "gone.example":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    browser._resolves_public.cache_clear()


def test_a_blocked_name_is_told_as_the_dns_servers_doing(filtered, monkeypatch):
    assert not browser.allowed_url("https://ads.example/banner.js")
    why = ": blocked by the DNS server (it answered 0.0.0.0, ::)"
    assert browser.dns_refusal("https://ads.example/banner.js") == why
    assert browser.dns_refusal("ads.example") == why
    assert browser.dns_refusal("https://news.example/") == "" and browser.dns_refusal("http://10.0.0.1/") == ""
    error = Exception("Page.goto: net::ERR_BLOCKED_BY_CLIENT at https://ads.example/\nCall log: ...")
    assert browser.connection_refused(error) == why.lstrip(": ")
    # A name the servers do not know may be one they block, while they are in use.
    unknown = Exception("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://gone.example/")
    assert browser.connection_refused(unknown) == ""
    monkeypatch.setattr(browser, "DNS", True)
    assert browser.connection_refused(unknown) == "the DNS servers do not know gone.example, or block it"
    assert "or block it" in browser.unresolved("https://gone.example/f.pdf", "[Errno -2] Name or service not known")
    assert browser.unresolved("https://gone.example/f.pdf", "Connection refused") == ""


def test_the_downloads_say_why(filtered):
    from harvester.sources import SourceError  # pylint: disable=import-outside-toplevel
    from harvester.sources.download import guarded_client  # pylint: disable=import-outside-toplevel

    with guarded_client() as client, pytest.raises(SourceError, match="blocked by the DNS server"):
        client.get("https://ads.example/file.pdf")


def test_the_guard_says_a_blocked_name_is_blocked(filtered):
    with pytest.raises(guard.Refused) as refused:
        asyncio.run(guard.look_up("ads.example"))
    assert refused.value.reason == "blocked by the DNS server (it answered 0.0.0.0, ::)"
