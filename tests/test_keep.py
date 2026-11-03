# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pages kept as files (harvester.sources.keep), in Chromium against the fake site (routed, no
network): the pages file links lead to saved as PDF, HTML or Markdown (also when an element is
clicked, and the start page itself, as the template "Save a page" does for an on-demand job); the
files a page loads, embeds or holds as blobs captured, bounded and checked, and a page done not
opened again; and the recipe's test listing such pages without opening them."""

# pylint: disable=missing-function-docstring,protected-access

import dataclasses
import io
import json

import pytest
from test_browser import FREE, needs_chromium
from test_clicks import run, source
from test_images import Photos, png
from test_runner import FakeLibrary

from fake_site import LOGIN, SITE
from harvester import ondemand, runner
from harvester.presets import make_job
from harvester.sources import WebPage, keep
from harvester.sources import site as site_module
from harvester.store import Job

PDF_BODY = b"%PDF-1.7\n" + b"y" * 3000
ORDERS = """<html><body><a class="order" href="/order/1">Order 1</a> <a class="order" href="/order/2">Order 2</a>
<button class="open" onclick="window.open('/order/3')">Show order 3</button></body></html>"""
ORDER = """<html><head><title>Order {n}</title><link rel="stylesheet" href="/css/shop.css"></head><body>
<nav class="menu"><a href="/">Home</a> <a href="/orders">Orders</a></nav>
<main><h1>Order {n}</h1><p>Thank you for your order, it ships tomorrow, from our store, with care.</p>
<img src="/img/logo.png" alt="Logo"><table><tr><th>Item</th><th>Price</th></tr><tr><td>Apple</td><td>1.00</td></tr>
</table></main><script>document.body.dataset.ran = 'yes'</script></body></html>"""
VIEWERS = """<html><body><a class="viewer" href="/viewer/1">Viewer 1</a> <a class="viewer" href="/viewer/2">Viewer 2</a>
</body></html>"""
# A page that loads a PDF (a viewer's request), embeds another without loading it, holds a third as
# a blob, and shows two pictures.
VIEWER = """<html><head><title>Statement {n}</title></head><body>
<embed src="/doc/embedded-{n}.pdf" type="application/pdf" width="10" height="10">
<img src="/img/photo-{n}.png"><img src="/img/thumb-{n}.png">
<script>
fetch('/doc/loaded-{n}.pdf').then((r) => r.arrayBuffer())
const blob = new Blob(['%PDF-1.4 a blob of {n}' + 'z'.repeat(2000)], {{type: 'application/pdf'}})
const link = document.createElement('a')
link.href = URL.createObjectURL(blob)
link.textContent = 'Download'
document.body.append(link)
</script></body></html>"""


class Shop(Photos):  # pylint: disable=too-few-public-methods
    """The fake site with orders and viewers (and the photos, pictures for every address under /img/)."""

    def answer(  # pylint: disable=too-many-return-statements
        self, url: str, method: str = "GET", cookie: str = ""
    ) -> tuple[int, dict, bytes]:
        path = "/" + url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        html = {"content-type": "text/html"}
        self.requests.append(url)
        pages = {"/orders": ORDERS, "/viewers": VIEWERS, "/order/9": LOGIN}
        if path in pages:
            return 200, html, pages[path].encode()
        if path.startswith("/order/"):
            return 200, html, ORDER.format(n=path.rsplit("/", 1)[1]).encode()
        if path.startswith("/viewer/"):
            return 200, html, VIEWER.format(n=path.rsplit("/", 1)[1]).encode()
        if path == "/css/shop.css":
            return 200, {"content-type": "text/css"}, b"body { background: url(/img/paper.png) } h1 { color: #333 }"
        if path.startswith("/doc/"):
            return 200, {"content-type": "application/pdf"}, PDF_BODY + path.encode()
        return super().answer(url, method, cookie)


