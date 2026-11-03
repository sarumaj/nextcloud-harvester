# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unpacking ZIP files (harvester.unpack, the option Unpack ZIP files): the files into a folder of
the archive's name, the archive kept or not, tagged; a suspect archive (a path out of its folder,
an absolute one, a link, a zip bomb, too many files, too much in all, two of one name) saved as it
is, with the reason in the log; archives in it, and ZIPs of another name (.epub), stay files."""

# pylint: disable=missing-function-docstring

import io
import os
import stat
import zipfile

import pytest
from test_runner import FakeLibrary
from test_versions import go as run_job

from harvester import runner, unpack, versions
from harvester.sources import Item, Source
from harvester.sources.base import RECHECK
from harvester.store import Job, Store


def archive(members: dict[str, bytes], links: tuple[str, ...] = ()) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as made:
        for name, body in members.items():
            made.writestr(name, body)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            made.writestr(info, "/etc/passwd")
    return data.getvalue()


class Folders(FakeLibrary):
    """FakeLibrary whose folders (a path some files are in) are found too, as Nextcloud's are."""

    def find(self, path):
        found = super().find(path)
        inside = [index for index, name in enumerate(self.files) if name.startswith(path + "/")]
        return found if found is not None or not inside else 10000 + inside[0]


class Archives(Source):
    """Its files by path."""

    name = "archives"
    title = "Archives"
    options = {"recheck": RECHECK}
    files: dict[str, bytes] = {}

    def items(self, seen):
        for path in self.files:
            if not seen(path):
                yield Item(key=path, path=path, url=f"https://site.example/{path}", values={"name": path})

    def fetch(self, item, dest):
        dest.write(self.files[item.key])


def go(files: dict[str, bytes], options: dict, library: FakeLibrary | None = None) -> tuple[FakeLibrary, list]:
    library = library or Folders()
    job = Job(user="u", name="n", source="archives", target="/In", schedule="", options=options, tags=["{name}"])
    source = Archives({}, {}, None)
    source.files = files
    lines: list = []
    seen: set[str] = set()
    runner.run(job, source, library, seen.__contains__, seen.add, report=lambda level, text: lines.append(str(text)))
    return library, lines


def test_a_zip_is_unpacked_into_a_folder_of_its_name_beside_it():
    body = archive({"a.pdf": b"%PDF a", "sub/b.txt": b"b", ".DS_Store": b"x", "__MACOSX/._a.pdf": b"x"})
    library, lines = go({"Rechnungen.zip": body}, {"unpack": 1})
    assert library.files == {
        "/In/Rechnungen/a.pdf": b"%PDF a",
        "/In/Rechnungen/sub/b.txt": b"b",
        "/In/Rechnungen.zip": body,
    }
    assert "Unpacked 2 files of /In/Rechnungen.zip into /In/Rechnungen" in lines
    assert all(library.tags[list(library.files).index(path)] == ["Rechnungen.zip"] for path in library.files)


def test_without_keeping_only_its_files_are_saved_the_folder_standing_for_it():
    library, lines = go({"r.zip": archive({"a.pdf": b"%PDF a"})}, {"unpack": 1, "unpack_keep": 0})
    assert list(library.files) == ["/In/r/a.pdf"]
    assert not [line for line in lines if line.startswith("Saved")]


def test_off_a_zip_stays_a_file_and_other_zips_are_never_unpacked():
    body = archive({"a.pdf": b"%PDF a"})
    library, _ = go({"r.zip": body}, {})
    assert list(library.files) == ["/In/r.zip"]
    library, _ = go({"book.epub": body, "fake.zip": b"not a zip"}, {"unpack": 1})
    assert list(library.files) == ["/In/book.epub", "/In/fake.zip"]


def test_an_archive_in_the_archive_stays_an_archive():
    inner = archive({"deep.txt": b"deep"})
    library, _ = go({"outer.zip": archive({"inner.zip": inner})}, {"unpack": 1, "unpack_keep": 0})
    assert library.files == {"/In/outer/inner.zip": inner}


def test_a_taken_folder_gives_the_archive_a_numbered_name():
    library = Folders({"/In/r/mine.txt": b""})
    library, _ = go({"r.zip": archive({"a.pdf": b"%PDF a"})}, {"unpack": 1}, library)
    assert "/In/r (2)/a.pdf" in library.files and "/In/r (2).zip" in library.files


