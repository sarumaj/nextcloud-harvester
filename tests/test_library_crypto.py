# SPDX-License-Identifier: AGPL-3.0-or-later
"""The private library's encryption (librarycrypt.py) and what library.py makes of it: sealed
values and wrapped keys open with the right key alone; a blob's ranges decrypt at and across the
chunks' edges, reading only their chunks; a chunk changed, swapped, dropped or cut off, a header,
a sealed key or an encrypted name changed in the database are found; a large file streams through
in bounded memory; pictures get encrypted thumbnails."""

# pylint: disable=missing-function-docstring,protected-access

import hashlib
import io
import os
import tracemalloc

import pytest
from PIL import Image
from test_library import CHEAP, set_up

from harvester import librarycrypt as crypt
from harvester import library, thumbnails
from harvester.library import Shelf
from harvester.librarykeys import Keyring
from harvester.store import Store


@pytest.fixture(name="shelf")
def fixture_shelf(tmp_path, monkeypatch):
    """A library with anna's set up; her token on the shelf (token)."""
    monkeypatch.setattr(crypt, "SCRYPT", CHEAP)
    monkeypatch.setattr(library, "LIMITS", {"person": 0, "total": 0})
    store = Store(str(tmp_path / "db"))
    keys = Keyring(store)
    made = Shelf(store, tmp_path / "library", keys)
    made.token = set_up(keys, "anna", "anna's password")  # type: ignore[attr-defined]
    return made


def opened(shelf):
    return shelf.keys.session("anna", shelf.token)


def blob_of(shelf, file_id):
    return shelf.blob_path(
        "anna", shelf.store.rows("SELECT blob FROM library_files WHERE id = ?", (file_id,))[0]["blob"]
    )


def test_sealed_values_open_with_their_key_and_place_alone():
    private, public = crypt.new_key_pair()
    other, _ = crypt.new_key_pair()
    sealed = crypt.seal(public, b"secret", b"place")
    assert crypt.unseal(private, sealed, b"place") == b"secret"
    assert crypt.seal(public, b"secret", b"place") != sealed, "a new ephemeral key each time"
    for key, aad, data in (
        (other, b"place", sealed),
        (private, b"elsewhere", sealed),
        (private, b"place", sealed[:-1]),
    ):
        with pytest.raises(crypt.Tampered):
            crypt.unseal(key, data, aad)
    changed = bytearray(sealed)
    changed[40] ^= 1
    with pytest.raises(crypt.Tampered):
        crypt.unseal(private, bytes(changed), b"place")


def test_the_private_key_opens_with_the_password_or_the_recovery_key(monkeypatch):
    monkeypatch.setattr(crypt, "SCRYPT", CHEAP)
    private, public = crypt.new_key_pair()
    wrapped = crypt.wrap_with_password(private, "correct horse", b"anna")
    assert crypt.public_of(crypt.unwrap_with_password(wrapped, "correct horse", b"anna")) == public
    assert '"n": 16' in wrapped, "the costs are kept with it"
    for password, aad in (("wrong", b"anna"), ("correct horse", b"ben")):
        with pytest.raises(crypt.Tampered):
            crypt.unwrap_with_password(wrapped, password, aad)
    recovery = crypt.new_recovery_key()
    assert len(recovery) == 39 and recovery.count("-") == 7
    kept = crypt.wrap_with_recovery(private, recovery, b"anna")
    typed = recovery.lower().replace("-", "  ").replace("o", "0")
    assert crypt.public_of(crypt.unwrap_with_recovery(kept, typed, b"anna")) == public, "as typed, lookalikes too"
    for wrong in (crypt.new_recovery_key(), "ABCD", ""):
        with pytest.raises(crypt.Tampered):
            crypt.unwrap_with_recovery(kept, wrong, b"anna")


@pytest.mark.parametrize("size", [0, 1, crypt.CHUNK - 1, crypt.CHUNK, crypt.CHUNK + 1, 3 * crypt.CHUNK + 7])
def test_a_blob_round_trips_and_is_padded_to_whole_chunks(tmp_path, size):
    data = os.urandom(size)
    key, blob = os.urandom(32), os.urandom(crypt.BLOB_ID)
    with open(tmp_path / "b", "wb") as out:
        sealer = crypt.Sealer(out, key, blob)
        for start in range(0, size, 1000):
            sealer.write(data[start : start + 1000])
        assert sealer.finish() == size
    assert (tmp_path / "b").stat().st_size == crypt.stored_size(size)
    assert sealer.digest.hexdigest() == hashlib.sha256(data).hexdigest()
    assert b"".join(crypt.opened(tmp_path / "b", key, blob, size, 0, size - 1)) == data


