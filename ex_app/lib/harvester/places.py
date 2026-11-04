# SPDX-License-Identifier: AGPL-3.0-or-later
"""The destinations a job saves into (its Save to), each a Destination: how its run's library
(runner.Library: find, put, tag, untag, existing) is made for a job, whether its files may carry
Nextcloud's tags or it keeps labels itself, and the person's secrets it keeps for a job (deleted
with the job, or when its place changes). What its runs switch off is destinations.RULES, its
form's fields destinations.OPTIONS. The runner knows only the Library: a new destination is one
more Destination here, with its Library, options and rules.

- files: the person's files in Nextcloud (nextcloud.py);
- library: Harvester's private library (library.py), which keeps the labels itself;
- inbox: a vault's inbox in the person's files (inbox.py);
- vault: a Cryptomator vault, written over WebDAV (vaultlib.py), its keys a secret;
- immich: the person's Immich account (immich.py), with its own tags; the account is the
  person's (their preferences), no job's secret.

Where the files carry no tags of their place's own, the tags a run gives are Harvester's labels, which the
job's records keep (store.placed): Labelled."""

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from nc_py_api import AsyncNextcloudApp

from . import destinations, immich, vaultlib
from .l10n import Said
from .inbox import InboxLibrary
from .library import PrivateLibrary, Shelf
from .nextcloud import NextcloudLibrary
from .runner import Library
from .sources import SOURCES, SourceError
from .store import Job

Call = Callable[[Awaitable[Any]], Any]
# Harvester's private library on the app's storage, set at start (main.lifespan).
SHELF: list[Shelf] = []


class Labelled(Library):
    """A library whose files get no Nextcloud tags: the tags a run gives are Harvester's labels,
    which the job's records keep (store.placed), nothing more."""

    def __init__(self, inner: Library):
        self.inner = inner

    def find(self, path: str) -> int | None:
        return self.inner.find(path)

    def put(self, path: str, content: BinaryIO) -> int:
        return self.inner.put(path, content)

    def tag(self, file_id: int, tags: list[str]) -> None:
        return None

    def untag(self, file_id: int, tags: list[str]) -> None:
        return None

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        return self.inner.existing(file_ids)

    def item(self, values: dict) -> None:
        self.inner.item(values)

    def found(self, file_id: int) -> bool:
        return self.inner.found(file_id)

    def saved(self, path: str, size: str) -> Said:
        return self.inner.saved(path, size)

    def there(self, path: str) -> Said:
        return self.inner.there(path)

    def known(self, path: str) -> Said:
        return self.inner.known(path)


def nextcloud_files(_job: Job, nc: AsyncNextcloudApp, call: Call) -> Library:
    """The person's files."""
    return NextcloudLibrary(nc, call)


def private_library(job: Job, _nc: AsyncNextcloudApp, _call: Call) -> Library:
    """The person's private library."""
    if not SHELF:
        raise SourceError("Harvester's private library is not set up on this server")
    return PrivateLibrary(SHELF[0], job.user, job.id)


def no_secrets(_job: Job) -> list[str]:
    """A destination that keeps no secret of the person's."""
    return []


@dataclasses.dataclass(frozen=True)
class Destination:
    """Where a job's files go: make gives its run's library (nc acting as the person, call
    running a request to its end); own_tags, whether its files may carry tags of its own
    (Nextcloud's, Immich's);
    labels, whether its library keeps Harvester's labels itself (its tag and untag); secrets, the
    person's preferences it keeps for a job."""

    name: str
    make: Callable[[Job, AsyncNextcloudApp, Call], Library]
    own_tags: bool = False
    labels: bool = False
    secrets: Callable[[Job], list[str]] = no_secrets

    def library(self, job: Job, nc: AsyncNextcloudApp, call: Call) -> Library:
        """The run's library: its tags Harvester's labels unless it keeps them itself, or may
        carry its own and the job tags so."""
        made = self.make(job, nc, call)
        tagging = destinations.tag_as(job.options) in destinations.OWN_TAGS.values() and self.own_tags
        return made if tagging or self.labels else Labelled(made)


DESTINATIONS = {
    destinations.FILES: Destination(destinations.FILES, nextcloud_files, own_tags=True),
    destinations.LIBRARY: Destination(destinations.LIBRARY, private_library, labels=True),
    destinations.INBOX: Destination(destinations.INBOX, InboxLibrary.of),
    destinations.DIRECT: Destination(
        destinations.DIRECT, vaultlib.opened, secrets=lambda job: [vaultlib.key_name(job)]
    ),
    destinations.IMMICH: Destination(destinations.IMMICH, immich.opened, own_tags=True),
}


def library_for(job: Job, nc: AsyncNextcloudApp, call: Call) -> Library:
    """The library the job's run saves into."""
    return DESTINATIONS[destinations.save_to(job.options)].library(job, nc, call)


def credential_key(job: Job, name: str) -> str:
    """The user preference that holds a job's credential (stored by Nextcloud, encrypted)."""
    return f"credential.job{job.id}.{name}"


def preference_keys(job: Job) -> list[str]:
    """The person's preferences of a job: its account, kept session and its destination's secret
    (places.DESTINATIONS: a vault's keys)."""
    return (
        [credential_key(job, name) for name in SOURCES[job.source].credentials]
        + [session_key(job)]
        + [key for destination in DESTINATIONS.values() for key in destination.secrets(job)]
    )


def session_key(job: Job) -> str:
    """The user preference that holds a job's kept browser session (cookies; encrypted)."""
    return f"session.job{job.id}"