def saving(kind: str, **more) -> dict:
    return {
        "start": f"{SITE}/orders",
        "links": {"selector": "a.order", "pattern": r"/order/(?P<n>\d+)$", "save": kind} | more,
    }


@needs_chromium
@pytest.mark.parametrize("kind, extension", [("pdf", ".pdf"), ("html", ".html"), ("markdown", ".md")])
def test_the_pages_file_links_lead_to_are_saved_named_after_their_titles(monkeypatch, kind, extension):
    message, library, _ = run(source(Shop(), monkeypatch, saving(kind), path="{title}"))
    assert message == "2 downloaded"
    assert sorted(library.files) == [f"/In/Order 1{extension}", f"/In/Order 2{extension}"]
    saved = library.files[f"/In/Order 1{extension}"]
    if kind == "pdf":
        assert saved.startswith(b"%PDF-") and len(saved) > 1000
    elif kind == "html":
        text = saved.decode()
        assert text.startswith(f"<!DOCTYPE html>\n<!-- Saved by Harvester from {SITE}/order/1 -->")
        assert "<h1>Order 1</h1>" in text and 'data-ran="yes"' in text, "as shown, after its scripts"
        assert "<script" not in text and text.count("data:image/png;base64,") == 2, "the logo and the background"
    else:
        text = saved.decode()
        assert text.startswith(f"# Order 1\n\n<{SITE}/order/1>\n\nThank you for your order")
        assert "| Item | Price |" in text and "![Logo](" in text and "Home" not in text
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_an_element_clicked_whose_tab_shows_the_page_has_it_saved(monkeypatch):
    recipe = {
        "start": f"{SITE}/orders",
        "links": {"selector": "button.open", "pattern": "order", "click": True, "save": "pdf"},
    }
    made = source(Shop(), monkeypatch, recipe, path="{title} ({text})")
    try:
        items = made.items(lambda key: False)
        item = next(items)
        assert item.path == "Show order 3 (Show order 3).pdf", "a stand-in until the page is saved"
        dest = io.BytesIO()
        renamed = made.fetch(item, dest)
        assert renamed is not None and renamed.path == "Order 3 (Show order 3).pdf"
        assert dest.getvalue().startswith(b"%PDF-")
        tabs = [page.url for page in made.driver.context.pages]  # type: ignore[union-attr]
        assert not any("/order/3" in url for url in tabs), "the tab it opened is closed"
        items.close()
    finally:
        made.close()


@needs_chromium
def test_a_sign_in_instead_of_the_page_is_no_saved_page(monkeypatch):
    recipe = {"start": f"{SITE}/orders", "links": {"selector": "a.order", "pattern": "/order/", "save": "pdf"}}
    made = source(Shop(), monkeypatch, recipe)
    try:
        items = made.items(lambda key: False)
        item = next(items)
        # The session ended: the order's address answers the sign-in.
        with pytest.raises(WebPage, match="sign-in page") as raised:
            made.fetch(dataclasses.replace(item, url=f"{SITE}/order/9"), io.BytesIO())
        assert raised.value.login
        items.close()
    finally:
        made.close()


@needs_chromium
def test_the_template_saves_the_page_sent_to_an_on_demand_job(monkeypatch):
    made_job = make_job("save-page", {"name": "Receipts", "format": "markdown"})
    job = Job(user="u", name="Receipts", source="site", target="/In", schedule="", options=made_job["options"])
    fetching = ondemand.for_fetch(job, f"{SITE}/order/5")
    assert not ondemand.fetch_problem(job, f"{SITE}/order/5", account=False)
    routed_site = Shop()
    made = source(routed_site, monkeypatch, json.loads(fetching.options["recipe"]), **_options(fetching.options))
    message, library, _ = run(made)
    assert message == "1 downloaded" and list(library.files) == ["/In/Order 5.md"]
    assert library.files["/In/Order 5.md"].startswith(b"# Order 5\n")


def _options(options: dict) -> dict:
    return {key: value for key, value in options.items() if key not in ("recipe", "stop_at_seen")}