def test_ranges_decrypt_at_the_chunks_edges_reading_their_chunks_alone(tmp_path):
    size = 4 * crypt.CHUNK + 100
    data = os.urandom(size)
    key, blob = os.urandom(32), os.urandom(crypt.BLOB_ID)
    with open(tmp_path / "b", "wb") as out:
        sealer = crypt.Sealer(out, key, blob)
        sealer.write(data)
        sealer.finish()
    edge = crypt.CHUNK
    for start, end in [
        (0, 0),
        (edge - 1, edge - 1),
        (edge - 1, edge),
        (edge, edge),
        (edge, 2 * edge - 1),
        (edge - 5, 3 * edge + 5),
        (size - 1, size - 1),
        (4 * edge, size - 1),
    ]:
        assert b"".join(crypt.opened(tmp_path / "b", key, blob, size, start, end)) == data[start : end + 1]
    # A chunk other than the ones asked for may be damaged: it is never read.
    damaged = bytearray((tmp_path / "b").read_bytes())
    damaged[crypt.HEADER + 3 * (crypt.CHUNK + crypt.TAG) + 10] ^= 1
    (tmp_path / "b").write_bytes(bytes(damaged))
    assert b"".join(crypt.opened(tmp_path / "b", key, blob, size, edge, 2 * edge)) == data[edge : 2 * edge + 1]
    with pytest.raises(crypt.Tampered):
        b"".join(crypt.opened(tmp_path / "b", key, blob, size, 0, size - 1))


def test_a_blob_changed_swapped_or_cut_off_is_found(tmp_path):
    size = 3 * crypt.CHUNK
    key, blob = os.urandom(32), os.urandom(crypt.BLOB_ID)
    with open(tmp_path / "b", "wb") as out:
        sealer = crypt.Sealer(out, key, blob)
        sealer.write(os.urandom(size))
        sealer.finish()
    original = (tmp_path / "b").read_bytes()
    step = crypt.CHUNK + crypt.TAG
    first, second, third = (original[crypt.HEADER + i * step : crypt.HEADER + (i + 1) * step] for i in range(3))
    header = original[: crypt.HEADER]
    variants = {
        "a bit flipped": header + first[:5] + bytes([first[5] ^ 1]) + first[6:] + second + third,
        "chunks swapped": header + second + first + third,
        "the last chunk dropped": header + first + second,
        "the last chunk in the middle": header + first + third + second,
        "another blob's id": crypt.MAGIC + os.urandom(crypt.BLOB_ID) + first + second + third,
        "another format": b"HVL0" + header[4:] + first + second + third,
    }
    for why, data in variants.items():
        (tmp_path / "b").write_bytes(data)
        with pytest.raises(crypt.Tampered):
            b"".join(crypt.opened(tmp_path / "b", key, blob, size, 0, size - 1))
        assert why
    (tmp_path / "b").write_bytes(header + first + second)
    with pytest.raises(crypt.Tampered):
        # Cut off, and its size claimed so (it cannot be: the size is encrypted): the last is not last.
        b"".join(crypt.opened(tmp_path / "b", key, blob, 2 * crypt.CHUNK, 0, 2 * crypt.CHUNK - 1))


def test_a_sealed_key_or_name_changed_in_the_database_hides_the_entry(shelf):
    first = shelf.put("anna", "/a.txt", io.BytesIO(b"a"))
    second = shelf.put("anna", "/b.txt", io.BytesIO(b"b"))
    assert sorted(shelf.index(opened(shelf))) == [first, second]
    rows = {row["id"]: row for row in shelf.store.rows("SELECT * FROM library_files")}
    with shelf.store.writing() as db:
        # a's name given b's encrypted name: it does not open with a's key.
        db.execute("UPDATE library_files SET meta = ? WHERE id = ?", (rows[second]["meta"], first))
    assert sorted(shelf.index(opened(shelf))) == [second]
    with shelf.store.writing() as db:
        db.execute(
            "UPDATE library_files SET meta = ?, secret = ? WHERE id = ?", (rows[first]["meta"], b"x" * 80, first)
        )
    assert sorted(shelf.index(opened(shelf))) == [second], "a sealed key that does not open"
    with shelf.store.writing() as db:
        db.execute("UPDATE library_files SET secret = ? WHERE id = ?", (rows[first]["secret"], first))
        db.execute("UPDATE library_files SET lookup = ? WHERE id = ?", (rows[second]["lookup"][::-1], second))
    assert sorted(shelf.index(opened(shelf))) == [first], "an entry at another place than its own"
    folder = shelf.mkdir("anna", "/F")
    with shelf.store.writing() as db:
        db.execute("UPDATE library_files SET lookup = ? WHERE id = ?", (rows[second]["lookup"], second))
        db.execute("UPDATE library_files SET parent = ? WHERE id = ?", (folder, first))
    assert first not in shelf.index(opened(shelf)), "moved into a folder outside the app"


