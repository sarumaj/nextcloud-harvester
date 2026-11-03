# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin's DNS servers (settings.DNS, HARVESTER_DNS): every name the container looks up, by
the app, the guard, the browsers, yt-dlp, gallery-dl, rclone and the mail servers' connections,
goes to them, e.g. a filtering resolver such as AdGuard Home. Each is a plain address (DNS over
UDP and TCP, port 53 unless given), tls://host[:port] (DNS over TLS) or https://host[:port]/path
(DNS over HTTPS); a ClientID goes into the TLS name or the path as the server wants it.

AdGuard's dnsproxy listens on 127.0.0.1:53 and sends the lookups there; /etc/resolv.conf names
it alone, so every program in the container asks it (glibc and Go read the file again when it
changes, Chromium watches it). The servers' own names (of tls:// and https://) are looked up
with Docker's servers (the bootstrap), as are the names that must stay Docker's: Nextcloud's,
HaRP's and the proxy's, which a public resolver may not know. A server that does not answer
fails the lookups, unless the admin allows Docker's servers as a fallback: a filtering choice is
not bypassed unseen. dnsproxy runs as the app's user without its secrets (child_env); start.sh,
as root, keeps Docker's file as /etc/resolv.conf.docker and lets the app's user write
/etc/resolv.conf. Without servers set, the file stays Docker's and nothing runs.
"""

import ipaddress
import logging
import os
import re
import secrets
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

LOG = logging.getLogger("harvester.dns")
RESOLV = Path("/etc/resolv.conf")
# Docker's own file, kept by start.sh before the app changes /etc/resolv.conf.
DOCKERS = Path("/etc/resolv.conf.docker")
# The first line of the file the app writes: start.sh puts Docker's back when it finds it.
MARK = "# Harvester: its DNS proxy"
PROGRAM = "dnsproxy"
LISTEN = "127.0.0.1"
PORT = 53
# The name the status's lookup asks for (IANA's, never blocked).
TEST_NAME = "example.com"
MAX_SERVERS = 8
# How long dnsproxy has to listen after it starts (s), and how long the status's lookup waits
# for it: dnsproxy gives a server 10 s, then answers from the fallback.
START_TIMEOUT = 5.0
QUERY_TIMEOUT = 12.0
# dnsproxy's warnings (a server that does not answer: two lines a lookup) at most once a minute.
LOG_PAUSE = 60.0

# The status, the description of the admin's DNS form (settings.dns_form): whole sentences,
# each translated as it is.
UNSET = "No DNS servers are set: names are looked up with Docker's DNS, as the server's are."
WORKS = "In use: the DNS proxy runs, and a test lookup through the DNS servers worked."
FAILS = (
    "In use, but a test lookup through the DNS servers failed: names do not resolve, and the jobs fail "
    "(see the app's log)."
)
NOT_RUNNING = "Not in use: the DNS proxy does not start (see the app's log); names are looked up with Docker's DNS."
NOT_WRITABLE = (
    "Not in use: the container's /etc/resolv.conf cannot be changed (the container must start as root); "
    "names are looked up with Docker's DNS."
)
INVALID = (
    "Not in use: an entry is not a DNS server the app can use (see the app's log); names are looked up "
    "with Docker's DNS."
)
STATUSES = (UNSET, WORKS, FAILS, NOT_RUNNING, NOT_WRITABLE, INVALID)

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*")
_PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,200}")
# What an entry may hold at all: no spaces, quotes, ;, $ or anything a shell or dnsproxy's own
# syntax ([/domain/], a file's path) would make more of.
_CHARS = re.compile(r"[A-Za-z0-9.:/\[\]_~-]+")


def entries(value: str | None) -> list[str]:
    """The setting's entries: separated by commas, spaces or lines."""
    return [entry for entry in re.split(r"[\s,]+", value or "") if entry]


