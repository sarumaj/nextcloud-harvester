# SPDX-License-Identifier: AGPL-3.0-or-later
"""The private library's keys (librarycrypt.py) per person, and the unlocked sessions.

A person sets their library up once, with a password and a recovery key the server proposes
(propose) and the person types back (set_up): that makes the key pair, stores the public key and
the private key wrapped twice (with the password, with the recovery key), and a random lookup key
(library.py: a path's HMAC, so that a job finds the file it saved while the library is locked).

Unlocking (unlock, recover) unwraps the private key into a Session, kept in this process's
memory only, by a random token, for the person's minutes without use; lock, a lapse and a
restart drop it. The private key is never written anywhere. Wrong passwords and recovery keys in
a row make the next try wait (MAX_FAILED, FAILED_WAIT)."""

import dataclasses
import secrets
import threading
import time

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from . import librarycrypt as crypt
from .l10n import said
from .store import Store

SCHEMA = """
-- The private library's keys per person (librarykeys.py): the public key, the private key wrapped
-- with the password and with the recovery key, the paths' lookup key, the minutes it stays
-- unlocked without use.
CREATE TABLE IF NOT EXISTS library_keys (
    user TEXT PRIMARY KEY,
    public BLOB NOT NULL,
    by_password TEXT NOT NULL,
    by_recovery TEXT NOT NULL,
    lookup BLOB NOT NULL,
    minutes INTEGER NOT NULL,
    at REAL NOT NULL
);
"""
# Unlocked for so many minutes without use, by default; the person's choice from 1 to a day.
LOCK_MINUTES = 15
MAX_LOCK_MINUTES = 24 * 60
MIN_PASSWORD = 8
# Wrong passwords (or recovery keys) in a row before the next try waits FAILED_WAIT seconds.
MAX_FAILED = 5
FAILED_WAIT = 60
# A proposed recovery key waits so long for the person to type it back.
PROPOSAL_SECONDS = 30 * 60


class Locked(Exception):
    """The person's library is locked (or a wrong password was given): the routes answer 403."""


class NotSetUp(Exception):
    """The person has not set their library up (no password, no keys): nothing can be written."""


def _aad(user: str, public: bytes) -> bytes:
    # The wrapped private key belongs to this person's public key.
    return b"harvester library private key|" + user.encode() + b"|" + public


@dataclasses.dataclass
class Session:
    """An unlocked library: its person, private key, when it lapses, its minutes, and what was
    decrypted with it (library.py's cache), all dropped at lock."""

    user: str
    private: X25519PrivateKey | None
    expires: float
    minutes: int
    cache: dict = dataclasses.field(default_factory=dict)

    def drop(self) -> None:
        """Forgets the private key and everything decrypted with it."""
        self.private = None
        self.cache.clear()


