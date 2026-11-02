"""Videos and audio from the sites yt-dlp knows (YouTube, Vimeo, podcasts' pages, ...): a video, or
the newest entries of a playlist or channel.

A format of separate video and audio streams (YouTube's best, from 720p up) is joined with ffmpeg
into one file: mp4 where the streams' codecs allow it, else mkv. ffmpeg runs through FFMPEG, without
the app's environment.

The account's cookies go to yt-dlp as a cookies.txt file (0600, removed when the run ends); its
user name and password as yt-dlp's own, for the sites whose extractors sign in.
"""

import contextlib
import datetime
import os
import pathlib
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

import yt_dlp

from .. import browser, cookies, patterns
from ..browser import allowed_url
from .base import ACCOUNT_HELP, Item, Option, Source, SourceError, Stopped, stop_requested

# The quiet, offline-safe options every YoutubeDL here starts from.
# Timeouts and retries: a stalled server must not hold the executor (one run at a time).
BASE = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "noplaylist": True,
    "socket_timeout": 60,
    "retries": 3,
    "fragment_retries": 3,
}
# Where yt-dlp finds ffmpeg and ffprobe: the image's wrappers, which start them without the app's
# environment (its secret). Elsewhere (tests, a computer without them) yt-dlp looks in PATH.
FFMPEG = "/usr/local/lib/harvester-ffmpeg"
# The joined file's container: mp4 when the streams fit in it (H.264 or AV1 with AAC), else mkv,
# which takes any (VP9 with Opus, YouTube's usual); never re-encoded.
MERGED = "mp4/mkv"
# Entries that are not (yet) a file to keep.
SKIPPED = {"is_live", "is_upcoming", "post_live"}


def safe(text: str) -> str:
    """Text for a file or folder name: no separators or characters file systems refuse."""
    return re.sub(r"[\\/:*?\"<>|\s]+", " ", str(text)).strip()[:150] or "untitled"


def dated(value: str | None) -> dict:
    """year, month and day of a YYYYMMDD date, when there is one."""
    try:
        day = datetime.datetime.strptime(str(value), "%Y%m%d").date()
    except (TypeError, ValueError):
        return {}
    return {"year": day.year, "month": day.month, "day": day.day}


def cancel_when_stopped(_progress: dict) -> None:
    """yt-dlp's progress hook: cancels the download when the job was stopped."""
    if stop_requested():
        raise yt_dlp.utils.DownloadCancelled("stopped")