def test_a_large_file_streams_through_in_bounded_memory(shelf):
    size = 50 * 1000**2
    piece = os.urandom(1 << 20)

    class Endless(io.RawIOBase):
        """size bytes, made as they are read."""

        def __init__(self):
            self.left = size

        def readable(self):
            return True

        def seek(self, offset, whence=0):  # pylint: disable=unused-argument
            return 0

        def read(self, wanted=-1):
            count = min(self.left, len(piece), wanted if wanted > 0 else len(piece))
            self.left -= count
            return piece[:count]

    tracemalloc.start()
    file_id = shelf.put("anna", "/big.bin", Endless())  # type: ignore[arg-type]
    _, peak = tracemalloc.get_traced_memory()
    assert peak < 8 * 1000**2, f"{peak} bytes at most in memory"
    tracemalloc.reset_peak()
    digest = hashlib.sha256()
    for part in shelf.content(opened(shelf), file_id, 0, size - 1):
        digest.update(part)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 8 * 1000**2, f"{peak} bytes at most in memory"
    expected = hashlib.sha256()
    for _ in range(size // len(piece)):
        expected.update(piece)
    expected.update(piece[: size % len(piece)])
    assert digest.hexdigest() == expected.hexdigest()


def test_pictures_get_encrypted_thumbnails_and_other_files_none(shelf):
    picture = io.BytesIO()
    Image.new("RGB", (1200, 800), (200, 30, 30)).save(picture, "PNG")
    file_id = shelf.put("anna", "/p.png", picture)
    small = shelf.thumbnail(opened(shelf), file_id)
    assert small is not None and small.startswith(b"\xff\xd8")
    with Image.open(io.BytesIO(small)) as shown:
        assert max(shown.size) == thumbnails.SIZE and shown.format == "JPEG"
    thumb = blob_of(shelf, file_id).with_suffix(".thumb").read_bytes()
    assert b"JFIF" not in thumb and b"\xff\xd8" not in thumb[:20], "encrypted on the disk"
    assert shelf.entry(opened(shelf), file_id)["thumb"]
    text = shelf.put("anna", "/t.txt", io.BytesIO(b"x"))
    broken = shelf.put("anna", "/broken.jpg", io.BytesIO(b"\xff\xd8 not a picture"))
    assert shelf.thumbnail(opened(shelf), text) is None and shelf.thumbnail(opened(shelf), broken) is None
    assert thumbnails.thumbnail(b"") is None


def test_writes_need_only_the_public_key_and_another_persons_keys_open_nothing(shelf):
    shelf.keys.lock(shelf.token)
    file_id = shelf.put("anna", "/locked.txt", io.BytesIO(b"written while locked"))
    shelf.label("anna", file_id, add=["x"])
    ben = set_up(shelf.keys, "ben", "ben's password")
    assert shelf.index(shelf.keys.session("ben", ben)) == {}, "ben sees none of anna's"
    row = shelf.store.rows("SELECT secret, blob FROM library_files WHERE id = ?", (file_id,))[0]
    with pytest.raises(crypt.Tampered):
        crypt.unseal(
            shelf.keys.session("ben", ben).private, bytes(row["secret"]), b"secret|" + bytes.fromhex(row["blob"])
        )
    anna = shelf.keys.unlock("anna", "anna's password")
    entry = shelf.entry(shelf.keys.session("anna", anna), file_id)
    assert entry["labels"] == ["x"]
    assert b"".join(shelf.content(shelf.keys.session("anna", anna), file_id, 0, 30)) == b"written while locked"
