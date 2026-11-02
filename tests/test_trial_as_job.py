"""Test the recipe as the job runs: a fresh browser context (the recorder's session does not sign
it in), the saved job's account resolved by the app (never from the page), its one-time code,
the recipe's window, the form's file type and naming settings, and the first bytes of the first
file fetched to tell a file from a sign-in page. Served by route fulfilment; no network."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_browser import FREE, Driver, needs_chromium
from test_recorder_features import recording, trial_result
from test_security import Nextcloud, Socket

import main
from harvester import otp
from harvester.browser.recorder import Recorder
from harvester.sources import download
from harvester.sources import site as site_module
from harvester.store import Job, Store

SHOP = "https://shop.example"
SECRET = "JBSWY3DPEHPK3PXP"
PDF = b"%PDF-1.7\n" + b"x" * 200_000
LOGIN = b"""<html><body><form method="post" action="/session">
<input id="user" name="user"><input id="pass" name="pass" type="password"><input id="otp" name="otp">
<button id="in" type="submit">Sign in</button></form></body></html>"""


class Shop:  # pylint: disable=too-few-public-methods
    """A shop whose invoices need its sign-in (the cookie sid=ok, set by /session for the right
    user, password and current code); its file links carry the size of the window shown in."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.posted: list[dict] = []

    def answer(self, url: str, method: str = "GET", cookie: str = "", body: str = "") -> tuple[int, dict, bytes]:
        self.requests.append(url)
        path, html = urlsplit(url).path, {"content-type": "text/html"}
        signed_in = "sid=ok" in cookie
        if path == "/login" or (path in ("/invoices", "/files/a") and not signed_in):
            return 200, html, LOGIN
        if path == "/session" and method == "POST":
            form = {key: value[0] for key, value in parse_qs(body).items()}
            self.posted.append(form)
            right = form.get("user") == "anna" and form.get("pass") == "pw" and form.get("otp") == otp.code(SECRET)
            headers = html | ({"set-cookie": "sid=ok; Path=/"} if right else {})
            return 200, headers, b"<script>location.href = '/invoices'</script>"
        if path == "/invoices":
            links = '<a class="file" href="/files/a">Invoice A</a><a class="file" href="/files/page">Invoice B</a>'
            # Before the page is read: each file link carries the window's size in its query.
            seen = "<script>for (const a of document.links) a.search = `w=${innerWidth}x${innerHeight}`</script>"
            return 200, html, f"<html><body>{links}{seen}</body></html>".encode()
        files = {"/files/a": ({"content-type": "application/pdf"}, PDF), "/files/page": (html, b"<html>a page</html>")}
        headers, content = files.get(path, ({"content-type": "text/plain"}, b"ok"))
        return 200, headers, content


def routed(shop: Shop):
    async def fulfill(route) -> None:
        request = route.request
        headers = await request.all_headers()
        status, answer, body = shop.answer(
            request.url, request.method, headers.get("cookie", ""), request.post_data or ""
        )
        await route.fulfill(status=status, headers=answer, body=body)

    return fulfill


class AsJob:  # pylint: disable=too-few-public-methods
    """The recorder as the app asks it for the test as the job runs (main.test_as_job): with the
    saved job's account."""

    def __init__(self, recorder: Recorder, account: dict):
        self.recorder = recorder
        self.account = account

    async def handle(self, message: dict) -> None:
        await self.recorder.test(message | {"as_job": True}, self.account)


async def result_of(driver: Driver, recorder: Recorder, message: dict, account: dict | None = None) -> dict:
    """The test's result; with an account, as the app asks for it."""
    return await trial_result(driver, recorder if account is None else AsJob(recorder, account), message)  # type: ignore[arg-type]


async def recording_shop(shop: Shop) -> tuple[Driver, Recorder]:
    """The recorder at the shop, which sees the cookies its pages are asked for with."""
    driver, recorder = await recording(shop)  # type: ignore[arg-type]
    recorder._guard = routed(shop)  # type: ignore[method-assign]
    return driver, recorder