def problem(entry: str) -> str:
    """Why the entry is not a DNS server the app can use, or "" if it is: an address (with a
    port), tls://host[:port] or https://host[:port]/path, nothing else."""
    if len(entry) > 300 or not _CHARS.fullmatch(entry):
        return "not an address, tls:// or https:// address"
    if "://" not in entry:
        return "" if plain_server(entry) else "not an IP address (with a port)"
    address = urlparse(entry)
    try:
        port = address.port
    except ValueError:
        port = 0
    host = (address.hostname or "").lower()
    checks = (
        (
            address.scheme not in ("tls", "https") or bool(address.username or address.password or address.query),
            "only tls://host[:port] or https://host[:port]/path",
        ),
        (not _HOST.fullmatch(host) and not public_ip(host), "not a host name"),
        (port == 0, "not a port"),
        (address.scheme == "tls" and bool(address.path), "tls:// takes no path"),
        (
            address.scheme == "https" and not _PATH.fullmatch(address.path or ""),
            "https:// needs its path, e.g. /dns-query",
        ),
    )
    return next((why for wrong, why in checks if wrong), "")


def plain_server(entry: str) -> str:
    """The address of a plain DNS server's entry (1.2.3.4, 1.2.3.4:53, 2001:db8::1,
    [2001:db8::1]:53), "" if it is none or this container itself."""
    host, port = entry, "53"
    if entry.startswith("["):
        host, _, port = entry[1:].partition("]")
        port = port.removeprefix(":") or "53"
    elif entry.count(":") == 1:
        host, port = entry.split(":")
    if not port.isdigit() or not 0 < int(port) < 65536:
        return ""
    return host if public_ip(host) else ""


def public_ip(host: str) -> bool:
    """Whether the host is an IP address a DNS server may have here: not unspecified (0.0.0.0)
    nor loopback (the proxy itself)."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (address.is_unspecified or address.is_loopback)


def parsed(value: str | None) -> tuple[list[str], list[str]]:
    """The setting's servers, and what is wrong with it (then no server is used)."""
    found = list(dict.fromkeys(entries(value)))
    problems = [f"{entry}: {why}" for entry in found if (why := problem(entry))]
    if len(found) > MAX_SERVERS:
        problems.append(f"more than {MAX_SERVERS} servers")
    return ([] if problems else found), problems


def nameservers(text: str) -> list[str]:
    """The servers a resolv.conf names (an IPv6 address with a zone left out)."""
    found = []
    for line in text.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "nameserver" and "%" not in words[1]:
            try:
                found.append(str(ipaddress.ip_address(words[1])))
            except ValueError:
                continue
    return found


def search_domains(text: str) -> list[str]:
    """The domains a resolv.conf searches (search, domain)."""
    found = []
    for line in text.splitlines():
        words = line.split()
        if words and words[0] in ("search", "domain"):
            found += [word.lower().rstrip(".") for word in words[1:] if _HOST.fullmatch(word.lower().rstrip("."))]
    return list(dict.fromkeys(found))


def own_file(original: str) -> str:
    """The resolv.conf that sends every lookup to the proxy, with Docker's search domains and
    options (ndots, ...)."""
    kept = [line for line in original.splitlines() if line.split()[:1] in (["search"], ["domain"], ["options"])]
    return "\n".join([f"{MARK} (the admin's DNS servers); Docker's: {DOCKERS}", f"nameserver {LISTEN}", *kept]) + "\n"


