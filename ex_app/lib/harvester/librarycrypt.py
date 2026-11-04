# SPDX-License-Identifier: AGPL-3.0-or-later
"""The private library's encryption (library.py): the key hierarchy and the file format, spelled
out in docs/developer.md (The private library's format).

- Each person's library has an X25519 key pair. The public key is stored as it is: whatever
  writes into the library (a job, an upload, a move from Files) needs only it, so writes work
  while the library is locked. The private key is stored twice, wrapped: with a key derived from
  the library password (scrypt, its costs stored beside it) and with one derived from the recovery
  key (160 random bits, shown once; HKDF). Unwrapped, it lives in the server's memory only, for an
  unlocked session (librarykeys.py).
- Each file (and folder) has a random 32-byte secret, sealed to the public key (seal: ephemeral
  X25519, HKDF-SHA256, AES-256-GCM). Its subkeys (HKDF): one for the content, one for the
  metadata (name, size, type) and one for the thumbnail.
- The content is encrypted in chunks of CHUNK bytes, each with AES-256-GCM, its nonce the chunk's
  index, its associated data the format's magic, the blob's id, the index and whether it is the
  last chunk: a chunk swapped, moved, dropped or cut off is found (Tampered). The last chunk is
  padded to CHUNK, so that a file's size on the disk tells only its number of chunks; its exact
  size is in its encrypted metadata.

A blob on the disk: MAGIC, the blob's id (16 bytes), then the chunks (CHUNK + 16 bytes each).
"""

import base64
import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"HVL1"
BLOB_ID = 16
HEADER = len(MAGIC) + BLOB_ID
CHUNK = 64 * 1024
TAG = 16
NONCE = 12
# The password's key: scrypt with these costs (some 32 MB, a tenth of a second), stored with the
# wrapped key, so that later costs leave earlier libraries readable.
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
SCRYPT_MAXMEM = 128 * 1024**2
# The recovery key's bits (a multiple of 40: base32 without padding).
RECOVERY_BYTES = 20
BASE32_LOOKALIKES = str.maketrans({"0": "O", "1": "I", "8": "B"})


class Tampered(ValueError):
    """Encrypted data that does not open with its key: changed, cut off, swapped or wrongly keyed."""


def _raw(public: X25519PublicKey) -> bytes:
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _hkdf(secret: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt, info).derive(secret)


def new_key_pair() -> tuple[X25519PrivateKey, bytes]:
    """A library's key pair: the private key, and the public key's 32 bytes."""
    private = X25519PrivateKey.generate()
    return private, _raw(private.public_key())


def private_bytes(private: X25519PrivateKey) -> bytes:
    """The private key's 32 bytes (to wrap them)."""
    return private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )


def public_of(private: X25519PrivateKey) -> bytes:
    """The public key's 32 bytes of a private key."""
    return _raw(private.public_key())


def seal(public: bytes, data: bytes, aad: bytes) -> bytes:
    """data encrypted to the public key (ECIES): the ephemeral public key, the nonce and the
    AES-GCM ciphertext; aad is bound to it (a sealed key moved to another place does not open)."""
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(public))
    ephemeral_public = _raw(ephemeral.public_key())
    key = _hkdf(shared, ephemeral_public + public, b"harvester library seal v1")
    nonce = secrets.token_bytes(NONCE)
    return ephemeral_public + nonce + AESGCM(key).encrypt(nonce, data, aad)


def unseal(private: X25519PrivateKey, sealed: bytes, aad: bytes) -> bytes:
    """What seal sealed, with the private key; Tampered if it does not open."""
    if len(sealed) < 32 + NONCE + TAG:
        raise Tampered("too short")
    ephemeral_public, nonce, body = sealed[:32], sealed[32 : 32 + NONCE], sealed[32 + NONCE :]
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
        key = _hkdf(shared, ephemeral_public + public_of(private), b"harvester library seal v1")
        return AESGCM(key).decrypt(nonce, body, aad)
    except (InvalidTag, ValueError) as error:
        raise Tampered("a sealed value does not open") from error


