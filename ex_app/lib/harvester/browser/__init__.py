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
