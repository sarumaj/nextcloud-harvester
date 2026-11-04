# SPDX-License-Identifier: AGPL-3.0-or-later
"""harvester.vault against the other implementation: the sister app Vaults for Cryptomator
(its js/vault.js, run by node through tests/vault_js.mjs) reads what is written here, byte for
byte, with its health check (cryptofs' checks) finding nothing, and this module reads what it
writes. Skipped without node or the sister app's repository (HARVESTER_CRYPTOMATOR_REPO, by
default ~/forgejo/nextcloud-cryptomator)."""

import hashlib
import json
import os
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

from harvester.vault import CHUNK, FolderStorage, Vault

DATA = Path(__file__).parent / "data" / "cryptomator"
HELPER = Path(__file__).parent / "vault_js.mjs"
SISTER = Path(os.environ.get("HARVESTER_CRYPTOMATOR_REPO", Path.home() / "forgejo" / "nextcloud-cryptomator"))
NODE = shutil.which("node")
VAULTS = ["vault-gcm", "vault-ctrmac"]

pytestmark = [
    pytest.mark.skipif(NODE is None, reason="node is not installed"),
    pytest.mark.skipif(
        not (SISTER / "app" / "cryptomator_vaults" / "js" / "vault.js").is_file(),
        reason=f"the sister app's repository is not at {SISTER} (HARVESTER_CRYPTOMATOR_REPO)",
    ),
]


def run_js(*arguments: str) -> dict:
    """The helper's answer (JSON)."""
    done = subprocess.run(
        [NODE, str(HELPER), str(SISTER), *arguments], capture_output=True, text=True, timeout=600, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def password(name: str) -> str:
    """The fixture's password."""
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))["password"]


def described(data: bytes) -> dict:
    """A file as the helper describes it."""
    return {"type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def samples() -> dict[str, bytes]:
    """What each side writes: the sizes around the chunks, a long name (.c9s), names right at the
    shortening threshold, a decomposed name, files in new nested folders (one of a long name)."""
    files = {f"sizes/{size}.bin": os.urandom(size) for size in [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 1_000_000]}
    files["long-" * 40 + ".txt"] = b"long name\n"
    files["k" * 146] = b"220\n"
    files["s" * 147] = b"224\n"
    files[unicodedata.normalize("NFD", "Zdjęcie ✓ ünïcode.txt")] = b"nfc\n"
    files["Docs & Stuff/new/" + "folder-" * 25 + "/deep.txt"] = b"deep\n"
    return files


@pytest.mark.parametrize("name", VAULTS)
def test_sister_app_reads_what_is_written(name, tmp_path):
    """Files written here into a copy of a fixture vault: the sister app's code lists and
    decrypts them all, every directory's dirid.c9r holds its ID, its health check finds nothing."""
    folder = tmp_path / name
    shutil.copytree(DATA / name, folder)
    before = run_js("dump", str(folder), password(name))["entries"]
    vault = Vault.unlock(FolderStorage(folder), password(name))
    files = samples()
    for path, data in files.items():
        vault.write_file(path, iter([data]))
    found = run_js("dump", str(folder), password(name))
    expected = dict(before)
    for path, data in files.items():
        path = unicodedata.normalize("NFC", path)
        parts = path.split("/")
        for end in range(1, len(parts)):
            expected.setdefault("/".join(parts[:end]), {"type": "dir"})
        expected[path] = described(data)
    assert found["entries"] == expected
    assert found["health"] == []
    assert set(found["dirIdBackups"].values()) == {"ok"}
    assert len(found["dirIdBackups"]) == sum(entry["type"] == "dir" for entry in expected.values()) + 1


@pytest.mark.parametrize("name", VAULTS)
def test_what_the_sister_app_writes_is_read(name, tmp_path):
    """Files and folders the sister app's code writes are read here, byte for byte."""
    folder = tmp_path / name
    shutil.copytree(DATA / name, folder)
    files = samples()
    spec = []
    for number, (path, data) in enumerate(files.items()):
        source = tmp_path / f"source-{number}"
        source.write_bytes(data)
        spec.append({"path": path, "file": str(source)})
    spec.append({"path": "empty folder/inner", "dir": True})
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    run_js("write", str(folder), password(name), str(tmp_path / "spec.json"))
    vault = Vault.unlock(FolderStorage(folder), password(name))
    for path, data in files.items():
        assert vault.read_bytes(path) == data, path
    assert vault.find("empty folder/inner").kind == "dir"
    assert not vault.listdir("empty folder/inner")