def subkey(secret: bytes, purpose: str) -> bytes:
    """A file's key for one purpose (content, meta, thumb) from its secret."""
    return _hkdf(secret, b"", b"harvester library " + purpose.encode())


def encrypt_small(key: bytes, data: bytes, aad: bytes) -> bytes:
    """A small value (a name, a thumbnail) encrypted: a random nonce and the ciphertext."""
    nonce = secrets.token_bytes(NONCE)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def decrypt_small(key: bytes, data: bytes, aad: bytes) -> bytes:
    """What encrypt_small encrypted; Tampered if it does not open."""
    try:
        return AESGCM(key).decrypt(data[:NONCE], data[NONCE:], aad)
    except (InvalidTag, ValueError) as error:
        raise Tampered("an encrypted value does not open") from error


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def wrap_with_password(private: X25519PrivateKey, password: str, aad: bytes) -> str:
    """The private key wrapped with a key derived from the password (scrypt), as stored (JSON)."""
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(NONCE)
    key = hashlib.scrypt(password.encode(), salt=salt, maxmem=SCRYPT_MAXMEM, dklen=32, **SCRYPT)
    wrapped = AESGCM(key).encrypt(nonce, private_bytes(private), aad)
    return json.dumps({"kdf": "scrypt", **SCRYPT, "salt": _b64(salt), "nonce": _b64(nonce), "key": _b64(wrapped)})


def unwrap_with_password(stored: str, password: str, aad: bytes) -> X25519PrivateKey:
    """The private key wrap_with_password wrapped; Tampered for a wrong password."""
    try:
        kept = json.loads(stored)
        if kept["kdf"] != "scrypt":
            raise Tampered("unknown key derivation")
        key = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(kept["salt"]),
            n=int(kept["n"]),
            r=int(kept["r"]),
            p=int(kept["p"]),
            maxmem=SCRYPT_MAXMEM,
            dklen=32,
        )
        raw = AESGCM(key).decrypt(base64.b64decode(kept["nonce"]), base64.b64decode(kept["key"]), aad)
    except (InvalidTag, ValueError, KeyError, TypeError) as error:
        raise Tampered("wrong password") from error
    return X25519PrivateKey.from_private_bytes(raw)


def new_recovery_key() -> str:
    """A recovery key as shown: 160 random bits in base32, in groups of four (ABCD-EFGH-…)."""
    text = base64.b32encode(secrets.token_bytes(RECOVERY_BYTES)).decode()
    return "-".join(text[index : index + 4] for index in range(0, len(text), 4))


def recovery_bytes(text: str) -> bytes:
    """A recovery key's bits as typed (any case, with or without dashes and spaces; 0, 1 and 8
    read as O, I and B); Tampered for one that is no recovery key."""
    cleaned = "".join(char for char in text.upper() if char.isalnum()).translate(BASE32_LOOKALIKES)
    try:
        raw = base64.b32decode(cleaned)
    except ValueError as error:
        raise Tampered("not a recovery key") from error
    if len(raw) != RECOVERY_BYTES:
        raise Tampered("not a recovery key")
    return raw


def wrap_with_recovery(private: X25519PrivateKey, recovery: str, aad: bytes) -> str:
    """The private key wrapped with a key derived from the recovery key (HKDF: its 160 random bits
    need no slow derivation), as stored (JSON)."""
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(NONCE)
    key = _hkdf(recovery_bytes(recovery), salt, b"harvester library recovery")
    wrapped = AESGCM(key).encrypt(nonce, private_bytes(private), aad)
    return json.dumps({"kdf": "hkdf", "salt": _b64(salt), "nonce": _b64(nonce), "key": _b64(wrapped)})


