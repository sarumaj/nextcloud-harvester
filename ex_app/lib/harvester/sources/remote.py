# SPDX-License-Identifier: AGPL-3.0-or-later
"""File stores and cloud drives through rclone: WebDAV (a Nextcloud's public share too), SFTP, FTP,
SMB, S3, Google Drive, Dropbox, OneDrive and the other storage services rclone speaks
(ALLOWED_TYPES; REFUSED_TYPES says why the rest are not).

The job's account is an rclone configuration with one remote, as `rclone config` writes it (OAuth
tokens come from `rclone authorize` on a computer with a browser). It runs in the app's
container, which holds the app's secret, so only remotes of other machines are allowed: no local
files (the local remote, the wrappers that could point at one, keys that name a file), no
commands, no local or private hosts.

rclone refreshes OAuth tokens and writes them into its configuration: the newer configuration is
kept as the job's session and used by the next run (the stored account stays as the user gave it).
"""

import configparser
import hashlib
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
from ..browser import allowed_host, allowed_url, child_env
from .base import Item, Option, Source, SourceError, check_stop, killed_on_stop, stoppable_run

# rclone's remotes that may not be used, and why (scripts/rclone_backends.py lists the rest from
# rclone itself; a test fails while a remote of the pinned rclone is in neither list, so a new
# one is looked at before it is offered).
REFUSED_TYPES = {
    "local": "the container's own files (the app's secret, every person's jobs)",
    "alias": "wraps another remote, which may be a local path",
    "archive": "wraps another remote, which may be a local path",
    "cache": "wraps another remote, which may be a local path",
    "chunker": "wraps another remote, which may be a local path",
    "combine": "wraps other remotes, which may be local paths",
    "compress": "wraps another remote, which may be a local path",
    "crypt": "wraps another remote, which may be a local path",
    "hasher": "wraps another remote, which may be a local path",
    "union": "wraps other remotes, which may be local paths",
    "memory": "keeps nothing: there is nothing to fetch",
    "doi": "its server comes from the DOI's resolution, which the app cannot check beforehand",
    "hdfs": "a cluster in a private network, signed in with Kerberos files of the machine",
    "sia": "its daemon's address defaults to this machine (127.0.0.1)",
    "oos": "signs in with the machine's cloud identity or a configuration file of the machine",
}
# Remotes on other machines, whose server the configuration names (SERVER_KEYS) or rclone knows.
ALLOWED_TYPES = frozenset(
    {
        "azureblob",
        "azurefiles",
        "b2",
        "box",
        "cloudinary",
        "drime",
        "drive",
        "dropbox",
        "fichier",
        "filefabric",
        "filelu",
        "filen",
        "filescom",
        "ftp",
        "gcs",
        "gofile",
        "gphotos",
        "hidrive",
        "http",
        "huaweidrive",
        "iclouddrive",
        "imagekit",
        "internetarchive",
        "internxt",
        "jottacloud",
        "koofr",
        "linkbox",
        "mailru",
        "mega",
        "netstorage",
        "onedrive",
        "opendrive",
        "pcloud",
        "pikpak",
        "pixeldrain",
        "premiumizeme",
        "protondrive",
        "putio",
        "qingstor",
        "quatrix",
        "s3",
        "seafile",
        "sftp",
        "shade",
        "sharefile",
        "smb",
        "storj",
        "sugarsync",
        "swift",
        "tardigrade",
        "ulozto",
        "webdav",
        "yandex",
        "zoho",
    }
)
# Keys that name a file of the container or run a command in it.
FORBIDDEN_KEY = re.compile(
    r"(_file|_files|_command|_path)$|^(ssh|ca_cert|client_cert|client_key|config_dir|cache_dir|temp_dir)$"
)
# Keys that, set, have rclone take credentials from where it runs instead of the configuration:
# the app's environment, or the cloud's metadata service (S3's, Azure's and Swift's env_auth).
# The local emulator (Azure's, at 127.0.0.1) and Kerberos (a credential cache of the machine) too.
AMBIENT_KEYS = ("env_auth", "use_msi", "use_emulator", "use_kerberos")
# Keys that name the remote's server: a URL or a host.
# Each allowed remote's settings that name one, as rclone calls them (a test checks rclone's list).
SERVER_KEYS = (
    "url",
    "endpoint",
    "host",
    "hostname",
    "auth",
    "auth_url",
    "server",
    "token_url",
    "storage_url",
    "sas_url",
    "download_url",
    "api_url",
    "sts_endpoint",
    "ibm_iam_endpoint",
    "front_endpoint",
    "satellite_address",
    "upload_prefix",
    "tenant_url",
)
TIMEOUT = 900
LOG_PREFIX = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d [A-Z]+\s*: (Fatal error: )?")


