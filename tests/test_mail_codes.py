"""The step "type the code from e-mail" against a fake IMAP server whose messages arrive as the
step waits: the newest message of the sender and subject after the step began, its code from the
subject, the text or the HTML; old messages and other senders left alone until the wait is over;
read-only (no flags set); the code in no error; the account's mailbox for codes; the replay."""

# pylint: disable=missing-function-docstring

import imaplib
import json
import re
import time
from email.message import EmailMessage

import pytest
from test_browser import FREE, needs_chromium

from harvester.recipe import check_recipe, step_label
from harvester.sources import AuthError, SourceError
from harvester.sources import mail as mail_source
from harvester.sources import site as site_module

MAILBOX = "host = mail.example.org\nuser = me@example.org\npassword = secret\n"
STEP = {"action": "mail-code", "selector": "#code", "from": r"no-reply@shop\.example", "subject": "code|Code"}


def mail(sender: str, subject: str, text: str = "", html: str = "") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@example.org"
    message["Subject"] = subject
    if text:
        message.set_content(text)
    if html:
        if text:
            message.add_alternative(html, subtype="html")
        else:
            message.set_content(html, subtype="html")
    return message.as_bytes()


class CodeIMAP:
    """An IMAP server whose messages come in over time: each (arrival time, raw message, the poll
    from which it is there). It notes every command; one that would change a message (STORE,
    COPY, EXPUNGE) does not exist here."""

    def __init__(self, messages: list[tuple[float, bytes, int]]):
        self.messages = messages
        self.commands: list[tuple] = []
        self.polls = 0

    def login(self, user, password):
        self.commands.append(("LOGIN", user))
        if password != "secret":
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"Logged in"]

    def select(self, folder, readonly=False):
        self.commands.append(("SELECT", folder, readonly))
        return "OK", [b"1"]

    def noop(self):
        self.polls += 1
        return "OK", [b""]

    def there(self) -> dict[bytes, tuple[float, bytes]]:
        return {
            str(uid).encode(): (arrived, raw)
            for uid, (arrived, raw, poll) in enumerate(self.messages, 1)
            if poll <= self.polls
        }

    def uid(self, command, *args):
        self.commands.append(("UID", command, *args))
        there = self.there()
        if command == "SEARCH":
            return "OK", [b" ".join(sorted(there, key=int))]
        uids, what = args
        if what == "(BODY.PEEK[])":
            raw = there[uids][1]
            return "OK", [(b"1 (UID " + uids + b" BODY[] {%d}" % len(raw), raw), b")"]
        assert what.startswith("(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS")
        data: list = []
        for uid in self.expand(uids):
            arrived, raw = there[uid]
            header = raw.split(b"\n\n", 1)[0] + b"\n\n"
            date = imaplib.Time2Internaldate(arrived).encode()
            data += [(b"1 (UID " + uid + b" INTERNALDATE " + date + b" BODY[HEADER] {%d}" % len(header), header), b")"]
        return "OK", data

    @staticmethod
    def expand(text: str) -> list[bytes]:
        """An IMAP set's UIDs (1:3,7)."""
        found = []
        for part in text.split(","):
            low, _, high = part.partition(":")
            found += [str(uid).encode() for uid in range(int(low), int(high or low) + 1)]
        return found

    def logout(self):
        self.commands.append(("LOGOUT",))


