# SPDX-License-Identifier: AGPL-3.0-or-later
"""The gallery source (gallery-dl) with a stand-in for gallery-dl and a fake site: no network."""

# pylint: disable=missing-function-docstring

import contextlib
import importlib
import inspect
import io
import json
import pathlib
import re
import subprocess

import gallery_dl
import httpx
import pytest
import urllib3

from harvester.sources import SourceError
from harvester.sources.gallery import Gallery

PAGE = "https://pics.example/album/42"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100


def message(number: int, num: int = 0) -> list:
    """A Url message of gallery-dl's JSON output (num: the file's place in a post of several)."""
    data = {
        "category": "pics",
        "subcategory": "album",
        "id": number,
        "num": num,
        "filename": f"photo{number}",
        "extension": "jpg",
        "date": "2025-07-14 10:20:30",
        "tags": ["beach", "sea"],
    }
    return [3, f"https://cdn.pics.example/{number}-{num}.jpg", data]


def gallery(stdout: str = "", returncode: int = 0, stderr: str = "", handler=None, **options) -> Gallery:
    client = httpx.Client(transport=httpx.MockTransport(handler or (lambda request: httpx.Response(404))))
    made = Gallery({"url": PAGE} | options, {}, client)
    made.calls = []

    def run(command, **kwargs):
        made.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    made.run = run
    made.sleep = lambda seconds: None
    return made


def test_the_galleries_new_files_with_their_names_and_values():
    stdout = json.dumps([[2, {"category": "pics"}], message(1), message(2), [6, "https://pics.example/x", {}]])
    source = gallery(stdout, path="{year}/{filename}.{extension}")
    items = list(source.items(lambda key: key == "pics:1:0"))
    assert [item.key for item in items] == ["pics:2:0"]
    assert items[0].path == "2025/photo2.jpg"
    assert items[0].values["month"] == 7 and "tags" not in items[0].values
    command = source.calls[0][0]
    assert command[-1] == PAGE and "--resolve-json" in command


def test_the_download_has_the_gallery_as_referer_and_is_checked():
    referers = []

    def handler(request: httpx.Request) -> httpx.Response:
        referers.append(request.headers.get("referer"))
        if request.url.path == "/1-0.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(200, text="<html>sign in</html>", headers={"content-type": "text/html"})

    source = gallery(json.dumps([message(1), message(2)]), handler=handler)
    first, second = list(source.items(lambda key: False))
    dest = io.BytesIO()
    source.fetch(first, dest)
    assert dest.getvalue() == JPEG and referers == [PAGE]
    with pytest.raises(SourceError, match="web page instead of a file"):
        source.fetch(second, io.BytesIO())


def test_gallery_dls_errors_and_refused_pages():
    failed = gallery(returncode=1, stderr="[gallery-dl][error] Unsupported URL 'https://pics.example/album/42'\n")
    with pytest.raises(SourceError, match="Unsupported URL"):
        list(failed.items(lambda key: False))
    with pytest.raises(SourceError, match="no JSON"):
        list(gallery("not json").items(lambda key: False))
    for url in ("file:///etc/passwd", "http://10.0.0.1/a"):
        with pytest.raises(SourceError, match="may not be opened"):
            Gallery({"url": url}, {}, None)
    unsafe = gallery(json.dumps([[3, "http://169.254.169.254/latest", {"category": "pics", "id": 9}]]))
    assert not list(unsafe.items(lambda key: False))


def test_every_file_of_a_post_of_several():
    stdout = json.dumps([message(7, 1), message(7, 2), message(7, 3)])
    seen: set[str] = set()
    keys = []
    for item in gallery(stdout).items(seen.__contains__):
        keys.append(item.key)
        seen.add(item.key)  # as the runner records each file before the next is listed
    assert keys == ["pics:7:1", "pics:7:2", "pics:7:3"]


def test_a_partial_failure_keeps_what_was_listed():
    stdout = json.dumps([message(1), message(2)])
    partial = gallery(stdout, returncode=1, stderr="[gallery-dl][error] HttpError: 500 for page 3\n")
    assert len(list(partial.items(lambda key: False))) == 2


def test_impersonation_is_gallery_dls_own_browser_emulation_in_its_config():
    plain = gallery()
    options, files = plain.account_files()
    assert "--config" not in options and not files, "off: nothing changes"
    made = gallery(impersonate=1)
    options, files = made.account_files()
    try:
        with open(options[options.index("--config") + 1], encoding="utf-8") as file:
            assert json.load(file) == {"extractor": {"browser": "chrome"}}
    finally:
        for path in files:
            __import__("os").unlink(path)


def test_the_urllib3_names_gallery_dl_calls_exist():
    # gallery-dl calls urllib3.connection.create_urllib3_context, which urllib3 2.8.0 dropped:
    # every gallery job failed. Each urllib3 name its code calls (a module, then attributes) must
    # be there.
    used = set()
    for path in pathlib.Path(gallery_dl.__file__).parent.rglob("*.py"):
        used |= set(re.findall(r"\burllib3\.((?:\w+\.)*\w+)\(", path.read_text(encoding="utf-8")))
    assert "connection.create_urllib3_context" in used
    for name in sorted(used):
        found: object = urllib3
        for part in name.split("."):
            if not hasattr(found, part) and inspect.ismodule(found):
                # A submodule not imported yet (urllib3.util.ssl_).
                with contextlib.suppress(ModuleNotFoundError):
                    importlib.import_module(f"{found.__name__}.{part}")
            assert hasattr(found, part), f"urllib3.{name}"
            found = getattr(found, part)
