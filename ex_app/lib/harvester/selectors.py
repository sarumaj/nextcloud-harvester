"""Selectors with alternatives: how a replay finds a recorded element again when the site changed.

The recorder (recorder.js) finds an element by several selectors, each matching it alone when it
was recorded, ranked as Playwright's codegen ranks them: a test id (data-testid, data-test,
data-qa, …), an id that does not look generated, its role and accessible name, its aria-label,
its name attribute, its stable classes, its text, and its place among its siblings last. A step
keeps the best as "selector" and the others as "alternatives"; a pick (a level's links, the file
links, the next page) its selector and, where there are any, alternatives matching the same
links. A replay tries the best, then the alternatives (resolve), and says when an alternative
stood in: "step 3: selector repaired (#old → [data-testid="new"])". The recipe stays as it is.
Steps and picks without alternatives are found by their selector alone, as always.

The selectors are Playwright's: CSS, and two of its own engines the recorder writes,
internal:role=<role>[name="…"s] (get_by_role's, the exact name) and <tag>:text-is("…"), either
of them maybe after a scope (<css> >> …). Pages read over HTTP (BeautifulSoup) understand the
same forms for links and buttons (soup_select).
"""

import json
import re
import time
from collections.abc import Callable
from typing import Any

import soupsieve
from bs4 import Tag
from playwright.sync_api import Error as PlaywrightError

# How long the best selector has to find its element before an alternative may stand in (ms):
# a page still building finds neither, and the best should win once it is there.
GRACE = 2_000
# How often a replay looks again for the element (ms).
POLL = 250
# What a selector a page cannot read raises: in the browser, and over HTTP (an engine of the
# browser's soup_select does not know, or CSS soupsieve does not).
SELECTOR_ERRORS = (PlaywrightError, ValueError, soupsieve.SelectorSyntaxError)

ROLE = re.compile(r'^internal:role=(\w+)\[name=("(?:[^"\\]|\\.)*")s\]$')
TEXT = re.compile(r'^([a-zA-Z][\w-]*):text-is\(("(?:[^"\\]|\\.)*")\)$')
# The elements of a role over HTTP: those a link or a button can be (the walk follows links,
# a next page's button needs the browser in any case).
SOUP_ROLES = {
    "link": "a[href], [role=link]",
    "button": "button, input[type=submit], input[type=button], [role=button]",
}


def candidates(spec: dict) -> list[str]:
    """The selectors of a step or a pick, in their order: its own, then its alternatives (each
    once, only non-empty strings)."""
    found = [spec.get("selector")] + list(spec.get("alternatives") or [])
    return list(dict.fromkeys(value for value in found if isinstance(value, str) and value.strip()))


def repair_line(what: str, old: str, new: str) -> str:
    """The progress log's line of an alternative that stood in for a selector."""
    return f"{what}: selector repaired ({old} → {new})"


def _count(target: Any, selector: str) -> int:
    try:
        return target.locator(selector).count()
    except PlaywrightError:
        return 0  # a selector this page cannot read finds nothing here


def resolve(
    target: Any, step: dict, timeout: float = 30_000, repaired: Callable[[str, str], None] | None = None
) -> str:
    """The selector a step's element is found by in target (a page or a frame, Playwright's sync
    API or trial.Blocking's): its own as soon as it finds anything; after GRACE without, the
    first alternative that finds exactly one element (told to repaired, old and new); after
    timeout (ms) without either, its own, for the step to fail as it always did. A step without
    alternatives gets its own at once."""
    tries = candidates(step)
    if len(tries) < 2:
        return str(step.get("selector") or "")
    started = time.monotonic()
    grace = min(GRACE, timeout / 2)
    while True:
        if _count(target, tries[0]):
            return tries[0]
        waited = (time.monotonic() - started) * 1000
        if waited >= grace:
            for alternative in tries[1:]:
                if _count(target, alternative) == 1:
                    if repaired is not None:
                        repaired(tries[0], alternative)
                    return alternative
        if waited >= timeout:
            return tries[0]
        target.wait_for_timeout(min(POLL, max(1.0, timeout - waited)))


def _normal(text: str) -> str:
    return " ".join((text or "").split())


def _name(tag: Tag) -> str:
    """A link's or a button's accessible name as far as HTML tells it: its aria-label, else its
    text (a button input's value)."""
    label = _normal(str(tag.get("aria-label") or ""))
    if label:
        return label
    if tag.name == "input":
        return _normal(str(tag.get("value") or ""))
    return _normal(tag.get_text())


def _own_text(tag: Tag, text: str) -> bool:
    """Whether the element's text is the text, and no element inside it has it all (Playwright's
    text-is: the smallest element)."""
    return _normal(tag.get_text()) == text and not any(
        _normal(child.get_text()) == text for child in tag.find_all(True, recursive=False)
    )


def _select_one(root: Any, part: str) -> list[Tag]:
    role = ROLE.match(part)
    if role:
        if role[1] not in SOUP_ROLES:
            raise ValueError(f"{part}: only links and buttons are found by their role without the browser")
        name = json.loads(role[2])
        return [tag for tag in root.select(SOUP_ROLES[role[1]]) if _name(tag) == name]
    text = TEXT.match(part)
    if text:
        wanted = json.loads(text[2])
        return [tag for tag in root.select(text[1]) if _own_text(tag, wanted)]
    if part.startswith("internal:") or "=" in part.split("[", 1)[0]:
        raise ValueError(f"{part}: a selector of the browser alone")
    return list(root.select(part))


def soup_select(root: Any, selector: str) -> list[Tag]:
    """The elements of a page read over HTTP (BeautifulSoup) that the selector finds: CSS, and the
    recorder's role and text selectors, maybe after a scope (" >> "); ValueError for another of
    the browser's engines, soupsieve's error for CSS it cannot read."""
    if " >> " not in selector and not selector.startswith("internal:") and ":text-is(" not in selector:
        return list(root.select(selector))
    roots = [root]
    for part in selector.split(" >> "):
        found: dict[int, Tag] = {}
        for base in roots:
            for tag in _select_one(base, part.strip()):
                found.setdefault(id(tag), tag)
        roots = list(found.values())
    return roots
