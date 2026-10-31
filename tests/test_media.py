"""The media source (yt-dlp) with a stand-in for yt-dlp: no network."""

# pylint: disable=missing-function-docstring

import io
import pathlib

import pytest
import yt_dlp

from harvester.sources import SourceError
from harvester.sources.media import Media

CHANNEL = "https://video.example/@family/videos"


def video(number: int, **extra) -> dict:
    """A video's full information, as yt-dlp gives it without downloading."""
    return {
        "id": f"v{number}",
        "title": f"Holiday/{number}: the beach",
        "ext": "mp4",
        "uploader": "Family",
        "channel": "Family",
        "extractor_key": "Example",
        "upload_date": f"2026100{number}",
        "webpage_url": f"https://video.example/watch?v={number}",
    } | extra


class FakeYoutubeDL:
    """Answers extract_info from a table of pages; process_info writes the file."""

    pages: dict[str, dict] = {}
    calls: list[tuple[str, dict]] = []

    def __init__(self, options: dict):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = True) -> dict:
        assert not download
        self.calls.append((url, self.options))
        if url not in self.pages:
            raise yt_dlp.utils.DownloadError(f"ERROR: Unsupported URL: {url}")
        return dict(self.pages[url])

    @staticmethod
    def sanitize_info(info: dict) -> dict:
        return info

    def process_info(self, info: dict) -> None:
        target = pathlib.Path(self.options["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", info["ext"]))
        target.write_bytes(b"video " + info["id"].encode())


def media(pages: dict[str, dict], **options) -> Media:
    FakeYoutubeDL.pages = pages
    FakeYoutubeDL.calls = []
    made = Media({"url": CHANNEL} | options, {}, None)
    made.downloader = FakeYoutubeDL
    return made


def test_a_channels_new_entries_with_their_names_and_values():
    flat = {
        "extractor_key": "ExampleTab",
        "entries": [
            {"id": "v1", "ie_key": "Example", "url": video(1)["webpage_url"]},
            {"id": "v2", "ie_key": "Example", "url": video(2)["webpage_url"]},
            {"id": "v3", "ie_key": "Example", "url": video(3)["webpage_url"], "live_status": "is_upcoming"},
        ],
    }
    pages = {CHANNEL: flat} | {video(n)["webpage_url"]: video(n) for n in (1, 2)}
    source = media(pages, max_entries=10)
    items = list(source.items(lambda key: key == "Example:v1"))
    assert [item.key for item in items] == ["Example:v2"]
    assert items[0].path == "Family/Holiday 2 the beach [v2].mp4"
    assert items[0].values["year"] == 2026 and items[0].values["day"] == 2
    listing_options = FakeYoutubeDL.calls[0][1]
    assert listing_options["extract_flat"] == "in_playlist" and listing_options["playlistend"] == 10


def test_a_single_video_and_its_download():
    page = video(4)["webpage_url"]
    source = media({page: video(4)}, url=page)
    (item,) = list(source.items(lambda key: False))
    assert item.key == "Example:v4"
    dest = io.BytesIO()
    source.fetch(item, dest)
    assert dest.getvalue() == b"video v4"
    options = FakeYoutubeDL.calls[-1][1]
    assert options["postprocessors"] == [] and options["noplaylist"]


def test_a_format_that_joins_streams_is_refused():
    page = video(5)["webpage_url"]
    joined = video(5, requested_formats=[{"format_id": "137"}, {"format_id": "140"}])
    source = media({page: joined}, url=page)
    (item,) = list(source.items(lambda key: False))
    with pytest.raises(SourceError, match="ffmpeg"):
        source.fetch(item, io.BytesIO())


def test_errors_and_refused_pages():
    with pytest.raises(SourceError, match="Unsupported URL"):
        list(media({}).items(lambda key: False))
    for url in ("file:///etc/passwd", "http://127.0.0.1/x", ""):
        with pytest.raises(SourceError, match="may not be opened"):
            Media({"url": url}, {}, None)
    with pytest.raises(SourceError, match="names nothing"):
        list(media({CHANNEL: video(6, webpage_url=CHANNEL)}, path="{nothing}").items(lambda key: False))