# Captured files.


def capturing(types: list[str]) -> dict:
    return {
        "start": f"{SITE}/viewers",
        "links": {"selector": "a.viewer", "pattern": r"/viewer/(?P<n>\d)$", "capture": types},
    }


def run_seen(made: site_module.Site, seen: set[str]) -> tuple[str, FakeLibrary]:
    library = FakeLibrary()
    job = Job(user="u", name="n", source="site", target="/In", schedule="* * * * *", options={"max_items": -1})
    try:
        message = runner.run(job, made, library, seen.__contains__, seen.add, lambda seconds: None)
    finally:
        made.close()
    return message, library


@needs_chromium
def test_the_files_a_page_loads_embeds_or_holds_as_blobs_are_captured_and_a_page_done_is_not_opened_again(
    monkeypatch,
):
    site = Shop()
    seen: set[str] = set()
    message, library = run_seen(source(site, monkeypatch, capturing(["application/pdf"]), path="{n}/{filename}"), seen)
    assert message == "6 downloaded"
    # The answer first, then the embedded file, then the blob (named by its place).
    assert sorted(library.files) == sorted(
        f"/In/{n}/{name}" for n in (1, 2) for name in (f"loaded-{n}.pdf", f"embedded-{n}.pdf", "file-3.pdf")
    )
    assert library.files["/In/1/loaded-1.pdf"] == PDF_BODY + b"/doc/loaded-1.pdf"
    assert library.files["/In/1/file-3.pdf"].startswith(b"%PDF-1.4 a blob of 1")
    assert {keep.CAPTURED + f"{SITE}/viewer/1", keep.CAPTURED + f"{SITE}/viewer/2"} <= seen, "the pages are done"
    before = len(site.requests)
    message, library = run_seen(source(site, monkeypatch, capturing(["application/pdf"])), seen)
    assert message.startswith("0 downloaded") and not library.files
    assert not any("/viewer/" in url for url in site.requests[before:]), "no page done is opened again"
    assert site_module.browser.free_browsers() == FREE


@needs_chromium
def test_captured_files_are_bounded_per_page_and_checked_as_downloads(monkeypatch):
    monkeypatch.setattr(keep, "CAPTURE_FILES", 1)
    message, library = run_seen(source(Shop(), monkeypatch, capturing(["image/*"]), path="{n}-{filename}"), set())
    assert message == "2 downloaded" and sorted(library.files) == ["/In/1-photo-1.png", "/In/2-photo-2.png"]
    assert library.files["/In/1-photo-1.png"] == png(len("/img/photo-1.png"))
    # The job's file types still hold: a PDF job keeps no pictures.
    log: list[str] = []
    made = source(Shop(), monkeypatch, capturing(["image/*"]), file_type="pdf")
    made.report = lambda level, text: log.append(text)
    message, library = run_seen(made, set())
    assert not library.files and message.startswith("0 downloaded")


# The recipe's test.


@needs_chromium
def test_the_recipes_test_lists_pages_to_save_or_capture_without_opening_them(monkeypatch):
    site = Shop()
    for recipe, flags in ((saving("html"), ("html", [])), (capturing(["image/*"]), ("", ["image/*"]))):
        made = source(site, monkeypatch, recipe, path="{title}")
        try:
            result = made.survey()
        finally:
            made.close()
        files = result["pages"][0]["files"]
        assert result["ended"] == "complete" and len(files) == 2
        assert all((file["save"], file["capture"]) == flags and not file["click"] for file in files)
    assert not any("/order/" in url or "/viewer/" in url for url in site.requests), "no page opened"
    made = source(site, monkeypatch, {"start": f"{SITE}/order/4", "save": "pdf"}, path="{title}")
    try:
        result = made.survey()
    finally:
        made.close()
    assert result["pages"][0]["selector"] == "(the page itself)"
    assert [file["path"] for file in result["pages"][0]["files"]] == ["4.pdf"], "its title comes as it is saved"
