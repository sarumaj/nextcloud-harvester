# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's picks for pages kept as files (in Chromium against the fake site, routed, no
network): the file links' pages saved as PDF, HTML or Markdown by the pick's choice (named after
their titles), and Test the recipe listing the pages it would save without opening them."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio

from test_browser import needs_chromium
from test_keep import Shop
from test_recorder_features import recording, trial_result

from fake_site import SITE


@needs_chromium
def test_the_file_links_pages_are_saved_by_the_picks_choice_and_the_test_lists_them():
    site = Shop()

    async def scenario():
        driver, recorder = await recording(site)
        try:
            await recorder.handle({"type": "goto", "url": f"{SITE}/orders"})
            await driver.mode("links")
            await driver.click("a.order >> nth=0")
            await driver.until(lambda m: m["recipe"]["links"].get("selector"))
            await recorder.handle({"type": "save", "save": "pdf"})
            saved = driver.last()
            await recorder.handle({"type": "save", "save": "docx"})
            unknown = driver.last()
            tested = await trial_result(driver, recorder, {"recipe": saved["recipe"], "settings": {"path": "{title}"}})
            await recorder.handle({"type": "save", "save": ""})
            files = driver.last()
            await recorder.handle({"type": "undo-pick"})
            return saved, unknown, tested, files, driver.last()
        finally:
            await recorder.close()

    saved, unknown, tested, files, undone = asyncio.run(scenario())
    assert saved["recipe"]["links"]["save"] == "pdf" and saved["proposals"]["links"]["path"] == "{title}"
    assert unknown["recipe"]["links"]["save"] == "pdf", "an unknown kind changes nothing"
    entries = tested["pages"][0]["files"]
    assert [(entry["path"], entry["save"]) for entry in entries] == [("Order 1.pdf", "pdf"), ("Order 2.pdf", "pdf")]
    assert not any("/order/" in url for url in site.requests), "the test opens no page to save"
    assert "save" not in files["recipe"]["links"] and files["proposals"]["links"]["path"] != "{title}"
    assert undone["recipe"]["links"]["save"] == "pdf", "Undo pick takes the choice back"
