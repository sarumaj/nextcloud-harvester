"""The feed source against a fake podcast (RSS) and a fake Atom feed (httpx's MockTransport)."""

# pylint: disable=missing-function-docstring

import io

import httpx
import pytest

from harvester.sources import SourceError
from harvester.sources.feed import Feed

PODCAST = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>The Show</title>
<item><title>Episode 1: Hello</title><guid>ep1</guid><pubDate>Mon, 06 Oct 2025 08:00:00 +0000</pubDate>
<enclosure url="https://cdn.example.org/show/ep1.mp3" length="5" type="audio/mpeg"/></item>
<item><title>Episode 2: More / less?</title><guid>ep2</guid><pubDate>Mon, 13 Oct 2025 08:00:00 +0000</pubDate>
<author>host@example.org (Host)</author>
<enclosure url="https://cdn.example.org/show/ep2.mp3" length="5" type="audio/mpeg"/></item>
<item><title>Notes</title><guid>notes</guid><pubDate>Tue, 14 Oct 2025 08:00:00 +0000</pubDate>
<enclosure url="https://cdn.example.org/show/notes.pdf" length="5" type="application/pdf"/></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Reports</title><id>urn:reports</id>
<updated>2025-10-20T10:00:00Z</updated>
<entry><title>Q3 report</title><id>urn:q3</id><updated>2025-10-20T10:00:00Z</updated>
<link href="https://reports.example.org/q3.pdf"/></entry>
<entry><title>Q2 report</title><id>urn:q2</id><updated>2025-07-20T10:00:00Z</updated>
<link href="https://reports.example.org/q2.pdf"/></entry>
</feed>"""
MP3 = b"ID3" + b"\x00" * 200


def client(pauses: list | None = None) -> httpx.Client:
    """A client to the fake feeds and their files."""
    asked: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        asked[url] = asked.get(url, 0) + 1
        if url == "https://example.org/podcast.rss":
            return httpx.Response(200, content=PODCAST, headers={"content-type": "application/rss+xml"})
        if url == "https://example.org/reports.atom":
            return httpx.Response(200, content=ATOM, headers={"content-type": "application/atom+xml"})
        if url.endswith(".mp3"):
            if pauses is not None and asked[url] == 1:
                return httpx.Response(429, headers={"retry-after": "3"})
            return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
        if url.endswith(".pdf"):
            return httpx.Response(200, content=b"%PDF-1.7 x", headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_podcast_episodes_newest_first_with_their_values():
    pauses: list[float] = []
    made = Feed(
        {"url": "https://example.org/podcast.rss", "link_pattern": r"\.mp3$", "file_type": "audio"}, {}, client(pauses)
    )
    made.sleep = pauses.append
    items = list(made.items(lambda key: False))
    assert [item.values["title"] for item in items] == ["Episode 2 More less", "Episode 1 Hello"]
    assert items[0].path == "The Show/2025/Episode 2 More less.mp3"
    assert items[0].key == "feed:ep2|https://cdn.example.org/show/ep2.mp3"
    assert (items[0].values["year"], items[0].values["month"], items[0].values["day"]) == (2025, 10, 13)
    assert items[0].values["feed"] == "The Show" and items[0].values["filename"] == "ep2.mp3"
    dest = io.BytesIO()
    made.fetch(items[0], dest)
    assert dest.getvalue() == MP3 and pauses == [3.0]


def test_known_episodes_are_left_out():
    made = Feed({"url": "https://example.org/podcast.rss"}, {}, client())
    known = {"feed:ep2|https://cdn.example.org/show/ep2.mp3", "feed:notes|https://cdn.example.org/show/notes.pdf"}
    assert [item.values["filename"] for item in made.items(known.__contains__)] == ["ep1.mp3"]


def test_atom_entries_links():
    made = Feed({"url": "https://example.org/reports.atom", "take": "links", "path": "{year}/{filename}"}, {}, client())
    items = list(made.items(lambda key: False))
    assert [item.path for item in items] == ["2025/q3.pdf", "2025/q2.pdf"]
    assert items[0].key == "feed:urn:q3|https://reports.example.org/q3.pdf"


def test_the_wrong_type_is_refused():
    made = Feed({"url": "https://example.org/podcast.rss", "file_type": "audio"}, {}, client())
    notes = next(item for item in made.items(lambda key: False) if item.url.endswith(".pdf"))
    with pytest.raises(SourceError, match="not audio"):
        made.fetch(notes, io.BytesIO())


def test_settings_are_checked_without_fetching():
    for options, problem in (
        ({}, "no feed"),
        ({"url": "file:///etc/passwd"}, "may not be opened"),
        ({"url": "http://10.0.0.1/feed"}, "may not be opened"),
        ({"url": "https://example.org/f", "take": "all"}, "take is"),
        ({"url": "https://example.org/f", "link_pattern": "("}, "not a regular expression"),
    ):
        with pytest.raises(SourceError, match=problem):
            Feed(options, {}, None)


def test_not_a_feed():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>hello</html>", headers={"content-type": "text/html"})

    made = Feed({"url": "https://example.org/x"}, {}, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SourceError, match="not a feed"):
        list(made.items(lambda key: False))


def test_relative_links_are_taken_against_the_feeds_address():
    atom = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Docs</title><id>urn:docs</id><updated>2025-05-01T00:00:00Z</updated>
<entry><title>Report</title><id>urn:r1</id><updated>2025-05-01T00:00:00Z</updated>
<link rel="enclosure" href="/files/report.pdf" type="application/pdf"/></entry></feed>"""
    serving = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=atom)))
    source = Feed({"url": "https://docs.example/feed.atom"}, {}, serving)
    assert [item.url for item in source.items(lambda key: False)] == ["https://docs.example/files/report.pdf"]
