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
- **Immich** (immich, immich.py): the person's Immich account (theirs, not the job's: its server
  and API key), photos and videos only, into an album the job may name.

Tag as: Nextcloud's tags (global: everyone sees their names), Immich's tags (the person's own, in
Immich), Harvester's labels (in the app's database: the job's file list and the library show
them) or none. A file in the library, a vault or an inbox carries no Nextcloud tag (labels or
none); one in Immich no Nextcloud tag either (Immich's tags, labels or none).

What a destination cannot do is switched off for it (effective), and the form says why (RULES):
an inbox's files leave it (the vault app moves them), so they are neither looked for again
(Download deleted files again) nor asked for changes; a vault replaces nothing, so a changed file
has nowhere to go (Check known files for changes). Immich replaces nothing either, and keeps no
archives: an unpacked ZIP file's photos and videos go there, the archive itself does not (Keep
the ZIP files).
"""

import dataclasses
import unicodedata

from .l10n import Said, said
from .sources.base import Option
from .store import Job

SAVE_TO, TAG_AS, VAULT, VAULT_TARGET, VERIFY = "save_to", "tag_as", "vault", "vault_target", "vault_verify"
ALBUM, LOCKED = "immich_album", "immich_locked"
FILES, LIBRARY, INBOX, DIRECT, IMMICH = "files", "library", "inbox", "vault", "immich"
# Tag as: Nextcloud's tags, Immich's (its value the place's name), Harvester's labels or none.
NEXTCLOUD, IMMICH_TAGS, LABELS, NONE = "nextcloud", "immich", "labels", "none"
# The tags of a place's own (Tag as NEXTCLOUD or IMMICH_TAGS where they are not: these).
OWN_TAGS = {FILES: NEXTCLOUD, IMMICH: IMMICH_TAGS}
# The options of the job's own runs these destinations switch off (the others' names, which
# runner.COMMON_OPTIONS and sources.base define: no import, they import this module).
AGAIN, RECHECK, UNPACK_KEEP = "again_deleted", "recheck", "unpack_keep"

OPTIONS = {
    SAVE_TO: Option(
        "Save to",
        FILES,
        "Where the job's files go: your files, Harvester's private library, a Cryptomator vault's inbox, a "
        "Cryptomator vault directly, or your Immich account (photos and videos). A change applies to the files of "
        "the next runs; those saved stay where they are.",
        choices=(FILES, LIBRARY, INBOX, DIRECT, IMMICH),
        group="save",
    ),
    TAG_AS: Option(
        "Tag as",
        NEXTCLOUD,
        "Nextcloud tags (everyone on this Nextcloud sees the tags' names in their lists of tags), Immich's tags "
        "(for Immich: yours, in Immich; a/b is the tag b inside a), Harvester's labels (only in Harvester: the "
        "job's files and the library) or none.",
        choices=(NEXTCLOUD, IMMICH_TAGS, LABELS, NONE),
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
    ALBUM: Option(
        "Album",
        "",
        "For Immich: the album the job's photos and videos go into, made if there is none of its name; the "
        "file's values may make it, e.g. Magazines {year}. Empty: none.",
        placeholder="Harvester {year}",
        group="save",
    ),
    LOCKED: Option(
        "Into the locked folder",
        0,
        "For Immich: the photos and videos go into your locked folder, which Immich shows only after your PIN. "
        "Immich lets no app read them there: no album for them, Immich's tags given before they go in (no "
        "change of them later), and Harvester tells them apart by their content.",
        kind="flag",
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
WHERE = (SAVE_TO, VAULT, VAULT_TARGET, VERIFY, ALBUM, LOCKED)

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
    IMMICH: {
        RECHECK: said("Off for Immich: it replaces no photo or video, so a changed file would have nowhere to go."),
        UNPACK_KEEP: said(
            "Off for Immich: it keeps photos and videos, not archives. The ZIP file's photos and videos go there."
        ),
    },
}


def save_to(options: dict) -> str:
    """The job's destination (FILES for a job saved before there was a choice)."""
    value = str(options.get(SAVE_TO) or FILES)
    return value if value in RULES else FILES


def tag_as(options: dict) -> str:
    """How the job tags its files: as chosen; the place's own tags (OWN_TAGS: NEXTCLOUD in the
    person's files, IMMICH_TAGS in Immich) for a job that did not choose, or chose another
    place's; LABELS where there are none of its own."""
    value = str(options.get(TAG_AS) or "")
    where = save_to(options)
    if value in (LABELS, NONE):
        return value
    return OWN_TAGS.get(where, LABELS)


def locked(options: dict) -> bool:
    """Whether the job's photos and videos go into Immich's locked folder."""
    try:
        return save_to(options) == IMMICH and bool(int(options.get(LOCKED) or 0))
    except (TypeError, ValueError):
        return False


def place_of(options: dict) -> tuple[str, str]:
    """Where the job's files are: its destination, and the vault's folder for a vault's."""
    where = save_to(options)
    return where, folder(str(options.get(VAULT) or "")) if where in (INBOX, DIRECT) else ""


def described(options: dict, target: str) -> Said:
    """Where the job's files go, in words for its log: the folder (target) and its place."""
    where, vault = place_of(options)
    path = folder(target)
    if where == LIBRARY:
        return said("{folder} in your library", folder=path)
    if where == INBOX:
        return said("the inbox {folder} of the vault {vault}", folder=path, vault=vault)
    if where == DIRECT:
        return said("{folder} in the vault {vault}", folder=path, vault=vault)
    if where == IMMICH:
        return said("your Immich account")
    return said("{folder} in your files", folder=path)


def elsewhere(before: Job, after: Job) -> bool:
    """Whether the job's files go elsewhere now: another place (Save to, vault) or folder."""
    return place_of(after.options) != place_of(before.options) or folder(after.target) != folder(before.target)


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
    (nor in it, nor holds it), no Nextcloud tags outside the person's files, nor Immich's outside
    Immich."""
    where = save_to(options)
    vault = folder(str(options.get(VAULT) or ""))
    inbox = folder(target)
    vaulted = where in (INBOX, DIRECT)
    checks = [
        (
            str(options.get(SAVE_TO) or FILES) not in RULES,
            said("Save to must be one of files, library, inbox, vault, immich."),
        ),
        (
            options.get(TAG_AS) == NEXTCLOUD and where != FILES,
            said("Tag as: Nextcloud tags only for files saved in your files (labels or none elsewhere)."),
        ),
        (
            options.get(TAG_AS) == IMMICH_TAGS and where != IMMICH,
            said("Tag as: Immich's tags only for files saved in Immich."),
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
