# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure parts of pages kept as files, without a browser: a page's main content as Markdown
(harvester.pagetext), an MHTML snapshot as one HTML file (harvester.mhtml), and the recipe's and
the job's checks of "save" and "capture" (harvester.recipe, harvester.sources.keep)."""

# pylint: disable=missing-function-docstring

import base64

import httpx
import pytest

from fake_site import SITE
from harvester import mhtml, pagetext
from harvester.recipe import check_recipe, uses_browser
from harvester.sources import SourceError, download, keep
from harvester.sources import site as site_module

LISTING = {"start": f"{SITE}/orders", "links": {"selector": "a.order", "pattern": r"/order/(?P<n>\d+)$"}}


# A page's main content as Markdown.

ORDER = """<html><head><title>Order 7 – Shop</title></head><body>
<nav><a href="/">Home</a> <a href="/shop">Shop</a></nav>
<div class="sidebar"><p>Buy more things, cheap, today, now, here, and with a discount.</p></div>
<div id="content"><h1>Order 7</h1>
<p>Thank you for your order, it ships tomorrow, from our store, with <b>care</b>.</p>
<ul><li>Apple</li><li>Pear<ul><li>green</li></ul></li></ul>
<table><tr><th>Item</th><th>Price</th></tr><tr><td>Apple</td><td>1.00</td></tr></table>
<p>See the <a href="/terms">terms</a>. <img src="/blank.gif" data-src="/receipt.png" alt="Receipt"></p>
<pre>line 1
  line 2</pre><blockquote><p>Quoted</p></blockquote></div>
<div class="cookie-banner" style="display:none">Cookies?</div>
<footer>© Shop</footer><script>var x = 1</script></body></html>"""


def test_the_main_content_is_markdown_under_the_title_and_address():
    text, title = pagetext.markdown(ORDER, f"{SITE}/order/7")
    assert title == "Order 7 – Shop"
    assert text.startswith(f"# Order 7 – Shop\n\n<{SITE}/order/7>\n\n# Order 7\n\n")
    assert "Thank you for your order, it ships tomorrow, from our store, with **care**." in text
    assert "- Apple\n- Pear\n  - green" in text
    assert "| Item | Price |\n|---|---|\n| Apple | 1.00 |" in text
    assert f"[terms]({SITE}/terms)" in text and f"![Receipt]({SITE}/receipt.png)" in text
    assert "```\nline 1\n  line 2\n```" in text and "> Quoted" in text
    for left_out in ("Home", "Buy more", "Cookies", "© Shop", "var x"):
        assert left_out not in text


def test_an_article_alone_is_the_content_and_a_page_without_paragraphs_its_body():
    html = (
        "<html><body><header>Site</header>"
        "<article><h2>News</h2><p>The text of the news, long enough.</p></article></body></html>"
    )
    text, _ = pagetext.markdown(html, f"{SITE}/n")
    assert text == f"# News\n\n<{SITE}/n>\n\nThe text of the news, long enough.\n", "its heading its title"
    text, title = pagetext.markdown("<html><head><title>T</title></head><body>Just a line</body></html>", SITE)
    assert title == "T" and text.endswith("Just a line\n")


# MHTML as one HTML file.

PNG = b"\x89PNG\r\n\x1a\nimage"


def snapshot() -> bytes:
    """An MHTML snapshot as Chromium writes it: the page (quoted-printable), a stylesheet with a
    background, an image and a frame by its Content-ID."""
    image = base64.b64encode(PNG).decode()
    parts = [
        (
            "text/html",
            "quoted-printable",
            f"{SITE}/order/7",
            "<frame-1@mhtml.blink>",
            f'<html><head><meta http-equiv=3D"Content-Type" content=3D"text/html; charset=3Dutf-8">'
            f'<title>Order 7</title><link rel=3D"stylesheet" href=3D"{SITE}/s.css"></head><body>'
            f'<h1>Caf=C3=A9</h1><img src=3D"{SITE}/i.png" srcset=3D"{SITE}/i.png 1x, {SITE}/big.png 2x">'
            '<iframe src=3D"cid:frame-2@mhtml.blink"></iframe><script>x()</script></body></html>',
        ),
        ("text/css", "quoted-printable", f"{SITE}/s.css", "", 'body { background: url("/i.png") }'),
        ("image/png", "base64", f"{SITE}/i.png", "", image),
        ("text/html", "quoted-printable", f"{SITE}/frame", "<frame-2@mhtml.blink>", "<p>In the frame</p>"),
    ]
    lines = [
        "From: <Saved by Blink>",
        f"Snapshot-Content-Location: {SITE}/order/7",
        "MIME-Version: 1.0",
        'Content-Type: multipart/related; type="text/html"; boundary="B"',
        "",
    ]
    for kind, encoding, location, cid, body in parts:
        lines += ["--B", f"Content-Type: {kind}", f"Content-Transfer-Encoding: {encoding}"]
        lines += [f"Content-ID: {cid}"] if cid else []
        lines += [f"Content-Location: {location}", "", body]
    return "\r\n".join([*lines, "--B--", ""]).encode()


