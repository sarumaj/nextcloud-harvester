# SPDX-License-Identifier: AGPL-3.0-or-later
"""The files linked in a message's body (the mail source's "Also the files linked in the
message"): which links are taken, their download through the guarded client (httpx's
MockTransport behind it) with its redirects, names and checks, a page handed to an on-demand job
or logged, an expired link skipped once; and ondemand.hand_over's checks. No network."""

# pylint: disable=missing-function-docstring

from email.message import EmailMessage

import httpx
import pytest
from test_mail import PDF, FakeIMAP, Run

from harvester import ondemand
from harvester.sources import SourceError, download
from harvester.sources import mail as mail_source
from harvester.sources import maillinks
from harvester.store import Job, Store

HTML = """<p>Your invoice is ready.</p>
<p><a href="https://t.example/c/abc">Download your invoice</a></p>
<p><a href="https://shop.example/portal">View it in your account</a></p>
<p><a href="https://shop.example/gone.pdf">Download the old invoice</a></p>
<p><a href="https://shop.example/unsubscribe?u=1">Download nothing more (unsubscribe)</a>
<a href="mailto:help@shop.example">Download help</a> <a href="tel:+491234">Download a call</a>
<a href="https://shop.example/prefs">Abmelden</a></p>"""
RECIPE = '{"links": {"selector": "a[href$=\'.pdf\']"}}'
PLAIN = "Your invoice: https://shop.example/files/plain.pdf.\nNewsletter: https://shop.example/news"


def linking(html: str = HTML, plain: str = PLAIN, message_id: str = "<l1@shop.example>") -> bytes:
    mail = EmailMessage()
    mail["From"] = "Shop <billing@shop.example>"
    mail["To"] = "me@example.org"
    mail["Subject"] = "Your invoice"
    mail["Date"] = "Tue, 14 Oct 2025 09:30:00 +0200"
    mail["Message-ID"] = message_id
    mail["List-Unsubscribe"] = "<mailto:u@shop.example>, <https://shop.example/u/9>"
    mail.set_content(plain)
    if html:
        mail.add_alternative(html + '<a href="https://shop.example/u/9">Download</a>', subtype="html")
    return mail.as_bytes()


