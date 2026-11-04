# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cryptomator vaults (vault format 8): files written into a vault in a person's folders, and
read back, as Cryptomator and the sister app *Vaults for Cryptomator* read and write them.

## What it covers

- **Both cipher combinations** of vault format 8, `SIV_GCM` and `SIV_CTRMAC`, with the key file
  (`masterkey.cryptomator`, version 999) as Cryptomator writes it. A vault whose key is kept by
  Cryptomator Hub is refused (`UnsupportedVault`).
- **Unlocking** with the password: scrypt with the key file's parameters, the two keys unwrapped
  (AES key wrap), the version's MAC and the signature of `vault.cryptomator` (a JWT, HS256,
  HS384 or HS512 with both keys) checked. `Vault.key_material` gives the two keys (64 bytes: the
  encryption key, then the MAC key, the order of Cryptomator's recovery key), and `Vault.open`
  opens the vault with them again without the password: a job keeps them, encrypted, instead of
  the password.
- **Names and folders**: a directory's files in `d/XX/YYYY…` (the SHA-1 of its ID encrypted with
  AES-SIV, in base32; the root's ID is the empty text), a name encrypted with AES-SIV (the parent's
  ID as associated data) in base64url as `<name>.c9r`, a name longer than the vault's shortening
  threshold as a folder `<SHA-1 of it>.c9s` with `name.c9s` and `contents.c9r` (a file) or
  `dir.c9r` (a folder), a folder as `<name>.c9r/dir.c9r` holding its new ID, and a new folder's
  directory with its ID's backup `dirid.c9r` (written before the entry that points to it, as the
  sister app and Cryptomator 1.7 do). Names are stored in Unicode NFC.
- **Contents**: a header (its nonce and the file's own content key, encrypted with the vault's
  key) and chunks of 32 KiB, each encrypted and authenticated with its number and the header's
  nonce. `Vault.encrypt_chunks` encrypts a file object or an iterable of bytes as it reads it,
  `Vault.write_file` hands the result to the storage as it is made: a file is never held whole.
- **Reading back**: `Vault.listdir`, `Vault.find`, `Vault.read_chunks`, `Vault.verify_file`.
  Whatever does not authenticate raises `CorruptVault`; a wrong password raises `WrongPassword`.

## The storage

The vault's folder is reached through a small storage (`Storage`): `list(path)` gives a folder's
entries (`StorageEntry`: name, whether a folder, size), `read(path)` a small file's bytes,
`mkdir(path)` makes a folder (`FileExistsError` if there is one), `write(path, chunks)` stores a
file from an iterable of bytes; an optional `stream(path)` gives a large file's bytes in pieces,
and an optional `remove(path)` takes away what a failed write left. Paths are relative to the
vault's folder, with `/`; a missing one raises `FileNotFoundError`. `MemoryStorage` and
`FolderStorage` are the two of the tests; a job's would be Nextcloud's WebDAV, where a file
appears complete or not at all (a PUT is stored under a temporary name and renamed).

## Writing safely

As the sister app, so that the vault never holds what Cryptomator cannot read: a new folder's own
directory and its `dirid.c9r` exist before the entry that points to it, a long name's
`contents.c9r` or `dir.c9r` is stored before its `name.c9s` (and the entry removed again if that
fails, where the storage can remove), and nothing is ever replaced: a name that is taken raises
`NameTaken` (with what has it), and the caller chooses another one (e.g. `free_name`, which
appends " (2)").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import struct
import unicodedata
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NamedTuple, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

CHUNK = 32 * 1024
VAULT_CONFIG = "vault.cryptomator"
MASTERKEY_VERSION = 999
KEY_FILE_PREFIX = "masterkeyfile:"
SHORTENING_THRESHOLD = 220
MAX_NAME_LENGTH = 255
JWT_HASHES = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}
# The longest dir.c9r that is read as a folder's ID (cryptofs' Constants.MAX_DIR_ID_LENGTH).
MAX_DIR_ID_LENGTH = 36


@dataclass(frozen=True)
class Scheme:
    """A cipher combination's layout: the header's and a chunk's nonce, and the bytes a chunk
    gains (its nonce and tag or MAC)."""

    header: int
    nonce: int
    overhead: int


SCHEMES = {"SIV_GCM": Scheme(header=68, nonce=12, overhead=28), "SIV_CTRMAC": Scheme(header=88, nonce=16, overhead=48)}


class VaultError(Exception):
    """A vault that cannot be used as asked; `code` names the reason for the page."""

    code = "vault"


class WrongPassword(VaultError):
    """The password does not unwrap the vault's keys."""

    code = "wrong-password"