STEPS = [
    {"action": "goto", "url": f"{SHOP}/login"},
    {"action": "fill", "selector": "#user", "credential": "username"},
    {"action": "fill", "selector": "#pass", "credential": "password"},
    {"action": "fill", "selector": "#otp", "credential": "totp"},
    {"action": "click", "selector": "#in"},
]
SIGNED_IN = {
    "steps": STEPS,
    "start": f"{SHOP}/invoices",
    "links": {"selector": "a.file", "pattern": "/files/"},
    "viewport": {"width": 1024, "height": 700},
}
WITHOUT_STEPS = {"start": f"{SHOP}/invoices", "links": {"selector": "a.file", "pattern": "/files/"}}
ACCOUNT = {"username": "anna", "password": "pw", "totp": SECRET, "cookies": "", "code_mailbox": ""}
SETTINGS = {"path": "{text}", "file_type": "pdf"}


@pytest.fixture(name="downloads")
def downloads_over_the_shop(monkeypatch):
    """The test's requests over HTTP (the probe) answered by the shop, with their cookies."""
    holder: dict = {}
    real = download.guarded_client

    def handle(request: httpx.Request) -> httpx.Response:
        status, headers, body = holder["shop"].answer(
            str(request.url), request.method, request.headers.get("cookie", "")
        )
        return httpx.Response(status, headers=headers, content=body)

    monkeypatch.setattr(
        download, "guarded_client", lambda **options: real(transport=httpx.MockTransport(handle), **options)
    )
    return holder


@needs_chromium
def test_as_the_job_runs_the_sign_in_is_replayed_with_the_account(downloads):
    shop = downloads["shop"] = Shop()

    async def scenario():
        driver, recorder = await recording_shop(shop)
        try:
            # The recorder's own session is signed in: the quick test passes on it, recipe or not.
            assert recorder._context is not None
            await recorder._context.add_cookies([{"name": "sid", "value": "ok", "url": SHOP}])
            quick = await result_of(driver, recorder, {"recipe": WITHOUT_STEPS, "settings": SETTINGS})
            driver.sent.clear()
            fresh = await result_of(driver, recorder, {"recipe": WITHOUT_STEPS, "settings": SETTINGS}, account={})
            driver.sent.clear()
            skipped = await result_of(driver, recorder, {"recipe": SIGNED_IN, "settings": SETTINGS})
            driver.sent.clear()
            account = ACCOUNT | {"totp": ""}
            no_secret = await result_of(driver, recorder, {"recipe": SIGNED_IN, "settings": SETTINGS}, account=account)
            driver.sent.clear()
            shop.requests.clear()
            as_job = await result_of(driver, recorder, {"recipe": SIGNED_IN, "settings": SETTINGS}, account=ACCOUNT)
            assert recorder._browser is not None
            return quick, fresh, skipped, no_secret, as_job, len(recorder._browser.contexts), recorder.viewport
        finally:
            await recorder.close()

    quick, fresh, skipped, no_secret, as_job, contexts, viewport = asyncio.run(scenario())
    assert quick["ended"] == "complete" and quick["files"] == 2 and "probe" not in quick
    assert fresh["ended"] == "failed" and fresh["as_job"]
    assert "start page has none of the recipe's links" in fresh["problems"][-1], "no recorder's session"
    # The recorder's session skips the steps (a kept session): the quick test never tries them.
    assert skipped["ended"] == "complete"
    assert no_secret["ended"] == "failed" and "set the code's secret" in no_secret["problems"][0]
    assert as_job["ended"] == "complete" and as_job["as_job"] and not as_job["problems"]
    assert shop.posted == [{"user": "anna", "pass": "pw", "otp": otp.code(SECRET)}], "the as-job test's sign-in alone"
    assert [file["path"] for file in as_job["pages"][0]["files"]] == ["Invoice A.pdf", "Invoice B.pdf"], "file_type"
    assert as_job["probe"]["ok"] and as_job["probe"]["type"] == "application/pdf"
    assert as_job["probe"]["bytes"] == 64 * 1024, "the first bytes only"
    assert as_job["pages"][0]["files"][0]["url"].endswith("?w=1024x700"), "the recipe's window"
    assert viewport == {"width": 1280, "height": 800} and contexts == 1


