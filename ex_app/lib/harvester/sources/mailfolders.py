# SPDX-License-Identifier: AGPL-3.0-or-later
"""A mailbox's folders, for a mail job whose folder the mailbox does not have: those LIST gives
(their names decoded from IMAP's modified UTF-7), the closest to the job's first, and the error
that names them (MissingFolder), which Try the mailbox offers to choose from."""

import base64
import difflib
import re

from .base import SourceError


def from_utf7_imap(name: str) -> str:
    """A folder name from IMAP's modified UTF-7 (mail.utf7_imap's reverse): Entw&APw-rfe as Entwürfe."""

    def decoded(match: re.Match) -> str:
        if not match[1]:
            return "&"
        encoded = match[1].replace(",", "/")
        try:
            return base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-16-be")
        except ValueError:
            return match[0]

    return re.sub(r"&([A-Za-z0-9+,]*)-", decoded, name)


# A line of LIST's answer: its flags, the hierarchy's delimiter (NIL: none) and the name, quoted
# or an atom.
LISTED = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|"(?:[^"\\]|\\.)*")\s+(?P<name>.*)', re.IGNORECASE)
# The folders a missing folder's error names, the closest to the one asked for first.
FOLDERS_SAID = 10


def listed_folders(data: list) -> tuple[list[str], str]:
    """The folders of a LIST's answer that can be selected, in the server's order, and its
    hierarchy's delimiter ("" when it gives none). A name may come as a literal (a tuple)."""
    folders, delimiter = [], ""
    for part in data or []:
        if isinstance(part, tuple) and len(part) > 1:
            line = (
                part[0].decode(errors="replace").rsplit("{", 1)[0]
                + '"'
                + part[1].decode(errors="replace").replace('"', '\\"')
                + '"'
            )
        elif isinstance(part, bytes):
            line = part.decode(errors="replace")
        else:
            continue
        match = LISTED.match(line.strip())
        if not match or "\\noselect" in match["flags"].lower():
            continue
        if match["delimiter"].upper() != "NIL":
            delimiter = delimiter or match["delimiter"][1:-1].replace("\\", "")
        name = match["name"].strip()
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        folders.append(from_utf7_imap(name))
    return folders, delimiter


def closest_folders(wanted: str, folders: list[str], delimiter: str) -> list[str]:
    """The folders, the closest to the one asked for first (difflib's ratio, regardless of case,
    with or without INBOX's prefix "INBOX/" or "INBOX." and whatever the delimiter); the
    server's order among those alike."""

    def plain(name: str) -> str:
        name = name.lower()
        for separator in {delimiter, "/", "."} - {""}:
            name = name.removeprefix(f"inbox{separator}").replace(separator, "/")
        return name

    target = plain(wanted)
    return sorted(folders, key=lambda name: -difflib.SequenceMatcher(None, target, plain(name)).ratio())


class MissingFolder(SourceError):
    """The mailbox has no folder of the job's: its folders, the closest first, come with it (Try
    the mailbox offers them)."""

    def __init__(self, message: str, folders: list[str]):
        super().__init__(message)
        self.folders = folders
