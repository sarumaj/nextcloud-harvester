"""The browser: what the recorder and the browser source share.

One Chromium at a time (LOCK): the recorder takes it while a person records, a job of the
browser source while it runs. The browser may open only http(s) addresses outside the local
and private networks: a file:// page would show the container's files and environment (the
app's secret among them), and the network rules of the External Apps keep it off the private
network in any case.
"""

import ipaddress
import threading
from pathlib import Path
from urllib.parse import urlparse

LOCK = threading.Lock()
SCRIPT = (Path(__file__).parent / "recorder.js").read_text(encoding="utf-8")
VIEWPORT = {"width": 1280, "height": 800}
LAUNCH_ARGS = ["--disable-dev-shm-usage"]


def allowed_url(url: str) -> bool:
    """Tells whether the browser may open the address: http(s), not a local or private host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


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
