# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recipe's test (harvester.sources.site: Site.survey): the site's walk as a run walks it,
downloading nothing, and what it found: each listing page read with the links its selector and
pattern matched, the first file links with their file names and values, and the problems met. The recorder's
Test the recipe (harvester.browser.trial) and a job's weekly test (harvester.watch) show it.
"""

import contextlib
from collections.abc import Callable
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .. import patterns
from . import keep
from .base import SourceError, Stopped

# The file links a test lists, and the listing pages it opens at most.
SURVEY_FILES = 20
SURVEY_PAGES = 30
# The values a test shows per file link at most (besides the link's own, LINK_VALUES).
SURVEY_VALUES = 16
# The values every link gives (its address, text, file name, ...): no news in a test.
LINK_VALUES = frozenset(patterns.link_values("https://example.org/", "", 1))


def shown_values(values: dict) -> dict:
    """A file link's values a test shows: those of the pattern and of the pages (a page's title,
    its tags), not the link's own; a list as it is."""
    kept = {name: value for name, value in values.items() if name not in LINK_VALUES and not name.startswith("_")}
    return dict(list(kept.items())[:SURVEY_VALUES])


def survey(
    site: Any, files: int = SURVEY_FILES, pages: int = SURVEY_PAGES, begin: Callable[[], None] = lambda: None
) -> dict:
    """The test of the site source's recipe: walks the site (after begin: the steps) and tells
    what it found. It ends after that many files or pages, or when stopped (Stopped)."""
    result: dict = {"pages": [], "problems": [], "files": 0, "ended": "complete", "shot": None, "skipped": []}
    # An observer set before (a job's weekly test) hears of the pages too.
    outer = site.observe

    def observe(event: dict) -> str | None:
        if len(result["pages"]) >= pages:
            raise Stopped("pages")
        where = "the listing" if event["level"] == 0 else f"a level {event['level']} page"
        where += f" (page {event['page']}, {event['url']})"
        if not event["found"]:
            result["problems"].append(f"{where}: {event['selector']} matched nothing")
        elif not event["matching"]:
            result["problems"].append(f"{where}: none of its {event['found']} links matched the pattern")
        result["pages"].append(event | {"files": []})
        return outer(event)

    site.observe = observe
    site.page_pause = 0.0
    site.listing_only = True
    # A page to save, or to capture files of, is listed: the test opens none.
    kept = {"save": site.saving, "capture": (site.recipe.get("links") or {}).get("capture") or []}
    items = site.items(lambda key: False)
    try:
        begin()
        for item in items:
            problem = str(item.problem) if item.problem else ""
            # A file link that is clicked is told by its text (its address is the listing's).
            entry = {"url": item.url, "path": item.path, "problem": problem, "text": item.values.get("text", "")}
            entry["values"] = shown_values(item.values)
            entry |= kept | {"click": item.trigger is not None and not isinstance(item.trigger, keep.Opened)}
            result["pages"][-1]["files"].append(entry)
            result["files"] += 1
            if result["files"] >= files:
                result["ended"] = "files"
                break
    except Stopped as stop:
        # "pages", "time" (the test's limits) or "stopped" (by the person).
        result["ended"] = str(stop) if str(stop) in ("pages", "time") else "stopped"
    except (SourceError, PlaywrightError) as error:
        result["ended"] = "failed"
        # Its kind: a page that asks for a person, or a refused sign-in, is no changed site.
        result["error"] = type(error).__name__
        result["problems"].append((str(error) or type(error).__name__).splitlines()[0])
    finally:
        # The walk's pages close; a test stopped meanwhile cannot close them (its browser
        # context goes as a whole).
        with contextlib.suppress(Exception):
            items.close()
        site.observe = outer
    result["shot"] = site.shot
    driver = site.driver
    # The optional steps it went on without: listed apart, no problems of the recipe.
    result["skipped"] = list(driver.skipped if driver else [])
    # The selectors alternatives stood in for (the recipe unchanged).
    result["repaired"] = [*(driver.repaired if driver else []), *site.repairs]
    # The choices it went through (a step "each"), by their texts.
    result["choices"] = list(site.choices)
    return result
