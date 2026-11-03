# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser views' cookies panel (harvester.browser.jar): the listing grouped by domain with
values masked, a value shown on asking, adding, changing and deleting cookies in a live browser
context with their checks (a foreign domain, control characters, sizes), the cookies.txt for the
account read back by harvester.cookies, the recorder's handling of the panel's messages, and
"Use for this job" keeping the session without ending the recording."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import time

import pytest
from starlette.websockets import WebSocketDisconnect
from test_browser import needs_chromium
from test_recorder_features import recording
from test_security import Nextcloud, Socket

import main
from fake_site import SITE, Site
from harvester import cookies as cookie_jar
from harvester.browser import jar
from harvester.store import Job, Store

NOW = 1_800_000_000
COOKIES = [
    {"name": "sid", "value": "secret-1", "domain": "shop.example", "path": "/", "expires": -1, "httpOnly": True,
     "secure": True, "sameSite": "Lax"},
    {"name": "pref", "value": "dark", "domain": ".example", "path": "/", "expires": NOW + 3600, "httpOnly": False,
     "secure": False, "sameSite": "Strict"},
    {"name": "track", "value": "t", "domain": ".ads.test", "path": "/", "expires": -1, "httpOnly": False,
     "secure": True, "sameSite": "None"},
]  # fmt: skip


def test_the_listing_groups_by_domain_and_never_carries_a_value():
    groups = jar.listing(COOKIES, "shop.example", everything=False)
    assert [group["domain"] for group in groups] == ["shop.example", "example"], "the page's host first"
    sid = groups[0]["cookies"][0]
    assert sid == {
        "name": "sid",
        "domain": "shop.example",
        "path": "/",
        "expires": None,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
        "size": 8,
    }
    assert groups[1]["cookies"][0]["expires"] == NOW + 3600
    everything = jar.listing(COOKIES, "shop.example", everything=True)
    assert [group["domain"] for group in everything] == ["shop.example", "example", "ads.test"]
    assert "secret" not in repr(everything) and "value" not in repr(everything)


def test_a_cookie_is_checked_before_it_is_set():
    hosts = {"shop.example.org", "ads.test"}
    good = {"name": "a", "value": "1", "domain": "shop.example.org", "path": "/", "expires": NOW + 60}
    assert jar.checked(good, hosts, NOW) == good | {"expires": float(NOW + 60), "secure": False, "httpOnly": False,
                                                    "sameSite": "Lax"}  # fmt: skip
    for domain in ("example.org", ".example.org", "sub.shop.example.org", "ADS.test"):
        assert jar.checked(good | {"domain": domain}, hosts, NOW)["domain"] == domain.lower(), domain

    def refused(**changes) -> str:
        with pytest.raises(jar.Refused) as refusal:
            jar.checked(good | changes, hosts, NOW)
        return str(refusal.value)

    assert "none of the sites" in refused(domain="bank.example.net")
    assert "none of the sites" in refused(domain="evil-shop.example.org")
    assert "none of the sites" in refused(domain=".org"), "no top-level domain alone"
    assert "none of the sites" in refused(domain="")
    assert refused(name="a b").startswith("a name without")
    assert refused(name="a\x00") and refused(name="")
    value = refused(value="x\r\nSet-Cookie: y=1")
    assert "control characters" in value and "Set-Cookie" not in value, "the refusal never repeats the value"
    assert "longer than" in refused(value="x" * jar.MAX_SIZE)
    assert "path" in refused(path="nope")
    assert "future" in refused(expires=NOW - 1) and "future" in refused(expires=NOW + 401 * 86400)
    assert "expiry" in refused(expires="tomorrow")
    assert "Secure" in refused(sameSite="None")
    assert "SameSite" in refused(sameSite="Sometimes")


def test_the_export_is_a_cookies_txt_harvester_reads_back():
    text = jar.export(COOKIES)
    assert text.startswith("# Netscape HTTP Cookie File\n")
    assert "#HttpOnly_shop.example\tFALSE\t/\tTRUE\t0\tsid\tsecret-1" in text
    assert cookie_jar.parse(text, "x") == [
        cookie_jar.Cookie("sid", "secret-1", "shop.example", "/", True, 0, http_only=True),
        cookie_jar.Cookie("pref", "dark", ".example", "/", False, NOW + 3600),
        cookie_jar.Cookie("track", "t", ".ads.test", "/", True, 0),
    ]