class Keyring:
    """The people's library keys and the unlocked sessions."""

    def __init__(self, store: Store):
        self.store = store
        store.tables(SCHEMA)
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        # By user: [wrong tries in a row, the time of the last].
        self._failed: dict[str, list] = {}
        # By user: [the recovery key proposed, when the proposal lapses].
        self._proposals: dict[str, list] = {}

    def _row(self, user: str):
        rows = self.store.rows("SELECT * FROM library_keys WHERE user = ?", (user,))
        return rows[0] if rows else None

    def is_set_up(self, user: str) -> bool:
        """Whether the person has set their library up."""
        return self._row(user) is not None

    def minutes(self, user: str) -> int:
        """The minutes the person's library stays unlocked without use."""
        row = self._row(user)
        return int(row["minutes"]) if row else LOCK_MINUTES

    def public(self, user: str) -> tuple[bytes, bytes]:
        """The person's public key and lookup key (what writing needs); NotSetUp without them."""
        row = self._row(user)
        if row is None:
            raise NotSetUp(
                said("Set up your library on Harvester's page (Library) first: its files are encrypted with your key.")
            )
        return bytes(row["public"]), bytes(row["lookup"])

    def propose(self, user: str) -> str:
        """A new recovery key for the person's setup (until it lapses); Locked if set up already."""
        if self.is_set_up(user):
            raise Locked(said("Your library is set up already."))
        recovery = crypt.new_recovery_key()
        with self._lock:
            self._proposals[user] = [recovery, time.monotonic() + PROPOSAL_SECONDS]
        return recovery

    def set_up(self, user: str, password: str, recovery: str, minutes: int) -> str:
        """Sets the person's library up with the password and the proposed recovery key, typed
        back (Locked if it is not the one proposed): its keys made and stored; a session's token."""
        with self._lock:
            proposal = self._proposals.get(user)
        try:
            typed = crypt.recovery_bytes(recovery)
        except crypt.Tampered as error:
            raise Locked(said("That is not the recovery key shown.")) from error
        if proposal is None or proposal[1] < time.monotonic() or typed != crypt.recovery_bytes(proposal[0]):
            raise Locked(said("That is not the recovery key shown."))
        private, public = crypt.new_key_pair()
        minutes = min(max(1, int(minutes)), MAX_LOCK_MINUTES)
        with self.store.writing() as db:
            if db.execute("SELECT 1 FROM library_keys WHERE user = ?", (user,)).fetchone():
                raise Locked(said("Your library is set up already."))
            db.execute(
                "INSERT INTO library_keys (user, public, by_password, by_recovery, lookup, minutes, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user,
                    public,
                    crypt.wrap_with_password(private, password, _aad(user, public)),
                    crypt.wrap_with_recovery(private, proposal[0], _aad(user, public)),
                    secrets.token_bytes(32),
                    minutes,
                    time.time(),
                ),
            )
        with self._lock:
            self._proposals.pop(user, None)
        return self._open(user, private, minutes)

    def _open(self, user: str, private: X25519PrivateKey, minutes: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep()
            self._sessions[token] = Session(user, private, time.monotonic() + 60 * minutes, minutes)
        return token

    def _sweep(self) -> None:
        """Drops the lapsed sessions (under _lock)."""
        now = time.monotonic()
        for token in [token for token, session in self._sessions.items() if session.expires < now]:
            self._sessions.pop(token).drop()

    def _tried(self, user: str, attempt) -> X25519PrivateKey:
        """The private key attempt unwraps; Locked for a wrong password or recovery key, and after
        MAX_FAILED of them in a row until FAILED_WAIT has passed."""
        now = time.monotonic()
        with self._lock:
            count, last = self._failed.get(user, [0, 0.0])
            if count >= MAX_FAILED and now - last < FAILED_WAIT:
                raise Locked(said("Too many wrong passwords: try again in a minute."))
        try:
            private = attempt()
        except crypt.Tampered as error:
            with self._lock:
                self._failed[user] = [count + 1, now]
            raise Locked(said("Wrong password.")) from error
        with self._lock:
            self._failed.pop(user, None)
        return private

    def _unwrapped(self, user: str, password: str):
        row = self._row(user)
        if row is None:
            raise Locked(said("Your library is not set up yet."))
        aad = _aad(user, bytes(row["public"]))
        return row, self._tried(user, lambda: crypt.unwrap_with_password(row["by_password"], password, aad))

    def unlock(self, user: str, password: str) -> str:
        """Unlocks the person's library: a session's token; Locked for a wrong password."""
        row, private = self._unwrapped(user, password)
        return self._open(user, private, int(row["minutes"]))

    def recover(self, user: str, recovery: str, password: str) -> str:
        """Unlocks the person's library with the recovery key and gives it a new password (every
        other session of theirs ends): a session's token; Locked for a wrong recovery key."""
        row = self._row(user)
        if row is None:
            raise Locked(said("Your library is not set up yet."))
        aad = _aad(user, bytes(row["public"]))
        private = self._tried(user, lambda: crypt.unwrap_with_recovery(row["by_recovery"], recovery, aad))
        self._rewrap(user, private, password, int(row["minutes"]))
        return self._open(user, private, int(row["minutes"]))

    def change_password(self, user: str, token: str, current: str, password: str, minutes: int) -> str:
        """Changes the person's password (current checked: an unlocked browser left alone is not
        enough) and minutes; every session of theirs ends; this browser's new token."""
        self.session(user, token)
        _, private = self._unwrapped(user, current)
        minutes = min(max(1, int(minutes)), MAX_LOCK_MINUTES)
        self._rewrap(user, private, password or current, minutes)
        return self._open(user, private, minutes)

    def _rewrap(self, user: str, private: X25519PrivateKey, password: str, minutes: int) -> None:
        public = crypt.public_of(private)
        with self.store.writing() as db:
            db.execute(
                "UPDATE library_keys SET by_password = ?, minutes = ? WHERE user = ?",
                (crypt.wrap_with_password(private, password, _aad(user, public)), minutes, user),
            )
        self.lock_all(user)

    def session(self, user: str, token: str) -> Session:
        """The person's unlocked session of this token (each use keeps it open longer); Locked if
        there is none, it is another's or it lapsed."""
        now = time.monotonic()
        with self._lock:
            self._sweep()
            session = self._sessions.get(token or "")
            if session is None or session.user != user or session.private is None:
                raise Locked(said("The library is locked."))
            session.expires = now + 60 * session.minutes
            return session

    def unlocked(self, user: str, token: str) -> bool:
        """Whether the token opens the person's library."""
        try:
            self.session(user, token)
        except Locked:
            return False
        return True

    def lock(self, token: str) -> None:
        """Ends the session (its private key dropped)."""
        with self._lock:
            session = self._sessions.pop(token or "", None)
        if session is not None:
            session.drop()

    def lock_all(self, user: str) -> None:
        """Ends every session of the person's."""
        with self._lock:
            for token in [token for token, session in self._sessions.items() if session.user == user]:
                self._sessions.pop(token).drop()

    def forget(self, user: str) -> None:
        """Removes the person's keys (the admin's reset): what was encrypted with them is lost."""
        self.lock_all(user)
        with self.store.writing() as db:
            db.execute("DELETE FROM library_keys WHERE user = ?", (user,))
        with self._lock:
            self._proposals.pop(user, None)
            self._failed.pop(user, None)