def unwrap_with_recovery(stored: str, recovery: str, aad: bytes) -> X25519PrivateKey:
    """The private key wrap_with_recovery wrapped; Tampered for a wrong recovery key."""
    try:
        kept = json.loads(stored)
        key = _hkdf(recovery_bytes(recovery), base64.b64decode(kept["salt"]), b"harvester library recovery")
        raw = AESGCM(key).decrypt(base64.b64decode(kept["nonce"]), base64.b64decode(kept["key"]), aad)
    except (InvalidTag, ValueError, KeyError, TypeError) as error:
        raise Tampered("wrong recovery key") from error
    return X25519PrivateKey.from_private_bytes(raw)


def chunks(size: int) -> int:
    """The number of chunks of a file of size bytes (an empty file has one, all padding)."""
    return max(1, -(-size // CHUNK))


def stored_size(size: int) -> int:
    """The bytes on the disk of a file of size bytes."""
    return HEADER + chunks(size) * (CHUNK + TAG)


def _aad(blob: bytes, index: int, last: bool) -> bytes:
    return MAGIC + blob + index.to_bytes(8, "big") + (b"\x01" if last else b"\x00")


def _nonce(index: int) -> bytes:
    # Each content key encrypts one blob once: the chunk's index is a nonce used once.
    return index.to_bytes(NONCE, "big")


class Sealer:
    """Encrypts a file's content into out as it comes (write), a chunk at a time, never the whole
    file in memory; finish pads and writes the last chunk. It keeps the plaintext's SHA-256 (to
    check a copy against) and, up to keep bytes, the plaintext itself (a picture's thumbnail)."""

    def __init__(self, out: BinaryIO, key: bytes, blob: bytes, keep: int = 0):
        self.out = out
        self.aead = AESGCM(key)
        self.blob = blob
        self.buffer = bytearray()
        self.index = 0
        self.size = 0
        self.digest = hashlib.sha256()
        self.keep = keep
        self.kept: bytearray | None = bytearray() if keep else None
        out.write(MAGIC + blob)

    def write(self, data: bytes) -> int:
        """Takes the next bytes of the file (a file object's write, for download2stream)."""
        self.size += len(data)
        self.digest.update(data)
        if self.kept is not None:
            self.kept += data
            if len(self.kept) > self.keep:
                self.kept = None
        self.buffer += data
        # The chunk that might be the last stays until more comes (or finish).
        while len(self.buffer) > CHUNK:
            self._chunk(bytes(self.buffer[:CHUNK]), False)
            del self.buffer[:CHUNK]
        return len(data)

    def _chunk(self, plain: bytes, last: bool) -> None:
        self.out.write(self.aead.encrypt(_nonce(self.index), plain, _aad(self.blob, self.index, last)))
        self.index += 1

    def finish(self) -> int:
        """Writes the last chunk (padded); the file's size."""
        self._chunk(bytes(self.buffer) + bytes(CHUNK - len(self.buffer)), True)
        self.buffer.clear()
        return self.size


def opened(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    path: str | os.PathLike, key: bytes, blob: bytes, size: int, start: int, end: int
) -> Iterator[bytes]:
    """The bytes start to end (inclusive) of a blob's file, decrypted: only the chunks that hold
    them are read. Tampered if the file is not the blob's, has another length than its size gives,
    or a chunk does not open (changed, swapped, cut off)."""
    aead = AESGCM(key)
    count = chunks(size)
    with open(path, "rb") as file:
        if file.read(HEADER) != MAGIC + blob:
            raise Tampered("not this file's blob")
        if os.fstat(file.fileno()).st_size != stored_size(size):
            raise Tampered("a blob of another length")
        end = min(end, size - 1)
        if end < start:
            return
        first, last = start // CHUNK, end // CHUNK
        file.seek(HEADER + first * (CHUNK + TAG))
        for index in range(first, last + 1):
            sealed = file.read(CHUNK + TAG)
            try:
                plain = aead.decrypt(_nonce(index), sealed, _aad(blob, index, index == count - 1))
            except InvalidTag as error:
                raise Tampered(f"chunk {index} does not open") from error
            low = start - index * CHUNK if index == first else 0
            high = end - index * CHUNK + 1 if index == last else CHUNK
            yield plain[low:high]
