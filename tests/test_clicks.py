# SPDX-License-Identifier: AGPL-3.0-or-later
"""Elements without an address that a job clicks (harvester.sources.clicks), in Chromium against
the fake site (routed, no network): file links whose click opens a tab or popup that shows the
file or starts its download, or takes their own tab to the file, with a download's checks; and
levels to follow whose elements are clicked (a row, a card, a button), read where each click
lands (another address, a new tab, a route of a single-page app, a dialog), and left again for
the listing (Back, Escape, else the listing opened anew)."""

# pylint: disable=missing-function-docstring,protected-access

import io
import json

import httpx
import pytest
from test_browser import FREE, needs_chromium
from test_runner import FakeLibrary

from fake_site import PDF, SITE, Site
from harvester import runner
from harvester.recipe import check_recipe, uses_browser
from harvester.sources import SourceError, WebPage, clicks
from harvester.sources import site as site_module
from harvester.store import Job

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 2000

# File links: buttons whose click opens a tab with a PDF (the viewer, or a download in a browser
# without one), a tab with a picture, takes this tab to a picture, opens a page that starts the
# download itself, or leads to a web page.
OPENERS = """<html><body>
<button class="dl" onclick="window.open('/view/a.pdf')">Get A</button>
<button class="dl" onclick="window.open('/img/b.png')">Get B</button>
<button class="dl" onclick="location.href = '/img/c.png'">Get C</button>
<button class="dl" onclick="window.open('/thanks/d')">Get D</button>
<button class="web" onclick="location.href = '/about'">Get E</button>
</body></html>"""

# Levels to follow: rows that are clicked; each opens its entry another way.
ROWS = """<html><body><div id="app"></div><script>
const entries = [
  ['Entry 1', () => { location.href = '/detail/1' }],
  ['Entry 2', () => { window.open('/detail/2') }],
  ['Entry 3', () => { history.pushState({}, '', '/rows/3'); detail(3) }],
  ['Entry 4', () => {
    const dialog = document.createElement('dialog')
    dialog.innerHTML = '<a class="file" href="/files/4.pdf">File 4</a>'
    document.body.append(dialog)
    dialog.addEventListener('close', () => dialog.remove())
    dialog.showModal()
  }],
  ['Entry 5', () => {
    const panel = document.createElement('div')
    panel.innerHTML = '<a class="file" href="/files/5.pdf">File 5</a>'
    document.body.append(panel)
  }],
]
function list() {
  app.innerHTML = ''
  for (const [text, open] of entries) {
    const row = document.createElement('div')
    row.className = 'row'
    row.textContent = text
    row.onclick = open
    app.append(row)
  }
}
function detail(n) { app.innerHTML = `<a class="file" href="/files/${n}.pdf">File ${n}</a>` }
window.addEventListener('popstate', list)
list()
</script></body></html>"""