def site(asked: list[str]) -> httpx.Client:
    """The guarded client before a fake shop: a tracker redirecting to the invoice (named by
    Content-Disposition), a portal's sign-in page, an expired file, a PDF in the plain text."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        asked.append(url)
        if url == "https://t.example/c/abc":
            return httpx.Response(302, headers={"location": "https://shop.example/dl?id=7"})
        if url == "https://shop.example/dl?id=7":
            disposition = "attachment; filename*=UTF-8''Rechnung%20Oktober.pdf"
            return httpx.Response(
                200, content=PDF, headers={"content-type": "application/pdf", "content-disposition": disposition}
            )
        if url == "https://shop.example/portal":
            page = b"<html><form><input type=password name=p></form></html>"
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        if url == "https://shop.example/files/plain.pdf":
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        if url == "https://t.example/private":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/secret.pdf"})
        return httpx.Response(410 if url.endswith("gone.pdf") else 404)

    return download.guarded_client(transport=httpx.MockTransport(handler))


def links_source(server: FakeIMAP, client: httpx.Client, **options) -> mail_source.Mail:
    made = mail_source.Mail(
        {"host": "imap.example.org", "since_days": 0, "links": 1, **options},
        {"username": "me", "password": "secret"},
        client,
    )
    made.connect = lambda host, port: server  # type: ignore[method-assign]
    return made


def message_of(raw: bytes) -> EmailMessage:
    import email  # pylint: disable=import-outside-toplevel
    import email.policy  # pylint: disable=import-outside-toplevel

    return email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[return-value]


def test_the_links_taken_from_html_and_plain_text():
    message = message_of(linking())
    found = dict(maillinks.all_links(message))
    assert found["https://t.example/c/abc"] == "Download your invoice"
    assert found["https://shop.example/files/plain.pdf"] == "Your invoice:", "its line, without the address"
    assert not any(address.startswith(("mailto:", "tel:")) for address in found)
    taken = [address for address, _ in maillinks.wanted_links(message, "", "Download|Your invoice")]
    assert taken == [
        "https://t.example/c/abc",
        "https://shop.example/gone.pdf",
        "https://shop.example/files/plain.pdf",
    ], "no unsubscribe link (its address, its text, List-Unsubscribe's), no mailto or tel"
    assert [address for address, _ in maillinks.wanted_links(message, r"/portal$", "")] == [
        "https://shop.example/portal"
    ]
    assert not maillinks.wanted_links(message, r"/portal$", "Download"), "both patterns given: both must match"
    many = "".join(f'<a href="https://shop.example/{number}.pdf">Download</a>' for number in range(30))
    assert len(maillinks.wanted_links(message_of(linking(many, "")), "", "Download")) == maillinks.MAX_LINKS
    private = message_of(linking('<a href="http://192.168.1.1/x.pdf">Download</a>', ""))
    assert not maillinks.wanted_links(private, "", "Download"), "no private address"


def test_the_options_are_checked():
    with pytest.raises(SourceError, match="link address or link text"):
        mail_source.Mail({"host": "imap.example.org", "links": 1}, {}, None)
    with pytest.raises(SourceError, match="Link text: not a regular expression"):
        mail_source.Mail({"host": "imap.example.org", "links": 1, "link_text_pattern": "("}, {}, None)
    mail_source.Mail({"host": "imap.example.org"}, {}, None).close()
    assert mail_source.Mail.options["links"].before == 0 and mail_source.Mail.options["links"].default == 0


def test_linked_files_are_downloaded_named_and_known_by_message_and_address():
    asked: list[str] = []
    run = Run()
    server = FakeIMAP({b"1": linking()})
    with site(asked) as client:
        made = links_source(server, client, link_text_pattern="Download|Your invoice")
        outcome, log = run.go(made)
    assert outcome == "2 downloaded, 1 skipped (not of the job's file types, or too large)"
    saved = sorted(run.library.files)
    assert saved == ["/In/2025/2025-10-14 Rechnung Oktober.pdf", "/In/2025/2025-10-14 plain.pdf"]
    assert "https://shop.example/dl?id=7" in asked, "the tracker's redirect followed"
    assert any("the link expired or is closed" in line and "410" in line for line in log)
    assert f"{maillinks.KEY}<l1@shop.example>|https://t.example/c/abc" in run.seen
    assert f"{mail_source.DONE}INBOX:1700000000:1" in run.seen, "every link handled: the message is done"
    asked.clear()
    with site(asked) as client:
        outcome, _ = run.go(links_source(FakeIMAP({b"1": linking()}), client, link_text_pattern="Download"))
    assert outcome == "0 downloaded" and not asked, "nothing asked again"


def test_a_page_is_logged_once_or_handed_to_the_on_demand_job():
    asked: list[str] = []
    run = Run()
    with site(asked) as client:
        outcome, log = run.go(links_source(FakeIMAP({b"1": linking()}), client, link_pattern="/portal$"))
    assert outcome == "0 downloaded, 1 skipped (not of the job's file types, or too large)"
    assert any("a page, not a file: https://shop.example/portal" in line for line in log)
    handed: list[tuple[int, str]] = []
    run = Run()
    with site(asked) as client:
        made = links_source(FakeIMAP({b"1": linking()}), client, link_pattern="/portal$", link_job=5)
        made.hand_over = lambda job_id, url: handed.append((job_id, url)) or ""  # type: ignore[func-returns-value]
        _, log = run.go(made)
    assert handed == [(5, "https://shop.example/portal")]
    assert any("(sent to the on-demand job)" in line for line in log)
    asked.clear()
    with site(asked) as client:
        made = links_source(FakeIMAP({b"1": linking()}), client, link_pattern="/portal$", link_job=5)
        made.hand_over = lambda job_id, url: handed.append((job_id, url)) or ""  # type: ignore[func-returns-value]
        run.go(made)
    assert len(handed) == 1 and not asked, "remembered: not tried again"


def test_a_redirect_to_a_private_address_is_refused():
    asked: list[str] = []
    run = Run()
    html = '<a href="https://t.example/private">Download</a>'
    with site(asked) as client, pytest.raises(SourceError, match="may not be reached"):
        run.go(links_source(FakeIMAP({b"1": linking(html, "")}), client, link_text_pattern="Download"))
    assert "http://10.0.0.5/secret.pdf" not in asked
    assert not run.library.files and f"{maillinks.KEY}<l1@shop.example>|https://t.example/private" not in run.seen


def test_hand_over_only_to_the_persons_on_demand_website_jobs(tmp_path):
    store = Store(str(tmp_path / "h.db"))
    mail_job = store.save(Job(user="anna", name="Mail", source="mail", target="/M", schedule="0 6 * * *"))
    portal = store.save(
        Job(user="anna", name="Portal", source="site", target="/P", schedule="", options={"recipe": RECIPE})
    )
    scheduled = store.save(
        Job(user="anna", name="Daily", source="site", target="/D", schedule="0 6 * * *", options={"recipe": RECIPE})
    )
    bens = store.save(
        Job(user="ben", name="Ben's", source="site", target="/B", schedule="", options={"recipe": RECIPE})
    )
    queued: list[tuple[int, str]] = []

    def hand(target: int, url: str) -> str:
        return ondemand.hand_over(store, mail_job, target, url, lambda job: False, lambda *sent: queued.append(sent))

    url = "https://shop.example/portal"
    assert hand(portal.id or 0, url) == "" and queued == [(portal.id, url)]
    for other in (scheduled.id or 0, bens.id or 0, 999):
        assert "no on-demand website job of yours" in hand(other, url)
    assert "Not a web address" in hand(portal.id or 0, "ftp://shop.example/x")
    assert len(queued) == 1
