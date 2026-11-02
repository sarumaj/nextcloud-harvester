"""The browser: what the recorder and the browser source share.

A few Chromiums at a time (SLOTS: HARVESTER_BROWSERS, 3 by default, or the admin's setting):
each recording takes one while a person records, and each running job of the browser source
one, so people record side by side and while jobs run. The browser may open only http(s) addresses outside the local
and private networks: a file:// page would show the container's files and environment (the
app's secret among them), and the network rules of the External Apps keep it off the private
network in any case.
"""

import ctypes
import functools
import ipaddress
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse


class Slots:
    """A semaphore whose number can change while it is in use (the admin's setting): lowered,
    the browsers open now stay until they close, and no new one starts until fewer are open.
    Each slot is taken for a kind ("job": a running job's browser; "recording": the recorder,
    a session continued in the browser, rclone's sign-in), so the page can say what holds them
    without saying whose they are."""

    def __init__(self, limit: int):
        self._limit = max(1, limit)
        self._taken: dict[str, int] = {"job": 0, "recording": 0}
        # The threads waiting for a slot (a running job's): their job waits for a browser.
        self._waiting: set[int] = set()
        self._changed = threading.Condition()

    @property
    def limit(self) -> int:
        """How many browsers may be open at once."""
        return self._limit

    @limit.setter
    def limit(self, value: int) -> None:
        with self._changed:
            self._limit = max(1, value)
            self._changed.notify_all()

    def acquire(self, blocking: bool = True, timeout: float | None = None, kind: str = "recording") -> bool:
        """Takes a slot for the kind, waiting for one (up to timeout) unless not blocking."""
        with self._changed:
            if blocking:
                self._waiting.add(threading.get_ident())
                try:
                    if not self._changed.wait_for(lambda: sum(self._taken.values()) < self._limit, timeout):
                        return False
                finally:
                    self._waiting.discard(threading.get_ident())
            if sum(self._taken.values()) >= self._limit:
                return False
            self._taken[kind] = self._taken.get(kind, 0) + 1
            return True

    def release(self, kind: str = "recording") -> None:
        """Gives a slot of the kind back."""
        with self._changed:
            if self._taken.get(kind, 0) == 0:
                raise ValueError("released more slots than taken")
            self._taken[kind] -= 1
            self._changed.notify()

    def free(self) -> int:
        """How many browsers may start now."""
        with self._changed:
            return max(0, self._limit - sum(self._taken.values()))

    def taken(self) -> dict[str, int]:
        """How many browsers are open, by kind: {"job": 2, "recording": 1}."""
        with self._changed:
            return dict(self._taken)

    def waiting(self, thread: int) -> bool:
        """Whether the thread (a runner's) waits for a slot now."""
        with self._changed:
            return thread in self._waiting


# Each Chromium takes some 300-600 MB: the slots keep their number within what the host has.
BROWSERS = max(1, int(os.environ.get("HARVESTER_BROWSERS", "3")))
SLOTS = Slots(BROWSERS)


def free_browsers() -> int:
    """How many browsers may start now (SLOTS not taken)."""
    return SLOTS.free()


