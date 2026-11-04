# SPDX-License-Identifier: AGPL-3.0-or-later
"""harvester.vault: the primitives against RFC 5297, the vaults Cryptomator's own library wrote
(tests/data/cryptomator, the sister app's fixtures with its manifests: every name, kind, size and
SHA-256), and what is written read back, in both cipher combinations. tests/test_vault_js.py has
the sister app's code read what is written here."""

import base64
import hashlib
import hmac
import json
import os
import shutil
import tracemalloc
import unicodedata
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

from harvester.vault import (
    CHUNK,
    CorruptVault,
    FolderStorage,
    InvalidName,
    MemoryStorage,
    NameTaken,
    NotInVault,
    Siv,
    UnsupportedVault,
    Vault,
    WrongPassword,
    numbered,
)

DATA = Path(__file__).parent / "data" / "cryptomator"
VAULTS = ["vault-gcm", "vault-ctrmac"]
# What Generate.java (the sister app's tests/fixtures/generator) wrote into both vaults.
KNOWN = {
    "hello.txt": b"Hello, vault!\n",
    "Zażółć gęślą jaźń ✓.txt": "Polish letters\n".encode(),
    "empty.txt": b"",
    "Docs & Stuff/inner/deep.txt": b"nested\n",
    ("a-very-long-name-" * 10) + "end-folder/inside.txt": b"in a long-named folder\n",
}
SIZES = [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 1_000_000]


def manifest(name: str) -> dict:
    """The fixture's manifest: its password, cipher combination and entries."""
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(name="keys", scope="module")
def fixture_keys() -> dict[str, bytes]:
    """Each fixture's key material, unlocked once (scrypt takes a moment)."""
    return {name: Vault.unlock(FolderStorage(DATA / name), manifest(name)["password"]).key_material for name in VAULTS}


@pytest.fixture(name="copy", params=VAULTS)
def fixture_copy(request, tmp_path, keys) -> tuple[str, Path, Vault]:
    """A copy of a fixture vault, opened with its key material."""
    folder = tmp_path / request.param
    shutil.copytree(DATA / request.param, folder)
    return request.param, folder, Vault.open(FolderStorage(folder), keys[request.param])


