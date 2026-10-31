"""File stores and cloud drives through rclone: WebDAV (a Nextcloud's public share too), SFTP, FTP,
S3, Google Drive, Dropbox, OneDrive and the other remotes rclone speaks.

The job's account is an rclone configuration with one remote, as `rclone config` writes it (OAuth
tokens come from `rclone authorize` on a computer with a browser). It runs in the app's
container, which holds the app's secret, so only remotes of other machines are allowed: no local
files (the local remote, the wrappers that could point at one, keys that name a file), no
commands, no local or private hosts.

rclone refreshes OAuth tokens and writes them into its configuration: the newer configuration is
kept as the job's session and used by the next run (the stored account stays as the user gave it).
"""

import configparser
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from typing import BinaryIO

from .. import patterns
from ..browser import allowed_host, allowed_url
from .base import Item, Option, Source, SourceError

# Remotes on other machines. Not: local, and the wrappers (alias, union, combine, crypt, chunker,
# hasher, compress, cache, memory, ...) that may lead to the container's own files.
ALLOWED_TYPES = frozenset(
    {
        "azureblob",
        "b2",
        "box",
        "drive",
        "dropbox",
        "ftp",
        "gcs",
        "http",
        "koofr",
        "mega",
        "onedrive",
        "opendrive",
        "pcloud",
        "protondrive",
        "s3",
        "sftp",
        "sharefile",
        "swift",
        "webdav",
        "yandex",
    }
)
# Keys that name a file of the container or run a command in it.
FORBIDDEN_KEY = re.compile(
    r"(_file|_files|_command|_path)$|^(ssh|ca_cert|client_cert|client_key|config_dir|cache_dir|temp_dir)$"
)
# Keys that name the remote's server: a URL or a host.
SERVER_KEYS = ("url", "endpoint", "host", "auth", "auth_url", "server", "token_url", "storage_url")
TIMEOUT = 900


def parse_config(text: str) -> tuple[str, dict[str, str]]:
    """The remote's name and settings in an rclone configuration with one remote; SourceError when
    the configuration is not one, or names something the app may not reach."""
    # configparser reads an indented line as the previous value's continuation, rclone as a key of
    # its own: a command or a file could hide in a value. Every setting is one line, not indented.
    for number, line in enumerate(text.splitlines(), 1):
        if line[:1] in (" ", "\t") and line.strip():
            raise SourceError(f"line {number} of the rclone configuration is indented: settings may not be")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise SourceError(f"the rclone configuration cannot be read: {error}") from error
    sections = parser.sections()
    if len(sections) != 1:
        raise SourceError(f"the rclone configuration must hold one remote, not {len(sections)}")
    name = sections[0]
    if not re.fullmatch(r"[\w.-]+", name):
        raise SourceError(f"the remote's name {name} is not allowed")
    settings = dict(parser[name])
    kind = settings.get("type", "")
    if kind not in ALLOWED_TYPES:
        raise SourceError(f"remotes of type {kind or '(none)'} are not allowed")
    for key, value in settings.items():
        if "\n" in value or "\r" in value:
            raise SourceError(f"the remote's setting {key} spans several lines")
        if FORBIDDEN_KEY.search(key):
            raise SourceError(f"the remote's setting {key} is not allowed (a file or a command of the app)")
        if key in SERVER_KEYS and value:
            reachable = allowed_url(value) if "://" in value else allowed_host(value.split(":")[0])
            if not reachable:
                raise SourceError(f"the remote's {key} {value} may not be reached")
    return name, settings


