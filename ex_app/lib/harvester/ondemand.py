# SPDX-License-Identifier: AGPL-3.0-or-later
"""On-demand jobs: no schedule, they run when their person sends them an address (a page, a video,
a feed), which takes the place of the job's own for that run alone; its options stay as they are.
Only sources with an address (Source.address_option) offer them."""

from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urlparse

from .sources import SOURCES, SourceError
from .store import Job, Store

# The longest address a fetch takes (browsers' own limit is about this).
MAX_URL = 2000
# An address that passes every source's check without a lookup (a public IP address), standing in
# for the address an on-demand job without one of its own gets with each fetch, so that its
# other options are checked as it is saved.
STAND_IN = "https://1.1.1.1/"


def offered(source: str) -> bool:
    """Whether jobs of the source may be on demand: it has an address a fetch replaces."""
    return source in SOURCES and bool(SOURCES[source].address_option)


def on_demand(job: Job) -> bool:
    """Whether the job runs only when asked (no schedule)."""
    return not job.schedule


def for_fetch(job: Job, url: str) -> Job:
    """The job as a fetch of the address runs it: the address in place of its own."""
    return replace(job, options=SOURCES[job.source].with_address(job.options, url))


def checked_options(source: str, options: dict) -> dict:
    """The options to check an on-demand job's by: without an address of its own, with STAND_IN's
    (each fetch brings one)."""
    if SOURCES[source].address_of(options):
        return options
    return SOURCES[source].with_address(options, STAND_IN)


def same_site(url: str, other: str) -> bool:
    """Whether two addresses are of one site: the same host, but for www., or one a subdomain of
    the other (m.example.org and example.org)."""
    first, second = (str(urlparse(value).hostname or "").lower().removeprefix("www.") for value in (url, other))
    return bool(first and second) and (first == second or first.endswith(f".{second}") or second.endswith(f".{first}"))


def fetch_problem(job: Job, url: str, account: bool) -> str:
    """Why the job may not fetch the address ("" if it may): it is no on-demand job, or the source
    refuses the address (its own checks: http(s), not a local host, ...), or the job has an
    account and an address of its own on another site: the account (a user name and password
    yt-dlp or gallery-dl sign in with, HTTP Basic) is for that site, not for any a page sends."""
    if not on_demand(job) or not offered(job.source):
        return "The job is not on demand: only an on-demand job fetches an address sent to it."
    if len(url) > MAX_URL or not url.startswith(("http://", "https://")):
        return "Not a web address (http or https)."
    try:
        SOURCES[job.source](for_fetch(job, url).options, {}, None).close()
    except SourceError as error:
        return f"The address may not be fetched: {error}"
    own = SOURCES[job.source].address_of(job.options)
    if account and own and not same_site(url, own):
        host = urlparse(own).hostname
        return f"The job's account is for {host}: it fetches only addresses of that site."
    return ""


def hand_over(  # pylint: disable=too-many-arguments
    store: Store,
    job: Job,
    target_id: int,
    url: str,
    account: Callable[[Job], bool],
    queue: Callable[[int, str], object],
) -> str:
    """Queues the address for another job of the job's person, as Send to Harvester does (the mail
    source's pages, whose sign-in that job's recipe knows): only an on-demand website job of the
    same person, the address checked as fetch_problem checks a sent one (account: whether the
    target's account is set). Returns why it was not queued, "" once it is."""
    target = store.job(target_id)
    if target is None or target.user != job.user or target.source != "site" or not on_demand(target):
        return "the job for pages is no on-demand website job of yours (deleted?)"
    problem = fetch_problem(target, url, account(target))
    if problem:
        return f"{target.name}: {problem}"
    queue(target_id, url)
    return ""
