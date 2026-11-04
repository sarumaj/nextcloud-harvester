# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mail source against a fake IMAP server holding real messages: no network."""

# pylint: disable=missing-function-docstring

import imaplib
import io
from email.message import EmailMessage

import pytest

from harvester.sources import AuthError, SourceError
from harvester.sources import mail as mail_source
from harvester.sources import mailfolders
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

    def __init__(self, messages: dict[bytes, bytes], password: str = "secret", folders: list[bytes] | None = None):
        self.messages = messages
        self.password = password
        # LIST's answer: a line per folder (its flags, the delimiter, its name).
        self.folders = (
            [
                b'(\\HasChildren) "/" INBOX',
                b'(\\HasNoChildren) "/" "INBOX/Rechnungen"',
                b'(\\HasNoChildren) "/" Archive',
            ]
            if folders is None
            else folders
        )
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
            # FROM: a substring of the From header, regardless of case, as servers search.
            sender = args[args.index("FROM") + 1].strip('"').lower().encode() if "FROM" in args else b""
            found = [uid for uid, raw in sorted(self.messages.items()) if sender in self.header(raw, b"From").lower()]
            return "OK", [b" ".join(found)]
        uids, what = args
        if what == "(BODY.PEEK[])":
            raw = self.messages[uids]
            return "OK", [(b"1 (UID " + uids + b" BODY[] {%d}" % len(raw), raw), b")"]
        # The headers of a set of messages (1:3,5), the size and UID after the header's literal
        # in every other answer, as some servers put them.
        data: list = []
        for uid in self.uid_set(uids.decode() if isinstance(uids, bytes) else uids):
            raw = self.messages[uid]
            header = raw.split(b"\n\n", 1)[0] + b"\n\n"
            meta = b"UID " + uid + b" RFC822.SIZE %d" % len(raw)
            if int(uid) % 2:
                data += [(b"1 (" + meta + b" BODY[HEADER.FIELDS (FROM)] {%d}" % len(header), header), b")"]
            else:
                data += [(b"1 (BODY[HEADER.FIELDS (FROM)] {%d}" % len(header), header), b" " + meta + b")"]
        return "OK", data

    def uid_set(self, text: str) -> list[bytes]:
        uids = []
        for part in text.split(","):
            first, _, last = part.partition(":")
            uids += [str(n).encode() for n in range(int(first), int(last or first) + 1)]
        return [uid for uid in uids if uid in self.messages]

    @staticmethod
    def header(raw: bytes, name: bytes) -> bytes:
        for line in raw.split(b"\n\n", 1)[0].splitlines():
            if line.lower().startswith(name.lower() + b":"):
                return line
        return b""

    def logout(self):
        self.logged_out = True

    # Last: the class's annotations above mean the builtin list.
    def list(self):
        self.commands.append(("LIST",))
        return "OK", self.folders


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
    # Each attachment is fetched while the listing is at it, as a run does: only it is held.
    items, contents = [], []
    for item in made.items(lambda key: False):
        dest = io.BytesIO()
        made.fetch(item, dest)
        items.append(item)
        contents.append(dest.getvalue())
    assert [item.values["filename"] for item in items] == ["Rechnung November.pdf", "Rechnung Oktober.pdf"]
    first, second = items[1], items[0]
    with pytest.raises(SourceError, match="not read"):
        made.fetch(first, io.BytesIO())
    assert first.key == "mail:<a1@shop.example>#1:Rechnung Oktober.pdf"
    assert second.key == "mail:1700000000:3#1:Rechnung November.pdf", "without a Message-ID: UIDVALIDITY and UID"
    assert first.path == "2025/2025-10-14 Rechnung Oktober.pdf"
    assert first.values["subject"] == "Rechnung für Oktober" and first.values["from"] == "billing@shop.example"
    assert (first.values["year"], first.values["month"], first.values["day"]) == (2025, 10, 14)
    assert contents == [PDF, PDF]
    assert ("SELECT", '"INBOX"', True) in server.commands
    fetched = [command[3] for command in server.commands if command[:2] == ("UID", "FETCH")]
    assert fetched and all("BODY.PEEK[" in what and "BODY[" not in what for what in fetched), "nothing marked read"
    assert ("UID", "SEARCH", None, "FROM", '"billing@shop"') in server.commands, "the server finds the sender"
    assert ("UID", "FETCH", "1,3", mail_source.HEADERS) in server.commands, "the headers at once"
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
    with pytest.raises(mail_source.MissingFolder) as missing:
        list(source(mailbox(), folder="rechnungen").items(lambda key: False))
    assert str(missing.value) == "The mailbox has no folder “rechnungen”. It has: INBOX/Rechnungen, Archive, INBOX"
    assert missing.value.args[0].template["text"] == "The mailbox has no folder “{folder}”. It has: {folders}"
    assert missing.value.folders == ["INBOX/Rechnungen", "Archive", "INBOX"], "the closest first"


