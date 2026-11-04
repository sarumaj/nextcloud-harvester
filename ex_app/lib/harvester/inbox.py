# SPDX-License-Identifier: AGPL-3.0-or-later
"""A job whose folder is a Cryptomator vault's inbox (Save to: a vault's inbox): the sister app
Vaults for Cryptomator moves the inbox's files into the vault, encrypted in the person's
browser, when they unlock it there, and deletes the plain copies (its docs/inbox.md, version 1
of the marker's contract).

Harvester writes the marker, `.cryptomator-inbox.json`, into the folder before its first file:
{"version": 1, "vault": the vault's folder, "target": the folder in the vault, "from": "Harvester
job "<name>""} (and the job's ID, a key of its own). It writes it only into a folder of the
person's own (not one shared with them), and never over one that is there: a marker of the same
vault is left as it is (the person may have changed its target in the vault app), one of
another vault (or one it cannot read) ends the run with why. Nothing else is written there but
the job's files, each in one piece (one PUT, or a chunked upload that appears at its MOVE:
nextcloud.NextcloudLibrary), never a file named as the marker at the inbox's top.

The files leave the inbox when they are moved into the vault: the job never fetches them again
(destinations.RULES: Download deleted files again and the checks of changes are off), and they
carry no Nextcloud tags (labels or none: places.Labelled)."""

import json
import posixpath

from nc_py_api import NextcloudException

from . import destinations
from .l10n import said
from .nextcloud import Call, NextcloudLibrary
from .sources import SourceError
from .store import Job

MARKER = ".cryptomator-inbox.json"
# The largest marker the vault app reads.
MAX_MARKER = 64 * 1024


def marker(job: Job) -> dict:
    """The marker of the job's inbox."""
    return {
        "version": 1,
        "vault": destinations.folder(str(job.options.get(destinations.VAULT) or "")),
        "target": destinations.folder(str(job.options.get(destinations.VAULT_TARGET) or "/")),
        "from": f'Harvester job "{job.name}"',
        "harvester_job": job.id,
    }


class InboxLibrary(NextcloudLibrary):
    """The person's files, the job's folder an inbox of a vault: marked before the first file."""

    def __init__(self, nc, call: Call, job: Job):
        super().__init__(nc, call)
        self.job = job
        self.inbox = destinations.folder(job.target)
        self._marked = False

    @classmethod
    def of(cls, job: Job, nc, call: Call) -> "InboxLibrary":
        """The job's inbox (places.Destination's make)."""
        return cls(nc, call, job)

    def put(self, path, content):
        if posixpath.normpath(path) == posixpath.join(self.inbox, MARKER):
            raise SourceError(said("{path} is the inbox's marker: a file of that name is not saved", path=path))
        if not self._marked:
            self.mark()
        return super().put(path, content)

    def mark(self) -> None:
        """Writes the inbox's marker unless there is one of the same vault (SourceError for one
        of another vault, one it cannot read, a folder shared with the person)."""
        self._make(self.inbox)
        node = self.call(self.nc.files.by_path(self.inbox))
        if node is not None and node.is_shared:
            raise SourceError(said("{folder} is shared with you: an inbox is a folder of your own", folder=self.inbox))
        place = posixpath.join(self.inbox, MARKER)
        try:
            found = self.call(self.nc.files.download(place))
        except NextcloudException as error:
            if error.status_code != 404:
                raise
            text = json.dumps(marker(self.job), ensure_ascii=False, indent=2) + "\n"
            self.call(self.nc.files.upload(place, text.encode()))
            self._marked = True
            return
        try:
            there = json.loads(found[:MAX_MARKER]) if len(found) <= MAX_MARKER else None
            vault = destinations.folder(str(there["vault"]))
        except (ValueError, TypeError, KeyError) as error:
            raise SourceError(
                said("{folder} has an inbox marker Harvester cannot read: choose another folder", folder=self.inbox)
            ) from error
        if vault != marker(self.job)["vault"]:
            raise SourceError(
                said(
                    "{folder} is the inbox of another vault ({vault}): choose another folder",
                    folder=self.inbox,
                    vault=vault,
                )
            )
        self._marked = True
