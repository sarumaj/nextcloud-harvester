# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pattern proposals, values and templates, and the job templates (presets)."""

# pylint: disable=missing-function-docstring

import json
import re

import pytest

from harvester import ondemand
from harvester.patterns import MAX_LIST_TAGS, group_values, propose, render_path, render_tags, tags_left_out
from harvester.presets import PRESETS, make_job
from harvester.sources.site import Site


def test_issues_of_one_year_still_give_a_year_group():
    proposal = propose([f"https://www.heise.de/select/ix/archiv/2025/{n}" for n in range(1, 14)])
    assert proposal["pattern"] == r"/select/ix/archiv/(?P<year>\d{4})/(?P<issue>\d+)$"
    assert proposal["path"] == "{year}/{text}" and proposal["tags"] == ["{year}"]
    assert re.search(proposal["pattern"], "https://www.heise.de/select/ix/archiv/2019/7")


def test_numbers_in_file_names_and_queries():
    files = propose([f"https://ex.org/files/report-{y}-q{q}.pdf" for y, q in ((2024, 1), (2025, 3), (2023, 2))])
    assert files["pattern"] == r"/files/report-(?P<year>\d{4})-q(?P<number>\d+)\.pdf$"
    assert files["path"] == "{year}/{filename}"
    query = propose(["https://ex.org/dl?id=3&type=pdf", "https://ex.org/dl?id=17&type=pdf", "https://ex.org/about"])
    assert query["pattern"] == r"/dl\?id=(?P<id>\d+)&type=pdf$" and query["matches"] == 2


def test_names_that_differ_keep_their_extension():
    proposal = propose(["https://ex.org/a/Invoice_March.pdf", "https://ex.org/a/Rechnung Juni.pdf"])
    assert proposal["pattern"] == r"/a/(?P<name>[^/?]+)\.pdf$"


def test_values_and_templates():
    values = group_values(re.search(r"/(?P<year>\d{4})/(?P<issue>\d+)", "/2025/3"))
    assert values == {"year": 2025, "issue": 3}
    assert render_path("../{year}/iX {year}-{issue:02d}.pdf", values) == "2025/iX 2025-03.pdf"
    assert render_tags(["iX", "{year}", "{missing}", ""], values) == ["iX", "2025"]


def test_a_list_value_alone_is_a_tag_per_entry_and_joined_elsewhere():
    values = {"tags": ["sky", " blue  sky ", "", "sky", "x" * 80], "year": 2026}
    assert render_tags(["{tags}", " {year} "], values) == ["sky", "blue sky", "x" * 64, "2026"]
    # In a template of more than itself, and in a file name, its entries are joined.
    assert render_tags(["{year}: {tags}"], {"tags": ["a", "b"], "year": 2026}) == ["2026: a, b"]
    assert render_path("{year}/{tags}.jpg", {"tags": ["a", "b"], "year": 2026}) == "2026/a, b.jpg"
    assert not render_tags(["{tags}"], {"tags": []}) and not render_tags(["{tags}"], {})
    assert tags_left_out(["{tags}"], values) == 0


def test_a_long_list_gives_its_first_tags_and_counts_the_rest():
    values = {"tags": [f"tag {number}" for number in range(MAX_LIST_TAGS + 7)]}
    assert render_tags(["{tags}"], values) == [f"tag {number}" for number in range(MAX_LIST_TAGS)]
    assert tags_left_out(["{tags}", "{other}"], values) == 7


def test_heise_template_makes_a_valid_job():
    assert "heise" in PRESETS
    job = make_job("heise", {"magazine": "ct", "name": "c't", "years": "202[4-9]"})
    recipe = json.loads(job["options"]["recipe"])
    assert recipe["start"] == "https://www.heise.de/select/ct/archiv"
    assert recipe["follow"][0]["pattern"] == r"/select/ct/archiv/(?P<year>202[4-9])$"
    assert job["options"]["path"] == "{year}/c't {year}-{issue:02d}.pdf"
    assert job["tags"] == ["c't", "{year}"]
    Site(job["options"], {}, None).close()
    with pytest.raises(ValueError):
        make_job("heise", {"magazine": "../x"})


SAMPLES = {
    "url": "https://feeds.example.org/show.xml",
    "name": "Example",
    "host": "mail.example.org",
    "folder": "INBOX",
    "sender": "shop.example",
    "repository": "https://github.com/sharkdp/fd",
    "files": r"linux-amd64\.zip$",
}


def accepted(job: dict) -> dict:
    """The options a template's job is checked by, as the app checks it: an on-demand job's
    without an address of its own with a stand-in (each fetch brings one)."""
    return ondemand.checked_options(job["source"], job["options"]) if not job["schedule"] else job["options"]


