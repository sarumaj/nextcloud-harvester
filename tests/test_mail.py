"""The mail source against a fake IMAP server holding real messages: no network."""

# pylint: disable=missing-function-docstring

import imaplib
import io
from email.message import EmailMessage

import pytest

from harvester.sources import AuthError, SourceError
from harvester.sources.mail import Mail

PDF = b"%PDF-1.7\n" + b"x" * 500


def message(sender: str, subject: str, attachments: list[tuple[str, bytes, str]], message_id: str = "") -> bytes:
    """A message with the attachments (file name, content, type)."""
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "me@example.org"
    mail["Subject"] = subject
    mail["Date"] = "Tue, 14 Oct 2025 09:30:00 +0200"
    if message_id:
        mail["Message-ID"] = message_id
    mail.set_content("See attached.")
    for filename, content, kind in attachments:
        maintype, subtype = kind.split("/")
        mail.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return mail.as_bytes()


class FakeIMAP:
    """An IMAP server with a folder of messages, recording what it is asked."""

    def __init__(self, messages: dict[bytes, bytes], password: str = "secret"):
        self.messages = messages
        self.password = password
        self.commands: list[tuple] = []
        self.logged_out = False

    def login(self, user, password):
        self.commands.append(("LOGIN", user))
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"Logged in"]

    def select(self, folder, readonly=False):
        self.commands.append(("SELECT", folder, readonly))
        return ("OK", [b"3"]) if folder == '"INBOX"' else ("NO", [b"no such folder"])

    def response(self, code):
        return code, [b"1700000000"] if code == "UIDVALIDITY" else [None]

    def uid(self, command, *args):
        self.commands.append(("UID", command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(sorted(self.messages))]
        uid, what = args
        raw = self.messages[uid]
        if what == "(BODY.PEEK[HEADER])":
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"1 (UID " + uid + b" " + what.encode() + b" {%d}" % len(raw), raw), b")"]

    def logout(self):
        self.logged_out = True


def source(server: FakeIMAP, **options) -> Mail:
    made = Mail(
        {"host": "imap.example.org", "since_days": 0, **options}, {"username": "me", "password": "secret"}, None
    )
    made.connect = lambda host, port: server  # type: ignore[method-assign]
    return made


def mailbox() -> FakeIMAP:
    return FakeIMAP(
        {
            b"1": message(
                "Shop <billing@shop.example>",
                "=?utf-8?q?Rechnung_f=C3=BCr_Oktober?=",
                [("Rechnung Oktober.pdf", PDF, "application/pdf"), ("logo.png", b"\x89PNG small", "image/png")],
                "<a1@shop.example>",
            ),
            b"2": message(
                "Friend <friend@example.org>", "Photos", [("beach.jpg", b"\xff\xd8\xff" + b"y" * 50, "image/jpeg")]
            ),
            b"3": message(
                "Shop <billing@shop.example>", "Invoice November", [("Rechnung November.pdf", PDF, "application/pdf")]
            ),
        }
    )


def test_attachments_of_the_sender_newest_first_read_only():
    server = mailbox()
    made = source(server, from_pattern="billing@shop", file_type="pdf")
    items = list(made.items(lambda key: False))
    assert [item.values["filename"] for item in items] == ["Rechnung November.pdf", "Rechnung Oktober.pdf"]
    first, second = items[1], items[0]
    assert first.key == "mail:<a1@shop.example>#1:Rechnung Oktober.pdf"
    assert second.key == "mail:1700000000:3#1:Rechnung November.pdf", "without a Message-ID: UIDVALIDITY and UID"
    assert first.path == "2025/2025-10-14 Rechnung Oktober.pdf"
    assert first.values["subject"] == "Rechnung für Oktober" and first.values["from"] == "billing@shop.example"
    assert (first.values["year"], first.values["month"], first.values["day"]) == (2025, 10, 14)
    dest = io.BytesIO()
    made.fetch(first, dest)
    assert dest.getvalue() == PDF
    assert ("SELECT", '"INBOX"', True) in server.commands
    fetched = [command[3] for command in server.commands if command[:2] == ("UID", "FETCH")]
    assert fetched and all(what.startswith("(BODY.PEEK[") for what in fetched), "nothing is marked as read"
    assert ("UID", "FETCH", b"2", "(BODY.PEEK[])") not in server.commands, "other senders' messages are not fetched"
    made.close()
    assert server.logged_out


def test_names_types_sizes_and_known_attachments_are_left_out():
    server = mailbox()
    known = {"mail:<a1@shop.example>#1:Rechnung Oktober.pdf"}
    items = list(source(server, filename_pattern=r"\.(pdf|png)$", min_size=100).items(known.__contains__))
    assert [item.values["filename"] for item in items] == ["Rechnung November.pdf"], "the small logo is left out"


def test_since_days_searches_from_a_date():
    server = mailbox()
    list(source(server, since_days=30).items(lambda key: False))
    search = next(command for command in server.commands if command[:2] == ("UID", "SEARCH"))
    assert search[3] == "SINCE" and len(search[4].split("-")) == 3


def test_a_refused_sign_in_and_a_missing_folder():
    with pytest.raises(AuthError):
        list(source(FakeIMAP({}, password="other")).items(lambda key: False))
    with pytest.raises(SourceError, match="no folder"):
        list(source(mailbox(), folder="Archive").items(lambda key: False))


def test_settings_are_checked_without_connecting():
    for options, problem in (
        ({"host": ""}, "no IMAP server"),
        ({"host": "127.0.0.1"}, "may not be reached"),
        ({"host": "mail.local"}, "may not be reached"),
        ({"host": "imap.example.org", "port": "x"}, "not a port"),
        ({"host": "imap.example.org", "subject_pattern": "("}, "not a regular expression"),
        ({"host": "imap.example.org", "file_type": "movie"}, "unknown file type"),
    ):
        with pytest.raises(SourceError, match=problem):
            Mail(options, {}, None)
    Mail({"host": "imap.example.org"}, {}, None).close()