SCRIPT = (Path(__file__).parent / "recorder.js").read_text(encoding="utf-8")
VIEWPORT = {"width": 1280, "height": 800}
# The sizes a person may give the recorder's window (the page's own size, within these).
VIEWPORT_MIN = {"width": 640, "height": 480}
VIEWPORT_MAX = {"width": 2560, "height": 1600}
LAUNCH_ARGS = ["--disable-dev-shm-usage"]
# Like a person's browser, not an automated one: sites behind Cloudflare and other bot
# protection refuse a browser that says it is automated, or cut its connection
# (ERR_HTTP2_PROTOCOL_ERROR). The person still answers any challenge; this only keeps
# the browser from giving itself away. Full Chromium in its new headless mode (the
# headless shell differs from Chrome), without the automation flag, with Chrome's own
# user agent and one language and time zone for the recorder and the jobs alike.
CHANNEL = os.environ.get("HARVESTER_BROWSER_CHANNEL", "chromium")
LOCALE = os.environ.get("HARVESTER_LOCALE", "en-US")
# A proxy for the jobs that choose it (http:// or https://, credentials in the address if it
# needs them, or socks5:// and socks5h:// without), e.g. a home connection: sites that refuse
# servers' addresses see the proxy's. Empty: none, and the jobs' option is not offered.
# HARVESTER_PROXY and HARVESTER_PROXY_NAME (what the job's form calls it) are the defaults; the
# admin's settings change both (settings.py).
PROXY = os.environ.get("HARVESTER_PROXY", "").strip()
PROXY_NAME = os.environ.get("HARVESTER_PROXY_NAME", "").strip() or "the proxy"
TIMEZONE = os.environ.get("HARVESTER_TIMEZONE", "Europe/Berlin")
# Whether the browsers started from now on go through a guard of their own (guard.py): on unless
# HARVESTER_GUARD or the admin's setting (settings.py) says off. Off, the routes check what
# Playwright sees, and the External Apps' firewall alone keeps the rest (a web worker's
# WebSockets, a name that changes after its check) off the private network.
GUARD = os.environ.get("HARVESTER_GUARD", "").strip().lower() not in ("0", "false", "off", "no")
# What the browser and the helper programs (rclone, gallery-dl) may see of the app's
# environment: the app's AppAPI secret and HaRP's key stay out (a page that broke out of
# Chromium, which has no sandbox in a container, would find nothing to take).
CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "PLAYWRIGHT_BROWSERS_PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


# Where the app asks which address sites see (Cloudflare's own view: ip=, loc= lines).
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def seen_address(trace: str) -> dict[str, str]:
    """The address and country in a trace's answer ({} if it has none)."""
    fields = dict(line.split("=", 1) for line in trace.splitlines() if "=" in line)
    return {"ip": fields["ip"], "country": fields.get("loc", "")} if fields.get("ip") else {}


# prctl's option (linux/prctl.h).
PR_SET_DUMPABLE = 4


def child_env() -> dict[str, str]:
    """The environment of a child process: the few variables it needs, no secret."""
    return {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}


def undumpable() -> bool:
    """Makes this process undumpable (Linux's prctl PR_SET_DUMPABLE 0): its /proc files (environ,
    which holds the AppAPI secret and HaRP's key; its memory) become root's, out of reach of the
    helper programs and browsers, which run as the same user. Whether it is; elsewhere than on
    Linux, nothing and False. (A program the app starts is dumpable again: they get child_env.)"""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def driver_env(environment: dict[str, str]) -> dict[str, str]:
    """The environment of Playwright's driver (the node process that starts the browsers): the
    child's, and Playwright's own variables."""
    return {key: value for key, value in environment.items() if key in CHILD_ENV_KEYS or key.startswith("PW_")}


def quiet_driver(transport=None) -> None:
    """Playwright starts its driver with the app's whole environment (the AppAPI secret, HaRP's
    key), as the same user as the browsers: a page that broke out of Chromium could read it from
    /proc. The driver gets driver_env instead. Playwright's own function, replaced where its
    transport looks it up (tests check that it still does); Patchright's transport is given
    (engine.py)."""
    if transport is None:
        # pylint: disable-next=import-outside-toplevel
        from playwright._impl import _transport as transport  # type: ignore[import-not-found]

    original = transport.get_driver_env
    if getattr(original, "quiet", False):
        return

    def quiet() -> dict:
        return driver_env(original())

    quiet.quiet = True  # type: ignore[attr-defined]
    transport.get_driver_env = quiet


quiet_driver()


def http_address(url: str) -> str:
    """A WebSocket's address as the http(s) one its connection starts with (ws: http, wss:
    https), for allowed_url; another address as it is."""
    scheme, separator, rest = url.partition("://")
    return {"ws": "http", "wss": "https"}.get(scheme.lower(), scheme) + separator + rest


