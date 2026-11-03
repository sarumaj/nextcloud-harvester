# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recipe's checks when a job is saved: the types of its parts, the actions, credentials and
keys a step may name, the key template, the levels' loading of more; and the dates a fill step
may hold, filled in at each replay."""

# pylint: disable=missing-function-docstring

import datetime
import json

import pytest

from fake_site import SITE
from harvester.presets import PRESETS, make_job
from harvester.recipe import check_recipe, dated, key_ok, sign_in_steps, uses_browser
from harvester.sources import SourceError
from harvester.sources.site import Site

LISTING = {"start": f"{SITE}/list", "links": {"selector": "a.file", "pattern": r"/files/(?P<n>\d+)-(?P<m>\d+)\.pdf$"}}


def problems(**parts) -> list[str]:
    return check_recipe(LISTING | parts)


def test_every_preset_and_old_recipe_still_passes():
    for name, preset in PRESETS.items():
        if preset["source"] != "site":
            continue
        given = {key: f"{SITE}/page" for key, spec in preset.get("parameters", {}).items() if not spec.get("default")}
        assert check_recipe(json.loads(make_job(name, given)["options"]["recipe"])) == [], name
    assert not problems(next={"selector": "a.next", "level": 0})


@pytest.mark.parametrize(
    "parts, problem",
    [
        ({"next": {"selector": "a.next", "level": "x"}}, "next page: the level is a number from 0 (the listing) to 0"),
        ({"next": {"selector": "a.next", "level": 2}}, "next page: the level is a number from 0"),
        ({"next": {"selector": "a.next", "level": True}}, "next page: the level is a number from 0"),
        ({"next": {"selector": ["a"]}}, "next page: the element (selector) is text"),
        ({"next": {"selector": "a", "buttons": "yes"}}, "next page: buttons is true or false"),
        ({"links": {"selector": 5}}, "file links: the element (selector) is text"),
        ({"links": {"selector": "a", "pattern": 5}}, "file links: the pattern is text"),
        ({"follow": {"selector": "a"}}, "the levels (follow) are a list"),
        ({"follow": [{"selector": "a", "scroll": 0}]}, "level 1: rounds of scrolling from 1 to 100"),
        ({"links": {"selector": "a", "load_more": ""}}, "file links: the Load more button's element"),
        ({"links": {"selector": "a", "click": 1}}, "file links: click is true or false"),
        ({"start": ["x"]}, "the listing (start) is an address"),
    ],
)
def test_parts_of_the_wrong_kind_are_named(parts, problem):
    assert any(found.startswith(problem) for found in problems(**parts)), problems(**parts)


@pytest.mark.parametrize(
    "step, problem",
    [
        ({"action": "fill", "selector": "#u", "credential": "pasword"}, "unknown credential pasword"),
        ({"action": "fill", "selector": "#u", "credential": "cookies"}, "unknown credential cookies"),
        ({"action": "press", "selector": "#q", "key": "Entr"}, "Entr is no key"),
        ({"action": "press", "selector": "#q", "key": 13}, "13 is no key"),
        ({"action": "click", "selector": 3}, "the selector is text"),
        ({"action": "goto", "url": 3}, "the browser may not open 3"),
        ({"action": "click", "selector": "a", "text": ["Go"]}, "the text is text"),
        ({"action": "fill", "selector": "#q", "value": 5}, "the value is text"),
        ({"action": "fill", "selector": "#q", "value": "{today-3x}"}, "{today-3x} is no date"),
        ({"action": "fill", "selector": "#q", "value": "{month:%Y}"}, "{month:%Y} is no date"),
        ({"action": "jump"}, "unknown action jump"),
    ],
)
def test_a_steps_fields_are_checked_with_readable_messages(step, problem):
    found = problems(steps=[step])
    assert found and found[0] == f"step 1: {problem}" or found[0].startswith(f"step 1: {problem}"), found


def test_keys_the_browser_knows():
    for key in ("Enter", "Tab", "a", "+", "F5", "Control+A", "Shift++", "ControlOrMeta+KeyC", "ArrowDown"):
        assert key_ok(key), key
    for key in ("Entr", "", "Foo+A", "Control+", "enter"):
        assert not key_ok(key), key


def test_a_bad_recipe_is_refused_when_the_job_is_saved():
    recipe = LISTING | {"steps": [{"action": "press", "selector": "#q", "key": "Entr"}]}
    with pytest.raises(SourceError, match="Entr is no key"):
        Site({"recipe": json.dumps(recipe)}, {}, None)


def test_the_key_template_names_only_values_the_links_have():
    assert not problems(links=LISTING["links"] | {"key": "{n}-{m}"})
    assert not problems(links=LISTING["links"] | {"key": "{filename}"})
    assert problems(links=LISTING["links"] | {"key": "{issue}"}) == [
        "file links: the key {issue} names issue, which no pattern gives"
    ]
    assert problems(links=LISTING["links"] | {"key": "fixed"})[0].startswith("file links: the key fixed names no value")
    assert problems(links=LISTING["links"] | {"key": "{n"})[0].startswith("file links: the key {n is no template")
    follow = [{"selector": "a.year", "pattern": r"/(?P<year>\d{4})$"}]
    assert not problems(follow=follow, links=LISTING["links"] | {"key": "{year}-{n}"}), "a level's group counts"


def test_what_needs_the_browser():
    assert not uses_browser(LISTING)
    assert uses_browser(LISTING | {"next": {"selector": "button.more", "buttons": True}})
    assert uses_browser(LISTING | {"links": {"selector": "button", "click": True}})
    assert uses_browser(LISTING | {"follow": [{"selector": "a", "load_more": "button.more"}]})
    assert uses_browser(LISTING | {"links": {"selector": "a", "scroll": 5}})


def test_dates_in_a_value_are_filled_in_for_the_day():
    day = datetime.date(2024, 3, 31)
    assert dated("{today}", day) == "2024-03-31"
    assert dated("from {today-30d:%d.%m.%Y} to {today:%d.%m.%Y}", day) == "from 01.03.2024 to 31.03.2024"
    assert dated("{today-1m}", day) == "2024-02-29", "a month back keeps the month's last day"
    assert dated("{today+1w}", day) == "2024-04-07"
    assert dated("{year}/{month:02d}/{day}", day) == "2024/03/31"
    assert dated("{year-1} {month-3:02d}", day) == "2023 12"
    assert dated('{"q": 1} {name} {', day) == '{"q": 1} {name} {', "other braces are typed as they are"


def test_the_sign_in_ends_with_what_sends_the_last_credential():
    steps = [
        {"action": "goto", "url": f"{SITE}/login"},
        {"action": "fill", "selector": "#u", "credential": "username"},
        {"action": "click", "selector": "#next"},
        {"action": "fill", "selector": "#p", "credential": "password"},
        {"action": "press", "selector": "#p", "key": "Enter"},
        {"action": "click", "selector": "#invoices"},
        {"action": "scroll"},
    ]
    assert sign_in_steps(steps) == 5, "username first: the password's Enter ends the sign-in"
    assert sign_in_steps(steps[:4]) == 4
    assert sign_in_steps([{"action": "goto", "url": f"{SITE}/list"}, {"action": "click", "selector": "a"}]) == 0
    popup = [{"action": "click", "selector": "#sso"}, {"action": "popup"}, *steps[1:5], {"action": "opener"}]
    assert sign_in_steps(popup + steps[5:]) == 7, "back from the sign-in's popup is the sign-in's"
