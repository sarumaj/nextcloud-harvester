"""Email attachments: the files attached to the messages of an IMAP mailbox, e.g. a supplier's invoices.

The mailbox is only read: the folder is selected read-only and the messages are fetched with
BODY.PEEK, so nothing is marked as read. A message's attachments are told apart by its Message-ID
(or, without one, the folder's UIDVALIDITY and the message's UID), their position and file name.
"""

import base64
import datetime
import email
import email.policy
import email.utils
import imaplib
import posixpath
import re
import ssl
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from typing import Any, BinaryIO, ClassVar

from .. import browser, patterns
from . import download
from .base import AuthError, Item, Option, Source, SourceError

# Month names IMAP's SINCE expects, whatever the locale.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
UNSAFE = re.compile(r"[\\/:*?\"<>|\s]+")


def plain_login(text: str) -> bool:
    """Whether imaplib's LOGIN can send it: ASCII without spaces or quotes."""
    return text.isascii() and not any(char in text for char in ' "\\\t')


def utf7_imap(name: str) -> str:
    """A folder name in IMAP's modified UTF-7 (RFC 3501 5.1.3), e.g. Entwürfe as Entw&APw-rfe."""
    out, pending = [], []

    def flush() -> None:
        if pending:
            encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + encoded.replace("/", ",") + "-")
            pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(out)


def connect_tls(host: str, port: int) -> Any:
    """An IMAP connection over TLS, with the certificate checked."""
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=60)


def safe(text: str, limit: int = 100) -> str:
    """The text as a part of a file name."""
    return UNSAFE.sub(" ", text or "").strip()[:limit]


def fetched_bytes(data: list) -> bytes:
    """The literal of an IMAP FETCH answer (the message or its header)."""
    for part in data or []:
        if isinstance(part, tuple) and len(part) > 1:
            return part[1]
    return b""


