# SPDX-License-Identifier: AGPL-3.0-or-later
"""Files of the private library (library.py) as one ZIP file, streamed as it is made: each file
decrypted piece by piece into it, never whole in memory and never in plain on the disk.

The files are stored, not compressed (pictures and videos, the library's usual files, do not get
smaller, and a stored file costs no CPU), with ZIP64 for files and archives past 4 GB. A folder
chosen goes in with everything in it, below its name; names taken twice in the archive get " (2)".
"""

import posixpath
import time
import zipfile
from collections.abc import Iterator

from .librarykeys import Session
from .runner import numbered

# The most files one ZIP holds.
MOST = 10000


class Sink:
    """What zipfile writes into: kept until the stream takes it (taken), never sought."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        """Nothing to flush: the stream takes what was written."""

    def taken(self) -> bytes:
        """What was written since the last time."""
        data = b"".join(self.parts)
        self.parts.clear()
        return data


def members(shelf, session: Session, ids: list[int]) -> list[tuple[str, dict]]:
    """The archive's names and entries of the files and folders chosen (a folder's files below
    its name, an empty folder as itself), each name once; KeyError for an ID that is none."""
    chosen: list[tuple[str, dict]] = []
    taken: set[str] = set()
    for file_id in dict.fromkeys(ids):
        entries = shelf.below(session, file_id)
        top = posixpath.dirname(entries[0]["path"])
        for entry in entries:
            relative = entry["path"][len(top) :].lstrip("/") if top != "/" else entry["path"].lstrip("/")
            name, number = relative, 1
            while name in taken:
                number += 1
                name = numbered(relative, number)
            if entry["dir"] and any(other["path"].startswith(entry["path"] + "/") for other in entries):
                continue  # a folder with something in it is made by its files' names
            taken.add(name)
            chosen.append((name + "/" if entry["dir"] else name, entry))
            if len(chosen) > MOST:
                raise ValueError(f"more than {MOST} files")
    return chosen


def stream(shelf, session: Session, chosen: list[tuple[str, dict]]) -> Iterator[bytes]:
    """The ZIP file of the members (members()), piece by piece."""
    sink = Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, entry in chosen:
            info = zipfile.ZipInfo(name, date_time=time.localtime(max(entry["at"], 315532800))[:6])
            if entry["dir"]:
                info.external_attr = 0o40755 << 16 | 0x10
                archive.writestr(info, b"")
                yield sink.taken()
                continue
            info.external_attr = 0o644 << 16
            info.file_size = entry["size"]
            with archive.open(info, "w", force_zip64=entry["size"] >= zipfile.ZIP64_LIMIT) as member:
                if entry["size"]:
                    for piece in shelf.content(session, entry["id"], 0, entry["size"] - 1):
                        member.write(piece)
                        yield sink.taken()
            yield sink.taken()
    yield sink.taken()