class Remote(Source):  # pylint: disable=too-many-instance-attributes
    """A folder of an rclone remote."""

    name = "remote"
    title = "File store or cloud drive (rclone)"
    description = (
        "Files in a folder of a WebDAV, SFTP, FTP or S3 server, a public share, or Google Drive, Dropbox or "
        "OneDrive, through an rclone configuration."
    )
    credentials = ("config",)
    options = {
        "path": Option("Folder on the remote", "", "The folder on the remote (empty: its root)."),
        "include": Option("Files", "", "rclone's filter for the files, e.g. *.pdf or **/*.jpg (empty: all)."),
        "recursive": Option("With subfolders", 1, "1: also the files in the folder's subfolders."),
        "path_template": Option(
            "File name",
            "{path}",
            "Relative to the job's folder: {path} (below the remote's folder), {dir}, {filename}, {stem}, {ext}, "
            "{year}, {month}, {day} (of the file's time).",
        ),
    }
    pause = 0.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = str(self.secrets.get("config", "") or "")
        self.remote = ""
        if self.config.strip():
            self.remote, _ = parse_config(self.config)
        self.folder = str(self.settings["path"]).strip("/")
        self._dir: str | None = None
        self._written = ""
        self.binary = os.environ.get("HARVESTER_RCLONE") or shutil.which("rclone") or ""
        self.run: Callable[..., subprocess.CompletedProcess] = subprocess.run
        self.popen: Callable[..., subprocess.Popen] = subprocess.Popen

    def restore(self, session: dict) -> None:
        newer = str(session.get("rclone_config", "") or "")
        if not newer or not self.remote:
            return
        try:
            name, _ = parse_config(newer)
        except SourceError:
            return
        # Only a configuration of the same remote: a changed account wins over the kept tokens.
        if name == self.remote:
            self.config = newer

    def session(self) -> dict | None:
        if not self._dir:
            return None
        try:
            with open(self._config_path(), encoding="utf-8") as file:
                text = file.read()
        except OSError:
            return None
        return {"rclone_config": text} if text != self._written else None

    def _config_path(self) -> str:
        if self._dir is None:
            self._dir = tempfile.mkdtemp(prefix="harvester-rclone-")
            path = os.path.join(self._dir, "rclone.conf")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(self.config)
            self._written = self.config
        return os.path.join(self._dir, "rclone.conf")

    def _command(self, *args: str) -> list[str]:
        if not self.remote:
            raise SourceError("the job has no rclone configuration: set it as the job's account")
        if not self.binary:
            raise SourceError("rclone is not installed")
        return [self.binary, "--config", self._config_path(), "--ask-password=false", *args]

    def _target(self, path: str = "") -> str:
        return f"{self.remote}:{posixpath.join(self.folder, path) if path else self.folder}"

    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        arguments = ["lsjson", "--files-only", "--no-mimetype"]
        if int(self.settings["recursive"]):
            arguments.append("--recursive")
        if str(self.settings["include"]).strip():
            arguments += ["--include", str(self.settings["include"]).strip()]
        result = self.run(
            self._command(*arguments, self._target()), capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
        if result.returncode != 0:
            raise SourceError(f"rclone could not list {self._target()}: {last_line(result.stderr)}")
        try:
            entries = json.loads(result.stdout or "[]")
        except ValueError as error:
            raise SourceError(f"rclone listed {self._target()} unreadably: {error}") from error
        # Newest first: a run stopped by max_items has the newest files.
        for entry in sorted(entries, key=lambda entry: entry.get("ModTime", ""), reverse=True):
            if entry.get("IsDir"):
                continue
            path = str(entry["Path"])
            key = self._target(path)
            if seen(key):
                continue
            filename = posixpath.basename(path)
            stem, ext = posixpath.splitext(filename)
            values: dict = {
                "path": path,
                "dir": posixpath.dirname(path),
                "filename": filename,
                "stem": stem,
                "ext": ext,
                "size": int(entry.get("Size", 0) or 0),
            }
            date = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(entry.get("ModTime", "")))
            if date:
                values |= {"year": int(date[1]), "month": int(date[2]), "day": int(date[3])}
            try:
                target = patterns.render_path(str(self.settings["path_template"]), values)
            except (KeyError, ValueError) as error:
                raise SourceError(str(error).strip("'\"")) from error
            yield Item(key=key, path=target, url=key, values=values)

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        # rclone's messages go to a file: a pipe read only at the end could fill up and stall it.
        with tempfile.TemporaryFile() as messages:
            with self.popen(self._command("cat", item.url), stdout=subprocess.PIPE, stderr=messages) as process:
                assert process.stdout is not None
                for chunk in iter(lambda: process.stdout.read(1 << 20), b""):  # type: ignore[union-attr]
                    dest.write(chunk)
                code = process.wait(timeout=TIMEOUT)
            if code != 0:
                messages.seek(0)
                error = messages.read().decode("utf-8", "replace")
                raise SourceError(f"rclone could not read {item.url}: {last_line(error)}")

    def close(self) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None


def last_line(text: str) -> str:
    """The last line rclone wrote, which says what went wrong."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1][-300:] if lines else "no message"
