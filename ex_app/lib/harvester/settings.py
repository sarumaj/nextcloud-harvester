# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin's settings (Administration settings > Additional settings > Harvester): how many jobs
run at once, how many browsers may be open, the proxy the jobs may choose, and whether the
browsers go through their network guard; in a form of its own, the DNS servers the container's
lookups go to (dns.py) and its status. Their defaults come from the environment
(HARVESTER_RUNNERS, HARVESTER_BROWSERS, HARVESTER_PROXY, HARVESTER_PROXY_NAME, HARVESTER_GUARD,
HARVESTER_DNS, HARVESTER_DNS_FALLBACK, which the deployment may set); a value the admin chooses
wins (an emptied proxy: none), and the executor reads them each minute, so a change needs no
restart.
"""

import logging
import os
from urllib.parse import urlparse

from nc_py_api.ex_app.ui.settings import SettingsField, SettingsFieldType, SettingsForm

from . import dns

LOG = logging.getLogger("harvester.settings")
RUNNERS = "runners"
BROWSERS = "browsers"
PROXY = "proxy"
PROXY_NAME = "proxy_name"
GUARD = "guard"
DNS = "dns"
DNS_FALLBACK = "dns_fallback"
KEYS = (RUNNERS, BROWSERS, PROXY, PROXY_NAME, GUARD, DNS, DNS_FALLBACK)
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
_REFUSED: set[str] = set()
DEFAULT_PROXY = os.environ.get("HARVESTER_PROXY", "").strip()
DEFAULT_PROXY_NAME = os.environ.get("HARVESTER_PROXY_NAME", "").strip() or "the proxy"
# The guard's choices (a select's plain strings, as choices), and the words that switch it.
ON, OFF = "On", "Off"
ON_WORDS = ("1", "true", "on", "yes")
OFF_WORDS = ("0", "false", "off", "no")
# On unless the deployment says off (as browser.GUARD, which the setting changes).
DEFAULT_GUARD = os.environ.get("HARVESTER_GUARD", "").strip().lower() not in OFF_WORDS
# None (Docker's DNS) unless the deployment names servers; their fallback off unless it says on.
DEFAULT_DNS = os.environ.get("HARVESTER_DNS", "").strip()
DEFAULT_DNS_FALLBACK = os.environ.get("HARVESTER_DNS_FALLBACK", "").strip().lower() in ON_WORDS
# The choices: each runner is a thread, each browser some 300-600 MB of the server's memory.
MAX_RUNNERS = 8
MAX_BROWSERS = 8


def form(runners: int, browsers: int, address: str = "", name: str = "the proxy", guard: bool = True) -> SettingsForm:
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
                options=choices(MAX_RUNNERS),
                description="How many jobs run side by side (never one twice, one per site at a time).",
            ),
            SettingsField(
                id=BROWSERS,
                title="Browsers at once",
                type=SettingsFieldType.SELECT,
                default=str(browsers),
                options=choices(MAX_BROWSERS),
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
                    "A proxy that jobs may choose (Through the proxy): sites that refuse servers' addresses "
                    "see the proxy's. HTTP(S), with credentials if needed (http://user:password@host:port), or "
                    "SOCKS5 without a password (socks5://host:port; socks5h:// has the proxy look up the names). "
                    "Empty: none, and jobs that chose it go directly."
                ),
            ),
            SettingsField(
                id=PROXY_NAME,
                title="The proxy's name",
                type=SettingsFieldType.TEXT,
                default=name,
                placeholder="the proxy",
                # No <name>: Nextcloud's page sanitizes the text as HTML, which drops it as a tag.
                description='What the job\'s form calls it, as in "Through the proxy": e.g. "the home connection".',
            ),
            SettingsField(
                id=GUARD,
                title="Browser network guard",
                type=SettingsFieldType.SELECT,
                default=ON if guard else OFF,
                options=[ON, OFF],
                description=(
                    "On: every connection of the browsers goes through a guard that refuses local and "
                    "private addresses, also those the browser's own checks do not see (a web worker's "
                    "WebSockets) and names that change after their check (DNS rebinding). Off: the browsers "
                    "connect by themselves, a little faster, and only the network's firewall keeps those two "
                    "off the private network. Switch it off only when the External Apps' network is already "
                    "firewalled from private networks and the cloud's metadata service (169.254.169.254). "
                    "A change applies to browsers started afterwards."
                ),
            ),
        ],
    )


def dns_form(servers: str = "", fallback: bool = False, status: str = dns.UNSET) -> SettingsForm:
    """The DNS servers' form, its description the status (dns.STATUSES): registered again when
    the status changes."""
    return SettingsForm(
        id="harvester_dns",
        section_type="admin",
        section_id="additional",
        title="Harvester: DNS servers",
        description=status,
        fields=[
            SettingsField(
                id=DNS,
                title="DNS servers",
                type=SettingsFieldType.TEXT,
                default=servers,
                placeholder="tls://dns.example",
                description=(
                    "Every name the app, its browsers and its helper programs look up goes to these servers, "
                    "e.g. a filtering resolver: plain addresses (9.9.9.9, port 53), DNS over TLS "
                    "(tls://dns.example, port 853) or DNS over HTTPS (https://dns.example/dns-query), a "
                    "ClientID in the name or the path as the server wants it; several separated by commas. "
                    "Empty: Docker's DNS. A name the servers block (they answer 0.0.0.0) is refused, and the "
                    "job's log says so. Nextcloud's and HaRP's names are still looked up with Docker's DNS."
                ),
            ),
            SettingsField(
                id=DNS_FALLBACK,
                title="Docker's DNS when the servers fail",
                type=SettingsFieldType.SELECT,
                default=ON if fallback else OFF,
                options=[OFF, ON],
                description=(
                    "Off: while the DNS servers do not answer, names do not resolve and the jobs fail, so "
                    "that a filtering choice is never bypassed unseen. On: Docker's DNS answers meanwhile."
                ),
            ),
        ],
    )


def choices(highest: int) -> list[str]:
    """A select's options: Nextcloud's select takes plain strings (nc_py_api makes a dict into
    {name, value} objects, which only radio buttons and checkboxes show)."""
    return [str(n) for n in range(1, highest + 1)]


def number(value: str | None, default: int, highest: int) -> int:
    """A setting's value as a number from 1 to highest; the default if unset or not a number."""
    try:
        return min(max(1, int(str(value))), highest)
    except ValueError:
        return default