def test_a_missing_folders_list_the_closest_first_at_most_ten():
    dotted = [b'(\\HasNoChildren) "." INBOX', b'(\\Noselect \\HasChildren) "." "[Gmail]"']
    dotted += [b'(\\HasNoChildren) "." "INBOX.Folder %d"' % number for number in range(12)]
    dotted += [b'(\\HasNoChildren) "." INBOX.Invoices', b'(\\HasNoChildren) "." "Entw&APw-rfe"']
    with pytest.raises(mail_source.MissingFolder) as missing:
        list(source(FakeIMAP({}, folders=dotted), folder="invoice").items(lambda key: False))
    assert missing.value.folders[0] == "INBOX.Invoices", "with INBOX's prefix and the server's delimiter"
    assert "[Gmail]" not in missing.value.folders, "a folder that cannot be selected is none"
    assert "Entwürfe" in missing.value.folders, "names in IMAP's UTF-7 decoded"
    assert str(missing.value).startswith("The mailbox has no folder “invoice”. It has: INBOX.Invoices, ")
    assert str(missing.value).endswith(", …") and str(missing.value).count(", ") == 10, "ten of them, then …"
    with pytest.raises(mail_source.MissingFolder) as missing:
        list(source(FakeIMAP({}, folders=[]), folder="Archiv").items(lambda key: False))
    assert str(missing.value) == "The mailbox has no folder “Archiv”." and not missing.value.folders
    literal = [(b'(\\HasNoChildren) "/" {6}', b"Belege"), b")"]
    assert mailfolders.listed_folders(literal) == (["Belege"], "/"), "a name sent as a literal"


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


class OAuthIMAP(FakeIMAP):
    """An IMAP server that takes XOAUTH2 only, with the access token it knows."""

    def authenticate(self, mechanism, answer):
        proof = answer(b"")
        self.commands.append(("AUTHENTICATE", mechanism, proof))
        if mechanism != "XOAUTH2" or proof != b"user=me@example.org\x01auth=Bearer access-1\x01\x01":
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"Logged in"]


def oauth_source(server, client, refresh="refresh-1"):
    made = Mail(
        {"host": "imap.example.org", "since_days": 0, "auth": "oauth2-microsoft"},
        {"username": "me@example.org", "client_id": "app-id", "refresh_token": refresh},
        client,
    )
    made.connect = lambda host, port: server  # type: ignore[method-assign]
    return made


def test_oauth2_signs_in_with_xoauth2_and_keeps_a_replaced_refresh_token():
    import httpx  # pylint: disable=import-outside-toplevel
    from urllib.parse import parse_qs  # pylint: disable=import-outside-toplevel

    asked: list = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        asked.append((str(request.url), {k: v[0] for k, v in parse_qs(request.content.decode()).items()}))
        return httpx.Response(200, json={"access_token": "access-1", "refresh_token": "refresh-2", "expires_in": 3600})

    server = OAuthIMAP(mailbox().messages)
    made = oauth_source(server, httpx.Client(transport=httpx.MockTransport(endpoint)))
    assert len(list(made.items(lambda key: False))) == 4, "the mailbox's four attachments"
    url, form = asked[0]
    assert url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert form == {"grant_type": "refresh_token", "refresh_token": "refresh-1", "client_id": "app-id"}
    assert server.commands[0][:2] == ("AUTHENTICATE", "XOAUTH2")
    assert made.session() == {"refresh_token": "refresh-2"}, "Microsoft's new refresh token kept for the next run"
    made.close()

    later = oauth_source(OAuthIMAP(mailbox().messages), httpx.Client(transport=httpx.MockTransport(endpoint)))
    later.restore({"refresh_token": "refresh-2"})
    list(later.items(lambda key: False))
    assert asked[1][1]["refresh_token"] == "refresh-2", "the next run uses the kept one"


