"""Email attachments: the files attached to the messages of an IMAP mailbox, e.g. a supplier's invoices.

The mailbox is only read: the folder is selected read-only and the messages are fetched with
BODY.PEEK, so nothing is marked as read. A message's attachments are told apart by its Message-ID
(or, without one, the folder's UIDVALIDITY and the message's UID), their position and file name.

A run asks the server for few things: a sender that is a plain address or domain is searched for
by the server, the headers come a hundred messages at a time, and a message whose attachments
were all handled (or that is not the job's) is remembered and not read again, until the job's
filters change.
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
from .base import MARK, AuthError, Item, Option, Refused, Source, SourceError

# Month names IMAP's SINCE expects, whatever the locale.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
UNSAFE = re.compile(r"[\\/:*?\"<>|\s]+")
# The headers a listing reads, with the message's size, for this many messages at a time.
HEADERS = "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
BATCH = 100
# The mark of a message done with: MARK, then the folder, UIDVALIDITY and UID.
DONE = MARK + "mail:"
# A sender pattern the server can search for (an address or a domain, dots escaped or not).
PLAIN_SENDER = re.compile(r"[A-Za-z0-9._+-]*@?[A-Za-z0-9.-]+")


# How a job signs in: a password, or OAuth2's XOAUTH2 with a refresh token at a provider.
AUTHS = ("password", "oauth2-google", "oauth2-microsoft", "oauth2")
OAUTH_TOKEN_URLS = {
    "oauth2-google": "https://oauth2.googleapis.com/token",
    "oauth2-microsoft": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}


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


def uid_set(uids: list[bytes]) -> str:
    """The UIDs as an IMAP set, runs as ranges: 1:3,7."""
    numbers = sorted({int(uid) for uid in uids})
    parts: list[str] = []
    for number in numbers:
        if parts and parts[-1].split(":")[-1] == str(number - 1):
            parts[-1] = f"{parts[-1].split(':')[0]}:{number}"
        else:
            parts.append(str(number))
    return ",".join(parts)


def fetched_headers(data: list) -> dict[bytes, tuple[int, bytes]]:
    """The size and header of each message of a FETCH of HEADERS, by UID (wherever the server
    puts UID and RFC822.SIZE: before the header's literal or after it)."""
    found = {}
    for index, part in enumerate(data or []):
        if not (isinstance(part, tuple) and len(part) > 1):
            continue
        after = data[index + 1] if index + 1 < len(data) and isinstance(data[index + 1], bytes) else b""
        text = part[0] + b" " + after
        uid, size = re.search(rb"UID (\d+)", text), re.search(rb"RFC822\.SIZE (\d+)", text)
        if uid:
            found[uid.group(1)] = (int(size.group(1)) if size else 0, part[1])
    return found


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
    credentials = ("username", "password", "client_id", "client_secret", "refresh_token")
    credential_help = {
        "username": ("Username", "The mailbox's login, usually its address."),
        "password": ("Password", "For Sign-in: password (an app password where the provider has them, e.g. Gmail)."),
        "client_id": ("OAuth client ID", "For Sign-in: OAuth2: the OAuth app's client ID."),
        "client_secret": ("OAuth client secret", "For Sign-in: OAuth2: its secret (empty for a public client)."),
        "refresh_token": ("OAuth refresh token", "For Sign-in: OAuth2: a refresh token with the mail scope."),
    }
    options = {
        "auth": Option(
            "Sign-in",
            "password",
            "password; or OAuth2 with a refresh token (Microsoft's mailboxes need it, Gmail's take it): "
            "oauth2-google, oauth2-microsoft, or oauth2 with the token endpoint below.",
            choices=AUTHS,
        ),
        "token_url": Option(
            "OAuth token endpoint",
            "",
            "For oauth2 only: the provider's token endpoint.",
            kind="url",
            placeholder="https://login.example.org/oauth2/token",
        ),
        "host": Option(
            "IMAP server", "", "e.g. mail.example.org (TLS on the port below).", placeholder="mail.example.org"
        ),
        "port": Option("Port", 993, "IMAP over TLS: 993.", minimum=1, maximum=65535),
        "folder": Option(
            "Folder",
            "INBOX",
            "The mailbox folder, e.g. INBOX or Invoices.",
            suggestions=("INBOX", "INBOX/Invoices", "INBOX.Invoices", "Archive", "Sent", "[Gmail]/All Mail"),
        ),
        "since_days": Option(
            "Days back",
            90,
            "Only messages of the last this many days (0: all).",
            minimum=0,
            suggestions=("30", "90", "365", "0"),
        ),
        "from_pattern": Option(
            "From",
            "",
            "Regular expression on the sender (name and address); empty: any.",
            kind="regex",
            placeholder="invoice@shop\\.example",
        ),
        "subject_pattern": Option(
            "Subject",
            "",
            "Regular expression on the subject; empty: any.",
            kind="regex",
            placeholder="Rechnung|Invoice",
            suggestions=("Invoice|Rechnung|Faktura", "Statement|Kontoauszug|Wyciąg", "Payslip|Gehaltsabrechnung"),
        ),
        "filename_pattern": Option(
            "Attachment names",
            ".*",
            "Regular expression on the attachment's file name.",
            kind="regex",
            placeholder="\\.pdf$",
            suggestions=(".*", "\\.pdf$", "\\.(pdf|xml)$", "\\.(jpe?g|png)$"),
        ),
        "path": Option(
            "File name",
            "{year}/{date} {filename}",
            "Relative to the folder: {filename}, {stem}, {ext}, {date}, {year}, {month}, {day}, {from}, {sender}, "
            "{subject}.",
            kind="template",
            values=("from", "sender", "subject", "date", "year", "month", "day", "filename", "stem", "ext"),
            suggestions=("{year}/{date} {filename}", "{sender}/{year}/{date} {filename}", "{date} {subject}{ext}"),
        ),
        "file_type": download.DOWNLOAD_OPTIONS["file_type"],
        "min_size": download.DOWNLOAD_OPTIONS["min_size"],
        "max_size": download.DOWNLOAD_OPTIONS["max_size"],
    }
    # What tells the messages done with from the others.
    listing_options = (
        "host",
        "port",
        "folder",
        "from_pattern",
        "subject_pattern",
        "filename_pattern",
        "file_type",
        "min_size",
        "max_size",
    )
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
        auth = str(self.settings["auth"])
        if auth not in AUTHS:
            problems.append(f"unknown sign-in {auth}")
        elif auth == "oauth2" and not browser.allowed_url(str(self.settings["token_url"]).strip()):
            problems.append("OAuth2 needs the provider's token endpoint (https://...)")
        if problems:
            raise SourceError("; ".join(problems))
        self.imap: Any = None
        self.refreshed = ""
        # The attachment the run is at (its key and content): only it is kept, the others of a
        # long listing are not held until the run ends.
        self.current: tuple[str, bytes] | None = None

    def open(self) -> Any:
        """Signs in and selects the folder, read-only."""
        try:
            self.imap = self.connect(str(self.settings["host"]).strip(), int(self.settings["port"]))
        except OSError as error:
            raise SourceError(f"cannot reach {self.settings['host']}: {error}") from error
        user, password = self.secrets.get("username", ""), self.secrets.get("password", "")
        try:
            if str(self.settings["auth"]).startswith("oauth2"):
                access = self.access_token()
                # XOAUTH2 (Google's and Microsoft's IMAP): the user and the bearer token.
                proof = f"user={user}\x01auth=Bearer {access}\x01\x01".encode()
                self.imap.authenticate("XOAUTH2", lambda _: proof)
            elif plain_login(user) and plain_login(password):
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

    def plain_sender(self) -> str:
        """The sender pattern as the server's search takes it, when it is a plain address or
        domain (invoice@shop\\.example); else "" (the pattern is applied to the headers only).
        The server's search, a substring regardless of case, only narrows what the pattern checks."""
        text = str(self.settings["from_pattern"]).replace("\\.", ".")
        return text if len(text) >= 3 and PLAIN_SENDER.fullmatch(text) else ""

    def search(self, imap: Any) -> list[bytes]:
        """The UIDs of the folder's messages in the time asked for (and of the sender, when the
        server can search for it), newest first."""
        days = int(self.settings["since_days"])
        criteria: list[str] = []
        if days:
            since = datetime.date.today() - datetime.timedelta(days=days)
            criteria = ["SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}"]
        if self.plain_sender():
            criteria += ["FROM", f'"{self.plain_sender()}"']
        criteria = criteria or ["ALL"]
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

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:  # pylint: disable=too-many-locals
        imap = self.imap or self.open()
        _, validity = imap.response("UIDVALIDITY")
        validity = (validity[0] if validity and validity[0] else b"0").decode()
        policy = email.policy.default
        largest = download.largest(self.settings)

        def done(uid: bytes) -> str:
            return f"{DONE}{self.settings['folder']}:{validity}:{uid.decode()}"

        # The messages done with are left out before anything of them is fetched.
        fresh = [uid for uid in self.search(imap) if not seen(done(uid))]
        for start in range(0, len(fresh), BATCH):
            batch = fresh[start : start + BATCH]
            # The headers first, of many messages at once: only messages of the sender and subject
            # asked for are fetched whole.
            status, data = imap.uid("FETCH", uid_set(batch), HEADERS)
            if status != "OK":
                raise SourceError(f"the IMAP server could not give the messages' headers in {self.settings['folder']}")
            headers = fetched_headers(data)
            for uid in batch:
                if uid not in headers:
                    continue
                size, raw = headers[uid]
                if not self.matches(email.message_from_bytes(raw, policy=policy)):  # type: ignore[arg-type]
                    self.remember(done(uid))
                    continue
                if size > largest:
                    # Skipped (and so remembered as done) by the run, which says so in its log.
                    yield Item(
                        key=done(uid),
                        path="",
                        url=self.address(uid.decode()),
                        problem=Refused(
                            f"message {uid.decode()} is {size // download.MB} MB, more than the largest file"
                        ),
                    )
                    continue
                status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK":
                    raise SourceError(f"the IMAP server could not give message {uid.decode()}")
                message = email.message_from_bytes(fetched_bytes(data), policy=policy)
                keys: list[str] = []
                yield from self.attachments(message, f"{validity}:{uid.decode()}", seen, keys)  # type: ignore[arg-type]
                # Back here once the run took each of them (a run that ends before does not come
                # back): the message is done when every one is saved or skipped.
                if all(seen(key) for key in keys):
                    self.remember(done(uid))

    def address(self, uid: str) -> str:
        """A message's address, for the progress log."""
        return f"imap://{self.settings['host']}/{self.settings['folder']};UID={uid}"

    def attachments(
        self, message: EmailMessage, uid: str, seen: Callable[[str], bool], keys: list[str]
    ) -> Iterator[Item]:
        """The message's new attachments of the names, type and size asked for (their keys added
        to keys)."""
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
            self.current = (key, payload)
            keys.append(key)
            yield self.item(key, filename, values | {"index": index}, uid)
            self.current = None

    def item(self, key: str, filename: str, values: dict, uid: str) -> Item:
        """An attachment: its path from the message's values and its file name (an item that
        fails alone when the name cannot be made)."""
        stem, ext = posixpath.splitext(filename)
        values = values | {"filename": filename, "stem": stem, "ext": ext}
        url = self.address(uid.split(":")[-1])
        try:
            path = patterns.render_path(str(self.settings["path"]), values, "the message")
        except (KeyError, ValueError) as error:
            problem = SourceError(f"the file name: {str(error).strip(chr(39))}")
            return Item(key=key, path="", url=url, values=values, problem=problem)
        return Item(key=key, path=path, url=url, values=values)

    def wanted(self, payload: bytes, content_type: str) -> bool:
        """Whether an attachment is of the job's type and size: another one (a signature's logo,
        say) is not the job's, and is left out rather than failing the run."""
        if not download.accepts(download.file_types(self.settings), content_type, payload):
            return False
        largest = download.largest(self.settings)
        return int(self.settings["min_size"] or 0) <= len(payload) <= largest

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
        if self.current is None or self.current[0] != item.key:
            raise SourceError(f"{item.path} was not read from the mailbox")
        dest.write(self.current[1])

    def access_token(self) -> str:
        """An access token for the mailbox, from the refresh token (and the provider's newer
        refresh token kept for the next run, when it sends one)."""
        if self.client is None:
            raise SourceError("no connection for OAuth2")
        refresh = self.refreshed or self.secrets.get("refresh_token", "")
        client_id = self.secrets.get("client_id", "")
        if not refresh or not client_id:
            raise AuthError("OAuth2 needs the job's client ID and refresh token (its account)")
        data = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
        if self.secrets.get("client_secret"):
            data["client_secret"] = self.secrets["client_secret"]
        answer = self.client.post(self.token_endpoint(), data=data)
        token = answer.json() if answer.headers.get("content-type", "").startswith("application/json") else {}
        if answer.status_code != 200 or not token.get("access_token"):
            reason = token.get("error_description") or token.get("error") or f"answer {answer.status_code}"
            raise AuthError(f"the OAuth2 provider refused the refresh token: {reason}")
        if token.get("refresh_token") and token["refresh_token"] != refresh:
            self.refreshed = str(token["refresh_token"])
        return str(token["access_token"])

    def token_endpoint(self) -> str:
        """The provider's token endpoint."""
        auth = str(self.settings["auth"])
        return OAUTH_TOKEN_URLS.get(auth) or str(self.settings["token_url"]).strip()

    def restore(self, session: dict) -> None:
        # A refresh token the provider replaced in an earlier run (Microsoft's rotate).
        if session.get("refresh_token"):
            self.refreshed = str(session["refresh_token"])

    def session(self) -> dict | None:
        return {"refresh_token": self.refreshed} if self.refreshed else None

    def close(self) -> None:
        if self.imap is not None:
            try:
                self.imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self.imap = None
        self.current = None