@pytest.mark.parametrize(
    "members,links,reason",
    [
        ({"../evil.txt": b"x"}, (), "../evil.txt leads out of its folder (..)"),
        ({"a/../../evil.txt": b"x"}, (), "leads out of its folder"),
        ({"/etc/cron.d/evil": b"x"}, (), "is an absolute path"),
        ({"C:\\Windows\\evil.dll": b"x"}, (), "is an absolute path"),
        ({"a.txt": b"x"}, ("link",), "link is a symbolic link"),
        ({"a.txt": b"x", "A.TXT": b"y"}, (), "two of its files have the same name"),
        ({"a": b"x", "a/b": b"y"}, (), "one of its files has the name of one of its folders"),
        ({"bomb.bin": bytes(5 * 1024 * 1024)}, (), "unpacks to more than 100 times its size (a zip bomb?)"),
        ({"bell\x07.txt": b"x"}, (), "is no name a file may have"),
    ],
)
def test_a_suspect_archive_is_saved_as_it_is(members, links, reason):
    body = archive(members, links)
    library, lines = go({"r.zip": body}, {"unpack": 1, "unpack_keep": 0})
    assert library.files == {"/In/r.zip": body}
    assert any(line.startswith("/In/r.zip is not unpacked: ") and reason in line for line in lines), lines


def test_too_many_files_or_too_much_in_all_is_not_unpacked(monkeypatch):
    monkeypatch.setattr(unpack, "MAX_FILES", 3)
    library, lines = go({"r.zip": archive({f"{n}.txt": b"x" for n in range(4)})}, {"unpack": 1})
    assert list(library.files) == ["/In/r.zip"]
    assert "/In/r.zip is not unpacked: it holds more than 3 files" in lines
    noise = os.urandom(1_500_000)
    files = {"r.zip": archive({"a.bin": noise})}
    library, lines = go(files, {"unpack": 1})
    assert list(library.files) == ["/In/r/a.bin", "/In/r.zip"]
    with pytest.raises(unpack.NotUnpacked, match="more than the job's largest file"):
        unpack.unpack(FakeLibrary(), io.BytesIO(files["r.zip"]), "/In/r.zip", 1_000_000, [], lambda *_: None)


def test_a_member_that_unpacks_to_more_than_it_says_is_caught_while_unpacking():
    body = bytearray(archive({"a.txt": b"x" * 5000}))
    # The central directory's uncompressed size (at its entry's offset 24) says 10 bytes.
    entry = body.rindex(b"PK\x01\x02")
    body[entry + 24 : entry + 28] = (10).to_bytes(4, "little")
    with pytest.raises(unpack.NotUnpacked, match="a.txt"):
        unpack.unpack(FakeLibrary(), io.BytesIO(bytes(body)), "/In/r.zip", 10**9, [], lambda *_: None)


def test_member_paths():
    assert unpack.member_path("a/./b//c.pdf") == "a/b/c.pdf"
    assert unpack.member_path("a\\b.pdf") == "a/b.pdf"
    assert unpack.member_path("dir/") == "dir"
    assert unpack.member_path(".git/config") == ""
    assert unpack.member_path("Q&A: why?.txt") == "Q&A_ why_.txt"
    with pytest.raises(unpack.NotUnpacked):
        unpack.member_path("a/" * 25 + "x")


def test_a_changed_archive_is_unpacked_again_into_its_folder(tmp_path):
    store = Store(str(tmp_path / "db"))
    job = store.save(
        Job(user="u", name="n", source="archives", target="/In", schedule="", options={"unpack": 1, "unpack_keep": 0})
    )
    job_id = job.id or 0
    library = Folders()
    source = Archives({"recheck": 1}, {}, None)
    source.contents = versions.Contents(store, job_id)
    source.files = {"r.zip": archive({"a.txt": b"first"})}

    def turn() -> str:
        return run_job(store, job, source, library)

    assert turn() == "1 downloaded"
    store._db.execute("UPDATE contents SET at = 0")  # pylint: disable=protected-access
    source.files = {"r.zip": archive({"a.txt": b"second", "b.txt": b"new"})}
    assert turn() == "0 downloaded, 1 changed (a new version)"
    assert library.files == {"/In/r/a.txt": b"second", "/In/r/b.txt": b"new"}
