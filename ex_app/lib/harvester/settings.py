"""The admin's settings (Administration settings > Additional settings > Harvester): how many jobs
run at once, how many browsers may be open, and the proxy the jobs may choose. Their defaults come
from the environment (HARVESTER_RUNNERS, HARVESTER_BROWSERS, HARVESTER_PROXY,
HARVESTER_PROXY_NAME, which the deployment may set); a value the admin chooses wins (an emptied
proxy: none), and the executor reads them each minute, so a change needs no restart.
"""

import logging
import os
from urllib.parse import urlparse

from nc_py_api.ex_app.ui.settings import SettingsField, SettingsFieldType, SettingsForm

LOG = logging.getLogger("harvester.settings")
RUNNERS = "runners"
BROWSERS = "browsers"
PROXY = "proxy"
PROXY_NAME = "proxy_name"
KEYS = (RUNNERS, BROWSERS, PROXY, PROXY_NAME)
DEFAULT_PROXY = os.environ.get("HARVESTER_PROXY", "").strip()
DEFAULT_PROXY_NAME = os.environ.get("HARVESTER_PROXY_NAME", "").strip() or "the proxy"
# The choices: each runner is a thread, each browser some 300-600 MB of the server's memory.
MAX_RUNNERS = 8
MAX_BROWSERS = 8


def form(runners: int, browsers: int, address: str = "", name: str = "the proxy") -> SettingsForm:
    """The admin's form, with the environment's values as its defaults."""
    return SettingsForm(
        id="harvester_admin",
        section_type="admin",
        section_id="additional",
        title="Harvester",
        description="Changes take effect within a minute; running jobs and open browsers finish first.",
        fields=[
            SettingsField(
                id=RUNNERS,
                title="Jobs at once",
                type=SettingsFieldType.SELECT,
                default=str(runners),
                options={str(n): str(n) for n in range(1, MAX_RUNNERS + 1)},
                description="How many jobs run side by side (never one twice, one per site at a time).",
            ),
            SettingsField(
                id=BROWSERS,
                title="Browsers at once",
                type=SettingsFieldType.SELECT,
                default=str(browsers),
                options={str(n): str(n) for n in range(1, MAX_BROWSERS + 1)},
                description=(
                    "How many browsers may be open: the website jobs and the recordings share them. "
                    "Each takes some 300-600 MB of the server's memory."
                ),
            ),
            SettingsField(
                id=PROXY,
                title="Proxy",
                type=SettingsFieldType.TEXT,
                default=address,
                placeholder="http://proxy.example:3128",
                description=(
                    "An HTTP proxy that jobs may choose (Through the proxy): sites that refuse servers' "
                    "addresses see the proxy's. With credentials: http://user:password@host:port. Empty: none, "
                    "and jobs that chose it go directly."
                ),
            ),
            SettingsField(
                id=PROXY_NAME,
                title="The proxy's name",
                type=SettingsFieldType.TEXT,
                default=name,
                placeholder="the proxy",
                description='What the job\'s form calls it: "Through <name>", e.g. "the home connection".',
            ),
        ],
    )


def number(value: str | None, default: int, highest: int) -> int:
    """A setting's value as a number from 1 to highest; the default if unset or not a number."""
    try:
        return min(max(1, int(str(value))), highest)
    except ValueError:
        return default


def proxy(value: str | None, default: str) -> str:
    """The proxy's address: the default if unset, "" if emptied or not an http(s) address."""
    if value is None:
        return default
    value = value.strip()
    if not value:
        return ""
    address = urlparse(value)
    if address.scheme not in ("http", "https") or not address.hostname:
        LOG.warning("the proxy %s is no http(s) address: none is used", shown(value))
        return ""
    return value


def shown(address: str) -> str:
    """A proxy's address for the log, without its password."""
    parsed = urlparse(address)
    if not parsed.password:
        return address
    return parsed._replace(netloc=f"{parsed.username}:***@{parsed.netloc.rpartition('@')[2]}").geturl()
