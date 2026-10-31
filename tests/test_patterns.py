"""Pattern proposals, values and templates, and the job templates (presets)."""

# pylint: disable=missing-function-docstring

import json
import re

import pytest

from harvester.patterns import group_values, propose, render_path, render_tags
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
