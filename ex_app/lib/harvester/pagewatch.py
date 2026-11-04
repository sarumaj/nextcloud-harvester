# SPDX-License-Identifier: AGPL-3.0-or-later
"""Watching a page for changes of its text: a website recipe's "watch" in place of levels and file
links saves no file, but compares the text of its start page (where its steps end), or of the
elements of a selector, with what the job's last run found there:

    {"start": "https://example.org/prices", "watch": {}}
    {"steps": [...], "watch": {"selector": "main .price-list"}}

The text is the page's as a person reads it (no scripts, styles or hidden templates), a line per
block, its whitespace collapsed: a change of whitespace alone (indentation, line breaks, spaces)
is no change. The job keeps the text of its last run (the store's texts, at most MAX_TEXT
characters); its first run only keeps it. A change is a line in the progress log and a
notification to the job's person, with an excerpt of what changed (the lines gone, "-", and
come, "+", at most EXCERPT characters), and the run's outcome says so. A selector that finds
nothing fails the run; where it found the text before, the site changed (watch.Watch: told once,
as a listing that finds nothing is).
"""

import difflib
from typing import Any

from bs4 import BeautifulSoup

from . import patterns, selectors
from .l10n import said
from .sources.base import SourceError

# The most of a page's text kept and compared (characters), and of a change's excerpt.
MAX_TEXT = 200_000
EXCERPT = 300
# The excerpt in the run's outcome (the jobs' list): one line of at most this many characters.
NOTE_EXCERPT = 100
# What a person does not read on a page.
UNREAD = ("script", "style", "noscript", "template")


def recipe_problems(recipe: dict) -> list[str]:
    """What is wrong with a recipe's watch: an object, with a selector (a non-empty string) or
    none, and no levels, file links or page to save beside it."""
    watch = recipe["watch"]
    if not isinstance(watch, dict) or set(watch) - {"selector"}:
        return ['watch is an object, {} or {"selector": "…"}']
    if "selector" in watch and not (isinstance(watch["selector"], str) and watch["selector"].strip()):
        return ["watch: its selector is a CSS selector"]
    if recipe.get("links") or recipe.get("follow") or "save" in recipe:
        return ["the recipe watches its start page (watch) or has levels and file links, not both"]
    return []


def text_of(html: str, selector: str = "") -> str | None:
    """The text a person reads on the page (or in the selector's elements), a line per block,
    whitespace collapsed; None when the selector finds nothing."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(UNREAD):
        tag.decompose()
    roots = selectors.soup_select(soup, selector) if selector else [soup.body or soup]
    if not roots:
        return None
    lines = [" ".join(line.split()) for root in roots for line in root.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_TEXT]


def same(before: str, after: str) -> bool:
    """Whether two texts differ in whitespace at most."""
    return before.split() == after.split()


def excerpt(before: str, after: str) -> str:
    """What changed from before to after: the lines gone (-) and come (+), at most EXCERPT
    characters."""
    lines = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm="")
        if not line.startswith(("---", "+++", "@@"))
    ]
    text = "\n".join(lines) or "(the text moved between lines)"
    return text if len(text) <= EXCERPT else text[: EXCERPT - 1] + "…"


def shortened(change: str) -> str:
    """The change's excerpt on one line, at most NOTE_EXCERPT characters: the run's outcome."""
    text = " ".join(change.split())
    return text if len(text) <= NOTE_EXCERPT else text[: NOTE_EXCERPT - 1] + "…"


def page_html(page: Any) -> str:
    """A page's HTML: one read over HTTP (its soup), or the browser's."""
    soup = getattr(page, "soup", None)
    return str(soup) if soup is not None else page.content()


def look(walk: Any) -> None:
    """The recipe's watched page (its start, or where its steps end) read and compared with the
    job's last run (the source's contents; none in the recipe's test, which compares nothing):
    the change logged and told, the run's outcome (the source's note) saying what it found."""
    site, driver = walk.site, walk.driver
    selector = str(site.recipe["watch"].get("selector") or "")
    page = walk.paced(driver.open, site.recipe.get("start") or None)
    try:
        url = driver.address(page)
        text = text_of(page_html(page), selector)
    finally:
        driver.done(page)
    event = {"level": 0, "page": 1, "url": url, "selector": selector or "(the page's text)", "kind": "links"}
    site.observe(event | {"found": int(text is not None), "matching": int(text is not None)})
    if text is None:
        raise SourceError(f"{url}: the watched {selector} is not on the page")
    contents = site.contents
    if contents is None:
        site.report("info", said("The watched text: {count} characters", count=len(text)))
        return
    key = f"{patterns.stable_url(str(site.recipe.get('start') or url))}#{selector}#{walk.choice}"
    before = contents.text(key)
    contents.keep_text(key, text)
    if before is None:
        site.report(
            "info", said("Watching {address}: its text is kept, a change is told from the next run", address=url)
        )
        site.note = said("The page's text is kept to compare with")
    elif same(before, text):
        site.note = said("The page is as before")
    else:
        change = excerpt(before, text)
        site.report("warning", said("The page changed: {excerpt}", excerpt=change))
        contents.tell(url, change)
        site.note = said("The page changed: {excerpt}", excerpt=shortened(change))