@needs_chromium
def test_as_the_job_runs_a_page_instead_of_the_file_is_told(downloads):
    shop = downloads["shop"] = Shop()
    recipe = SIGNED_IN | {"links": {"selector": "a.file", "pattern": "/files/page"}}

    async def scenario():
        driver, recorder = await recording_shop(shop)
        try:
            return await result_of(driver, recorder, {"recipe": recipe, "settings": SETTINGS}, account=ACCOUNT)
        finally:
            await recorder.close()

    result = asyncio.run(scenario())
    assert result["ended"] == "complete" and not result["probe"]["ok"]
    assert result["probe"]["problem"].startswith("a web page instead of a file")


class Recording:  # pylint: disable=too-few-public-methods
    """The recorder as the app sees it: the test it is asked for, with the account."""

    def __init__(self):
        self.tests: list[tuple[dict, dict | None]] = []

    async def test(self, message: dict, account: dict | None = None) -> None:
        self.tests.append((message, account))


def test_the_app_gives_the_saved_jobs_account_and_never_sends_it(tmp_path, monkeypatch):
    monkeypatch.setitem(main.STATE, "store", Store(str(tmp_path / "h.db")))
    job = main.store().save(Job(user="anna", name="a", source="site", target="/x", schedule="0 6 * * *", options={}))
    other = main.store().save(Job(user="ben", name="b", source="site", target="/x", schedule="0 6 * * *", options={}))
    nextcloud = Nextcloud()
    stored = {main.credential_key(job, "username"): "anna", main.credential_key(job, "password"): "pw"}

    async def get_value(key):
        return stored.get(key)

    nextcloud.preferences_ex.get_value = get_value  # type: ignore[attr-defined]
    socket, recorder = Socket(0), Recording()
    message = {"type": "test", "as_job": True, "job": job.id, "recipe": {}, "settings": {}}
    asyncio.run(main.test_as_job(socket, recorder, message, nextcloud))  # type: ignore[arg-type]
    assert recorder.tests == [
        (message, {"username": "anna", "password": "pw", "totp": "", "cookies": "", "code_mailbox": ""})
    ]
    assert not socket.sent, "the account goes to the recorder, not to the page"
    for unsaved in (None, other.id, "x"):
        asyncio.run(main.test_as_job(socket, recorder, message | {"job": unsaved}, nextcloud))  # type: ignore[arg-type]
    assert len(recorder.tests) == 1 and all("Save the job first" in sent["message"] for sent in socket.sent)


@needs_chromium
def test_a_job_takes_the_recipes_window(monkeypatch):
    shop = Shop()

    def fulfill(route) -> None:
        request = route.request
        cookie = request.all_headers().get("cookie", "")
        status, headers, body = shop.answer(request.url, request.method, cookie, request.post_data or "")
        route.fulfill(status=status, headers=headers, body=body)

    monkeypatch.setattr(site_module, "guard", fulfill)
    for recipe, seen in ((SIGNED_IN, "1024x700"), ({**SIGNED_IN, "viewport": None}, "1280x800")):
        source = site_module.Site({"recipe": json.dumps(recipe)}, ACCOUNT, None)
        try:
            urls = [item.url for item in source.items(lambda key: False)]
        finally:
            source.close()
        assert len(urls) == 2 and urls[0].endswith(f"?w={seen}"), seen
    assert site_module.browser.free_browsers() == FREE
