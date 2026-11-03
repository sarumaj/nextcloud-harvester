# SPDX-License-Identifier: AGPL-3.0-or-later
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


def test_a_format_of_separate_streams_is_joined_by_yt_dlp(monkeypatch, tmp_path):
    page = video(5)["webpage_url"]
    joined = video(5, requested_formats=[{"format_id": "137"}, {"format_id": "140"}])
    source = media({page: joined}, url=page)
    (item,) = list(source.items(lambda key: False))
    dest = io.BytesIO()
    source.fetch(item, dest)
    assert dest.getvalue() == b"video v5"
    options = FakeYoutubeDL.calls[-1][1]
    assert options["merge_output_format"] == "mp4/mkv", "mp4 where the codecs fit, else mkv"
    assert "ffmpeg_location" not in options, "no wrappers here: yt-dlp looks in PATH"
    monkeypatch.setattr("harvester.sources.media.FFMPEG", str(tmp_path))
    assert source.base()["ffmpeg_location"] == str(tmp_path), "the image's wrappers, without the app's environment"


def test_errors_and_refused_pages():
    with pytest.raises(SourceError, match="Unsupported URL"):
        list(media({}).items(lambda key: False))
    for url in ("file:///etc/passwd", "http://127.0.0.1/x", ""):
        with pytest.raises(SourceError, match="may not be opened"):
            Media({"url": url}, {}, None)
    # A name that cannot be made: that entry fails alone, in the run, saying what lacks the value.
    (item,) = media({CHANNEL: video(6, webpage_url=CHANNEL)}, path="{nothing}").items(lambda key: False)
    assert "names nothing, which the entry's information does not give" in str(item.problem)


def test_impersonation_is_yt_dlps_own_for_the_job_that_asks():
    from yt_dlp.networking.impersonate import ImpersonateTarget  # pylint: disable=import-outside-toplevel

    from harvester.sources.media import explained  # pylint: disable=import-outside-toplevel

    page = video(1)["webpage_url"]
    assert "impersonate" not in media({page: video(1)}, url=page).base(), "off by default"
    options = media({page: video(1)}, url=page, impersonate=1).base()
    assert options["impersonate"] == ImpersonateTarget()
    assert options["extractor_args"] == {"generic": {"impersonate": [""]}}
    error = Exception(
        "ERROR: [generic] 403 caused by Cloudflare anti-bot challenge; ... --extractor-args generic:impersonate"
    )
    assert "Impersonate a browser" in explained(error, {})
    assert "Impersonate a browser" not in explained(error, {"impersonate": 1}), "already on: no hint"
