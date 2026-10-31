"""Sign-ins without a password form: one-time codes (TOTP), cookies and HTTP Basic."""

# pylint: disable=missing-function-docstring

import json
import os
import pathlib
import stat
import subprocess

import httpx
import pytest

from harvester import cookies, otp
from harvester.recipe import steps_from_events
from harvester.sources import AuthError
from harvester.sources.base import HostBasicAuth
from harvester.sources.feed import Feed
from harvester.sources.gallery import Gallery
from harvester.sources.media import Media
from harvester.sources.site import Site, filled

# RFC 6238's key for SHA-1, as base32.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
COOKIES_TXT = (
    "# Netscape HTTP Cookie File\n"
    ".example.org\tTRUE\t/\tTRUE\t2000000000\tsession\tabc\n"
    "#HttpOnly_shop.example.org\tFALSE\t/account\tFALSE\t0\tcart\t42\n"
)


def test_one_time_codes_follow_rfc_6238():
    assert otp.code(RFC_SECRET, at=59) == "287082"
    assert otp.code(f"otpauth://totp/Site:me?secret={RFC_SECRET}&digits=8", at=1111111109) == "07081804"
    assert otp.code(RFC_SECRET.lower()[:16] + " " + RFC_SECRET.lower()[16:], at=59) == "287082"
    with pytest.raises(ValueError):
        otp.code("not base32!")


def test_a_code_field_becomes_the_one_time_code_step():
    events = [{"action": "fill", "selector": "#otp", "value": "123456", "field": "text", "form": "f", "otp": True}]
    assert steps_from_events(events) == [{"action": "fill", "selector": "#otp", "credential": "totp"}]


def test_a_code_step_types_the_current_code_or_says_what_is_missing():
    step = {"action": "fill", "selector": "#otp", "credential": "totp"}
    assert filled(step, {"totp": RFC_SECRET}) == otp.code(RFC_SECRET)
    with pytest.raises(AuthError, match="one-time code"):
        filled(step, {})
    with pytest.raises(AuthError):
        filled(step, {"totp": "%%%"})
    assert filled({"credential": "username"}, {"username": "me"}) == "me"
    assert filled({"value": "pdf"}, {}) == "pdf"


def test_cookies_from_cookies_txt_and_from_a_cookie_header():
    found = cookies.parse(COOKIES_TXT, "example.org")
    assert found == [
        cookies.Cookie("session", "abc", ".example.org", "/", True, 2000000000),
        cookies.Cookie("cart", "42", "shop.example.org", "/account"),
    ]
    assert cookies.parse("a=1; b = 2;broken", "www.example.org") == [
        cookies.Cookie("a", "1", ".www.example.org"),
        cookies.Cookie("b", "2", ".www.example.org"),
    ]
    assert cookies.to_playwright(found)[0]["expires"] == 2000000000
    assert "expires" not in cookies.to_playwright(found)[1]


def test_a_cookies_file_is_private_and_reads_back():
    path = cookies.to_file(cookies.parse(COOKIES_TXT, "example.org"))
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        text = pathlib.Path(path).read_text(encoding="utf-8")
        assert cookies.parse(text, "x") == cookies.parse(COOKIES_TXT, "example.org")
    finally:
        os.unlink(path)


def test_basic_auth_goes_to_its_host_only():
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers.get("authorization")
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.auth = HostBasicAuth("https://feeds.example.org/private.rss", "me", "secret")
        client.get("https://feeds.example.org/private.rss")
        client.get("https://cdn.example.org/ep1.mp3")
        client.get("http://feeds.example.org/private.rss")
    assert seen["https://feeds.example.org/private.rss"].startswith("Basic ")
    assert seen["https://cdn.example.org/ep1.mp3"] is None
    assert seen["http://feeds.example.org/private.rss"] is None


def test_a_private_feed_with_basic_auth_and_cookies():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.headers.get("authorization"), request.headers.get("cookie")))
        return httpx.Response(200, content=b"<rss version='2.0'><channel><title>T</title></channel></rss>")

    account = {"username": "me", "password": "secret", "cookies": "member=yes"}
    feed = Feed(
        {"url": "https://feeds.example.org/private.rss"}, account, httpx.Client(transport=httpx.MockTransport(handler))
    )
    feed.entries()
    assert asked[0][0].startswith("Basic ") and asked[0][1] == "member=yes"


def test_cookies_that_are_neither_format_are_an_auth_error():
    feed = Feed({"url": "https://feeds.example.org/x.rss"}, {"cookies": "nothing here"}, httpx.Client())
    with pytest.raises(AuthError, match="cookies"):
        feed.entries()


def test_media_gives_yt_dlp_the_cookies_file_and_removes_it():
    made = Media({"url": "https://video.example/channel"}, {"cookies": COOKIES_TXT, "username": "me"}, None)
    options = made.base()
    assert options["username"] == "me" and options["quiet"]
    path = options["cookiefile"]
    assert made.base()["cookiefile"] == path and os.path.exists(path)
    made.close()
    assert not os.path.exists(path)
    assert "cookiefile" not in Media({"url": "https://video.example/channel"}, {}, None).base()


def test_gallery_dl_gets_the_account_in_files_never_on_its_command_line():
    account = {"username": "me", "password": "secret", "cookies": "sid=1"}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    made = Gallery({"url": "https://pics.example/album/1"}, account, client)
    given = {}

    def run(command, **_):
        config = command[command.index("--config") + 1]
        given["config"] = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
        given["cookies"] = pathlib.Path(command[command.index("--cookies") + 1]).read_text(encoding="utf-8")
        given["files"] = [config, command[command.index("--cookies") + 1]]
        given["command"] = command
        return subprocess.CompletedProcess(command, 0, "[]", "")

    made.run = run
    assert not list(made.items(lambda key: False))
    assert given["config"] == {"extractor": {"username": "me", "password": "secret"}}
    assert "\tsid\t1" in given["cookies"]
    assert "secret" not in " ".join(given["command"])
    assert not any(os.path.exists(path) for path in given["files"])
    assert client.cookies.get("sid") == "1"


def test_a_website_without_steps_signs_its_client_in():
    recipe = {"start": "https://members.example.org/files", "links": {"selector": "a", "pattern": r"\.pdf$"}}
    client = httpx.Client()
    site = Site({"recipe": json.dumps(recipe)}, {"username": "me", "password": "pw", "cookies": "sid=7"}, client)
    site._driver()  # pylint: disable=protected-access
    assert isinstance(client.auth, HostBasicAuth) and client.cookies.get("sid") == "7"
    site.close()