class Clock:
    """The step's time: each pause moves it on, no real waiting."""

    def __init__(self):
        self.now = 1000.0
        self.pauses = 0

    def monotonic(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.pauses += 1
        self.now += seconds


@pytest.fixture(name="server")
def fake_server(monkeypatch):
    holder: dict = {}

    def connect(host, port):
        holder["host"] = (host, port)
        return holder["server"]

    monkeypatch.setattr(mail_source.Mail, "connect", staticmethod(connect))
    clock = Clock()
    monkeypatch.setattr(mail_source.time, "monotonic", clock.monotonic)
    holder["clock"] = clock
    return holder


def test_the_code_of_the_new_message_is_typed_and_nothing_is_marked(server):
    began = time.time()
    shop = "Shop <no-reply@shop.example>"
    server["server"] = CodeIMAP(
        [
            (began - 3600, mail(shop, "Your code", "Old code: 111111"), 0),
            (began + 5, mail("someone@else.example", "Your code", "Not this: 333333"), 0),
            (
                began + 20,
                mail(shop, "Sign-in code", html="<style>p{color:#123456}</style><p>Your code: <b>246810</b></p>"),
                2,
            ),
        ]
    )
    code = mail_source.code_from_mail(STEP, {"code_mailbox": MAILBOX}, began, server["clock"].pause)
    assert code == "246810"
    assert server["clock"].pauses == 2, "it waited for the message to come"
    assert server["host"] == ("mail.example.org", 993)
    commands = server["server"].commands
    assert ("SELECT", '"INBOX"', True) in commands, "read-only"
    assert all(c[1] == "SEARCH" or "PEEK" in c[-1] for c in commands if c[0] == "UID"), "only BODY.PEEK"
    assert commands[-1] == ("LOGOUT",)


def test_a_code_in_the_subject_or_with_the_steps_pattern(server):
    began = time.time()
    server["server"] = CodeIMAP([(began, mail("no-reply@shop.example", "Code 12345678 for you", "Hello"), 0)])
    assert mail_source.code_from_mail(STEP, {"code_mailbox": MAILBOX}, began, server["clock"].pause) == "12345678"
    text = "Reference 99999999. Your PIN: AB-4711"
    server["server"] = CodeIMAP([(began, mail("no-reply@shop.example", "Your code", text), 0)])
    step = STEP | {"regex": r"PIN: (?P<code>[A-Z]{2}-\d+)"}
    assert mail_source.code_from_mail(step, {"code_mailbox": MAILBOX}, began, server["clock"].pause) == "AB-4711"


def test_old_messages_and_other_senders_end_in_a_timeout_naming_no_code(server):
    began = time.time()
    server["server"] = CodeIMAP(
        [
            (began - mail_source.CODE_GRACE - 30, mail("no-reply@shop.example", "Your code", "Code 111111"), 0),
            (began + 1, mail("phish@shop.example.evil", "Your code", "Code 222222"), 1),
            (began + 2, mail("no-reply@shop.example", "Newsletter", "Win 333333"), 1),
        ]
    )
    with pytest.raises(SourceError) as failed:
        mail_source.code_from_mail(STEP | {"wait": 30}, {"code_mailbox": MAILBOX}, began, server["clock"].pause)
    assert "no code came by e-mail to mail.example.org within 30 s" in str(failed.value)
    assert not re.search(r"\d{6}", str(failed.value)), "no code in the message"
    assert server["clock"].pauses == 30 // mail_source.CODE_POLL


def test_the_mailbox_for_codes_comes_from_the_account():
    settings, sign_in = mail_source.code_mailbox(
        {
            "code_mailbox": "host: imap.example.org\nport = 143\nfolder = Codes\nnonsense\n",
            "username": "u",
            "password": "p",
        }
    )
    assert settings == {"host": "imap.example.org", "port": "143", "folder": "Codes"}
    assert sign_in == {"username": "u", "password": "p"}, "the job's own account signs in without one of its own"
    settings, sign_in = mail_source.code_mailbox(
        {"code_mailbox": "host=x.example\nauth = oauth2-google\nuser = a@b.example\nrefresh_token = r\nclient_id = c"}
    )
    assert settings["auth"] == "oauth2-google" and sign_in == {
        "username": "a@b.example",
        "refresh_token": "r",
        "client_id": "c",
    }
    with pytest.raises(AuthError, match="set the mailbox for codes"):
        mail_source.code_mailbox({"username": "u", "password": "p"})


def test_the_step_is_checked_and_labelled():
    goto = {"action": "goto", "url": "https://shop.example/login"}
    links = {"selector": "a", "pattern": "pdf"}
    assert not check_recipe({"steps": [goto, STEP | {"wait": 600}], "links": links})
    assert check_recipe({"steps": [goto, STEP | {"from": "("}], "links": links})[0].startswith(
        "step 2: the from pattern is not a regular expression"
    )
    assert check_recipe({"steps": [goto, STEP | {"wait": 5}], "links": links}) == [
        "step 2: the wait for the e-mail from 10 to 600 seconds"
    ]
    assert check_recipe({"steps": [goto, {"action": "mail-code"}], "links": links}) == ["step 2: no element"]
    assert step_label(STEP) == "type the code from e-mail (#code)"


class FakePage:
    """A page whose fields are noted as filled."""

    def __init__(self):
        self.filled: list[tuple[str, str]] = []
        self.waited = 0.0

    def fill(self, selector: str, value: str, **_options) -> None:
        self.filled.append((selector, value))

    def wait_for_timeout(self, ms: float) -> None:
        self.waited += ms

    def wait_for_load_state(self, *_args, **_options) -> None:
        pass


def test_the_replay_waits_in_the_page_and_types_the_code(server, monkeypatch):
    monkeypatch.setattr(mail_source.time, "monotonic", time.monotonic)
    server["server"] = CodeIMAP([(time.time() + 3, mail("no-reply@shop.example", "Code", "Use 777777"), 1)])
    page = FakePage()
    site_module.replay(page, STEP, {"code_mailbox": MAILBOX})
    assert page.filled == [("#code", "777777")] and page.waited == mail_source.CODE_POLL * 1000


CODE_FORM = b"""<html><body><input id="code"><button id="go"
  onclick="fetch('/done/' + document.getElementById('code').value)">Go</button></body></html>"""


@needs_chromium
def test_a_job_types_the_code_from_e_mail_in_the_browser(server, monkeypatch):
    monkeypatch.setattr(mail_source.time, "monotonic", time.monotonic)
    monkeypatch.setattr(mail_source, "CODE_POLL", 0.1)
    asked: list[str] = []

    def fulfill(route) -> None:
        asked.append(route.request.url)
        if route.request.url.endswith("/login"):
            route.fulfill(status=200, headers={"content-type": "text/html"}, body=CODE_FORM)
        else:
            route.fulfill(status=200, headers={"content-type": "text/plain"}, body=b"ok")

    monkeypatch.setattr(site_module, "guard", fulfill)
    server["server"] = CodeIMAP([(time.time() + 1, mail("no-reply@shop.example", "Your code", "Code: 135790"), 2)])
    steps = [{"action": "goto", "url": "https://shop.example/login"}, STEP, {"action": "click", "selector": "#go"}]
    recipe = {"steps": steps, "links": {"selector": "a.none", "pattern": "pdf"}}
    source = site_module.Site({"recipe": json.dumps(recipe)}, {"code_mailbox": MAILBOX}, None)
    try:
        with pytest.raises(SourceError, match="the start page has none"):
            list(source.items(lambda key: False))
    finally:
        source.close()
    assert "https://shop.example/done/135790" in asked
    assert site_module.browser.free_browsers() == FREE
