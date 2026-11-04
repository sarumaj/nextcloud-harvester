# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's patterns in plain words (harvester-main.js) in Chromium against the stubbed API
and WebSocket of test_page: a pick's "Address must match" ("Text must match" for one by its
text), a JSON listing's "File's address must match (optional)", each with the line on what a
part written (?P<name>…) gives, in English and German."""

# pylint: disable=missing-function-docstring

import pytest
from test_browser import needs_chromium
from test_page import RECIPE, fixture_page, level, receive  # pylint: disable=unused-import

HELP = "A part written (?P<name>…) becomes {name} for file names and tags, e.g. (?P<year>20\\d\\d) gives {year}."
JSON = {"kind": "json", "url": "https://site.example/api?page=1", "items": "data", "file": "pdf", "pattern": "pdf"}


def recording(page, recipe: dict, edit: str = "Edit", record: str = "Record in the browser") -> None:
    page.get_by_role("button", name=edit).click()
    page.get_by_role("button", name=record).click()
    page.wait_for_function("window.__sockets.length && window.__sockets[0].sent.length")
    receive(page, {"type": "recipe", "recipe": recipe, "proposals": {}})


def pattern_field(page, target: str, label: str):
    return level(page, target).locator(f'label.harvester-field:has(> span:text-is("{label}")) input')


@needs_chromium
def test_a_picks_pattern_says_what_must_match_and_what_its_groups_give(page):
    recording(page, RECIPE | {"links": {"selector": "button.get", "pattern": "Get", "click": True}})
    page.wait_for_selector(".harvester-level[data-target=links]")
    assert pattern_field(page, "follow", "Address must match").input_value() == "y"
    assert pattern_field(page, "links", "Text must match").input_value() == "Get", "a pick by its text"
    assert HELP in level(page, "links").inner_text()


@needs_chromium
def test_a_json_listings_pattern_too(page):
    recording(page, RECIPE | {"links": JSON})
    page.wait_for_selector(".harvester-level[data-target=links]")
    assert pattern_field(page, "links", "File’s address must match (optional)").input_value() == "pdf"
    assert HELP in level(page, "links").inner_text()


@needs_chromium
@pytest.mark.language("de")
def test_the_patterns_words_in_german(page):
    recording(page, RECIPE, "Bearbeiten", "Im Browser aufnehmen")
    page.wait_for_selector(".harvester-level[data-target=links]")
    assert pattern_field(page, "links", "Adresse muss passen").input_value() == "pdf"
    assert "wird zu {name} für Dateinamen und Schlagworte" in level(page, "links").inner_text()