class Media(Source):
    """A video, playlist or channel on a site yt-dlp supports."""

    name = "media"
    title = "Videos and audio (yt-dlp)"
    description = (
        "Videos and audio from YouTube, Vimeo and the other sites yt-dlp supports: a video, playlist or channel."
    )
    # Members-only and age-restricted videos: a signed-in browser's cookies (YouTube's way), or a
    # user name and password where yt-dlp's extractor signs in (Vimeo, Nebula, ...).
    credentials = ("username", "password", "cookies")
    credential_help = ACCOUNT_HELP | {
        "username": ("User name", "For the sites where yt-dlp signs in itself (not YouTube: use cookies)."),
        "password": ("Password", "The account's password."),
    }
    options = {
        "url": Option(
            "Page",
            "",
            "A video, playlist or channel page.",
            kind="url",
            placeholder="https://www.youtube.com/@channel/videos",
        ),
        "format": Option(
            "Format",
            "bv*+ba/b",
            "yt-dlp's format selection: bv*+ba/b joins the best video and the best audio (into mp4 where the "
            "codecs allow, mkv otherwise), [height<=1080] caps the picture, ba takes the audio alone.",
            suggestions=(
                "bv*+ba/b",
                "bv*[height<=1080]+ba/b[height<=1080]",
                "bv*[height<=720]+ba/b[height<=720]",
                "bv*[height<=480]+ba/b[height<=480]",
                "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "best[acodec!=none][vcodec!=none]/best",
                "ba[ext=m4a]/ba/b",
                "ba/b",
            ),
        ),
        "max_entries": Option(
            "Entries per run",
            30,
            "The newest entries of a playlist or channel looked at.",
            minimum=1,
            suggestions=("5", "15", "30", "100"),
        ),
        "path": Option(
            "File name",
            "{uploader}/{title} [{id}].{ext}",
            "Relative to the folder: {title}, {id}, {uploader}, {channel}, {extractor}, {ext}, {year}, {month}, "
            "{day}.",
            kind="template",
            values=("uploader", "channel", "title", "id", "extractor", "year", "month", "day", "ext"),
            suggestions=(
                "{uploader}/{title} [{id}].{ext}",
                "{year}/{year}-{month:02d}-{day:02d} {title} [{id}].{ext}",
                "{year}/{title} [{id}].{ext}",
                "{title} [{id}].{ext}",
            ),
        ),
    }
    pause = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = str(self.settings["url"]).strip()
        if not allowed_url(self.url):
            raise SourceError(f"the page {self.url or '(none)'} may not be opened")
        if int(self.settings["max_entries"]) < 1:
            raise SourceError("at least one entry per run")
        # The class used, so that tests can stand in for yt-dlp.
        self.downloader: Callable[[dict], Any] = yt_dlp.YoutubeDL
        self.cookie_file: str | None = None

    def base(self) -> dict:
        """BASE with the account: its cookies' file (written once per run), user name, password."""
        account: dict = {"proxy": browser.PROXY} if self.home and browser.PROXY else {}
        if self.cookie_file is None:
            found = self.account_cookies(self.url)
            self.cookie_file = cookies.to_file(found) if found else ""
        if self.cookie_file:
            account["cookiefile"] = self.cookie_file
        if self.secrets.get("username"):
            account |= {"username": self.secrets["username"], "password": self.secrets.get("password", "")}
        tools = {"ffmpeg_location": FFMPEG} if os.path.isdir(FFMPEG) else {}
        return BASE | tools | {"merge_output_format": MERGED} | account

    def close(self) -> None:
        if self.cookie_file:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.cookie_file)
        self.cookie_file = None

    def _info(self, url: str, **options) -> dict:
        try:
            with self.downloader(self.base() | options) as ydl:
                return ydl.sanitize_info(ydl.extract_info(url, download=False))
        except yt_dlp.utils.DownloadError as error:
            raise SourceError(f"yt-dlp: {str(error).removeprefix('ERROR: ')}") from error

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        listing = self._info(
            self.url,
            extract_flat="in_playlist",
            playlistend=int(self.settings["max_entries"]),
            noplaylist=False,
            format=str(self.settings["format"]),
        )
        entries = listing.get("entries")
        for entry in [listing] if entries is None else entries:
            if not entry or not entry.get("id"):
                continue
            key = f"{entry.get('ie_key') or entry.get('extractor_key') or listing.get('extractor_key')}:{entry['id']}"
            if seen(key) or entry.get("live_status") in SKIPPED:
                continue
            page = entry.get("webpage_url") or entry.get("url")
            if not page or not allowed_url(page):
                continue
            # A flat entry lacks title, extension and date: the full information without the file.
            # An entry that cannot be had (private, removed, region-locked, no single-file format)
            # is skipped, not the run's end: else each run would stop at it again.
            try:
                info = entry if entries is None else self._info(page, format=str(self.settings["format"]))
            except SourceError:
                if entries is None:
                    raise
                continue
            if info.get("live_status") in SKIPPED:
                continue
            yield self.item(key, info)

    def item(self, key: str, info: dict) -> Item:
        """The entry's file: its name from the template and its values."""
        ext = str(info.get("ext") or "mp4")
        values = {
            "title": safe(info.get("title") or info.get("id", "")),
            "id": safe(info.get("id", "")),
            "uploader": safe(info.get("uploader") or info.get("channel") or "unknown"),
            "channel": safe(info.get("channel") or info.get("uploader") or "unknown"),
            "extractor": safe(info.get("extractor_key") or info.get("extractor") or ""),
            "ext": ext,
            "url": info.get("webpage_url") or "",
        } | dated(info.get("upload_date"))
        values |= {"filename": f"{values['title']}.{ext}", "stem": values["title"]}
        try:
            path = patterns.render_path(str(self.settings["path"]), values)
        except (KeyError, ValueError) as error:
            raise SourceError(str(error).strip("'\"")) from error
        return Item(key=key, path=path, url=values["url"], values=values)

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        if not allowed_url(item.url):
            raise SourceError(f"{item.url} may not be opened")
        with tempfile.TemporaryDirectory(prefix="harvester-media-") as directory:
            options = self.base() | {
                "format": str(self.settings["format"]),
                "outtmpl": str(pathlib.Path(directory) / "%(id)s.%(ext)s"),
                # Only the joining of streams (base's merge_output_format), no other processing.
                "postprocessors": [],
                # yt-dlp reports its progress often: a stop ends the download there.
                "progress_hooks": [cancel_when_stopped],
            }
            try:
                with self.downloader(options) as ydl:
                    info = ydl.extract_info(item.url, download=False)
                    ydl.process_info(info)
            except (yt_dlp.utils.DownloadError, yt_dlp.utils.DownloadCancelled) as error:
                if stop_requested():
                    raise Stopped("stopped") from error
                raise SourceError(f"yt-dlp: {str(error).removeprefix('ERROR: ')}") from error
            files = [path for path in pathlib.Path(directory).iterdir() if path.is_file()]
            if len(files) != 1:
                raise SourceError(f"yt-dlp left {len(files)} files for {item.url}")
            with files[0].open("rb") as produced:
                shutil.copyfileobj(produced, dest)
