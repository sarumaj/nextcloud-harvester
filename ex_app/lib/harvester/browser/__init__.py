"""The browser: what the recorder and the browser source share.

A few Chromiums at a time (SLOTS: HARVESTER_BROWSERS, 3 by default, or the admin's setting):
each recording takes one while a person records, and each running job of the browser source
one, so people record side by side and while jobs run. The browser may open only http(s) addresses outside the local
and private networks: a file:// page would show the container's files and environment (the
app's secret among them), and the network rules of the External Apps keep it off the private
network in any case.
"""

import functools
import ipaddress
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse


class Slots:
    """A semaphore whose number can change while it is in use (the admin's setting): lowered,
    the browsers open now stay until they close, and no new one starts until fewer are open."""

    def __init__(self, limit: int):
        self._limit = max(1, limit)
        self._taken = 0
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

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Takes a slot, waiting for one (up to timeout) unless not blocking."""
        with self._changed:
            if blocking and not self._changed.wait_for(lambda: self._taken < self._limit, timeout):
                return False
            if self._taken >= self._limit:
                return False
            self._taken += 1
            return True

    def release(self) -> None:
        """Gives a slot back."""
        with self._changed:
            if self._taken == 0:
                raise ValueError("released more slots than taken")
            self._taken -= 1
            self._changed.notify()

    def free(self) -> int:
        """How many browsers may start now."""
        with self._changed:
            return max(0, self._limit - self._taken)


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
# needs them), e.g. a home connection: sites that refuse servers' addresses see the proxy's.
# Empty: none, and the jobs' option is not offered. HARVESTER_PROXY and HARVESTER_PROXY_NAME (what
# the job's form calls it) are the defaults; the admin's settings change both (settings.py).
PROXY = os.environ.get("HARVESTER_PROXY", "").strip()
PROXY_NAME = os.environ.get("HARVESTER_PROXY_NAME", "").strip() or "the proxy"
TIMEZONE = os.environ.get("HARVESTER_TIMEZONE", "Europe/Berlin")
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


def child_env() -> dict[str, str]:
    """The environment of a child process: the few variables it needs, no secret."""
    return {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}


def launch_options(http1: bool = False, home: bool = False) -> dict:
    """How Chromium starts (CHANNEL; HTTP/1.1 only for the sites that cut HTTP/2 connections
    of browsers they distrust; home: through PROXY, its credentials given apart as Chromium
    wants them)."""
    args = [*LAUNCH_ARGS, "--disable-blink-features=AutomationControlled", f"--lang={LOCALE}"]
    if http1:
        args.append("--disable-http2")
    options = {"args": args, "env": child_env()} | ({"channel": CHANNEL} if CHANNEL else {})
    if home and PROXY:
        address = urlparse(PROXY)
        server = address._replace(netloc=address.netloc.rpartition("@")[2]).geturl()
        options["proxy"] = {"server": server} | (
            {"username": unquote(address.username or ""), "password": unquote(address.password or "")}
            if address.username
            else {}
        )
    return options


def user_agent(version: str) -> str:
    """Chrome's user agent for the browser's version (reduced, as Chrome sends it): without
    the "HeadlessChrome" a headless browser would say."""
    major = version.split(".", 1)[0] or "0"
    return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def context_options(version: str, viewport: dict | None = None) -> dict:
    """A browser context's settings: the window's size, the user agent, language and time zone."""
    return {
        "viewport": viewport or VIEWPORT,
        "user_agent": user_agent(version),
        "locale": LOCALE,
        "timezone_id": TIMEZONE,
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


def allowed_host(host: str) -> bool:
    """Tells whether a source may connect to the host (IMAP, SFTP, ...): not a local or private
    name or address (the External Apps' network rules keep them off the private network too)."""
    host = host.strip().lower().strip("[]")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


@functools.lru_cache(maxsize=512)
def _resolves_public(host: str, _period: int) -> bool:
    """Whether every address the name resolves to is public (cached per five-minute period)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # a name that does not resolve fails by itself
    return all(ipaddress.ip_address(str(info[4][0]).split("%", maxsplit=1)[0]).is_global for info in infos)


def reachable(url: str) -> bool:
    """allowed_url, and the host's name resolves to public addresses only: a public name that
    points at a private address (or a redirect to one) is refused too, not only by the External
    Apps' network rules. (Between this lookup and the connection, the name could still change:
    those rules stay the guard against that.)"""
    if not allowed_url(url):
        return False
    host = (urlparse(url).hostname or "").strip("[]")
    try:
        ipaddress.ip_address(host)
        return True  # an address, already checked by allowed_url
    except ValueError:
        return _resolves_public(host.lower(), int(time.time() // 300))


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