def walk(vault: Vault, path: str = "") -> dict[str, dict]:
    """Every entry under a folder as the manifests list them."""
    entries = {}
    for node in vault.listdir(path):
        at = f"{path}/{node.name}" if path else node.name
        if node.kind == "file":
            data = vault.read_bytes(node)
            assert len(data) == node.size
            entries[at] = {"type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        elif node.kind == "dir":
            entries[at] = {"type": "dir"}
            entries.update(walk(vault, at))
        else:
            entries[at] = {"type": node.kind}
    return entries


def test_siv_matches_rfc_5297():
    """A.1 of RFC 5297: the first half of the key is S2V's (Cryptomator's MAC key)."""
    key = bytes.fromhex("fffefdfcfbfaf9f8f7f6f5f4f3f2f1f0f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
    associated = bytes.fromhex("101112131415161718191a1b1c1d1e1f2021222324252627")
    plaintext = bytes.fromhex("112233445566778899aabbccddee")
    siv = Siv(key[:16], key[16:])
    ciphertext = siv.encrypt(plaintext, associated)
    assert ciphertext.hex() == "85632d07c6e8f37f950acd320a2ecc9340c02b9690c4dc04daef7f6afe5c"
    assert siv.decrypt(ciphertext, associated) == plaintext
    with pytest.raises(CorruptVault):
        siv.decrypt(ciphertext, b"other")


def test_siv_matches_cryptography():
    """Cryptomator's 32-byte keys, as `cryptography`'s AES-SIV (the MAC key first) has them, for
    names of every length around the blocks; the empty one (the root's ID) only here."""
    mac_key, enc_key = os.urandom(32), os.urandom(32)
    siv = Siv(mac_key, enc_key)
    for length in [1, 15, 16, 17, 31, 32, 33, 100]:
        plaintext = os.urandom(length)
        assert siv.encrypt(plaintext, b"dir") == AESSIV(mac_key + enc_key).encrypt(plaintext, [b"dir"])
    assert len(siv.encrypt(b"")) == 16


@pytest.mark.parametrize("name", VAULTS)
def test_fixtures_read_as_their_manifests(name, keys):
    """Every name, kind, size and SHA-256 of the vaults Cryptomator wrote, and the known texts."""
    expected = manifest(name)
    vault = Vault.unlock(FolderStorage(DATA / name), expected["password"])
    assert vault.cipher_combo == expected["cipherCombo"]
    assert vault.key_material == keys[name]
    found = walk(vault)
    assert found == {
        path: {k: v for k, v in entry.items() if k != "target"} for path, entry in expected["entries"].items()
    }
    for path, data in KNOWN.items():
        assert vault.read_bytes(path) == data
    assert vault.find("link-to-hello").kind == "link"
    assert vault.find("missing.txt") is None
    with pytest.raises(NotInVault):
        vault.find("hello.txt/below")


@pytest.mark.parametrize("name", VAULTS)
def test_fixture_in_memory_reads_the_same(name, keys):
    """A storage without `stream` (the file read whole) reads the same."""
    vault = Vault.open(MemoryStorage.copy_of(DATA / name), keys[name])
    assert walk(vault) == walk(Vault.open(FolderStorage(DATA / name), keys[name]))


@pytest.mark.parametrize("name", VAULTS)
def test_wrong_password(name):
    """A wrong password is told as such; the password is taken in NFC."""
    with pytest.raises(WrongPassword):
        Vault.unlock(FolderStorage(DATA / name), "correct horse battery")
    decomposed = unicodedata.normalize("NFD", "correct horse ✓ battery ż")
    with pytest.raises(WrongPassword):
        Vault.unlock(FolderStorage(DATA / name), decomposed)


def test_key_material_opens_only_its_vault(keys):
    """The key material reopens its vault without the password; another vault's is refused, as
    its configuration is not signed with it."""
    vault = Vault.open(FolderStorage(DATA / "vault-gcm"), keys["vault-gcm"])
    assert vault.read_bytes("hello.txt") == KNOWN["hello.txt"]
    with pytest.raises(CorruptVault, match="not signed"):
        Vault.open(FolderStorage(DATA / "vault-gcm"), keys["vault-ctrmac"])
    with pytest.raises(ValueError):
        Vault.open(FolderStorage(DATA / "vault-gcm"), keys["vault-gcm"][:32])


def jwt(header: dict, payload: dict, key: bytes, digest=hashlib.sha256) -> bytes:
    """A vault.cryptomator signed with the key material."""

    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    signed = f"{part(header)}.{part(payload)}"
    signature = base64.urlsafe_b64encode(hmac.new(key, signed.encode(), digest).digest()).decode().rstrip("=")
    return f"{signed}.{signature}".encode()


HEADER = {"kid": "masterkeyfile:masterkey.cryptomator", "typ": "JWT", "alg": "HS256"}
PAYLOAD = {"jti": "x", "format": 8, "cipherCombo": "SIV_GCM", "shorteningThreshold": 220}


def with_config(name: str, config: bytes) -> MemoryStorage:
    """A fixture in memory with another vault.cryptomator."""
    storage = MemoryStorage.copy_of(DATA / name)
    storage.files["vault.cryptomator"] = config
    return storage


@pytest.mark.parametrize(("alg", "digest"), [("HS384", hashlib.sha384), ("HS512", hashlib.sha512)])
def test_other_signatures(keys, alg, digest):
    """HS384 and HS512, which cryptofs also accepts, with the key file as well."""
    storage = with_config("vault-gcm", jwt(HEADER | {"alg": alg}, PAYLOAD, keys["vault-gcm"], digest))
    assert Vault.unlock(storage, manifest("vault-gcm")["password"]).read_bytes("hello.txt") == KNOWN["hello.txt"]


@pytest.mark.parametrize(
    ("header", "payload", "message"),
    [
        (HEADER | {"kid": "hub+https://hub.example/vaults/1"}, PAYLOAD, "key hub"),
        (HEADER, PAYLOAD | {"format": 7}, "vault format 7"),
        (HEADER, PAYLOAD | {"cipherCombo": "SIV_CTR"}, "cipher combination"),
        (HEADER | {"alg": "none"}, PAYLOAD, "signature none"),
    ],
)
def test_unsupported_vaults(keys, header, payload, message):
    """A Hub vault, another format, cipher combination or signature: refused, by name."""
    storage = with_config("vault-gcm", jwt(header, payload, keys["vault-gcm"]))
    with pytest.raises(UnsupportedVault, match=message):
        Vault.unlock(storage, manifest("vault-gcm")["password"])
    with pytest.raises(UnsupportedVault):
        Vault.unlock(with_config("vault-gcm", b"not a jwt"), "x")
    with pytest.raises(UnsupportedVault, match="not a vault"):
        Vault.unlock(MemoryStorage(), "x")


def test_tampered_configuration(keys):
    """A configuration changed after it was signed is refused (its signature)."""
    storage = with_config("vault-gcm", jwt(HEADER, PAYLOAD, keys["vault-ctrmac"]))
    with pytest.raises(CorruptVault, match="not signed"):
        Vault.unlock(storage, manifest("vault-gcm")["password"])


def counting(sizes: list[int]) -> dict[str, bytes]:
    """Files of these sizes, random."""
    return {f"size-{size}.bin": os.urandom(size) for size in sizes}


def test_writes_read_back(copy):
    """Files of the sizes around the chunks, as the storage holds them (sizes as the format
    says), read back; the fixture's own files are untouched."""
    _name, folder, vault = copy
    before = walk(vault)
    for path, data in counting(SIZES).items():
        written = vault.write_file(f"new/{path}", iter([data[:1000], data[1000:]]))
        assert written.size == len(data) == written.node.size
        assert written.sha256 == hashlib.sha256(data).digest()
        assert (folder / written.node.contents).stat().st_size == vault.encrypted_size(len(data))
        assert vault.read_bytes(f"new/{path}") == data
        vault.verify_file(written)
    after = walk(vault)
    assert {path: entry for path, entry in after.items() if not path.startswith("new")} == before


def test_names_and_folders(copy):
    """A long name (.c9s with name.c9s and contents.c9r or dir.c9r), names right at the
    shortening threshold, a decomposed name stored in NFC, new nested folders with their
    dirid.c9r, into an existing folder."""
    _name, folder, vault = copy
    long_name = "long-" * 40 + ".txt"
    vault.write_file(long_name, [b"long\n"])
    node = vault.find(long_name)
    assert node.path.endswith(".c9s") and node.contents.endswith("/contents.c9r")
    assert (folder / node.path / "name.c9s").read_text() == vault.entry_path("", long_name).full_name
    # Encrypted names of 220 (kept) and 224 characters (shortened).
    assert not vault.write_file("k" * 146, [b"220"]).node.path.endswith(".c9s")
    assert vault.write_file("s" * 147, [b"224"]).node.path.endswith(".c9s")
    decomposed = unicodedata.normalize("NFD", "Zdjęcie ✓ ünïcode.txt")
    vault.write_file(decomposed, [b"nfc"])
    assert vault.read_bytes("Zdjęcie ✓ ünïcode.txt") == b"nfc"
    assert "Zdjęcie ✓ ünïcode.txt" in {node.name for node in vault.listdir()}
    long_folder = "folder-" * 25
    vault.write_file(f"Docs & Stuff/new one/{long_folder}/deep/file.txt", [b"deep"])
    assert vault.read_bytes(f"Docs & Stuff/new one/{long_folder}/deep/file.txt") == b"deep"
    assert vault.read_bytes("Docs & Stuff/inner/deep.txt") == KNOWN["Docs & Stuff/inner/deep.txt"]
    for path in [
        "Docs & Stuff/new one",
        f"Docs & Stuff/new one/{long_folder}",
        f"Docs & Stuff/new one/{long_folder}/deep",
    ]:
        node = vault.find(path)
        assert node.kind == "dir" and len(node.dir_id) == 36
        backup = vault.dir_path(node.dir_id) + "/dirid.c9r"
        assert b"".join(vault.decrypt_chunks([(folder / backup).read_bytes()])) == node.dir_id.encode()
    assert vault.find(f"Docs & Stuff/new one/{long_folder}").path.endswith(".c9s")


def test_name_clashes(copy):
    """A taken name is reported with what has it, never replaced; free_name numbers it."""
    _name, _folder, vault = copy
    with pytest.raises(NameTaken) as taken:
        vault.write_file("hello.txt", [b"other"])
    assert taken.value.kind == "file"
    with pytest.raises(NameTaken) as taken:
        vault.write_file("Docs & Stuff", [b"other"])
    assert taken.value.kind == "dir"
    with pytest.raises(NameTaken):
        vault.write_file("hello.txt/below.txt", [b"other"])
    assert vault.read_bytes("hello.txt") == KNOWN["hello.txt"]
    assert vault.free_name("", "hello.txt") == "hello (2).txt"
    vault.write_file("hello (2).txt", [b"2"])
    assert vault.free_name("", "hello.txt") == "hello (3).txt"
    assert vault.free_name("", "new.txt") == "new.txt"
    assert vault.free_name("not yet", "new.txt") == "new.txt"
    assert numbered("archive", 2) == "archive (2)" and numbered(".hidden", 2) == ".hidden (2)"


@pytest.mark.parametrize("name", ["", ".", "..", "a\\b", "line\nbreak", "x" * 256])
def test_invalid_names(copy, name):
    """Names no vault can hold are refused before anything is written."""
    _name, _folder, vault = copy
    with pytest.raises(InvalidName):
        vault.write_file(f"folder/{name}" if name else "", [b"x"])
    assert vault.find("folder") is None


def tampered(data: bytes, position: int) -> bytes:
    """The bytes with one bit changed."""
    return data[:position] + bytes([data[position] ^ 1]) + data[position + 1 :]


def test_tampering_is_found(copy):
    """A changed chunk, header, truncated chunk or swapped chunks do not authenticate."""
    _name, folder, vault = copy
    written = vault.write_file("three.bin", [os.urandom(2 * CHUNK + 100)])
    path = folder / written.node.contents
    original = path.read_bytes()
    header = vault.scheme.header
    chunk = CHUNK + vault.scheme.overhead
    cases = {
        "chunk 1 does not authenticate": tampered(original, header + chunk + 40),
        "header does not authenticate": tampered(original, 20),
        "chunk 2 truncated": original[: header + 2 * chunk + 10],
        "chunk 0 does not authenticate": original[:header]
        + original[header + chunk : header + 2 * chunk]
        + original[header : header + chunk]
        + original[header + 2 * chunk :],
        "header too short": original[:30],
    }
    for message, data in cases.items():
        path.write_bytes(data)
        with pytest.raises(CorruptVault, match=message):
            vault.read_bytes("three.bin")
    path.write_bytes(tampered(original, len(original) - 1))
    with pytest.raises(CorruptVault):
        vault.verify_file(written)


def test_tampered_name_is_left_out(copy):
    """An entry whose name does not decrypt (or a sync tool's copy) is not listed."""
    _name, folder, vault = copy
    node = vault.find("hello.txt")
    stem = node.path.rsplit("/", 1)[1].removesuffix(".c9r")
    shutil.copy(folder / node.path, folder / vault.dir_path("") / f"{stem[:-4]}AAA=.c9r")
    assert sorted(walk(vault)) == sorted(manifest(_name)["entries"])


class Sink:
    """A storage's write that keeps only the SHA-256 and the largest piece it was given."""

    def __init__(self, inner: MemoryStorage):
        self.inner = inner
        self.digests: dict[str, bytes] = {}
        self.largest = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def write(self, path, chunks):
        """Hashes the pieces without keeping them."""
        digest = hashlib.sha256()
        for chunk in chunks:
            self.largest = max(self.largest, len(chunk))
            digest.update(chunk)
        self.digests[path] = digest.digest()
        self.inner.write(path, [b""])


def test_large_file_is_streamed(keys):
    """50 MB from a generator are encrypted as they are read: never more than a chunk ahead of
    what the storage took, and far less than the file in memory."""
    vault = Vault.open(Sink(MemoryStorage.copy_of(DATA / "vault-gcm")), keys["vault-gcm"])
    total = 50 * 1024 * 1024
    read = {"bytes": 0}
    piece = os.urandom(1 << 20)

    def source():
        for _ in range(total // len(piece)):
            read["bytes"] += len(piece)
            yield piece

    expected = hashlib.sha256()
    for _ in range(total // len(piece)):
        expected.update(piece)
    tracemalloc.start()
    try:
        written = vault.write_file("large.bin", source())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert read["bytes"] == total and written.size == total
    assert written.sha256 == expected.digest()
    assert vault.storage.largest == CHUNK + vault.scheme.overhead
    assert peak < 4 * 1024 * 1024


def failing(data: bytes):
    """A source that breaks off after its first piece (a download that fails midway)."""
    yield data
    raise OSError("the download broke off")


@pytest.mark.parametrize("storage", ["memory", "folder"])
def test_failed_write_leaves_nothing(keys, tmp_path, storage):
    """A long name's entry whose contents fail midway is removed again, as is a new folder whose
    entry cannot be written: the vault lists as before."""
    shutil.copytree(DATA / "vault-gcm", tmp_path / "vault")
    store = MemoryStorage.copy_of(tmp_path / "vault") if storage == "memory" else FolderStorage(tmp_path / "vault")
    vault = Vault.open(store, keys["vault-gcm"])
    before = walk(vault)
    long_name = "long-" * 40 + ".txt"
    with pytest.raises(OSError, match="broke off"):
        vault.write_file(long_name, failing(os.urandom(CHUNK + 1)))
    assert vault.find(long_name) is None
    assert not [
        entry
        for entry in store.list(vault.dir_path(""))
        if entry.name.endswith(".c9s")
        and entry.is_dir
        and "name.c9s" not in {item.name for item in store.list(f"{vault.dir_path('')}/{entry.name}")}
    ]
    original = store.write

    def refuse_dir_files(path, chunks):
        if path.endswith("/dir.c9r"):
            raise OSError("no room")
        original(path, chunks)

    store.write = refuse_dir_files
    with pytest.raises(OSError, match="no room"):
        vault.write_file("new folder/file.txt", [b"x"])
    store.write = original
    assert walk(vault) == before
    folders = {f"{first.name}/{second.name}" for first in store.list("d") for second in store.list(f"d/{first.name}")}
    assert len(folders) == 1 + sum(entry["type"] == "dir" for entry in before.values())


def test_file_object_source(keys, tmp_path):
    """A file object is read in chunks, short reads included."""
    vault = Vault.open(MemoryStorage.copy_of(DATA / "vault-ctrmac"), keys["vault-ctrmac"])
    data = os.urandom(3 * CHUNK + 5)
    (tmp_path / "source").write_bytes(data)
    with open(tmp_path / "source", "rb", buffering=0) as source:
        vault.write_file("from-file.bin", source)
    assert vault.read_bytes("from-file.bin") == data