def host_of(address: str) -> str:
    """The host name in an address (scheme://host:port/..., host:port), "" for an IP address or
    none."""
    host = (urlparse(address if "://" in address else f"//{address}").hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        return host if _HOST.fullmatch(host) else ""


def own_names(*addresses: str) -> list[str]:
    """The names that stay Docker's: Nextcloud's (NEXTCLOUD_URL), HaRP's (HP_FRP_ADDRESS) and
    those of the addresses given (the proxy's)."""
    found = (os.environ.get("NEXTCLOUD_URL", ""), os.environ.get("HP_FRP_ADDRESS", ""), *addresses)
    return [name for name in dict.fromkeys(map(host_of, found)) if name]


def command(servers: list[str], dockers: list[str], fallback: bool, own: list[str]) -> list[str]:
    """dnsproxy's command line, its arguments a list (no shell): the servers (checked by
    problem), Docker's servers to look their names up, for the names that stay Docker's (own,
    with Docker's search domains) and, if allowed, as the fallback."""
    line = [PROGRAM, f"--listen={LISTEN}", f"--port={PORT}", "--cache"]
    line += [f"--upstream={server}" for server in servers]
    line += [f"--bootstrap={server}" for server in dockers]
    names = "/".join(dict.fromkeys(name for name in own if _HOST.fullmatch(name)))
    if names:
        line += [f"--upstream=[/{names}/]{server}" for server in dockers]
    if fallback:
        line += [f"--fallback={server}" for server in dockers]
    return line


def query(name: str, server: str = LISTEN, port: int = PORT, timeout: float = QUERY_TIMEOUT) -> tuple[int, list[str]]:
    """Asks the server for the name's IPv4 addresses over UDP: the answer's code (0 found, 3 no
    such name) and its addresses; OSError (a timeout too) when nothing answers."""
    ident = secrets.randbelow(65536)
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\0"
    packet = struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0) + question + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, port))
        deadline = time.monotonic() + timeout
        while True:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            answer = sock.recv(4096)
            if len(answer) >= 12 and struct.unpack(">H", answer[:2])[0] == ident:
                return answer_of(answer)


def answer_of(answer: bytes) -> tuple[int, list[str]]:
    """A DNS answer's code and the IPv4 addresses in its answers."""
    _, flags, questions, count = struct.unpack(">HHHH", answer[:8])
    offset = 12
    for _ in range(questions):
        offset = skip_name(answer, offset) + 4
    addresses = []
    for _ in range(count):
        offset = skip_name(answer, offset)
        kind, _, _, length = struct.unpack(">HHIH", answer[offset : offset + 10])
        offset += 10
        if kind == 1 and length == 4:
            addresses.append(str(ipaddress.IPv4Address(answer[offset : offset + 4])))
        offset += length
    return flags & 0xF, addresses