class Mail(Source):
    """Attachments of an IMAP mailbox."""

    name = "mail"
    title = "Email attachments"
    description = "Attachments of the messages in a mailbox (IMAP), e.g. invoices from a sender; read-only."
    credentials = ("username", "password")
    options = {
        "host": Option("IMAP server", "", "e.g. mail.example.org (TLS on the port below)."),
        "port": Option("Port", 993, "IMAP over TLS: 993."),
        "folder": Option("Folder", "INBOX", "The mailbox folder, e.g. INBOX or Invoices."),
        "since_days": Option("Days back", 90, "Only messages of the last this many days (0: all)."),
        "from_pattern": Option("From", "", "Regular expression on the sender (name and address); empty: any."),
        "subject_pattern": Option("Subject", "", "Regular expression on the subject; empty: any."),
        "filename_pattern": Option("Attachment names", ".*", "Regular expression on the attachment's file name."),
        "path": Option(
            "File name",
            "{year}/{date} {filename}",
            "Relative to the folder: {filename}, {stem}, {ext}, {date}, {year}, {month}, {day}, {from}, {sender}, "
            "{subject}.",
        ),
        "file_type": download.DOWNLOAD_OPTIONS["file_type"],
        "min_size": download.DOWNLOAD_OPTIONS["min_size"],
    }
    # Opens the connection (host, port); a test replaces it with a fake server.
    connect: ClassVar[Callable[[str, int], Any]] = staticmethod(connect_tls)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        problems = []
        host = str(self.settings["host"]).strip()
        if not host:
            problems.append("no IMAP server")
        elif not browser.allowed_host(host):
            problems.append(f"the IMAP server {host} may not be reached")
        try:
            port = int(self.settings["port"])
            if not 0 < port < 65536:
                raise ValueError
        except (TypeError, ValueError):
            problems.append(f"the port {self.settings['port']} is not a port")
        try:
            if int(self.settings["since_days"]) < 0:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("the days back are not a number of days")
        for name in ("from_pattern", "subject_pattern", "filename_pattern"):
            try:
                re.compile(str(self.settings[name]))
            except re.error as error:
                problems.append(f"{self.options[name].title}: not a regular expression ({error})")
        problems += download.problems(self.settings)
        if problems:
            raise SourceError("; ".join(problems))
        self.imap: Any = None
        self.payloads: dict[str, bytes] = {}

    def open(self) -> Any:
        """Signs in and selects the folder, read-only."""
        try:
            self.imap = self.connect(str(self.settings["host"]).strip(), int(self.settings["port"]))
        except OSError as error:
            raise SourceError(f"cannot reach {self.settings['host']}: {error}") from error
        user, password = self.secrets.get("username", ""), self.secrets.get("password", "")
        try:
            if plain_login(user) and plain_login(password):
                self.imap.login(user, password)
            else:
                # imaplib's LOGIN sends ASCII only and does not quote the user: SASL PLAIN (UTF-8).
                credentials = f"\0{user}\0{password}".encode()
                self.imap.authenticate("PLAIN", lambda _: credentials)
        except imaplib.IMAP4.error as error:
            raise AuthError(f"the IMAP server refused the sign-in: {error}") from error
        folder = utf7_imap(str(self.settings["folder"])).replace("\\", "\\\\").replace('"', '\\"')
        status, _ = self.imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise SourceError(f"no folder {self.settings['folder']}")
        return self.imap

    def search(self, imap: Any) -> list[bytes]:
        """The UIDs of the folder's messages in the time asked for, newest first."""
        days = int(self.settings["since_days"])
        criteria: list[str] = ["ALL"]
        if days:
            since = datetime.date.today() - datetime.timedelta(days=days)
            criteria = ["SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}"]
        status, data = imap.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise SourceError(f"the IMAP server could not search {self.settings['folder']}")
        return list(reversed((data[0] or b"").split()))

    def matches(self, header: EmailMessage) -> bool:
        """Whether the message's sender and subject are those asked for."""
        sender = " ".join(email.utils.parseaddr(str(header.get("From", "")))).strip()
        subject = str(header.get("Subject", ""))
        return bool(
            re.search(str(self.settings["from_pattern"]), sender)
            and re.search(str(self.settings["subject_pattern"]), subject)
        )

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        imap = self.imap or self.open()
        _, validity = imap.response("UIDVALIDITY")
        validity = (validity[0] if validity and validity[0] else b"0").decode()
        policy = email.policy.default
        for uid in self.search(imap):
            # The header first: only messages of the sender and subject asked for are fetched whole.
            status, data = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
            header = email.message_from_bytes(fetched_bytes(data), policy=policy)
            if status != "OK" or not self.matches(header):  # type: ignore[arg-type]
                continue
            status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK":
                raise SourceError(f"the IMAP server could not give message {uid.decode()}")
            message = email.message_from_bytes(fetched_bytes(data), policy=policy)
            yield from self.attachments(message, f"{validity}:{uid.decode()}", seen)  # type: ignore[arg-type]

    def attachments(self, message: EmailMessage, uid: str, seen: Callable[[str], bool]) -> Iterator[Item]:
        """The message's new attachments of the names, type and size asked for."""
        message_id = str(message.get("Message-ID", "")).strip() or uid
        values = self.message_values(message)
        names = re.compile(str(self.settings["filename_pattern"]))
        index = 0
        for part in message.walk():
            filename = part.get_filename()
            if not filename or part.is_multipart():
                continue
            index += 1
            filename = safe(filename, 200) or f"attachment-{index}"
            key = f"mail:{message_id}#{index}:{filename}"
            if not names.search(filename) or seen(key):
                continue
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes) or not self.wanted(payload, part.get_content_type()):
                continue
            self.payloads[key] = payload
            yield self.item(key, filename, values | {"index": index}, uid)

    def item(self, key: str, filename: str, values: dict, uid: str) -> Item:
        """An attachment: its path from the message's values and its file name."""
        stem, ext = posixpath.splitext(filename)
        values = values | {"filename": filename, "stem": stem, "ext": ext}
        try:
            path = patterns.render_path(str(self.settings["path"]), values)
        except (KeyError, ValueError) as error:
            raise SourceError(f"the file name: {str(error).strip(chr(39))}") from error
        url = f"imap://{self.settings['host']}/{self.settings['folder']};UID={uid.split(':')[-1]}"
        return Item(key=key, path=path, url=url, values=values)

    def wanted(self, payload: bytes, content_type: str) -> bool:
        """Whether an attachment is of the job's type and size: another one (a signature's logo,
        say) is not the job's, and is left out rather than failing the run."""
        file_type = str(self.settings["file_type"])
        magic, _ = download.FILE_TYPES[file_type]
        if magic and not payload.startswith(magic):
            return False
        if file_type in ("audio", "video") and not content_type.startswith(f"{file_type}/"):
            return False
        return len(payload) >= int(self.settings["min_size"] or 0)

    @staticmethod
    def message_values(message: EmailMessage) -> dict:
        """What a message gives its attachments' names and tags."""
        name, address = email.utils.parseaddr(str(message.get("From", "")))
        try:
            sent = email.utils.parsedate_to_datetime(str(message.get("Date", "")))
        except (TypeError, ValueError):
            sent = None
        sent = sent or datetime.datetime.now(datetime.UTC)
        return {
            "from": safe(address),
            "sender": safe(name or address),
            "subject": safe(str(message.get("Subject", ""))),
            "date": sent.strftime("%Y-%m-%d"),
            "year": sent.year,
            "month": sent.month,
            "day": sent.day,
        }

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        payload = self.payloads.pop(item.key, None)
        if payload is None:
            raise SourceError(f"{item.path} was not read from the mailbox")
        dest.write(payload)

    def close(self) -> None:
        if self.imap is not None:
            try:
                self.imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self.imap = None
        self.payloads.clear()