@pytest.mark.parametrize("preset_id", sorted(PRESETS))
def test_every_template_makes_a_job_its_source_accepts(preset_id):
    from harvester.sources import SOURCES  # pylint: disable=import-outside-toplevel

    preset = PRESETS[preset_id]
    job = make_job(preset_id, {key: value for key, value in SAMPLES.items() if key in preset.get("parameters", {})})
    assert job["source"] in SOURCES and (preset.get("source") or job["source"]) == job["source"]
    SOURCES[job["source"]](accepted(job), {}, None).close()
    for parameter in preset.get("parameters", {}).values():
        assert parameter["title"] and "description" in parameter


ADDRESSES = sorted(
    (preset_id, name)
    for preset_id, preset in PRESETS.items()
    for name, parameter in preset.get("parameters", {}).items()
    if parameter.get("check", "").startswith("^https?://")
)


@pytest.mark.parametrize(("preset_id", "name"), ADDRESSES)
def test_a_templates_address_may_have_any_letter_but_no_space(preset_id, name):
    # The check once refused every address with an "s" after the scheme (a backslash too many).
    assert make_job(preset_id, {name: "https://www.sounds.example/shows/s"})
    with pytest.raises(ValueError):
        make_job(preset_id, {name: "https://example.org/a b"})


# Each suggestion of a template's parameter: (template, parameter, value).
SUGGESTIONS = sorted(
    (preset_id, name, suggestion["value"] if isinstance(suggestion, dict) else suggestion)
    for preset_id, preset in PRESETS.items()
    for name, parameter in preset.get("parameters", {}).items()
    for suggestion in parameter.get("suggestions", [])
)


@pytest.mark.parametrize(("preset_id", "name", "value"), SUGGESTIONS)
def test_a_templates_suggestions_pass_its_check_and_make_a_job(preset_id, name, value):
    from harvester.sources import SOURCES  # pylint: disable=import-outside-toplevel

    parameter = PRESETS[preset_id]["parameters"][name]
    assert re.fullmatch(parameter.get("check", ".*"), value), f"{preset_id}: {name} suggests {value}"
    given = {key: sample for key, sample in SAMPLES.items() if key in PRESETS[preset_id]["parameters"]}
    job = make_job(preset_id, given | {name: value})
    SOURCES[job["source"]](accepted(job), {}, None).close()


def test_a_templates_suggestions_are_several_and_labelled_ones_have_a_label():
    # One value alone is what the person complained of: a list that offers nothing to choose.
    for preset_id, preset in PRESETS.items():
        for name, parameter in preset.get("parameters", {}).items():
            suggestions = parameter.get("suggestions", [])
            assert len(suggestions) != 1, f"{preset_id}: {name} suggests one value"
            for suggestion in suggestions:
                assert not isinstance(suggestion, dict) or (suggestion["value"] and suggestion["label"])


def test_the_schedules_templates_suggest_are_valid_cron():
    from harvester import schedule  # pylint: disable=import-outside-toplevel

    for preset_id, name, value in SUGGESTIONS:
        if name == "schedule":
            assert schedule.describe(value), f"{preset_id}: {value}"


def test_media_templates_choose_their_quality():
    video = make_job("video-channel", {"url": SAMPLES["url"], "quality": "bv*[height<=720]+ba/b[height<=720]"})
    assert video["options"]["format"] == "bv*[height<=720]+ba/b[height<=720]"
    assert make_job("channel-audio", {"url": SAMPLES["url"]})["options"]["format"] == "ba[ext=m4a]/ba"
    with pytest.raises(ValueError):
        make_job("video-channel", {"url": SAMPLES["url"], "quality": "${name}"})


def test_options_offer_several_values_that_they_accept():
    from harvester.runner import COMMON_OPTIONS  # pylint: disable=import-outside-toplevel
    from harvester.sources import SOURCES  # pylint: disable=import-outside-toplevel

    for source in SOURCES.values():
        for key, option in (source.options | COMMON_OPTIONS).items():
            assert len(option.choices) != 1 and len(option.suggestions) != 1, f"{source.name}: {key}"
            assert option.default in option.choices or not option.choices
            for value in option.suggestions:
                if isinstance(option.default, int):
                    number = int(value)
                    assert option.minimum is None or number >= option.minimum, f"{source.name}: {key} {value}"
                    assert option.maximum is None or number <= option.maximum, f"{source.name}: {key} {value}"
                if option.kind == "regex":
                    re.compile(value)
                if option.kind == "template":
                    assert render_path(value, dict.fromkeys(option.values, 1)), f"{source.name}: {key} {value}"
