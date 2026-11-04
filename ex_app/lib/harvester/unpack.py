# SPDX-License-Identifier: AGPL-3.0-or-later
"""ZIP archives a job downloads, unpacked into a folder named after the archive (the job's option
"Unpack ZIP files"): Rechnungen.zip's files go into Rechnungen/, beside the archive, which is kept
unless "Keep the ZIP files" is off (then its folder stands for it).

An archive is a stranger's file, so it is unpacked only when nothing in it is suspect, else it is
saved as it is, with a line in the log saying why:

- names: no absolute path (/etc/passwd, C:\\), no "..", no control characters; the archive's own
  files are never written to the server's disk by their names (they go to numbered scratch files,
  then into Nextcloud under the folder), hidden names (.DS_Store, __MACOSX/) are left out, and
  the characters Windows refuses (:*?"<>|) become "_";
- no symbolic links, no encrypted members, no two members of one name or a file where another
  needs a folder;
- at most MAX_FILES files, at most the job's largest file (Largest file) unpacked in all, at
  most MAX_RATIO times the size compressed (a member's, and the whole archive's): a zip bomb is
  refused before it is unpacked, and while it is (the bytes read count, not what the archive says);
- archives in the archive stay archives (no nested unpacking). Only ZIP: an archive whose name
  ends with .zip and whose content starts as one (an .epub, .docx or .jar is a ZIP too, and stays
  a file). tar.gz is not unpacked.
"""

import posixpath
import re
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from typing import BinaryIO

from .l10n import error_text, said, said_n
from .patterns import render_tags
from .sources.base import Refused, check_stop

# The most files an archive may hold to be unpacked.
MAX_FILES = 1000
# How much larger a member (or the whole archive) may be unpacked than compressed; a member of
# less than RATIO_FROM bytes is not judged so alone (a small text file packs well).
MAX_RATIO = 100
RATIO_FROM = 1024 * 1024
# The deepest a member's path may be, and the longest one of its names.
MAX_DEPTH = 20
MAX_NAME = 200
# How a ZIP file starts.
MAGIC = b"PK\x03\x04"
CHUNK = 1 << 20
# A drive's letter (C:), which a ZIP made on Windows may begin a name with.
DRIVE = re.compile(r"^[A-Za-z]:")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Characters Windows (and Nextcloud's clients there) refuse in a name: replaced by "_".
RESERVED = re.compile(r'[:*?"<>|]')


class NotUnpacked(Exception):
    """The archive is not unpacked (suspect, or not a ZIP after all): it is saved as it is."""


def unpacks(options: dict, path: str) -> bool:
    """Whether the job unpacks a file of that path, if it is a ZIP: the option is on, and its
    name ends with .zip."""
    return bool(int(options.get("unpack", 0) or 0)) and path.lower().endswith(".zip")


def wanted(options: dict, path: str, content: BinaryIO) -> bool:
    """Whether the job unpacks the file: it unpacks its path's (unpacks), and it starts as a ZIP
    does."""
    if not unpacks(options, path):
        return False
    content.seek(0)
    start = content.read(len(MAGIC))
    content.seek(0)
    return start == MAGIC


def keeps(options: dict) -> bool:
    """Whether an unpacked archive is kept beside its folder (the default)."""
    return bool(int(options.get("unpack_keep", 1) or 0))


def folder_of(path: str) -> str:
    """The folder an archive's files go into: its path without .zip."""
    return posixpath.splitext(path)[0]


def member_path(name: str) -> str:
    """A member's path inside the folder, made of its names; "" for one left out (hidden, a
    folder of its own); NotUnpacked for a suspect one."""
    name = name.replace("\\", "/")
    if name.startswith("/") or DRIVE.match(name):
        raise NotUnpacked(f"{name} is an absolute path")
    parts = name.split("/")
    if ".." in parts:
        raise NotUnpacked(f"{name} leads out of its folder (..)")
    parts = [part for part in parts if part not in ("", ".")]
    if not parts or parts[0] == "__MACOSX" or any(part.startswith(".") for part in parts):
        return ""
    if CONTROL.search(name) or any(len(part) > MAX_NAME or part != part.strip() for part in parts):
        raise NotUnpacked(f"{name!r} is no name a file may have")
    if len(parts) > MAX_DEPTH:
        raise NotUnpacked(f"{name} lies deeper than {MAX_DEPTH} folders")
    return "/".join(RESERVED.sub("_", part) for part in parts)


def members(archive: zipfile.ZipFile, most: int, size: int) -> list[tuple[zipfile.ZipInfo, str]]:
    """The archive's files to unpack with their paths in the folder, checked as the module says
    before anything is unpacked; most is the most unpacked in all, size the archive's."""
    chosen: list[tuple[zipfile.ZipInfo, str]] = []
    total = 0
    for info in archive.infolist():
        if stat.S_ISLNK(info.external_attr >> 16):
            raise NotUnpacked(f"{info.filename} is a symbolic link")
        if info.flag_bits & 0x1:
            raise NotUnpacked(f"{info.filename} is encrypted")
        path = member_path(info.filename)
        if info.is_dir() or not path:
            continue
        if info.file_size > RATIO_FROM and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise NotUnpacked(f"{info.filename} unpacks to more than {MAX_RATIO} times its size (a zip bomb?)")
        total += info.file_size
        chosen.append((info, path))
        if len(chosen) > MAX_FILES:
            raise NotUnpacked(f"it holds more than {MAX_FILES} files")
    if not chosen:
        raise NotUnpacked("it holds no files")
    if total > most:
        raise NotUnpacked(f"it unpacks to {total // 1000000} MB, more than the job's largest file")
    if total > RATIO_FROM and total > MAX_RATIO * max(size, 1):
        raise NotUnpacked(f"it unpacks to more than {MAX_RATIO} times its size (a zip bomb?)")
    paths = [path.lower() for _, path in chosen]
    if len(set(paths)) != len(paths):
        raise NotUnpacked("two of its files have the same name")
    folders = {posixpath.dirname(path) for path in paths} - {""}
    folders |= {parent for folder in folders for parent in _parents(folder)}
    if folders & set(paths):
        raise NotUnpacked("one of its files has the name of one of its folders")
    return chosen


def _parents(folder: str) -> set[str]:
    """The folders above a folder's path (a/b/c: a and a/b)."""
    parts = folder.split("/")
    return {"/".join(parts[:end]) for end in range(1, len(parts))}


def extract(
    archive: zipfile.ZipFile, chosen: list[tuple[zipfile.ZipInfo, str]], scratch: str, most: int
) -> list[tuple[str, str]]:
    """The chosen members written to numbered files in scratch (never by their names): their
    paths in the folder with those files. The bytes read count: a member that unpacks to more
    than it says, or all to more than most, is NotUnpacked (so is a broken one)."""
    written: list[tuple[str, str]] = []
    total = 0
    for number, (info, path) in enumerate(chosen):
        local = posixpath.join(scratch, str(number))
        size = 0
        try:
            with archive.open(info) as source, open(local, "wb") as dest:
                while chunk := source.read(CHUNK):
                    check_stop()
                    size += len(chunk)
                    total += len(chunk)
                    if size > info.file_size or total > most:
                        raise NotUnpacked(f"{info.filename} unpacks to more than it says (a zip bomb?)")
                    dest.write(chunk)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, ValueError) as error:
            raise NotUnpacked(f"{info.filename} cannot be unpacked: {error}") from error
        written.append((path, local))
    return written