def switch(value: str | None, default: bool) -> bool:
    """An on/off setting's value: the default if unset or neither (On, off, 1, false, ...)."""
    word = (value or "").strip().lower()
    if word in ON_WORDS:
        return True
    if word in OFF_WORDS:
        return False
    return default


def proxy(value: str | None, default: str) -> str:
    """The proxy's address: the default if unset, "" if emptied or not one the app can use
    (the default is checked as well)."""
    value = (default if value is None else value).strip()
    if not value:
        return ""
    problem = proxy_problem(value)
    if problem:
        # Read each minute: said once per address (Nextcloud's form cannot show it).
        if value not in _REFUSED:
            _REFUSED.add(value)
            LOG.warning("the proxy %s: %s; none is used", shown(value), problem)
        return ""
    return value


def proxy_problem(value: str) -> str:
    """Why the app cannot use this proxy, or "" if it can: HTTP(S) or SOCKS5 (socks5h: the
    proxy looks up the names), a SOCKS5 one without a password, which Chromium cannot send."""
    address = urlparse(value)
    if address.scheme not in PROXY_SCHEMES or not address.hostname:
        return "not an http://, https://, socks5:// or socks5h:// address"
    if address.scheme.startswith("socks") and (address.username or address.password):
        return "a SOCKS5 proxy with a password, which the browser cannot send"
    return ""


def shown(address: str) -> str:
    """A proxy's address for the log, without its password."""
    parsed = urlparse(address)
    if not parsed.password:
        return address
    return parsed._replace(netloc=f"{parsed.username}:***@{parsed.netloc.rpartition('@')[2]}").geturl()
