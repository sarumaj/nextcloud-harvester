# SPDX-License-Identifier: AGPL-3.0-or-later
"""A job that saves straight into a Cryptomator vault (Save to: a vault): each file encrypted on
the server (vault.py) as it is read, and written into the vault's folder of the person's files
over WebDAV (davstorage.py).

The person enters the vault's password once, in the job's form: the server unlocks the vault
with it (routes/vault.py) and keeps only the vault's two keys (64 bytes, vault.Vault.key_material)
as a sensitive preference of the person's, as a job's account is kept (encrypted by Nextcloud),
never the password. Whoever takes over the server can read the whole vault with them: the form
says so, and suggests a vault of the job's own.

Nothing in a vault is replaced (vault.py): a name taken by another file of the vault gives the
new one a numbered name ("Report (2).pdf"), so a job of a vault does not check its files for
changes (destinations.RULES). A file is known to its job by the Nextcloud ID of its encrypted
entry, which a move or rename in the vault keeps: Download deleted files again works as in
Files. A vault whose keys are not kept, or no longer open it, fails the run as a refused account
does (AuthError), and the form asks for its password again."""

import base64
import posixpath
from typing import BinaryIO

from . import destinations
from .davstorage import WebDavStorage
from .l10n import said
from .nextcloud import Call
from .runner import Library
from .sources import AuthError, SourceError
from .store import Job
from .vault import CorruptVault, NameTaken, NotInVault, UnsupportedVault, Vault, VaultError


class VaultLocked(AuthError):
    """The job has no keys of its vault, or they do not open it: its password is needed."""


def key_name(job: Job) -> str:
    """The person's preference that holds the job's vault's keys (sensitive)."""
    return f"vaultkey.job{job.id}"


def encoded(key_material: bytes) -> str:
    """The keys as the preference keeps them."""
    return base64.b64encode(key_material).decode("ascii")


def decoded(text: str | None) -> bytes | None:
    """The keys of the preference, or None (none kept, or not 64 bytes)."""
    try:
        key = base64.b64decode(text or "", validate=True)
    except ValueError:
        return None
    return key if len(key) == 64 else None


class VaultLibrary(Library):
    """A job's files in a vault: storage is the vault's folder (WebDavStorage, or a test's with
    file_id and existing), verify reads each file back."""

    def __init__(self, vault: Vault, storage, verify: bool = True):
        self.vault = vault
        self.storage = storage
        self.verify = verify

    def find(self, path: str) -> int | None:
        try:
            node = self.vault.find(path)
        except (NotInVault, VaultError):
            return None
        return self.storage.file_id(node.path) if node is not None else None

    def put(self, path: str, content: BinaryIO) -> int:
        content.seek(0)
        try:
            try:
                written = self.vault.write_file(path, content)
            except NameTaken:
                folder, name = posixpath.split(path)
                content.seek(0)
                written = self.vault.write_file(posixpath.join(folder, self.vault.free_name(folder, name)), content)
            if self.verify:
                self.vault.verify_file(written)
        except VaultError as error:
            raise SourceError(
                said("{path} could not be written into the vault: {error}", path=path, error=str(error))
            ) from error
        file_id = self.storage.file_id(written.node.path)
        if file_id is None:
            raise SourceError(said("{path} is not in the vault after its upload", path=path))
        return file_id

    def tag(self, file_id: int, tags: list[str]) -> None:
        return None

    def untag(self, file_id: int, tags: list[str]) -> None:
        return None

    def existing(self, file_ids: list[int]) -> dict[int, str]:
        return self.storage.existing(file_ids)


def opened(job: Job, nc, call: Call) -> VaultLibrary:
    """The job's vault, opened with the keys kept for it (VaultLocked without them, or when they
    do not open it)."""
    folder = destinations.folder(str(job.options.get(destinations.VAULT) or ""))
    key = decoded(call(nc.preferences_ex.get_value(key_name(job))))
    if key is None:
        raise VaultLocked(said("The vault's password is needed: enter it in the job's form"))
    storage = WebDavStorage(nc, call, folder)
    try:
        vault = Vault.open(storage, key)
    except CorruptVault as error:
        raise VaultLocked(
            said(
                "The vault's kept keys do not open {vault} (another vault there?): enter its password in the form",
                vault=folder,
            )
        ) from error
    except UnsupportedVault as error:
        raise SourceError(
            said("No vault Harvester can write to at {vault}: {error}", vault=folder, error=str(error))
        ) from error
    return VaultLibrary(vault, storage, verify=bool(int(job.options.get(destinations.VERIFY, 1) or 0)))
