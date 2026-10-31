"""The browser: what the recorder and the browser source share.

One Chromium at a time (LOCK): the recorder takes it while a person records, a job of the
browser source while it runs. The browser may open only http(s) addresses outside the local
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
from urllib.parse import urlparse

LOCK = threading.Lock()
SCRIPT = (Path(__file__).parent / "recorder.js").read_text(encoding="utf-8")
VIEWPORT = {"width": 1280, "height": 800}
LAUNCH_ARGS = ["--disable-dev-shm-usage"]
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