class CorruptVault(VaultError):
    """Something in the vault does not authenticate or is not where the format puts it."""

    code = "corrupt"


class UnsupportedVault(VaultError):
    """Another vault format, cipher combination or key (a Hub vault's)."""

    code = "unsupported"


class InvalidName(VaultError):
    """A name no vault can hold: empty, `.` or `..`, with a slash, backslash or control character,
    or longer than 255 characters."""

    code = "invalid-name"


class NotInVault(VaultError):
    """A path that leads to nothing in the vault, or through something that is not a folder."""

    code = "not-found"


class NameTaken(VaultError):
    """The name is taken in its folder: `kind` says by what ("file", "dir" or "link")."""

    code = "exists"

    def __init__(self, name: str, kind: str):
        super().__init__(f"{name} exists ({kind})")
        self.name = name
        self.kind = kind


class StorageEntry(NamedTuple):
    """An entry of a folder of the storage: its name, whether it is a folder, its size."""

    name: str
    is_dir: bool
    size: int = 0


class Storage(Protocol):
    """The vault's folder, as `Vault` reads and writes it (paths relative to it, with `/`)."""

    def list(self, path: str) -> Iterable[StorageEntry]:
        """The entries of a folder; FileNotFoundError if there is none."""

    def read(self, path: str) -> bytes:
        """A file's bytes; FileNotFoundError if there is none."""

    def mkdir(self, path: str) -> None:
        """Makes a folder in an existing one; FileExistsError if the name is taken."""

    def write(self, path: str, chunks: Iterable[bytes]) -> None:
        """Stores a file from its bytes in pieces (consumed as they come)."""


# ----- Encodings -----