def reachable_server(value: str) -> bool:
    """Whether a remote's server setting names a host the app may reach: an address, or a host
    with what rclone's remotes put around it (a node's id@, a :port, a /path: Storj, NetStorage)."""
    if "://" in value:
        return allowed_url(value)
    host = value.rpartition("@")[2].split("/", 1)[0]
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return bool(host) and allowed_host(host)


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
        if key in AMBIENT_KEYS and value.strip().lower() not in ("", "false", "0", "no"):
            raise SourceError(f"the remote's setting {key} is not allowed (credentials from the app's machine)")
        if key in SERVER_KEYS and value and not reachable_server(value):
            raise SourceError(f"the remote's {key} {value} may not be reached")
    return name, settings


class Remote(Source):  # pylint: disable=too-many-instance-attributes
    """A folder of an rclone remote."""

    name = "remote"
    title = "File store or cloud drive (rclone)"
    description = (
        "Files in a folder of a WebDAV, SFTP, FTP, SMB or S3 server, a public share, or Google Drive, Dropbox, "
        "OneDrive, pCloud, Mega, Proton Drive and the other storage services rclone speaks."
    )
    credentials = ("config",)
    options = {
        "path": Option(
            "Folder on the remote", "", "The folder on the remote (empty: its root).", placeholder="Documents/Invoices"
        ),
        "include": Option(
            "Files",
            "",
            "rclone's filter for the files, e.g. *.pdf or **/*.jpg (empty: all).",
            placeholder="*.pdf",
            suggestions=("*.pdf", "*.{jpg,jpeg,png,heic}", "*.{mp4,mov}", "*.{doc,docx,odt,pdf}"),
        ),
        "recursive": Option("With subfolders", 1, "Also the files in the folder's subfolders.", kind="flag"),
        "path_template": Option(
            "File name",
            "{path}",
            "Relative to the job's folder: {path} (below the remote's folder), {dir}, {filename}, {stem}, {ext}, "
            "{year}, {month}, {day} (of the file's time).",
            kind="template",
            values=("path", "dir", "filename", "stem", "ext", "year", "month", "day"),
            suggestions=("{path}", "{filename}", "{year}/{filename}", "{year}/{month:02d}/{filename}"),
        ),
    }
    pause = 0.0
    listing_options = ("path", "include", "recursive")

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
        # rclone is killed when the job is stopped.
        self.run: Callable[..., subprocess.CompletedProcess] = stoppable_run
        self.popen: Callable[..., subprocess.Popen] = subprocess.Popen

    def restore(self, session: dict) -> None:
        newer = str(session.get("rclone_config", "") or "")
        if not newer or not self.remote:
            return
        # Only the configuration kept from this account: one set up anew (the wizard names every
        # remote "remote") wins over the tokens kept from the one before. A session kept before
        # it named its account (before 0.8.2) counts while the remote's name is the same.
        if "account" in session and session["account"] != account_sum(self.secrets.get("config", "")):
            return
        try:
            name, _ = parse_config(newer)
        except SourceError:
            return
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
        if text == self._written:
            return None
        return {"rclone_config": text, "account": account_sum(self.secrets.get("config", ""))}

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
            self._command(*arguments, self._target()),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
            env=child_env(),
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
                target = patterns.render_path(str(self.settings["path_template"]), values, "the remote's file")
            except (KeyError, ValueError) as error:
                yield Item(key=key, path="", url=key, values=values, problem=SourceError(str(error).strip("'\"")))
                continue
            yield Item(key=key, path=target, url=key, values=values)

    def fetch(self, item: Item, dest: BinaryIO) -> None:
        # rclone's messages go to a file: a pipe read only at the end could fill up and stall it.
        with tempfile.TemporaryFile() as messages:
            command = self._command("cat", item.url)
            with self.popen(command, stdout=subprocess.PIPE, stderr=messages, env=child_env()) as process:
                assert process.stdout is not None
                with killed_on_stop(process):
                    for chunk in iter(lambda: process.stdout.read(1 << 20), b""):  # type: ignore[union-attr]
                        check_stop()
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


def account_sum(config: str) -> str:
    """Which account (the configuration its user gave) a kept configuration comes from."""
    return hashlib.sha256(str(config or "").encode("utf-8")).hexdigest()


def last_line(text: str) -> str:
    """The last line rclone wrote, which says what went wrong."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    # Without rclone's log prefix (time, level, "Fatal error:"): the reason is what the page needs.
    return LOG_PREFIX.sub("", lines[-1])[-300:] if lines else "no message"
