# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's picks of elements without an address (in Chromium against the fake site, routed,
no network): buttons, links to javascript: and rows a script opens, picked as file links or as a
level to follow, become picks that are clicked, matched by their texts, switched on and off in
the panel; and Test the recipe walks such levels by clicking, and lists file links that are
clicked without clicking them."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio

from test_browser import needs_chromium
from test_clicks import ROWS, Clicked
from test_recorder_features import recording, trial_result

from fake_site import SITE
from harvester.browser.linkpicks import link_proposal, text_pattern

# Buttons and links without an address, and plain links, side by side.
MIXED = """<html><body>
<button class="dl">Get report 2024</button><button class="dl">Get report 2025</button>
<a class="js" href="javascript:void(0)">Invoice 7</a><a class="js" href="javascript:void(0)">Invoice 8</a>
<a class="file" href="/files/a.pdf">A</a><a class="file" href="/files/b.pdf">B</a>
</body></html>"""


class Picks(Clicked):  # pylint: disable=too-few-public-methods
    """The fake site with the mixed page, and rows that show the hand cursor."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        if url.endswith("/mixed"):
            return 200, {"content-type": "text/html"}, MIXED.encode()
        if url.endswith("/rows"):
            page = ROWS.replace("<body>", "<body><style>.row { cursor: pointer; padding: 4px }</style>")
            return 200, {"content-type": "text/html"}, page.encode()
        return super().answer(url, method, cookie)


def test_a_pattern_on_texts_takes_what_differs_as_a_value():
    assert text_pattern(["Get report 2024", "Get report 2025"]) == r"^Get\ report\ (?P<label>.+)"
    assert text_pattern(["2024 (PDF)", "2025 (PDF)"]) == r"^(?P<label>.+?)\ \(PDF\)"
    assert text_pattern(["Alpha", "Beta"]) == "." and text_pattern(["Same", "Same"]) == "."
    proposal, clicked = link_proposal([["", "Invoice 7\n12.03.2026"], ["", "Invoice 8\n13.03.2026"]], "links")
    assert clicked and proposal["path"] == "{disposition}" and proposal["matches"] == 2
    assert proposal["pattern"] == r"^Invoice\ (?P<label>.+)", "the first line: a row's text goes on below"
    assert link_proposal([[f"{SITE}/a.pdf", "A"], ["", "B"]], "links")[1] is False, "one has an address"


@needs_chromium
def test_buttons_and_links_without_an_address_are_picked_as_file_links_that_are_clicked():
    async def scenario():
        driver, recorder = await recording(Picks())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/mixed"})
            await driver.mode("links")
            await driver.click("button.dl >> nth=1")
            buttons = await driver.until(lambda m: m["recipe"]["links"].get("selector"))
            await driver.click("a.js >> nth=0")
            scripts = await driver.until(lambda m: "a" in m["recipe"]["links"].get("selector", ""))
            await driver.click("a.file >> nth=0")
            files = await driver.until(lambda m: "file" in m["recipe"]["links"].get("selector", ""))
            return buttons, scripts, files
        finally:
            await recorder.close()

    buttons, scripts, files = asyncio.run(scenario())
    links, proposal = buttons["recipe"]["links"], buttons["proposals"]["links"]
    assert links["click"] is True and links["pattern"] == r"^Get\ report\ (?P<label>.+)"
    assert proposal["click"] and proposal["matching"] == 2 and proposal["path"] == "{disposition}"
    assert [example["values"] for example in proposal["examples"]] == [{"label": 2024}, {"label": 2025}]
    assert scripts["recipe"]["links"]["click"] is True, "javascript: is no address"
    assert scripts["recipe"]["links"]["pattern"] == r"^Invoice\ (?P<label>.+)"
    assert "click" not in files["recipe"]["links"] and files["recipe"]["links"]["pattern"].endswith(r"\.pdf$")


@needs_chromium
def test_a_pick_is_switched_between_clicked_and_links_in_the_panel():
    async def scenario():
        driver, recorder = await recording(Picks())
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/mixed"})
            await driver.mode("links")
            await driver.click("a.file >> nth=0")
            await driver.until(lambda m: m["recipe"]["links"].get("selector"))
            await recorder.handle({"type": "click", "target": "links", "level": 0, "click": True})
            on = driver.last()
            await recorder.handle({"type": "click", "target": "links", "level": 0, "click": False})
            off = driver.last()
            await recorder.handle({"type": "undo-pick"})
            return on, off, driver.last()
        finally:
            await recorder.close()

    on, off, undone = asyncio.run(scenario())
    assert on["recipe"]["links"]["click"] is True and on["recipe"]["links"]["pattern"] == "."
    assert on["proposals"]["links"]["click"] and on["proposals"]["links"]["matching"] == 2, "matched by texts"
    assert "click" not in off["recipe"]["links"] and off["recipe"]["links"]["pattern"].endswith(r"\.pdf$")
    assert undone["recipe"]["links"]["click"] is True, "Undo pick takes the switch back"


@needs_chromium
def test_rows_a_script_opens_are_a_level_that_is_clicked_and_the_test_walks_it():
    site = Picks()

    async def scenario():
        driver, recorder = await recording(site)
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/rows"})
            await driver.mode("follow")
            await driver.click("div.row >> nth=2")
            picked = await driver.until(lambda m: m["recipe"]["follow"])
            # The first row's detail is at another address: below the level.
            await driver.mode("browse")
            await driver.click("div.row >> nth=0")
            assert recorder._page is not None
            await recorder._page.wait_for_url("**/detail/1")
            await driver.mode("links")
            await driver.click("a.file")
            files = await driver.until(lambda m: m["recipe"]["links"].get("selector"))
            # One file link on the page: a pattern for it alone, made general for the test.
            recipe = files["recipe"] | {"links": files["recipe"]["links"] | {"pattern": r"\.pdf$"}}
            tested = await trial_result(
                driver, recorder, {"recipe": recipe, "settings": {"path": "{label}-{filename}"}}
            )
            return picked, files, tested
        finally:
            await recorder.close()

    picked, files, tested = asyncio.run(scenario())
    level = picked["recipe"]["follow"][0]
    assert level["click"] is True and level["pattern"] == r"^Entry\ (?P<label>.+)"
    assert files["recipe"]["start"] == f"{SITE}/rows" and files["proposals"]["links"]["level"] == 1
    assert tested["ended"] == "complete" and tested["files"] == 5 and tested["problems"] == []
    assert [page["kind"] for page in tested["pages"]][:2] == ["follow", "links"]
    assert [file["path"] for page in tested["pages"] for file in page["files"]] == [f"{n}-{n}.pdf" for n in range(1, 6)]


@needs_chromium
def test_the_test_lists_file_links_that_are_clicked_without_clicking_them():
    site = Picks()
    recipe = {
        "start": f"{SITE}/openers",
        "links": {"selector": "button.dl", "pattern": r"^Get (?P<letter>\w)$", "click": True},
    }

    async def scenario():
        driver, recorder = await recording(site)
        try:
            # As the job runs (an account, the app's): the page's own test message only waits for it.
            await recorder.test({"recipe": recipe, "settings": {"path": "{letter}.pdf"}}, account={})
            return await trial_result(driver, recorder, {"recipe": recipe})
        finally:
            await recorder.close()

    result = asyncio.run(scenario())
    files = result["pages"][0]["files"]
    assert [(file["path"], file["text"], file["click"]) for file in files] == [
        ("A.pdf", "Get A", True),
        ("B.pdf", "Get B", True),
        ("C.pdf", "Get C", True),
        ("D.pdf", "Get D", True),
    ]
    assert result["as_job"] and result["probe"] is None, "no address to probe: nothing clicked"
    assert not any("/view/" in url or "/img/" in url or "/dl/" in url for url in site.requests)