def test_a_snapshot_becomes_one_html_file_with_its_resources_inside():
    html = mhtml.single_html(snapshot(), "Saved from the shop").decode()
    data = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    assert html.startswith('<!DOCTYPE html>\n<!-- Saved from the shop -->\n<html><head><meta charset="utf-8"/>')
    assert "Café" in html and "<script" not in html and "http-equiv" not in html
    assert f'<style>body {{ background: url("{data}") }}</style>' in html, "the stylesheet inlined, its image too"
    assert (
        f'src="{data}"' in html and f'srcset="{data} 1x, {SITE}/big.png 2x"' in html
    ), "what it lacks keeps its address"
    assert "srcdoc=" in html and "In the frame" in html and "cid:" not in html


def test_a_snapshot_without_a_page_is_refused():
    with pytest.raises(ValueError):
        mhtml.single_html(b"From: <Saved by Blink>\r\nContent-Type: text/plain\r\n\r\nnothing")


# The checks.


@pytest.mark.parametrize(
    "parts, problem",
    [
        ({"save": "docx"}, "file links: save is one of pdf, html, markdown"),
        ({"capture": "application/pdf"}, "file links: capture is a list of 1 to 10 media types"),
        ({"capture": []}, "file links: capture is a list of 1 to 10 media types"),
        ({"capture": ["pdf"]}, "file links: capture is a list of 1 to 10 media types"),
        ({"capture": ["text/html"]}, "file links: capture keeps files, not web pages"),
        ({"save": "pdf", "capture": ["image/*"]}, "file links: save the pages (save) or capture"),
    ],
)
def test_save_and_capture_are_checked(parts, problem):
    found = check_recipe(LISTING | {"links": LISTING["links"] | parts})
    assert len(found) == 1 and found[0].startswith(problem)


def test_a_recipe_saves_its_start_page_or_has_file_links():
    assert not check_recipe({"start": f"{SITE}/order/1", "save": "markdown"})
    assert check_recipe({"start": f"{SITE}/order/1", "save": "txt"}) == ["save is one of pdf, html, markdown"]
    assert check_recipe(LISTING | {"save": "pdf"}) == [
        "the recipe saves its start page (save) or has levels and file links, not both"
    ]
    for recipe in (
        {"start": SITE, "save": "pdf"},
        LISTING | {"links": LISTING["links"] | {"save": "html"}},
        LISTING | {"links": LISTING["links"] | {"capture": ["image/*", "application/pdf"]}},
    ):
        assert not check_recipe(recipe) and uses_browser(recipe), recipe
    assert not uses_browser(LISTING)


def test_the_jobs_file_types_must_take_what_its_pages_are_saved_as():
    recipe = LISTING | {"links": LISTING["links"] | {"save": "html"}}
    assert keep.problems(recipe, {"file_type": "pdf,zip"}) == [
        "the pages are saved as html, which the job's file types (pdf, zip) refuse"
    ]
    assert not keep.problems(recipe, {"file_type": "html"}) and not keep.problems(recipe, {"file_type": "any"})
    assert not keep.problems({"start": SITE, "save": "markdown"}, {"file_type": "markdown"})
    with pytest.raises(SourceError, match="saved as pdf, which the job's file types"):
        site_module.Site(
            {"recipe": '{"start": "https://site.example/", "save": "pdf"}', "file_type": "image"}, {}, None
        )
    assert download.accepts(["markdown"], "text/markdown; charset=utf-8", b"# A")


def test_captured_types_and_names():
    assert keep.wanted("image/png", ["image/*"]) and keep.wanted("Application/PDF; q=1", ["application/pdf"])
    assert not keep.wanted("text/html", ["image/*"]) and not keep.wanted("", ["image/*"])
    assert keep.sniffed("application/octet-stream", b"%PDF-1.7") == "application/pdf"
    assert keep.sniffed("", b"\x89PNG\r\n") == "image/png" and keep.sniffed("image/gif", b"%PDF") == "image/gif"
    assert keep.with_extension("Order 7", "pdf") == "Order 7.pdf" and keep.with_extension("a.md", "markdown") == "a.md"
    assert keep.with_extension("a", "") == "a"


def test_over_http_pages_are_neither_saved_nor_captured():
    driver = site_module.HttpDriver(httpx.Client())
    with pytest.raises(SourceError, match="saved as pdf, which only the browser can"):
        driver.keep_page(f"{SITE}/order/1", None, {}, {"save": "pdf"})  # type: ignore[arg-type]
    with pytest.raises(SourceError, match="captured, which only the browser can"):
        driver.capture(f"{SITE}/order/1", {}, {"capture": ["image/*"]})