def test_a_refused_refresh_token_is_an_auth_error():
    import httpx  # pylint: disable=import-outside-toplevel

    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS70000: expired"})

    made = oauth_source(OAuthIMAP(mailbox().messages), httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(AuthError, match="AADSTS70000: expired"):
        list(made.items(lambda key: False))
    with pytest.raises(SourceError, match="token endpoint"):
        Mail({"host": "imap.example.org", "auth": "oauth2", "token_url": "http://10.0.0.1/token"}, {}, None)


def test_a_sender_pattern_is_searched_for_by_the_server_only_when_it_is_plain():
    for pattern, searched in (
        (r"billing@shop\.example", "billing@shop.example"),
        ("@shop.example", "@shop.example"),
        ("billing|invoice", ""),
        (".*", ""),
        ("", ""),
    ):
        assert source(mailbox(), from_pattern=pattern).plain_sender() == searched, pattern
    server = mailbox()
    list(source(server, from_pattern="Shop|Friend").items(lambda key: False))
    assert ("UID", "SEARCH", None, "ALL") in server.commands


def test_uid_sets_and_headers_wherever_the_server_puts_uid_and_size():
    assert mail_source.uid_set([b"7", b"1", b"2", b"3", b"9", b"10"]) == "1:3,7,9:10"
    data = mailbox().uid("FETCH", "1:2", mail_source.HEADERS)[1]
    headers = mail_source.fetched_headers(data)
    assert set(headers) == {b"1", b"2"} and all(size > 100 for size, _ in headers.values())


class Run:  # pylint: disable=too-few-public-methods
    """The runner's record of a job's keys, and its run of the mail source (fake Nextcloud)."""

    def __init__(self):
        from test_runner import FakeLibrary  # pylint: disable=import-outside-toplevel

        self.seen: set[str] = set()
        self.library = FakeLibrary()

    def __call__(self, server, **options):
        return self.go(source(server, **options))

    def go(self, made: Mail):
        """A run of the mail source given, into the same library."""
        from harvester import runner  # pylint: disable=import-outside-toplevel
        from harvester.store import Job  # pylint: disable=import-outside-toplevel

        job = Job(user="u", name="n", source="mail", target="/In", schedule="", options={"max_items": -1})
        log: list = []
        outcome = runner.run(
            job,
            made,
            self.library,
            self.seen.__contains__,
            self.seen.add,
            lambda seconds: None,
            report=lambda level, text: log.append(text),
        )
        return outcome, log


def test_messages_done_with_are_not_read_again():
    run = Run()
    first = mailbox()
    outcome, _ = run(first, file_type="pdf")
    assert outcome == "2 downloaded"
    assert {key for key in run.seen if key.startswith(mail_source.DONE)} == {
        f"{mail_source.DONE}INBOX:1700000000:{uid}" for uid in (1, 2, 3)
    }, "the friend's photo is no pdf: its message is done with too"
    again = mailbox()
    outcome, _ = run(again, file_type="pdf")
    assert outcome == "0 downloaded"
    assert not [command for command in again.commands if command[:2] == ("UID", "FETCH")], "nothing fetched"


def test_a_message_with_an_attachment_left_for_later_is_read_again():
    run = Run()
    run.seen.add("mail:<a1@shop.example>#1:Rechnung Oktober.pdf")
    server = mailbox()
    made = source(server, file_type="pdf")
    made.remember = run.seen.add
    items = made.items(run.seen.__contains__)
    assert next(items).key.endswith("Rechnung November.pdf")  # message 3's, never taken: the run ended
    items.close()
    assert f"{mail_source.DONE}INBOX:1700000000:3" not in run.seen, "its attachment was not taken: read again"


def test_a_message_larger_than_the_largest_file_is_skipped_unread():
    run = Run()
    server = mailbox()
    server.messages[b"3"] += b"z" * 1_100_000
    outcome, log = run(server, file_type="pdf", max_size=1)
    assert outcome == "1 downloaded, 1 skipped (not of the job's file types, or too large)"
    assert any(line.startswith("Skipped imap://") and "more than the largest file" in line for line in log)
    assert ("UID", "FETCH", b"3", "(BODY.PEEK[])") not in server.commands


def test_the_log_says_what_the_search_found_and_what_each_message_gives():
    run = Run()
    outcome, log = run(mailbox(), subject_pattern="Rechnung|Invoice", file_type="pdf")
    assert outcome == "2 downloaded"
    found = next(line for line in log if line.startswith("Folder INBOX"))
    assert found == "Folder INBOX: 3 new messages, 2 from the sender and with the subject asked for"
    assert found.template["plural"] == "Folder {folder}: %n new messages, {matching}", "translated on the page"
    told = [line for line in log if line.startswith("Message from")]
    assert told == [
        "Message from billing@shop.example of 2025-10-14, “Invoice November”: 1 attachment",
        "Message from billing@shop.example of 2025-10-14, “Rechnung für Oktober”: 1 attachment",
    ], "the friend's message only counted; the logo is no pdf"
    assert log.index(found) < log.index(told[0]) < log.index(next(line for line in log if line.startswith("Saved")))
    _, log = run(mailbox(), subject_pattern="Rechnung|Invoice", file_type="pdf")
    assert "Folder INBOX: no new messages, 3 done with in earlier runs" in log
    assert not [line for line in log if line.startswith("Message from")]
    _, log = Run()(FakeIMAP({}))
    assert "Folder INBOX: no messages found" in log


def test_new_messages_none_of_the_sender_and_subject_are_a_warning_naming_the_patterns():
    said: list[tuple[str, str]] = []
    made = source(mailbox(), from_pattern="^Nobody", subject_pattern="Rechnung")
    made.report = lambda level, text: said.append((level, text))
    assert not list(made.items(lambda key: False))
    assert said == [
        (
            "warning",
            "Folder INBOX: none of the 3 new messages is from “^Nobody” with a subject like “Rechnung”",
        )
    ]
    assert said[0][1].template["params"]["subject"] == "Rechnung", "translated on the page"
    said.clear()
    made = source(FakeIMAP({b"1": mailbox().messages[b"2"]}), subject_pattern="Invoice")
    made.report = lambda level, text: said.append((level, text))
    list(made.items(lambda key: False))
    assert said == [("warning", "Folder INBOX: 1 new message, without a subject like “Invoice”")]


def test_a_message_with_nothing_to_fetch_says_so_and_a_long_list_is_cut(monkeypatch):
    run = Run()
    outcome, log = run(mailbox(), from_pattern="billing@shop", filename_pattern=r"\.xml$")
    assert outcome == "0 downloaded"
    assert (
        "Message from billing@shop.example of 2025-10-14, “Invoice November”: nothing to fetch: no attachment of the "
        "names, types and sizes asked for"
    ) in log, "without links: no word of them"
    assert f"{mail_source.DONE}INBOX:1700000000:3" in run.seen
    monkeypatch.setattr(mail_source, "TOLD", 1)
    _, log = Run()(mailbox(), file_type="pdf")
    assert len([line for line in log if line.startswith("Message from")]) == 1
    assert "2 more messages from the sender and with the subject asked for, not listed one by one" in log


def test_a_message_larger_than_the_largest_file_is_named_too():
    server = mailbox()
    server.messages[b"3"] += b"z" * 1_100_000
    _, log = Run()(server, file_type="pdf", max_size=1)
    assert (
        "Message from billing@shop.example of 2025-10-14, “Invoice November”: larger than the largest file, not read"
        in log
    )