def skip_name(answer: bytes, offset: int) -> int:
    """Where the name at offset ends (labels, or a pointer that ends it)."""
    while True:
        length = answer[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += length + 1


def blocked(addresses: list[str]) -> bool:
    """Whether an answer is a filter's refusal: only 0.0.0.0 or :: (AdGuard Home's, Pi-hole's)."""
    return bool(addresses) and all(ipaddress.ip_address(address).is_unspecified for address in addresses)


class Proxy:
    """dnsproxy and /etc/resolv.conf as the setting wants them (apply, each minute), and the
    status the admin's form shows."""

    def __init__(self, resolv: Path = RESOLV, dockers: Path = DOCKERS):
        self.resolv, self.dockers = resolv, dockers
        self.status = UNSET
        self.running: subprocess.Popen | None = None
        self.line: list[str] = []
        self._said: set[str] = set()

    @property
    def active(self) -> bool:
        """Whether the container's lookups go to the admin's servers."""
        return self.status in (WORKS, FAILS)

    def docker_file(self) -> str:
        """Docker's resolv.conf: the one start.sh kept, else the file as it is if it is not the
        app's ("" if unknown)."""
        for path in (self.dockers, self.resolv):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not text.startswith(MARK):
                return text
        return ""

    def apply(self, value: str | None, fallback: bool, own: list[str]) -> str:
        """Runs the proxy with the setting's servers and points the container at it, or stops it
        and gives the file back to Docker's servers; returns the status."""
        servers, problems = parsed(value)
        if problems:
            self.once("the DNS servers %s: %s; Docker's DNS is used", value, "; ".join(problems))
        original = self.docker_file()
        if servers and original and not os.access(self.resolv, os.W_OK):
            self.once("%s cannot be changed (not the app's to write); Docker's DNS is used", self.resolv)
            original = ""
        if not servers or not original:
            self.stop()
            self._restore(original)
            self.status = INVALID if problems else UNSET if not servers else NOT_WRITABLE
            return self.status
        dockers = nameservers(original)
        line = command(servers, dockers, fallback, [*own, *search_domains(original)])
        if line != self.line or self.running is None or self.running.poll() is not None:
            self.stop()
            self.start(line)
        if not self.listens():
            self.stop()
            self._restore(original)
            self.status = NOT_RUNNING
            return self.status
        if not self.write(own_file(original)):
            self.stop()
            self.status = NOT_WRITABLE
            return self.status
        try:
            code, addresses = query(TEST_NAME)
        except OSError:
            code, addresses = -1, []
        works = code == 0 and addresses and not blocked(addresses)
        if not works and self.status != FAILS:
            LOG.warning("the DNS servers did not resolve %s (code %s, %s)", TEST_NAME, code, addresses or "no address")
        self.status = WORKS if works else FAILS
        return self.status

    def start(self, line: list[str]) -> None:
        """Starts dnsproxy (its output into the app's log)."""
        # Imported here: the browser package imports this module's users.
        from .browser import child_env  # pylint: disable=import-outside-toplevel

        self.line = line
        LOG.info("the DNS proxy starts")
        try:
            self.running = subprocess.Popen(  # pylint: disable=consider-using-with
                line, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=child_env()
            )
        except OSError as error:
            self.once("the DNS proxy could not be started: %s", error)
            self.running = None
            return
        threading.Thread(target=self._log, args=(self.running,), name="dnsproxy-log", daemon=True).start()

    def listens(self) -> bool:
        """Whether the proxy runs and takes connections (whatever its servers do), waiting up to
        START_TIMEOUT for it."""
        deadline = time.monotonic() + START_TIMEOUT
        while self.running is not None and self.running.poll() is None:
            try:
                socket.create_connection((LISTEN, PORT), timeout=0.5).close()
                return True
            except OSError:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)
        self.once("the DNS proxy does not listen on %s:%s; Docker's DNS is used", LISTEN, PORT)
        return False

    def stop(self) -> None:
        """Ends the proxy, if it runs."""
        if self.running is not None:
            self.running.terminate()
            try:
                self.running.wait(5)
            except subprocess.TimeoutExpired:
                self.running.kill()
                self.running.wait()
        self.running, self.line = None, []

    def write(self, text: str) -> bool:
        """Writes the container's resolv.conf in place (a file Docker mounts, never replaced)."""
        try:
            if self.resolv.read_text(encoding="utf-8") != text:
                with self.resolv.open("w", encoding="utf-8") as file:
                    file.write(text)
                LOG.info("the container's names are looked up through the DNS proxy")
            return True
        except OSError as error:
            self.once("%s cannot be changed (%s); Docker's DNS is used", self.resolv, error)
            return False

    def _restore(self, original: str) -> None:
        """Gives the container's resolv.conf back to Docker's servers, if the app had changed it."""
        try:
            if original and self.resolv.read_text(encoding="utf-8").startswith(MARK):
                with self.resolv.open("w", encoding="utf-8") as file:
                    file.write(original)
                LOG.info("the container's names are looked up with Docker's DNS again")
        except OSError as error:
            self.once("%s cannot be changed back (%s)", self.resolv, error)

    def once(self, message: str, *values: object) -> None:
        """A warning, said once (apply runs each minute)."""
        key = message % values
        if key not in self._said:
            self._said.add(key)
            LOG.warning(message, *values)

    @staticmethod
    def _log(process: subprocess.Popen) -> None:
        """dnsproxy's lines in the app's log: its warnings and errors (its start's dozen lines
        only when debugging)."""
        assert process.stdout is not None
        said, quiet = -LOG_PAUSE, 0
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if " INFO " in line:
                LOG.debug("dnsproxy: %s", line)
            elif time.monotonic() - said < LOG_PAUSE:
                quiet += 1
            else:
                LOG.warning("dnsproxy: %s%s", line, f" ({quiet} more meanwhile)" if quiet else "")
                said, quiet = time.monotonic(), 0


PROXY = Proxy()
