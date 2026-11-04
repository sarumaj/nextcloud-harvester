# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a job saves its files (Save to) and how it tags them (Tag as): job options, every job's.

- **Your files** (files, the default and what every job saved before did): the job's folder in
  the person's files, tagged with Nextcloud's collaborative tags.
- **Harvester's private library** (library, library.py): in the app's own storage, per person,
  not in Nextcloud at all: Files, Photos, search, WebDAV and the sync clients never see them.
- **A Cryptomator vault's inbox** (inbox, inbox.py): the folder is the inbox of a vault (the
  sister app Vaults for Cryptomator's contract), which moves the files into the vault when its
  person unlocks it there.
- **A Cryptomator vault** (vault, vaultlib.py): each file encrypted on the server into the vault,
  with its keys kept for the job (never its password).

Tag as: Nextcloud's tags (global: everyone sees their names), Harvester's labels (in the app's
database: the job's file list and the library show them) or none. A file in the library, a vault
or an inbox carries no Nextcloud tag (labels or none).

What a destination cannot do is switched off for it (effective), and the form says why (RULES):
an inbox's files leave it (the vault app moves them), so they are neither looked for again
(Download deleted files again) nor asked for changes; a vault replaces nothing, so a changed file
has nowhere to go (Check known files for changes).
"""

import dataclasses
import unicodedata

from .l10n import said
from .sources.base import Option
from .store import Job

SAVE_TO, TAG_AS, VAULT, VAULT_TARGET, VERIFY = "save_to", "tag_as", "vault", "vault_target", "vault_verify"
FILES, LIBRARY, INBOX, DIRECT = "files", "library", "inbox", "vault"
NEXTCLOUD, LABELS, NONE = "nextcloud", "labels", "none"
# The options of the job's own runs these destinations switch off (the others' names, which
# runner.COMMON_OPTIONS and sources.base define: no import, they import this module).
AGAIN, RECHECK = "again_deleted", "recheck"

OPTIONS = {
    SAVE_TO: Option(
        "Save to",
        FILES,
        "Where the job's files go: your files, Harvester's private library, a Cryptomator vault's inbox, or a "
        "Cryptomator vault directly. A change applies to the files of the next runs; those saved stay where they are.",
        choices=(FILES, LIBRARY, INBOX, DIRECT),
        group="save",
    ),
    TAG_AS: Option(
        "Tag as",
        NEXTCLOUD,
        "Nextcloud tags (everyone on this Nextcloud sees the tags' names in their lists of tags), Harvester's "
        "labels (only in Harvester: the job's files and the library) or none.",
        choices=(NEXTCLOUD, LABELS, NONE),
        group="save",
    ),
    VAULT: Option(
        "Vault",
        "",
        "The vault's folder in your files (the folder holding vault.cryptomator), e.g. /Vaults/Papers.",
        placeholder="/Vaults/Papers",
        group="save",
    ),
    VAULT_TARGET: Option(
        "Folder in the vault",
        "/",
        "Where in the vault the inbox's files go (/ is the vault's top). Its name is stored unencrypted in the "
        "inbox's marker.",
        group="save",
    ),
    VERIFY: Option(
        "Read each file back",
        1,
        "Each file written into the vault is read back and compared before the job counts it as saved.",
        kind="flag",
        group="save",
    ),
}
# The options that say where the files go: as the folder, never in a recipe file.
WHERE = (SAVE_TO, VAULT, VAULT_TARGET, VERIFY)

# What each destination switches off, and why (the form shows the reason beside the option).
RULES: dict[str, dict[str, str]] = {
    FILES: {},
    LIBRARY: {},
    INBOX: {
        AGAIN: said(
            "Off for an inbox: the vault app moves its files away, and a file moved into the vault is not "
            "downloaded again."
        ),
        RECHECK: said("Off for an inbox: its files are moved into the vault, where the job cannot save over them."),
    },
    DIRECT: {
        RECHECK: said("Off for a vault: nothing in it is replaced, so a changed file would have nowhere to go."),
    },
}


def save_to(options: dict) -> str:
    """The job's destination (FILES for a job saved before there was a choice)."""
    value = str(options.get(SAVE_TO) or FILES)
    return value if value in RULES else FILES


def tag_as(options: dict) -> str:
    """How the job tags its files: as chosen, NEXTCLOUD for a job in the person's files that
    did not choose; LABELS for one elsewhere (no Nextcloud tags there)."""
    value = str(options.get(TAG_AS) or "")
    where = save_to(options)
    if value not in (NEXTCLOUD, LABELS, NONE):
        value = NEXTCLOUD if where == FILES else LABELS
    return LABELS if value == NEXTCLOUD and where != FILES else value


def place_of(options: dict) -> tuple[str, str]:
    """Where the job's files are: its destination, and the vault's folder for a vault's."""
    where = save_to(options)
    return where, folder(str(options.get(VAULT) or "")) if where in (INBOX, DIRECT) else ""


def effective(job: Job) -> Job:
    """The job as its runs see it: the options its destination switches off off, and no tags
    where it tags nothing."""
    off = {name: 0 for name in RULES[save_to(job.options)]}
    tags = [] if tag_as(job.options) == NONE else job.tags
    if not off and tags is job.tags:
        return job
    return dataclasses.replace(job, options=job.options | off, tags=tags)


def folder(path: str) -> str:
    """A folder's path as a vault's marker writes it: from the top, "/" between its parts, in
    Unicode NFC, without a "/" at its end."""
    return "/" + "/".join(part for part in unicodedata.normalize("NFC", path).split("/") if part)


def bad_path(path: str) -> bool:
    """Whether a folder's path has "." or ".." among its parts, a control character or a
    backslash."""
    return any(
        part in (".", "..") or any(ord(char) < 32 or char in "\\\x7f" for char in part) for part in path.split("/")
    )


def problem(options: dict, target: str) -> str:
    """What is wrong with the job's Save to and Tag as ("" if nothing), in words the form shows
    (translated: said()): a vault's folder for an inbox or a vault, an inbox that is not the vault
    (nor in it, nor holds it), no Nextcloud tags outside the person's files."""
    where = save_to(options)
    vault = folder(str(options.get(VAULT) or ""))
    inbox = folder(target)
    vaulted = where in (INBOX, DIRECT)
    checks = [
        (str(options.get(SAVE_TO) or FILES) not in RULES, said("Save to must be one of files, library, inbox, vault.")),
        (
            options.get(TAG_AS) == NEXTCLOUD and where != FILES,
            said("Tag as: Nextcloud tags only for files saved in your files (labels or none elsewhere)."),
        ),
        (bad_path(target), said("Folder: no . or .. as a folder's name, no control characters or backslashes.")),
        (vaulted and vault == "/", said("Vault: choose the vault's folder in your files.")),
        (
            vaulted and bad_path(vault),
            said("Vault: no . or .. as a folder's name, no control characters or backslashes."),
        ),
        (
            where == INBOX
            and (inbox in ("/", vault) or inbox.startswith(vault + "/") or vault.startswith(inbox + "/")),
            said("Folder: the inbox is a folder of its own, neither the vault's folder nor in it nor above it."),
        ),
        (
            where == INBOX and bad_path(str(options.get(VAULT_TARGET) or "/")),
            said("Folder in the vault: no . or .. as a folder's name, no control characters or backslashes."),
        ),
    ]
    return next((text for wrong, text in checks if wrong), "")