@needs_chromium
def test_the_recorders_cookies_are_listed_shown_changed_and_deleted():
    async def scenario():
        driver, recorder = await recording(Site())
        sent = driver.sent
        try:
            assert recorder._context is not None
            await recorder.handle({"type": "goto", "url": f"{SITE}/list?p=1"})
            await recorder._context.add_cookies(
                [
                    {"name": "sid", "value": "secret-1", "domain": "site.example", "path": "/", "httpOnly": True},
                    {"name": "other", "value": "o", "domain": "elsewhere.example", "path": "/"},
                ]
            )

            async def answer(message: dict) -> dict:
                start = len(sent)
                await recorder.handle(message)
                return sent[start:][-1]

            listed = await answer({"type": "cookies"})
            everything = await answer({"type": "cookies", "all": True})
            shown = await answer({"type": "cookie-show", "name": "sid", "domain": "site.example", "path": "/"})
            later = time.time() + 86400
            added = await answer(
                {"type": "cookie-set", "cookie": {"name": "lang", "value": "de", "domain": "site.example",
                                                  "path": "/", "expires": later}}  # fmt: skip
            )
            foreign = await answer(
                {"type": "cookie-set", "cookie": {"name": "x", "value": "1", "domain": "bank.example.org"}}
            )
            # Renamed and moved, its value (never shown to the page) kept.
            changed = await answer(
                {
                    "type": "cookie-set",
                    "cookie": {"name": "session", "value": None, "domain": "site.example", "path": "/account"},
                    "original": {"name": "sid", "domain": "site.example", "path": "/"},
                }
            )
            cookies = {c["name"]: c for c in await recorder._context.cookies()}
            deleted = await answer({"type": "cookie-delete", "name": "lang", "domain": "site.example", "path": "/"})
            exported = await answer({"type": "cookies-export"})
            gone = await answer({"type": "cookies-delete-site", "domain": "site.example", "all": True})
            return listed, everything, shown, added, foreign, changed, cookies, deleted, exported, gone
        finally:
            await recorder.close()

    listed, everything, shown, added, foreign, changed, cookies, deleted, exported, gone = asyncio.run(scenario())
    assert listed["type"] == "cookies" and listed["host"] == "site.example"
    assert [g["domain"] for g in listed["groups"]] == ["site.example"], "this site's only"
    assert [g["domain"] for g in everything["groups"]] == ["site.example", "elsewhere.example"]
    assert "secret-1" not in repr(listed) + repr(everything)
    assert shown == {"type": "cookie-value", "name": "sid", "domain": "site.example", "path": "/", "value": "secret-1"}
    assert [c["name"] for c in added["groups"][0]["cookies"]] == ["lang", "sid"]
    assert foreign["type"] == "cookies-error" and "bank.example.org" in foreign["message"]
    assert [c["name"] for c in changed["groups"][0]["cookies"]] == ["lang", "session"], "the site's, any path"
    assert cookies["session"]["value"] == "secret-1" and cookies["session"]["path"] == "/account"
    assert "sid" not in cookies
    assert [c["name"] for c in deleted["groups"][0]["cookies"]] == ["session"]
    assert exported["type"] == "cookies-export" and exported["count"] == 1
    assert "\tsession\tsecret-1" in exported["text"], "the value only when exported"
    assert gone["groups"] == [{"domain": "elsewhere.example", "cookies": gone["groups"][0]["cookies"]}]


class SessionRecorder:
    """A recorder whose browser is a session; it notes what the page asked of it."""

    def __init__(self, send):
        self.send = send
        self.handled: list[dict] = []

    async def start(self, *_args, **_kwargs):
        pass

    async def handle(self, message):
        self.handled.append(message)

    async def session(self):
        return {"cookies": [{"name": "sid", "value": "v"}], "origins": []}

    async def close(self):
        pass


class Talking(Socket):
    """The recorder's WebSocket with the page's messages, then the page gone."""

    def __init__(self, job: int, messages: list[dict]):
        super().__init__(job)
        self.messages = list(messages)

    async def receive_json(self):
        if not self.messages:
            raise WebSocketDisconnect()
        return self.messages.pop(0)


def test_use_for_this_job_keeps_the_session_and_the_recording_goes_on(tmp_path, monkeypatch):
    monkeypatch.setitem(main.STATE, "store", Store(str(tmp_path / "h.db")))
    job = main.store().save(Job(user="anna", name="a", source="site", target="/x", schedule="0 6 * * *", options={}))
    monkeypatch.setattr(main, "Recorder", SessionRecorder)
    kept: list[dict] = []

    async def no_session(*_args):
        return None

    async def save_session(_nc, saved, session):
        kept.append({"job": saved.id, **session})

    monkeypatch.setattr(main, "load_session", no_session)
    monkeypatch.setattr(main, "save_session", save_session)
    websocket = Talking(job.id or 0, [{"type": "cookies-keep"}, {"type": "cookies"}])
    asyncio.run(main.record(websocket, Nextcloud()))  # type: ignore[arg-type]
    assert kept == [{"job": job.id, "cookies": [{"name": "sid", "value": "v"}], "origins": []}]
    assert {"type": "cookies-kept"} in websocket.sent and {"type": "session-saved"} not in websocket.sent
    assert main.store().job(job.id or 0).last_status is None, "no run started, the challenge left as it is"