def free(path: str, taken: Callable[[str], bool], numbered: Callable[[str, int], str]) -> str:
    """The archive's path whose folder is not taken (a folder of the person's, or another
    archive's of the job): the path, else numbered as the runner numbers names."""
    for number in range(1, 101):
        candidate = numbered(path, number) if number > 1 else path
        if not taken(folder_of(candidate)) and (number == 1 or not taken(candidate)):
            return candidate
    raise NotUnpacked(f"{folder_of(path)} and 99 numbered folders are taken")


def unpack(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    library,
    content: BinaryIO,
    path: str,
    most: int,
    tags: list[str],
    report: Callable[[str, str], None],
) -> int:
    """The archive's files put into its folder (folder_of(path)) and given the tags; how many. A
    file the library refuses (Refused: Immich's, not a photo or video) is left out, the log
    saying so."""
    content.seek(0, 2)
    size = content.tell()
    content.seek(0)
    folder = folder_of(path)
    refused = 0
    try:
        with zipfile.ZipFile(content) as archive:
            chosen = members(archive, most, size)
            with tempfile.TemporaryDirectory(prefix="harvester-unpack-") as scratch:
                for inner, local in extract(archive, chosen, scratch, most):
                    check_stop()
                    try:
                        with open(local, "rb") as file:
                            file_id = library.put(posixpath.join(folder, inner), file)
                    except Refused as error:
                        # A file the destination keeps none of (Immich: no photo or video).
                        report("info", said("Left out {path}: {error}", path=inner, error=error_text(error)))
                        refused += 1
                        continue
                    if tags:
                        library.tag(file_id, tags)
    except zipfile.BadZipFile as error:
        raise NotUnpacked(f"it is no ZIP file that can be read: {error}") from error
    finally:
        content.seek(0)
    report(
        "info",
        said_n(
            "Unpacked %n file of {path} into {folder}",
            "Unpacked %n files of {path} into {folder}",
            len(chosen) - refused,
            path=path,
            folder=folder,
        ),
    )
    return len(chosen) - refused


def saved(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    library,
    job,
    values: dict,
    path: str,
    content: BinaryIO,
    most: int,
    report: Callable[[str, str], None],
) -> tuple[int, bool]:
    """The downloaded archive at path (its folder free already), unpacked as the job says: the ID
    the job records of it (the archive's, or without it its folder's), and whether the archive was
    saved. A suspect archive is saved as it is, the log saying why."""
    try:
        unpack(library, content, path, most, render_tags(job.tags, values), report)
    except NotUnpacked as error:
        report("info", said("{path} is not unpacked: {error}", path=path, error=str(error)))
        return library.put(path, content), True
    if keeps(job.options):
        return library.put(path, content), True
    # Without the archive its folder stands for it (its tags, a later check for changes).
    found = library.find(folder_of(path))
    return (found, False) if found is not None else (library.put(path, content), True)