def launch_options(
    guard: int, http1: bool = False, home: bool = False, credentials: tuple[str, str] | None = None
) -> dict:
    """How Chromium starts (CHANNEL; HTTP/1.1 only for the sites that cut HTTP/2 connections
    of browsers they distrust), with every connection through its guard on 127.0.0.1 (the
    guard's port and its credentials for this browser; guard.Guard, which takes the home
    connection, PROXY, from there on). Without
    a guard (0: GUARD off), Chromium connects by itself, home through PROXY, its credentials
    given apart as Chromium wants them."""
    args = [*LAUNCH_ARGS, "--disable-blink-features=AutomationControlled", f"--lang={LOCALE}"]
    if http1:
        args.append("--disable-http2")
    options = {"args": args, "env": child_env()} | ({"channel": CHANNEL} if CHANNEL else {})
    if guard:
        # The guard for loopback too: Chromium would otherwise connect to 127.0.0.1 and localhost
        # by itself ("<-loopback>" removes that implicit exception). Playwright's own requests of
        # the context (context.request) take the same proxy.
        options["proxy"] = {"server": f"http://127.0.0.1:{guard}", "bypass": "<-loopback>"}
        if credentials:
            # Playwright answers the guard's 407 with them, for the pages and context.request.
            options["proxy"] |= {"username": credentials[0], "password": credentials[1]}
    elif home and PROXY:
        address = urlparse(PROXY)
        # Chromium knows socks5 only, and looks up the names at a SOCKS proxy anyway.
        scheme = "socks5" if address.scheme == "socks5h" else address.scheme
        server = address._replace(scheme=scheme, netloc=address.netloc.rpartition("@")[2]).geturl()
        options["proxy"] = {"server": server} | (
            {"username": unquote(address.username or ""), "password": unquote(address.password or "")}
            if address.username
            else {}
        )
    # WebRTC's UDP passes by any proxy: a page's peer connection would send to whatever address
    # its STUN or TURN servers have. Only what goes through the proxy (TCP, the guard's).
    args.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")
    return options


def user_agent(version: str) -> str:
    """Chrome's user agent for the browser's version (reduced, as Chrome sends it): without
    the "HeadlessChrome" a headless browser would say."""
    major = version.split(".", 1)[0] or "0"
    return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def context_options(version: str, viewport: dict | None = None) -> dict:
    """A browser context's settings: the window's size, the user agent, language and time zone;
    no service workers (their requests would pass by the context's routes, the guard)."""
    return {
        "viewport": viewport or VIEWPORT,
        "user_agent": user_agent(version),
        "locale": LOCALE,
        "timezone_id": TIMEZONE,
        "service_workers": "block",
    }


def fitted_viewport(width: object, height: object) -> dict:
    """The window size asked for, within VIEWPORT_MIN and VIEWPORT_MAX (whole pixels)."""
    size = {}
    for key, value in (("width", width), ("height", height)):
        try:
            number = int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            number = VIEWPORT[key]
        size[key] = max(VIEWPORT_MIN[key], min(VIEWPORT_MAX[key], number))
    return size


def connection_refused(error: Exception) -> str:
    """A clearer word for a page the site would not give the browser at all, else ""."""
    text = str(error)
    if "ERR_HTTP2_PROTOCOL_ERROR" in text or "ERR_CONNECTION_RESET" in text:
        return (
            "the site cut the browser's connection (its bot protection, most likely): try the job's option "
            "HTTP/1.1 only, or continue in the browser"
        )
    return ""


def local_name(host: str) -> bool:
    """Whether the name is a local one by itself (empty, localhost, *.localhost, *.local,
    *.internal), whatever it resolves to; host in lower case, without brackets."""
    return not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))


