# SPDX-License-Identifier: AGPL-3.0-or-later
"""The README and the guides in docs/ keep up with the app: every source and template is described
in the user's guide, the README links each guide, lines wrap at 100.

CI's markdownlint job holds the same limit (.markdownlint.yaml); this test runs with the others.
"""

# pylint: disable=missing-function-docstring

import pathlib
import re

import pytest

from harvester.presets import PRESETS
from harvester.sources import SOURCES

ROOT = pathlib.Path(__file__).parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
GUIDES = sorted((ROOT / "docs").glob("*.md"))
USER = (ROOT / "docs" / "user.md").read_text(encoding="utf-8")


def test_every_source_and_template_has_its_entry():
    # A list entry starts with its names: "- `podcast`:" or "- `video-channel` and `channel-audio`:".
    heads = re.findall(r"^  - ((?:`[a-z-]+`(?: and )?)+)", USER, re.MULTILINE)
    entries = {name for head in heads for name in re.findall(r"`([a-z-]+)`", head)}
    assert not set(SOURCES) - entries, "sources missing from docs/user.md"
    assert not set(PRESETS) - entries, "templates missing from docs/user.md"


def test_the_readme_links_each_guide():
    assert {path.name for path in GUIDES} >= {"user.md", "admin.md", "developer.md"}
    for path in GUIDES:
        assert f"](docs/{path.name})" in README, f"README.md does not link docs/{path.name}"


@pytest.mark.parametrize("path", [ROOT / "README.md", *GUIDES], ids=lambda path: path.name)
def test_lines_wrap_at_100_characters_outside_code_and_tables(path):
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and not line.startswith("|"):
            assert len(line) <= 100, f"{path.name}:{number} has {len(line)} characters"
