"""The README keeps up with the app: every source and template is described, lines wrap at 100.

CI's image has no npm for markdownlint; .markdownlint.yaml holds the same limit for editors.
"""

# pylint: disable=missing-function-docstring

import pathlib
import re

from harvester.presets import PRESETS
from harvester.sources import SOURCES

README = (pathlib.Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")


def test_every_source_and_template_has_its_entry():
    # A list entry starts with its names: "- `podcast`:" or "- `video-channel` and `channel-audio`:".
    heads = re.findall(r"^  - ((?:`[a-z-]+`(?: and )?)+)", README, re.MULTILINE)
    entries = {name for head in heads for name in re.findall(r"`([a-z-]+)`", head)}
    assert not set(SOURCES) - entries, "sources missing from the README"
    assert not set(PRESETS) - entries, "templates missing from the README"


def test_lines_wrap_at_100_characters_outside_code_and_tables():
    fenced = False
    for number, line in enumerate(README.splitlines(), 1):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and not line.startswith("|"):
            assert len(line) <= 100, f"README.md:{number} has {len(line)} characters"
