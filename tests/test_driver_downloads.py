# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser driver's downloads (harvester.sources.site: BrowserDriver.download) in Chromium
against pages served by the routes (no network): one guarded client for all of a driver's files,
closed with it, each request with the browser's cookies for its address as they are when it is
sent, an answer's cookies kept for that download alone, and the guard's check of every address,
a redirect's too, as strict as before."""

# pylint: disable=missing-function-docstring,protected-access

import httpx
import pytest
from test_browser import FREE, needs_chromium

from harvester.sources import SourceError
from harvester.sources import site as site_module

SITE = "https://files.example"


def page(route) -> None:
    route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=b"<html><body>files</body></html>")


class Server:  # pylint: disable=too-few-public-methods
    """The site's files over HTTP: what each request sent, and the clients made."""

    def __init__(self):
        self.cookies: list[str] = []
        self.clients: list[httpx.Client] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.cookies.append(request.headers.get("cookie", ""))
        if request.url.path == "/set":
            return httpx.Response(200, headers={"Set-Cookie": "answered=1; Path=/"}, content=b"%PDF-1")
        if request.url.path == "/inside":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1")


def driver_with(server: Server, monkeypatch) -> site_module.BrowserDriver:
    monkeypatch.setattr(site_module, "guard", page)
    real = site_module.download.guarded_client

    def client(**options) -> httpx.Client:
        # The real guarded client (its checks), its requests answered by the server.
        options.pop("proxy", None)
        made = real(transport=httpx.MockTransport(server.answer), **options)
        server.clients.append(made)
        return made

    monkeypatch.setattr(site_module.download, "guarded_client", client)
    return site_module.BrowserDriver([], {})


def fetched(driver: site_module.BrowserDriver, url: str) -> bytes:
    with driver.download(url) as response:
        return b"".join(response.chunks)


@needs_chromium
def test_one_client_serves_the_drivers_downloads_with_the_browsers_cookies_as_they_are(monkeypatch):
    server = Server()
    driver = driver_with(server, monkeypatch)
    try:
        driver.context.add_cookies([{"name": "sid", "value": "a", "url": SITE}])
        assert fetched(driver, f"{SITE}/set") == b"%PDF-1"
        # The browser's session changed since (a sign-in anew): the next request has it.
        driver.context.add_cookies([{"name": "sid", "value": "b", "url": SITE}])
        driver.context.add_cookies([{"name": "other", "value": "x", "url": "https://other.example"}])
        fetched(driver, f"{SITE}/file.pdf")
        driver.impersonate = False
        fetched(driver, f"{SITE}/again.pdf")
        clients = list(server.clients)
    finally:
        driver.close()
    assert server.cookies == ["sid=a", "sid=b", "sid=b"], "an answer's cookie stays with its download"
    assert len(clients) == 1 and clients[0].is_closed, "one client, closed with the driver"
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_the_kept_client_checks_every_address_and_redirect(monkeypatch):
    server = Server()
    driver = driver_with(server, monkeypatch)
    try:
        with pytest.raises(SourceError, match="may not be reached"):
            fetched(driver, "http://127.0.0.1/file.pdf")
        with pytest.raises(SourceError, match="127.0.0.1/secret may not be reached"):
            fetched(driver, f"{SITE}/inside")
        assert fetched(driver, f"{SITE}/file.pdf") == b"%PDF-1", "the client goes on after a refusal"
    finally:
        driver.close()
    assert len(server.clients) == 1