class Clicked(Site):  # pylint: disable=too-few-public-methods
    """The fake site with the pages above, their files and details."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        path = url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        html = {"content-type": "text/html"}
        pages = {"openers": OPENERS, "rows": ROWS, "about": "<html><body>About us</body></html>"}
        if path in pages:
            self.requests.append(url)
            return 200, html, pages[path].encode()
        if path.startswith("detail/"):
            number = path.rsplit("/", 1)[1]
            return 200, html, f'<html><body><a class="file" href="/files/{number}.pdf">File</a></body></html>'.encode()
        if path.startswith("thanks/"):
            # A page that starts the download itself, a moment after it showed.
            script = "<script>setTimeout(() => { location.href = '/dl/d' }, 300)</script>"
            return 200, html, f"<html><body>Thanks{script}</body></html>".encode()
        if path.startswith(("view/", "img/", "dl/")):
            self.requests.append(url)
            name = path.rsplit("/", 1)[1]
            if path.startswith("img/"):
                return 200, {"content-type": "image/png"}, PNG
            disposition = "attachment" if path.startswith("dl/") else "inline"
            return (
                200,
                {"content-type": "application/pdf", "content-disposition": f'{disposition}; filename="{name}"'},
                PDF,
            )
        return super().answer(url, method, cookie)


def routed(site: Site, monkeypatch) -> None:
    """The fake site for the browser, and for the downloads over HTTP (with the browser's
    cookies) instead of the network."""

    def fulfill(route) -> None:
        status, headers, body = site.answer(route.request.url, route.request.method)
        route.fulfill(status=status, headers=headers, body=body)

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = site.answer(str(request.url), request.method)
        return httpx.Response(status, headers=headers, content=body)

    def client(**options) -> httpx.Client:
        options.pop("proxy", None)
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, **options)

    monkeypatch.setattr(site_module, "guard", fulfill)
    monkeypatch.setattr(site_module.download, "guarded_client", client)


def source(site: Site, monkeypatch, recipe: dict, **options) -> site_module.Site:
    routed(site, monkeypatch)
    made = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0} | options, {}, None)
    made.sleep = lambda seconds: None
    made.page_pause = 0.0
    return made


def run(made: site_module.Site) -> tuple[str, FakeLibrary, list[str]]:
    library = FakeLibrary()
    log: list[str] = []
    job = Job(user="u", name="n", source="site", target="/In", schedule="* * * * *", options={"max_items": -1})
    try:
        message = runner.run(
            job,
            made,
            library,
            lambda key: False,
            lambda key: None,
            lambda seconds: None,
            report=lambda _, text: log.append(text),
        )
    finally:
        made.close()
    return message, library, log


# File links that are clicked.


@needs_chromium
def test_a_click_that_opens_a_tab_or_takes_its_own_to_the_file_gives_the_file(monkeypatch):
    site = Clicked()
    recipe = {
        "start": f"{SITE}/openers",
        "links": {"selector": "button.dl", "pattern": r"^Get (?P<letter>\w)$", "click": True},
    }
    message, library, _ = run(source(site, monkeypatch, recipe, path="{letter}-{disposition}", file_type="pdf,image"))
    assert message == "4 downloaded"
    assert sorted(library.files) == ["/In/A-a.pdf", "/In/B-b.png", "/In/C-c.png", "/In/D-d"]
    assert library.files["/In/A-a.pdf"] == PDF and library.files["/In/B-b.png"] == PNG
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_a_click_that_leads_to_a_web_page_is_no_file(monkeypatch):
    monkeypatch.setattr(clicks, "LANDED_WAIT", 1)
    recipe = {"start": f"{SITE}/openers", "links": {"selector": "button.web", "pattern": ".", "click": True}}
    made = source(Clicked(), monkeypatch, recipe)
    try:
        items = made.items(lambda key: False)
        item = next(items)
        with pytest.raises(WebPage, match=r"clicking “Get E” \(button.web\) led to a web page \(.*/about\)"):
            made.fetch(item, io.BytesIO())
        assert item.trigger[0].url == f"{SITE}/openers", "back on the listing"
        items.close()
    finally:
        made.close()


@needs_chromium
def test_a_file_a_tab_shows_keeps_the_jobs_file_types(monkeypatch):
    recipe = {"start": f"{SITE}/openers", "links": {"selector": "button.dl", "pattern": "^Get B$", "click": True}}
    message, library, log = run(source(Clicked(), monkeypatch, recipe, file_type="pdf"))
    assert not library.files and message.startswith("0 downloaded")
    assert any("not of the job's file types" in line for line in log)


# Levels to follow that are clicked.


@needs_chromium
def test_a_level_of_rows_is_clicked_read_where_each_lands_and_left_for_the_listing(monkeypatch):
    recipe = {
        "start": f"{SITE}/rows",
        "follow": [{"selector": "div.row", "pattern": r"^Entry (?P<entry>\d)$", "click": True}],
        "links": {"selector": "a.file", "pattern": r"\.pdf$"},
    }
    made = source(Clicked(), monkeypatch, recipe, path="{entry}-{filename}")
    try:
        items = list(made.items(lambda key: False))
        tabs = made.driver.context.pages if made.driver else []
    finally:
        made.close()
    # 1: another address, 2: a new tab, 3: a route, 4: a dialog (Escape), 5: a panel (opened anew).
    assert [item.path for item in items] == [f"{n}-{n}.pdf" for n in range(1, 6)]
    assert [item.values["entry"] for item in items] == [1, 2, 3, 4, 5]
    assert len(tabs) == 1, "the tab the click opened is closed"
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_an_element_that_changes_nothing_is_a_gap_and_the_walk_goes_on(monkeypatch):
    monkeypatch.setattr(site_module, "STEP_TIMEOUT", 1500)
    page = ROWS.replace("['Entry 2', () => { window.open('/detail/2') }]", "['Entry 2', () => {}]")
    site = Clicked()
    original = site.answer
    site.answer = lambda url, method="GET", cookie="": (  # type: ignore[method-assign]
        (200, {"content-type": "text/html"}, page.encode()) if url.endswith("/rows") else original(url, method, cookie)
    )
    recipe = {
        "start": f"{SITE}/rows",
        "follow": [{"selector": "div.row", "pattern": "^Entry [12]$", "click": True}],
        "links": {"selector": "a.file", "pattern": r"\.pdf$"},
    }
    made = source(site, monkeypatch, recipe)
    log: list[str] = []
    made.report = lambda level, text: log.append(text)
    try:
        found = [item.path for item in made.items(lambda key: False)]
    finally:
        made.close()
    assert found == ["1.pdf"] and made.gaps == 1
    assert any("level 1: clicking “Entry 2” failed" in line and "changed nothing" in line for line in log)


def test_click_is_checked_on_every_level_and_needs_the_browser():
    recipe = {
        "start": f"{SITE}/rows",
        "follow": [{"selector": "div.row", "pattern": ".", "click": True}],
        "links": {"selector": "a.file", "pattern": "pdf"},
    }
    assert not check_recipe(recipe) and uses_browser(recipe)
    assert check_recipe(recipe | {"follow": [{"selector": "div.row", "click": "yes"}]}) == [
        "level 1: click is true or false"
    ]
    assert check_recipe(recipe | {"links": {"selector": "a", "click": 1}}) == ["file links: click is true or false"]


def test_over_http_a_clicked_level_says_it_needs_the_browser():
    driver = site_module.HttpDriver(httpx.Client())
    target = clicks.Target(None, "div.row", 0, "Entry 1", f"{SITE}/rows", {}, "")
    with pytest.raises(SourceError, match="only the browser can"):
        driver.open_clicked(target)
