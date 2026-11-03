# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exports a site prepares first (recipe: a level's "wait_for"; harvester.sources.tabs
.wait_until_shown): a step clicks "Create export", and the file links' page is looked at, and
reloaded if asked, until the element of the ready file shows; then the download goes on as before,
clicked file links too. In Chromium against the fake site (routed, no network), and the checks."""

# pylint: disable=missing-function-docstring,protected-access

import httpx
import pytest
from test_browser import FREE, needs_chromium
from test_clicks import run, source

from fake_site import PDF, SITE, Site
from harvester import recipe as recipes
from harvester.recipe import check_recipe, uses_browser
from harvester.sources import SourceError, tabs
from harvester.sources import site as site_module
from harvester.sources.driver import HttpPage

# A page whose script prepares the export: its link shows a while after the click.
CREATE = """<html><body><button id="create" onclick="setTimeout(() => {
  document.body.insertAdjacentHTML('beforeend', '<a class=file href=/export/all.pdf>Export</a>')
}, 1500)">Create export</button></body></html>"""


class Exports(Site):  # pylint: disable=too-few-public-methods
    """The fake site with an export a script prepares, and one the server prepares: its list shows
    the entry pending until it was asked for ready times, then ready, with a button that downloads."""

    def __init__(self, ready: int = 3) -> None:
        super().__init__()
        self.listed = 0
        self.ready = ready

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        path = url.split("://", 1)[1].split("/", 1)[1]
        html = {"content-type": "text/html"}
        if path == "create":
            return 200, html, CREATE.encode()
        if path == "reports":
            self.listed += 1
            ready = self.listed >= self.ready
            row = "<tr class=ready><td>Ready</td><td><button>Get</button></td></tr>" if ready else "<tr><td>…</td></tr>"
            script = "<script>for (const b of document.querySelectorAll('button')) b.onclick = () => {"
            script += " location.href = '/export/r.pdf' }</script>"
            return 200, html, f"<html><body><table>{row}</table>{script}</body></html>".encode()
        if path.startswith("export/"):
            self.requests.append(url)
            name = path.rsplit("/", 1)[1]
            return (
                200,
                {"content-type": "application/pdf", "content-disposition": f'attachment; filename="{name}"'},
                PDF,
            )
        return super().answer(url, method, cookie)


@needs_chromium
def test_a_link_a_script_shows_after_the_click_is_waited_for(monkeypatch):
    monkeypatch.setattr(tabs, "WAIT_POLL", 200)
    recipe = {
        "steps": [{"action": "goto", "url": f"{SITE}/create"}, {"action": "click", "selector": "#create", "text": ""}],
        "links": {"selector": "a.file", "pattern": r"\.pdf$", "wait_for": {"selector": "a.file", "timeout": 20}},
    }
    message, library, log = run(source(Exports(), monkeypatch, recipe))
    assert message == "1 downloaded" and list(library.files) == ["/In/all.pdf"]
    assert "waiting for a.file to show, at most 20 s" in log
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_list_the_server_prepares_is_reloaded_until_its_entry_is_ready_and_clicked(monkeypatch):
    monkeypatch.setattr(tabs, "WAIT_POLL", 200)
    monkeypatch.setattr(recipes, "WAIT_FOR_RELOAD", 1)
    site = Exports()
    wait = {"selector": "tr.ready button", "timeout": 30, "reload": 1}
    recipe = {
        "start": f"{SITE}/reports",
        "links": {"selector": "tr.ready button", "pattern": "^Get$", "click": True, "wait_for": wait},
    }
    message, library, _ = run(source(site, monkeypatch, recipe, path="{disposition}"))
    assert message == "1 downloaded" and list(library.files) == ["/In/r.pdf"]
    assert site.listed >= 3, "reloaded until ready"


@needs_chromium
def test_an_export_that_never_comes_ends_the_wait(monkeypatch):
    monkeypatch.setattr(tabs, "WAIT_POLL", 200)
    site = Exports(ready=1000)
    recipe = {
        "start": f"{SITE}/reports",
        "links": {"selector": "tr.ready button", "click": True, "wait_for": {"selector": "tr.ready", "timeout": 1}},
    }
    made = source(site, monkeypatch, recipe)
    try:
        with pytest.raises(SourceError, match=r"tr.ready did not show within 1 s \(the file not ready yet\?\)"):
            list(made.items(lambda key: False))
    finally:
        made.close()
    assert site_module.browser.free_browsers() == FREE


def test_wait_for_is_checked_and_needs_the_browser():
    links = {"selector": "a.file", "wait_for": {"selector": "a.file", "timeout": 600, "reload": 30}}
    recipe = {"start": f"{SITE}/reports", "links": links}
    assert not check_recipe(recipe) and uses_browser(recipe)
    assert not check_recipe(recipe | {"follow": [{"selector": "a", "wait_for": {"selector": "a"}}]})
    wrong = (
        'file links: wait_for is {"selector": <the element to wait for>, "timeout": seconds from 1 to 1800, '
        '"reload": seconds from 5 (optional)}'
    )
    for wait in ({"timeout": 5}, {"selector": "a", "timeout": 3600}, {"selector": "a", "reload": 1}, "a.file"):
        assert check_recipe(recipe | {"links": links | {"wait_for": wait}}) == [wrong], wait
    listing = {"kind": "json", "url": f"{SITE}/api", "file": "url", "wait_for": {"selector": "a"}}
    assert check_recipe(recipe | {"links": listing}) == ["file links: a JSON listing has no page to wait on (wait_for)"]


def test_over_http_a_wait_says_it_needs_the_browser():
    driver = site_module.HttpDriver(httpx.Client())
    page = HttpPage(f"{SITE}/reports", None)  # type: ignore[arg-type]
    with pytest.raises(SourceError, match="waits on them, which only the browser can"):
        driver.expand(page, {"wait_for": {"selector": "a"}})
