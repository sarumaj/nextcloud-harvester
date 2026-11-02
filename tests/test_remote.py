"""The rclone source: what its configuration may hold, and its runs with a fake rclone (no network).

One test runs the real rclone (HARVESTER_RCLONE or rclone on the PATH) on the configuration the
source writes; it is skipped where there is none.
"""

# pylint: disable=missing-function-docstring,protected-access

import io
import json
import os
import shutil
import subprocess

import pytest

from harvester.sources import SourceError
from harvester.sources.remote import Remote, parse_config

WEBDAV = """[share]
type = webdav
url = https://cloud.example.org/public.php/webdav
vendor = nextcloud
user = AbCdEf
"""
DRIVE = """[gdrive]
type = drive
scope = drive.readonly
token = {"access_token":"old","refresh_token":"r","expiry":"2026-10-31T00:00:00Z"}
"""
LISTING = [
    {
        "Path": "2025/report.pdf",
        "Name": "report.pdf",
        "Size": 10,
        "ModTime": "2025-12-01T10:00:00.123456789Z",
        "IsDir": False,
    },
    {"Path": "2026/plan.pdf", "Name": "plan.pdf", "Size": 20, "ModTime": "2026-10-01T10:00:00Z", "IsDir": False},
    {"Path": "2026", "Name": "2026", "Size": -1, "ModTime": "2026-10-01T10:00:00Z", "IsDir": True},
]


def remote(config: str = WEBDAV, **options) -> Remote:
    made = Remote(options, {"config": config}, None)
    made.binary = "/usr/bin/rclone"
    return made


class FakeRclone:
    """subprocess.run and Popen for rclone: lsjson answers LISTING, cat a file's bytes."""

    def __init__(self, refresh: bool = False):
        self.calls: list[list[str]] = []
        self.refresh = refresh

    def run(self, command, **_kwargs):
        self.calls.append(command)
        config = command[command.index("--config") + 1]
        if self.refresh:
            # rclone writes refreshed OAuth tokens into its configuration.
            with open(config, encoding="utf-8") as file:
                text = file.read()
            with open(config, "w", encoding="utf-8") as file:
                file.write(text.replace('"access_token":"old"', '"access_token":"new"'))
        return subprocess.CompletedProcess(command, 0, json.dumps(LISTING), "")

    def popen(self, command, **_kwargs):
        self.calls.append(command)
        return subprocess.Popen(
            ["printf", "%s", f"content of {command[-1]}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )


def test_items_newest_first_with_values():
    made = remote(path="/Reports/", include="*.pdf", path_template="{year}/{filename}")
    fake = FakeRclone()
    made.run, made.popen = fake.run, fake.popen
    try:
        items = list(made.items(lambda key: key == "share:Reports/2025/report.pdf"))
        assert [item.path for item in items] == ["2026/plan.pdf"]
        assert items[0].key == "share:Reports/2026/plan.pdf" and items[0].values["month"] == 10
        command = fake.calls[0]
        assert command[0] == "/usr/bin/rclone" and command[-1] == "share:Reports"
        assert ["--include", "*.pdf"] == command[command.index("--include") : command.index("--include") + 2]
        assert "--recursive" in command and "--files-only" in command
        assert oct(os.stat(command[command.index("--config") + 1]).st_mode & 0o777) == "0o600"
        dest = io.BytesIO()
        made.fetch(items[0], dest)
        assert dest.getvalue() == b"content of share:Reports/2026/plan.pdf"
    finally:
        made.close()
    assert made._dir is None


def test_a_failure_says_what_rclone_said():
    made = remote()
    made.run = lambda command, **_: subprocess.CompletedProcess(
        command, 1, "", "ERROR : x\nCRITICAL: directory not found\n"
    )
    try:
        with pytest.raises(SourceError, match="directory not found"):
            list(made.items(lambda key: False))
    finally:
        made.close()


def test_refreshed_tokens_are_kept_for_the_next_run():
    made = remote(DRIVE)
    fake = FakeRclone(refresh=True)
    made.run = fake.run
    try:
        list(made.items(lambda key: False))
        session = made.session()
    finally:
        made.close()
    assert session and '"access_token":"new"' in session["rclone_config"]

    nothing_new = remote(DRIVE)
    nothing_new.run = FakeRclone().run
    try:
        list(nothing_new.items(lambda key: False))
        assert nothing_new.session() is None, "an unchanged configuration is not kept again"
    finally:
        nothing_new.close()

    later = remote(DRIVE)
    later.restore(session)
    assert '"access_token":"new"' in later.config
    other = remote(WEBDAV)
    other.restore(session)
    assert other.config == WEBDAV, "a kept configuration of another remote is not used"


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        ("[x]\ntype = local\n", "type local"),
        ("[x]\ntype = alias\nremote = /etc\n", "type alias"),
        ("[x]\ntype = crypt\nremote = y:\n", "type crypt"),
        ("[x]\nurl = https://a.example\n", "type"),
        ("[a]\ntype = s3\n[b]\ntype = s3\n", "one remote"),
        ("[x]\ntype = drive\nservice_account_file = /proc/self/environ\n", "service_account_file"),
        ("[x]\ntype = sftp\nhost = a.example.org\nkey_file = /root/.ssh/id\n", "key_file"),
        ("[x]\ntype = sftp\nhost = a.example.org\nssh = sh -c id\n", "ssh"),
        ("[x]\ntype = webdav\nurl = https://a.example\nbearer_token_command = cat /proc/self/environ\n", "command"),
        ("[x]\ntype = sftp\nhost = 10.0.0.26\n", "may not be reached"),
        ("[x]\ntype = webdav\nurl = http://localhost:8080/dav\n", "may not be reached"),
        ("[x]\ntype = s3\nendpoint = http://169.254.169.254\n", "may not be reached"),
        ("not a configuration", "cannot be read"),
        ("[x]\ntype = s3\nenv_auth = true\n", "env_auth"),
        ("[x]\ntype = azureblob\nuse_msi = True\n", "use_msi"),
    ],
)
def test_configurations_that_reach_the_container_or_private_hosts_are_refused(config, problem):
    with pytest.raises(SourceError, match=problem):
        parse_config(config)


def test_credentials_switched_off_from_the_machine_are_fine():
    assert parse_config("[x]\ntype = s3\nenv_auth = false\naccess_key_id = a\n")[1]["env_auth"] == "false"


def test_saving_without_an_account_and_running_without_one():
    made = Remote({}, {}, None)
    with pytest.raises(SourceError, match="no rclone configuration"):
        list(made.items(lambda key: False))
    assert parse_config(WEBDAV) == (
        "share",
        {
            "type": "webdav",
            "url": "https://cloud.example.org/public.php/webdav",
            "vendor": "nextcloud",
            "user": "AbCdEf",
        },
    )


RCLONE = os.environ.get("HARVESTER_RCLONE") or shutil.which("rclone")


@pytest.mark.skipif(not RCLONE, reason="rclone is not installed")
def test_rclone_reads_the_configuration_the_source_writes():
    made = remote(DRIVE)
    made.binary = RCLONE or ""
    try:
        result = subprocess.run(made._command("listremotes", "--long"), capture_output=True, text=True, check=True)
    finally:
        made.close()
    assert result.stdout.split() == ["gdrive:", "drive"]