def allowed_host(host: str) -> bool:
    """Tells whether a source may connect to the host (IMAP, SFTP, ...): not a local or private
    name or address, nor a name that resolves to one (127.0.0.1.nip.io, say), for the helper
    programs and servers that connect by themselves too (the External Apps' network rules keep
    them off the private network as well). (Between this lookup and the connection, the name
    could still change: those rules stay the guard against that.)"""
    host = host.strip().lower().strip("[]")
    if local_name(host):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        return _resolves_public(host, int(time.time() // 300))
    except TimeoutError:
        return False  # no answer in time: not known to be public


# How long a name's lookup may take (s): the checks run as a job is saved, and the name of a
# server that does not answer in time is refused.
LOOKUP_TIMEOUT = 5.0


@functools.lru_cache(maxsize=512)
def _resolves_public(host: str, _period: int) -> bool:
    """Whether every address the name resolves to is public (cached per five-minute period; a
    lookup that takes longer than LOOKUP_TIMEOUT raises TimeoutError, and is not cached)."""
    found: list = []

    def look_up() -> None:
        try:
            found.append(socket.getaddrinfo(host, None))
        except OSError:
            found.append([])  # a name that does not resolve fails by itself

    # In a thread of its own: getaddrinfo has no timeout of its own.
    lookup = threading.Thread(target=look_up, name="lookup", daemon=True)
    lookup.start()
    lookup.join(LOOKUP_TIMEOUT)
    if not found:
        raise TimeoutError(f"{host} did not resolve within {LOOKUP_TIMEOUT:.0f} s")
    return all(ipaddress.ip_address(str(info[4][0]).split("%", maxsplit=1)[0]).is_global for info in found[0])


def reachable(url: str) -> bool:
    """allowed_url: the browser's and the downloads' check of every request (a redirect's too);
    allowed_host resolves the names."""
    return allowed_url(url)


def allowed_url(url: str) -> bool:
    """Tells whether the browser may open the address: http(s), not a local or private host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return allowed_host(parsed.hostname)


# What a page asking for a person looks like: the widgets of the common CAPTCHA services (their
# frames or containers) and the interstitial pages of bot protections. Detected, never answered.
CHALLENGE_SCRIPT = """() => {
  const found = (selector) => document.querySelector(selector)
  const frames = [...document.querySelectorAll('iframe')].map((frame) => frame.src || '')
  if (frames.some((src) => /recaptcha\\/(api2|enterprise)\\/(anchor|bframe)/.test(src)) || found('.g-recaptcha')) return 'reCAPTCHA'
  if (frames.some((src) => /hcaptcha\\.com/.test(src)) || found('.h-captcha')) return 'hCaptcha'
  if (frames.some((src) => /challenges\\.cloudflare\\.com/.test(src)) || found('.cf-turnstile, #challenge-form, #challenge-running')) return 'Cloudflare'
  if (frames.some((src) => /captcha-delivery\\.com|geo\\.captcha/.test(src))) return 'DataDome'
  const title = (document.title || '').toLowerCase()
  const text = (document.body ? document.body.innerText : '').slice(0, 2000).toLowerCase()
  if (/just a moment|attention required|access denied/.test(title) && /cloudflare|ray id/.test(text)) return 'Cloudflare'
  if (/verify (that )?you are (a )?human|are you a robot|ich bin kein roboter|confirm you are human/.test(text)) return 'a human check'
  return null
}"""
# The same signs in a page's HTML, for pages fetched without the browser.
CHALLENGE_MARKERS = {
    "reCAPTCHA": ("google.com/recaptcha/", 'class="g-recaptcha'),
    "hCaptcha": ("hcaptcha.com/1/api.js", 'class="h-captcha'),
    "Cloudflare": ("challenges.cloudflare.com", "cf-chl-", 'id="challenge-form"', 'class="cf-turnstile'),
}


def challenge_in_html(html: str) -> str | None:
    """The kind of challenge a page's HTML holds, or None."""
    for kind, markers in CHALLENGE_MARKERS.items():
        if any(marker in html for marker in markers):
            return kind
    return None
