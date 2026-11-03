# SPDX-License-Identifier: AGPL-3.0-or-later
"""Downloads that impersonate a browser (curl_cffi under httpx): the transport only when the job
asks, the guarded client's check on every redirect, cookies and headers kept by httpx, the answer
in chunks under the size limit, the job's proxy; with a fake curl session (no network), and one
real request where the network is there."""

# pylint: disable=missing-function-docstring

import io
import socket

import httpx
import pytest

from harvester.sources import Refused, SourceError, download, impersonate
from harvester.sources.feed import Feed
from harvester.sources.releases import Releases
from harvester.sources.site import Site


class Headers:  # pylint: disable=too-few-public-methods
    """curl_cffi's headers, as the transport reads them."""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = pairs

    def multi_items(self) -> list[tuple[str, str]]:
        return self.pairs


class Answer:
    """A curl_cffi answer: its chunks are counted as they are read."""

    def __init__(self, status: int, headers: list[tuple[str, str]], chunks: list[bytes]):
        self.status_code = status
        self.headers = Headers(headers)
        self.chunks = chunks
        self.read = 0
        self.closed = False

    def iter_content(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


class Session:
    """A fake curl_cffi session: the answers by address, and the requests it was asked."""

    def __init__(self, answers: dict[str, Answer]):
        self.answers = answers
        self.asked: list[dict] = []

    def request(self, method: str, url: str, **options) -> Answer:
        self.asked.append({"method": method, "url": url} | options)
        return self.answers[url]

    def close(self) -> None:
        pass


@pytest.fixture(name="curl")
def fake_curl(monkeypatch):
    sessions: list[Session] = []

    def make(answers: dict[str, Answer]) -> Session:
        session = Session(answers)
        sessions.append(session)
        monkeypatch.setattr(impersonate, "curl_session", lambda: session)
        return session

    return make


PDF = b"%PDF-1.7\n" + b"x" * 2000


def test_the_transport_is_curls_only_when_the_job_asks(curl):
    curl({})
    # pylint: disable=protected-access
    with download.guarded_client() as plain:
        assert not isinstance(plain._transport, impersonate.CurlTransport)
    with download.guarded_client(True) as posing:
        assert isinstance(posing._transport, impersonate.CurlTransport)
    assert download.impersonates({"impersonate": "1"}) and not download.impersonates({})
    for source in (Site, Feed, Releases):
        assert source.options["impersonate"].kind == "flag", source.name


def test_every_redirect_is_checked_and_httpx_follows_and_keeps_the_cookies(curl):
    session = curl(
        {
            "https://files.example/a": Answer(
                302, [("location", "https://cdn.example/a.pdf"), ("set-cookie", "s=1")], []
            ),
            "https://cdn.example/a.pdf": Answer(200, [("content-type", "application/pdf")], [PDF]),
            "https://files.example/inside": Answer(302, [("location", "http://10.0.0.26/secret")], []),
        }
    )
    hops: list[str] = []
    hooks = {"request": [lambda request: hops.append(str(request.url))]}
    with download.guarded_client(True, event_hooks=hooks, headers={"Referer": "https://files.example/"}) as client:
        dest = io.BytesIO()
        download.fetch(download.http(client), "https://files.example/a", dest, {}, lambda seconds: None)
        assert dest.getvalue() == PDF
        assert hops == ["https://files.example/a", "https://cdn.example/a.pdf"], "each hop a request of httpx's"
        assert all(not asked["allow_redirects"] and asked["stream"] for asked in session.asked)
        assert all(asked["discard_cookies"] for asked in session.asked), "httpx's jar, not curl's"
        assert client.cookies.get("s") == "1"
        sent = session.asked[0]["headers"]
        assert sent["Referer" if "Referer" in sent else "referer"] == "https://files.example/"
        assert not any(key.lower() == "user-agent" for key in sent), "Chrome's own, not the app's"
        with pytest.raises(SourceError, match="may not be reached"):
            download.fetch(download.http(client), "https://files.example/inside", io.BytesIO(), {}, lambda s: None)
    assert len(session.asked) == 3, "the private address is never asked for"


def test_the_answer_comes_in_chunks_and_stops_at_the_largest_file(curl, monkeypatch):
    big = Answer(200, [("content-type", "application/pdf")], [PDF] + [b"x" * 1000] * 1000)
    curl({"https://files.example/big.pdf": big})
    monkeypatch.setattr(download, "MB", 1000)
    with download.guarded_client(True) as client:
        with pytest.raises(Refused, match="larger than the job's largest file"):
            download.fetch(
                download.http(client), "https://files.example/big.pdf", io.BytesIO(), {"max_size": 5}, lambda s: 0
            )
    assert big.read < 10 and big.closed, "not read to its end"


def test_a_decoded_answer_loses_its_encoding_and_length(curl):
    curl(
        {
            "https://files.example/z": Answer(
                200,
                [("content-encoding", "gzip"), ("content-length", "10"), ("content-type", "text/plain")],
                [b"plain"],
            )
        }
    )
    with download.guarded_client(True) as client:
        answer = client.get("https://files.example/z")
    assert answer.content == b"plain" and "content-encoding" not in answer.headers
    assert "content-length" not in answer.headers or answer.headers["content-length"] == "5"


def test_the_jobs_proxy_goes_to_curl(curl):
    session = curl({"https://files.example/a.pdf": Answer(200, [], [PDF])})
    with download.guarded_client(True, proxy="socks5h://home.example:1080") as client:
        client.get("https://files.example/a.pdf")
    assert session.asked[0]["proxy"] == "socks5h://home.example:1080"
    session = curl({"https://files.example/a.pdf": Answer(200, [], [PDF])})
    with download.guarded_client(True) as client:
        client.get("https://files.example/a.pdf")
    assert session.asked[0]["proxy"] is None


def test_curls_failures_are_httpx_transport_errors(curl):
    from curl_cffi.requests import exceptions  # pylint: disable=import-outside-toplevel

    session = curl({})

    def fail(method, url, **options):  # pylint: disable=unused-argument
        raise exceptions.ConnectionError("Failed to connect")

    session.request = fail
    with download.guarded_client(True) as client:
        with pytest.raises(httpx.TransportError):
            client.get("https://files.example/a.pdf")


def network() -> bool:
    try:
        socket.create_connection(("www.cloudflare.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not network(), reason="no network here")
def test_a_real_request_presents_itself_as_chrome():
    try:
        with download.guarded_client(True, timeout=20) as client:
            answer = client.get("https://www.cloudflare.com/cdn-cgi/trace")
    except httpx.TransportError as error:
        pytest.skip(f"the network refused: {error}")
    assert answer.status_code == 200
    assert "Chrome/" in answer.text, "the user agent the site saw is the browser's"