def b64url(data: bytes) -> str:
    """Base64url with padding, as Cryptomator writes names."""
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Base64url with or without padding (a JWT's parts have none); ValueError if it is not."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64decode(text: str) -> bytes:
    """Standard base64 (the key file's bytes)."""
    return base64.b64decode(text, validate=True)


def join(*parts: str) -> str:
    """A storage path from its parts (empty parts left out)."""
    return "/".join(part for part in parts if part)


def check_name(name: str) -> str:
    """The name as the vault stores it (NFC); InvalidName if no vault could hold it."""
    normal = unicodedata.normalize("NFC", name)
    if (
        normal in ("", ".", "..")
        or len(normal) > MAX_NAME_LENGTH
        or any(char in "/\\\x7f" or ord(char) < 0x20 for char in normal)
    ):
        raise InvalidName(repr(normal))
    return normal


def split_path(path: str) -> list[str]:
    """A cleartext path's names (`/` between them, leading and doubled ones ignored), checked."""
    return [check_name(part) for part in path.split("/") if part]


def numbered(name: str, number: int) -> str:
    """The name with a number before its extension: "report.pdf", 2 -> "report (2).pdf"."""
    stem, dot, extension = name.rpartition(".")
    if not stem:
        return f"{name} ({number})"
    return f"{stem} ({number}){dot}{extension}"


# ----- AES-SIV (RFC 5297), as Cryptomator's siv-mode -----


def _dbl(block: bytes) -> bytes:
    value = int.from_bytes(block, "big") << 1
    if value >> 128:
        value = (value & ((1 << 128) - 1)) ^ 0x87
    return value.to_bytes(16, "big")


def _xor(first: bytes, second: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(first, second))


def _aes_ctr(key: bytes, counter: bytes, data: bytes) -> bytes:
    """AES-CTR with the whole 16-byte block as the counter (as Java's and Web Crypto's)."""
    encryptor = Cipher(algorithms.AES(key), modes.CTR(counter)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


class Siv:
    """AES-SIV with Cryptomator's keys: the MAC key for S2V (CMAC), the encryption key for CTR.
    Unlike `cryptography`'s AESSIV it takes an empty plaintext, the root directory's ID."""

    def __init__(self, mac_key: bytes, ctr_key: bytes):
        self.mac_key = mac_key
        self.ctr_key = ctr_key

    def _cmac(self, data: bytes) -> bytes:
        mac = cmac.CMAC(algorithms.AES(self.mac_key))
        mac.update(data)
        return mac.finalize()

    def _s2v(self, plaintext: bytes, associated: Iterable[bytes]) -> bytes:
        digest = self._cmac(bytes(16))
        for data in associated:
            digest = _xor(_dbl(digest), self._cmac(data))
        if len(plaintext) >= 16:
            last = _xor(plaintext[-16:], digest)
            return self._cmac(plaintext[:-16] + last)
        padded = plaintext + b"\x80" + bytes(15 - len(plaintext))
        return self._cmac(_xor(_dbl(digest), padded))

    @staticmethod
    def _counter(tag: bytes) -> bytes:
        counter = bytearray(tag)
        counter[8] &= 0x7F
        counter[12] &= 0x7F
        return bytes(counter)

    def encrypt(self, plaintext: bytes, *associated: bytes) -> bytes:
        """The synthetic IV, then the ciphertext."""
        tag = self._s2v(plaintext, associated)
        return tag + _aes_ctr(self.ctr_key, self._counter(tag), plaintext)

    def decrypt(self, ciphertext: bytes, *associated: bytes) -> bytes:
        """The plaintext; CorruptVault if it does not authenticate."""
        if len(ciphertext) < 16:
            raise CorruptVault("encrypted name too short")
        tag = ciphertext[:16]
        plaintext = _aes_ctr(self.ctr_key, self._counter(tag), ciphertext[16:])
        if not hmac.compare_digest(self._s2v(plaintext, associated), tag):
            raise CorruptVault("encrypted name does not authenticate")
        return plaintext


# ----- Keys -----


@dataclass(frozen=True)
class VaultConfig:
    """vault.cryptomator: its JWT's parts and what the vault needs of it."""

    header: dict
    payload: dict
    signed: bytes
    signature: bytes

    @classmethod
    def parse(cls, token: bytes) -> VaultConfig:
        """The configuration, its format checked (not yet its signature)."""
        try:
            parts = token.decode("ascii").strip().split(".")
            if len(parts) != 3:
                raise ValueError("not a JWT")
            header, payload = (json.loads(b64url_decode(part)) for part in parts[:2])
            config = cls(header, payload, f"{parts[0]}.{parts[1]}".encode("ascii"), b64url_decode(parts[2]))
        except ValueError as error:
            raise UnsupportedVault(f"vault.cryptomator unreadable: {error}") from error
        if payload.get("format") != 8:
            raise UnsupportedVault(f"vault format {payload.get('format')}")
        if payload.get("cipherCombo") not in SCHEMES:
            raise UnsupportedVault(f"cipher combination {payload.get('cipherCombo')}")
        if header.get("alg") not in JWT_HASHES:
            raise UnsupportedVault(f"signature {header.get('alg')}")
        threshold = payload.get("shorteningThreshold", SHORTENING_THRESHOLD)
        if not isinstance(threshold, int) or threshold < 36:
            raise UnsupportedVault(f"shortening threshold {threshold}")
        return config

    @classmethod
    def read(cls, storage: Storage) -> VaultConfig:
        """A vault's configuration in its storage, its format checked."""
        try:
            return cls.parse(storage.read(VAULT_CONFIG))
        except FileNotFoundError as error:
            raise UnsupportedVault("no vault.cryptomator: not a vault") from error

    @property
    def key_file(self) -> str:
        """The key file's name; UnsupportedVault for a Hub vault's key."""
        kid = str(self.header.get("kid", ""))
        name = kid.removeprefix(KEY_FILE_PREFIX)
        if not kid.startswith(KEY_FILE_PREFIX) or not name or "/" in name or "\\" in name:
            raise UnsupportedVault(f"key {kid}")
        return name

    def signed_with(self, key_material: bytes) -> bool:
        """Whether the configuration is signed with these keys (it belongs to them)."""
        expected = hmac.new(key_material, self.signed, JWT_HASHES[self.header["alg"]]).digest()
        return hmac.compare_digest(expected, self.signature)


def unwrap_keys(key_file: bytes, password: str) -> bytes:
    """The key material (encryption key, MAC key) of a key file and its password."""
    try:
        data = json.loads(key_file)
        version = int(data["version"])
        salt = b64decode(data["scryptSalt"])
        cost, block_size = int(data["scryptCostParam"]), int(data["scryptBlockSize"])
        wrapped = (b64decode(data["primaryMasterKey"]), b64decode(data["hmacMasterKey"]))
        version_mac = b64decode(data["versionMac"])
    except (ValueError, KeyError, TypeError) as error:
        raise UnsupportedVault(f"key file unreadable: {error}") from error
    if version != MASTERKEY_VERSION:
        raise UnsupportedVault(f"key file version {version}")
    if cost < 2 or cost & (cost - 1) or not 1 <= block_size <= 64 or cost * block_size > 2**24:
        raise UnsupportedVault(f"scrypt parameters N={cost}, r={block_size}")
    password_bytes = unicodedata.normalize("NFC", password).encode("utf-8")
    kek = Scrypt(salt=salt, length=32, n=cost, r=block_size, p=1).derive(password_bytes)
    try:
        enc_key, mac_key = (aes_key_unwrap(kek, key) for key in wrapped)
    except InvalidUnwrap as error:
        raise WrongPassword("the password does not unlock the vault") from error
    if len(enc_key) != 32 or len(mac_key) != 32:
        raise UnsupportedVault("keys of another length")
    if not hmac.compare_digest(hmac.new(mac_key, struct.pack(">i", version), "sha256").digest(), version_mac):
        raise CorruptVault("the key file's version does not authenticate")
    return enc_key + mac_key


# ----- Contents -----


def cleartext_size(scheme: Scheme, size: int) -> int:
    """The size of a file whose encrypted file has this many bytes."""
    payload = size - scheme.header
    if payload <= 0:
        return 0
    full, rest = divmod(payload, CHUNK + scheme.overhead)
    return full * CHUNK + max(0, rest - scheme.overhead)


def rechunk(source: BinaryIO | Iterable[bytes], size: int = CHUNK) -> Iterator[bytes]:
    """A file object's or an iterable's bytes in pieces of exactly `size` (the last shorter),
    nothing for none: read as they are asked for."""
    pieces: Iterable[bytes] = iter(lambda: source.read(size), b"") if hasattr(source, "read") else source
    buffer = bytearray()
    for piece in pieces:
        buffer += piece
        while len(buffer) >= size:
            yield bytes(buffer[:size])
            del buffer[:size]
    if buffer:
        yield bytes(buffer)


class FileCipher:
    """One file's encryption: its header's nonce and content key, and its chunks."""

    def __init__(self, vault: Vault, nonce: bytes, content_key: bytes):
        self.vault = vault
        self.gcm = vault.cipher_combo == "SIV_GCM"
        self.nonce = nonce
        self.content_key = content_key

    @classmethod
    def new(cls, vault: Vault) -> FileCipher:
        """A new nonce and content key."""
        return cls(vault, os.urandom(vault.scheme.nonce), os.urandom(32))

    def header(self) -> bytes:
        """The header: the nonce, 8 reserved bytes (all ones, as Cryptomator writes them) and the
        content key, encrypted (GCM; CTR with an HMAC)."""
        payload = b"\xff" * 8 + self.content_key
        if self.gcm:
            return self.nonce + AESGCM(self.vault.enc_key).encrypt(self.nonce, payload, None)
        encrypted = _aes_ctr(self.vault.enc_key, self.nonce, payload)
        return self.nonce + encrypted + hmac.new(self.vault.mac_key, self.nonce + encrypted, "sha256").digest()

    @classmethod
    def from_header(cls, vault: Vault, header: bytes) -> FileCipher:
        """The cipher of an encrypted file's header; CorruptVault if it does not authenticate."""
        scheme = vault.scheme
        if len(header) != scheme.header:
            raise CorruptVault("file header too short")
        nonce = header[: scheme.nonce]
        if vault.cipher_combo == "SIV_GCM":
            try:
                payload = AESGCM(vault.enc_key).decrypt(nonce, header[scheme.nonce :], None)
            except InvalidTag as error:
                raise CorruptVault("file header does not authenticate") from error
        else:
            mac = hmac.new(vault.mac_key, header[:56], "sha256").digest()
            if not hmac.compare_digest(mac, header[56:]):
                raise CorruptVault("file header does not authenticate")
            payload = _aes_ctr(vault.enc_key, nonce, header[16:56])
        return cls(vault, nonce, payload[8:40])

    def encrypt_chunk(self, number: int, plain: bytes) -> bytes:
        """A chunk: its nonce, ciphertext and tag (GCM, the chunk's number and the header's nonce
        authenticated) or MAC (HMAC-SHA256 of the header's nonce, number, nonce, ciphertext)."""
        nonce = os.urandom(self.vault.scheme.nonce)
        position = struct.pack(">Q", number)
        if self.gcm:
            return nonce + AESGCM(self.content_key).encrypt(nonce, plain, position + self.nonce)
        encrypted = _aes_ctr(self.content_key, nonce, plain)
        mac = hmac.new(self.vault.mac_key, self.nonce + position + nonce + encrypted, "sha256").digest()
        return nonce + encrypted + mac

    def decrypt_chunk(self, number: int, chunk: bytes) -> bytes:
        """A chunk's bytes; CorruptVault if it does not authenticate."""
        scheme = self.vault.scheme
        if len(chunk) < scheme.overhead:
            raise CorruptVault(f"chunk {number} truncated")
        nonce = chunk[: scheme.nonce]
        position = struct.pack(">Q", number)
        if self.gcm:
            try:
                return AESGCM(self.content_key).decrypt(nonce, chunk[scheme.nonce :], position + self.nonce)
            except InvalidTag as error:
                raise CorruptVault(f"chunk {number} does not authenticate") from error
        encrypted = chunk[scheme.nonce : -32]
        mac = hmac.new(self.vault.mac_key, self.nonce + position + nonce + encrypted, "sha256").digest()
        if not hmac.compare_digest(mac, chunk[-32:]):
            raise CorruptVault(f"chunk {number} does not authenticate")
        return _aes_ctr(self.content_key, nonce, encrypted)


# ----- The vault -----


@dataclass
class Node:
    """An entry of the vault: its (NFC) name, kind ("file", "dir" or "link"), `path` (its entry
    in the storage: the .c9r file or folder, or the .c9s folder), `contents` (a file's encrypted
    file), `dir_id` (a folder's) and `size` (a file's, decrypted)."""

    name: str
    kind: str
    path: str
    contents: str = ""
    dir_id: str = ""
    size: int = 0


@dataclass(frozen=True)
class EntryPath:
    """Where an entry of a name lives: its folder (d/XX/YYYY…), its path, whether its name is
    shortened (.c9s) and its encrypted name (with .c9r)."""

    folder: str
    path: str
    long: bool
    full_name: str


@dataclass(frozen=True)
class Written:
    """A file written: its entry, size and SHA-256 (for `Vault.verify_file`)."""

    node: Node
    size: int
    sha256: bytes


class Vault:
    """An unlocked vault in a storage (`Vault.unlock` with the password, `Vault.open` with the
    key material)."""

    def __init__(self, storage: Storage, config: VaultConfig, key_material: bytes):
        if len(key_material) != 64:
            raise ValueError("the key material is 64 bytes")
        if not config.signed_with(key_material):
            raise CorruptVault("vault.cryptomator is not signed with these keys")
        self.storage = storage
        self.config = config
        self.enc_key = key_material[:32]
        self.mac_key = key_material[32:]
        self.siv = Siv(self.mac_key, self.enc_key)
        self._dir_paths: dict[str, str] = {}

    @classmethod
    def unlock(cls, storage: Storage, password: str) -> Vault:
        """The vault, unlocked with its password (WrongPassword if it is not the vault's)."""
        config = VaultConfig.read(storage)
        try:
            key_file = storage.read(config.key_file)
        except FileNotFoundError as error:
            raise CorruptVault(f"no key file {config.key_file}") from error
        return cls(storage, config, unwrap_keys(key_file, password))

    @classmethod
    def open(cls, storage: Storage, key_material: bytes) -> Vault:
        """The vault, unlocked with its key material (`key_material`): CorruptVault if
        vault.cryptomator is not signed with it."""
        return cls(storage, VaultConfig.read(storage), key_material)

    @property
    def key_material(self) -> bytes:
        """The vault's two keys, 64 bytes: the encryption key, then the MAC key. Whoever has them
        can read and write the vault without its password: keep them as a password is kept."""
        return self.enc_key + self.mac_key

    @property
    def cipher_combo(self) -> str:
        """SIV_GCM or SIV_CTRMAC."""
        return self.config.payload["cipherCombo"]

    @property
    def scheme(self) -> Scheme:
        """The cipher combination's layout."""
        return SCHEMES[self.cipher_combo]

    @property
    def shortening_threshold(self) -> int:
        """The longest encrypted name (with .c9r) not shortened to a .c9s folder."""
        return self.config.payload.get("shorteningThreshold", SHORTENING_THRESHOLD)

    # ----- Names -----

    def dir_path(self, dir_id: str) -> str:
        """A directory's folder in the storage: d/XX/YYYY… from its ID's hash."""
        if dir_id not in self._dir_paths:
            digest = hashlib.sha1(self.siv.encrypt(dir_id.encode("utf-8"))).digest()
            hashed = base64.b32encode(digest).decode("ascii")
            self._dir_paths[dir_id] = join("d", hashed[:2], hashed[2:])
        return self._dir_paths[dir_id]

    def _encrypt_name(self, name: str, dir_id: str) -> str:
        """A name's encrypted name in a directory, with .c9r (not shortened)."""
        return b64url(self.siv.encrypt(name.encode("utf-8"), dir_id.encode("utf-8"))) + ".c9r"

    def _decrypt_name(self, encrypted: str, dir_id: str) -> str:
        """The name in an encrypted name (with or without .c9r); CorruptVault if it is none."""
        try:
            data = b64url_decode(encrypted.removesuffix(".c9r"))
        except ValueError as error:
            raise CorruptVault("not an encrypted name") from error
        try:
            return self.siv.decrypt(data, dir_id.encode("utf-8")).decode("utf-8")
        except UnicodeDecodeError as error:
            raise CorruptVault("an encrypted name not in UTF-8") from error

    def entry_path(self, dir_id: str, name: str) -> EntryPath:
        """Where the entry of a (checked) name lives in a directory."""
        folder = self.dir_path(dir_id)
        full_name = self._encrypt_name(name, dir_id)
        if len(full_name) > self.shortening_threshold:
            short = b64url(hashlib.sha1(full_name.encode("ascii")).digest()) + ".c9s"
            return EntryPath(folder, join(folder, short), True, full_name)
        return EntryPath(folder, join(folder, full_name), False, full_name)

    # ----- Reading -----

    def _list(self, path: str) -> list[StorageEntry]:
        try:
            return list(self.storage.list(path))
        except FileNotFoundError as error:
            raise CorruptVault(f"{path} is missing") from error

    def _node(self, name: str, entry: StorageEntry, path: str, long: bool) -> Node:
        """The node of an entry found in the storage (path: its .c9r or .c9s)."""
        if not entry.is_dir:
            if long:
                raise CorruptVault(f"{path} is not a folder")
            return Node(name, "file", path, contents=path, size=cleartext_size(self.scheme, entry.size))
        inside = {item.name: item for item in self._list(path)}
        if long and "contents.c9r" in inside and not inside["contents.c9r"].is_dir:
            contents = join(path, "contents.c9r")
            return Node(
                name, "file", path, contents=contents, size=cleartext_size(self.scheme, inside["contents.c9r"].size)
            )
        if "dir.c9r" in inside:
            dir_id = self.storage.read(join(path, "dir.c9r"))
            if len(dir_id) > MAX_DIR_ID_LENGTH:
                raise CorruptVault(f"{path}/dir.c9r is too long for a folder's ID")
            return Node(name, "dir", path, dir_id=dir_id.decode("utf-8").strip())
        if "symlink.c9r" in inside:
            return Node(name, "link", path)
        raise CorruptVault(f"{path} is neither a file, a folder nor a link")

    def listdir(self, path: str = "") -> list[Node]:
        """A folder's entries, by name: those whose names do not decrypt (dirid.c9r, a sync
        tool's copies) left out, as Cryptomator leaves them out."""
        folder = self.find(path)
        if folder is None or folder.kind != "dir":
            raise NotInVault(path)
        storage_folder = self.dir_path(folder.dir_id)
        nodes = []
        for entry in self._list(storage_folder):
            entry_path = join(storage_folder, entry.name)
            if entry.name.endswith(".c9s") and entry.is_dir:
                try:
                    full_name = self.storage.read(join(entry_path, "name.c9s")).decode("ascii").strip()
                    name = self._decrypt_name(full_name, folder.dir_id)
                except (FileNotFoundError, UnicodeDecodeError, CorruptVault):
                    continue
                nodes.append(self._node(name, entry, entry_path, True))
            elif entry.name.endswith(".c9r"):
                try:
                    name = self._decrypt_name(entry.name, folder.dir_id)
                except CorruptVault:
                    continue
                nodes.append(self._node(name, entry, entry_path, False))
        return sorted(nodes, key=lambda node: node.name)

    def _child(self, dir_id: str, name: str) -> Node | None:
        """The entry of a name in a directory, or None: found by its encrypted name."""
        entry = self.entry_path(dir_id, name)
        basename = entry.path.rsplit("/", 1)[1]
        for item in self._list(entry.folder):
            if item.name == basename:
                return self._node(name, item, entry.path, entry.long)
        return None

    def find(self, path: str) -> Node | None:
        """The entry of a cleartext path ("" is the vault's top folder), or None."""
        node = Node("", "dir", self.dir_path(""), dir_id="")
        for name in split_path(path):
            if node.kind != "dir":
                raise NotInVault(f"{node.name} is not a folder")
            child = self._child(node.dir_id, name)
            if child is None:
                return None
            node = child
        return node

    def decrypt_chunks(self, data: Iterable[bytes]) -> Iterator[bytes]:
        """An encrypted file's bytes (in pieces of any size) decrypted, chunk by chunk, each
        authenticated before it is given: nothing for an empty file."""
        scheme = self.scheme
        size = CHUNK + scheme.overhead
        buffer = bytearray()
        cipher = None
        number = 0
        for piece in data:
            buffer += piece
            if cipher is None:
                if len(buffer) < scheme.header:
                    continue
                cipher = FileCipher.from_header(self, bytes(buffer[: scheme.header]))
                del buffer[: scheme.header]
            while len(buffer) >= size:
                yield cipher.decrypt_chunk(number, bytes(buffer[:size]))
                del buffer[:size]
                number += 1
        if cipher is None:
            if buffer:
                raise CorruptVault("file header too short")
            return
        if buffer:
            yield cipher.decrypt_chunk(number, bytes(buffer))

    def read_chunks(self, path: str | Node) -> Iterator[bytes]:
        """A file's decrypted bytes in chunks (the storage's `stream`, if it has one)."""
        node = self.find(path) if isinstance(path, str) else path
        if node is None or node.kind != "file":
            raise NotInVault(str(path))
        stream = getattr(self.storage, "stream", None)
        return self.decrypt_chunks(stream(node.contents) if stream else [self.storage.read(node.contents)])

    def read_bytes(self, path: str | Node) -> bytes:
        """A (small) file's decrypted bytes."""
        return b"".join(self.read_chunks(path))

    def verify_file(self, written: Written) -> None:
        """Reads a written file back: CorruptVault unless it decrypts to what was written."""
        digest = hashlib.sha256()
        size = 0
        for chunk in self.read_chunks(written.node):
            digest.update(chunk)
            size += len(chunk)
        if size != written.size or digest.digest() != written.sha256:
            raise CorruptVault(f"{written.node.name} differs from what was written")

    # ----- Writing -----

    def encrypted_size(self, size: int) -> int:
        """The size of a file of this many bytes, encrypted."""
        return self.scheme.header + size + -(-size // CHUNK) * self.scheme.overhead

    def encrypt_chunks(self, source: BinaryIO | Iterable[bytes], digest=None) -> Iterator[bytes]:
        """A file's encrypted bytes, its header first, then each chunk: the source (a file
        object, or an iterable of bytes) read as the result is consumed. `digest` (a hashlib
        object), if given, is updated with the plain bytes."""
        cipher = FileCipher.new(self)
        yield cipher.header()
        for number, plain in enumerate(rechunk(source)):
            if digest is not None:
                digest.update(plain)
            yield cipher.encrypt_chunk(number, plain)

    def _mkdir_quiet(self, path: str) -> None:
        try:
            self.storage.mkdir(path)
        except FileExistsError:
            pass

    def _write(self, path: str, data: bytes) -> None:
        self.storage.write(path, [data])

    def _discard(self, path: str) -> None:
        """Removes what a failed write left (the storage's `remove`, if it has one)."""
        remove = getattr(self.storage, "remove", None)
        if remove is not None:
            try:
                remove(path)
            except OSError:
                pass

    def create_dir(self, parent: Node, name: str) -> Node:
        """A new folder in a folder: its own directory (with its ID's backup, dirid.c9r) made
        before its entry. NameTaken if the name is taken."""
        name = check_name(name)
        entry = self.entry_path(parent.dir_id, name)
        taken = self._child(parent.dir_id, name)
        if taken is not None:
            raise NameTaken(name, taken.kind)
        dir_id = str(uuid.uuid4())
        own = self.dir_path(dir_id)
        self._mkdir_quiet(own.rsplit("/", 1)[0])
        self.storage.mkdir(own)
        made_entry = False
        try:
            self.storage.write(join(own, "dirid.c9r"), self.encrypt_chunks([dir_id.encode("ascii")]))
            try:
                self.storage.mkdir(entry.path)
            except FileExistsError as error:
                raise NameTaken(name, "unknown") from error
            made_entry = True
            self._write(join(entry.path, "dir.c9r"), dir_id.encode("ascii"))
            if entry.long:
                self._write(join(entry.path, "name.c9s"), entry.full_name.encode("ascii"))
        except BaseException:
            if made_entry:
                self._discard(entry.path)
            self._discard(own)
            raise
        return Node(name, "dir", entry.path, dir_id=dir_id)

    def makedirs(self, path: str) -> Node:
        """The folder of a cleartext path, with the folders along it made where missing.
        NameTaken if a file or link has one of the names."""
        node = Node("", "dir", self.dir_path(""), dir_id="")
        for name in split_path(path):
            child = self._child(node.dir_id, name)
            if child is None:
                child = self.create_dir(node, name)
            elif child.kind != "dir":
                raise NameTaken(name, child.kind)
            node = child
        return node

    def free_name(self, folder: str, name: str) -> str:
        """The name, or the first of "name (2)", "name (3)"… not taken in the folder."""
        parent = self.find(folder)
        if parent is None or parent.kind != "dir":
            return check_name(name)
        names = {node.name for node in self.listdir(folder)}
        candidate, number = check_name(name), 1
        while candidate in names:
            number += 1
            candidate = check_name(numbered(name, number))
        return candidate

    def write_file(self, path: str, source: BinaryIO | Iterable[bytes]) -> Written:
        """Writes a file at a cleartext path (the folders along it made where missing), its
        contents encrypted as the storage takes them. NameTaken if the name is taken: nothing is
        replaced."""
        *folders, name = split_path(path) or [""]
        name = check_name(name)
        parent = self.makedirs("/".join(folders))
        taken = self._child(parent.dir_id, name)
        if taken is not None:
            raise NameTaken(name, taken.kind)
        entry = self.entry_path(parent.dir_id, name)
        digest = hashlib.sha256()
        size = 0

        def counted() -> Iterator[bytes]:
            nonlocal size
            for chunk in self.encrypt_chunks(source, digest):
                size += len(chunk)
                yield chunk

        contents = entry.path
        if entry.long:
            try:
                self.storage.mkdir(entry.path)
            except FileExistsError as error:
                raise NameTaken(name, "unknown") from error
            contents = join(entry.path, "contents.c9r")
        try:
            self.storage.write(contents, counted())
            if entry.long:
                self._write(join(entry.path, "name.c9s"), entry.full_name.encode("ascii"))
        except BaseException:
            if entry.long:
                self._discard(entry.path)
            raise
        plain_size = cleartext_size(self.scheme, size)
        return Written(Node(name, "file", entry.path, contents=contents, size=plain_size), plain_size, digest.digest())


# ----- Storages of the tests -----


class MemoryStorage:
    """A vault's folder in memory: files (path -> bytes) and folders."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {""}
        for path, data in (files or {}).items():
            parts = path.split("/")
            for end in range(1, len(parts)):
                self.dirs.add("/".join(parts[:end]))
            self.files[path] = data

    @classmethod
    def copy_of(cls, folder: str | os.PathLike) -> MemoryStorage:
        """A folder of the file system's files, in memory."""
        root = Path(folder)
        storage = cls(
            {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        )
        storage.dirs.update(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())
        return storage

    def list(self, path: str) -> list[StorageEntry]:
        """The entries of a folder."""
        if path not in self.dirs:
            raise FileNotFoundError(path)
        prefix = f"{path}/" if path else ""
        dirs = [StorageEntry(name[len(prefix) :], True) for name in self.dirs if self._child_of(prefix, name)]
        files = [
            StorageEntry(name[len(prefix) :], False, len(data))
            for name, data in self.files.items()
            if self._child_of(prefix, name)
        ]
        return dirs + files

    @staticmethod
    def _child_of(prefix: str, name: str) -> bool:
        return bool(name) and name.startswith(prefix) and "/" not in name[len(prefix) :]

    def read(self, path: str) -> bytes:
        """A file's bytes."""
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def mkdir(self, path: str) -> None:
        """Makes a folder."""
        if path in self.dirs or path in self.files:
            raise FileExistsError(path)
        if path.rpartition("/")[0] not in self.dirs:
            raise FileNotFoundError(path)
        self.dirs.add(path)

    def write(self, path: str, chunks: Iterable[bytes]) -> None:
        """Stores a file (complete once all its pieces are in)."""
        if path.rpartition("/")[0] not in self.dirs or path in self.dirs:
            raise FileNotFoundError(path)
        self.files[path] = b"".join(chunks)

    def remove(self, path: str) -> None:
        """Removes a file, or a folder with everything in it."""
        self.files.pop(path, None)
        self.files = {name: data for name, data in self.files.items() if not name.startswith(f"{path}/")}
        self.dirs = {name for name in self.dirs if name != path and not name.startswith(f"{path}/")}


class FolderStorage:
    """A vault's folder in the file system; a file is written under a temporary name and renamed,
    as Nextcloud stores an upload."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def list(self, path: str) -> list[StorageEntry]:
        """The entries of a folder."""
        with os.scandir(self.root / path) as entries:
            return [
                StorageEntry(entry.name, entry.is_dir(), 0 if entry.is_dir() else entry.stat().st_size)
                for entry in entries
            ]

    def read(self, path: str) -> bytes:
        """A file's bytes."""
        return (self.root / path).read_bytes()

    def stream(self, path: str) -> Iterator[bytes]:
        """A file's bytes in pieces of 1 MiB."""
        with open(self.root / path, "rb") as file:
            yield from iter(lambda: file.read(1 << 20), b"")

    def mkdir(self, path: str) -> None:
        """Makes a folder."""
        (self.root / path).mkdir()

    def remove(self, path: str) -> None:
        """Removes a file, or a folder with everything in it."""
        target = self.root / path
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    def write(self, path: str, chunks: Iterable[bytes]) -> None:
        """Stores a file from its pieces."""
        target = self.root / path
        temporary = target.with_name(f".{target.name}.part")
        try:
            with open(temporary, "wb") as file:
                for chunk in chunks:
                    file.write(chunk)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
