# SPDX-License-Identifier: AGPL-3.0-or-later
"""Warnings that "the site changed": what each run of a website job found, level by level, and a
warning when a level that found links in the job's last good runs finds none now (or its links no
longer match its pattern). A site that is redesigned breaks a recipe quietly: its runs go on, find
nothing new and say "0 downloaded", which looks like a site without news.

A job may also ask for the recipe's test once a week (Site.survey, nothing downloaded), for a site
whose runs are rare or end early at known files: the same summary, judged the same way."""

import datetime
import random

from croniter import croniter

from .l10n import Said, said, said_n
from .schedule import zone
from .sources import SOURCES, Option
from .store import Job

# The summaries a job keeps: of its good runs (the baseline a run is judged by), and as many of
# the others (the changed ones, to look at).
KEPT = 5

# Options of a website job beside its source's own (its recipe), in its form.
WATCH_OPTIONS = {
    "weekly_test": Option(
        "Test the recipe weekly",
        0,
        "Once a week (on Sundays, at an hour of the job's own) the recipe is walked as Test the recipe does, "
        "nothing downloaded: you are told when a level finds nothing it found before, as a run tells it.",
        kind="flag",
    ),
}


def options_of(source: str) -> dict[str, Option]:
    """The options the watch adds to the source's (a website's only: its recipe has levels)."""
    return WATCH_OPTIONS if source == "site" else {}


class Watch:
    """Collects what each listing page of a run found (the site's observe), level by level: how
    many pages, links its selector found and links that matched its pattern; and asks the walk
    for a picture of the first page that found less than the runs before (whether the level as a
    whole changed is known only at the run's end, so the picture is kept only then).

    before are the summaries of the job's last good runs, newest first."""

    def __init__(self, before: list[dict]):
        self.before = before
        self.levels: dict[int, dict] = {}
        self.pictured = False

    def __call__(self, event: dict) -> str:
        """Takes a listing page's event; returns what went wrong there when the walk should keep
        a picture of the page ("" else)."""
        level = self.levels.setdefault(
            event["level"],
            {"kind": event["kind"], "selector": event["selector"], "pages": 0, "found": 0, "matching": 0, "url": ""},
        )
        level["pages"] += 1
        level["found"] += event["found"]
        level["matching"] += event["matching"]
        # The first page without a match: where a changed level shows it.
        if not event["matching"] and not level["url"]:
            level["url"] = event["url"]
        if self.pictured:
            return ""
        reason = self._judge(event["level"], event)
        self.pictured = bool(reason)
        return reason

    def summary(self) -> dict:
        """The run's summary as the job keeps it (JSON): by level, its kind, selector, pages read,
        links found and matched, and the first page without a match."""
        return {"levels": {str(number): level for number, level in sorted(self.levels.items())}}

    def change(self) -> dict | None:
        """The first level (from the listing down) that found less than the job's last good runs:
        {level, reason (for the notification), detail (for the progress log), url}; None when
        nothing changed, or there is nothing to compare with."""
        for number, level in sorted(self.levels.items()):
            reason = self._judge(number, level)
            if reason:
                values = {"reason": reason, "selector": level["selector"], "address": level["url"]}
                if level["kind"] == "links":
                    detail = said_n(
                        "{reason}; file links {selector}, on %n page, first at {address}",
                        "{reason}; file links {selector}, on %n pages, first at {address}",
                        level["pages"],
                        **values,
                    )
                else:
                    detail = said_n(
                        "{reason}; links to follow {selector}, on %n page, first at {address}",
                        "{reason}; links to follow {selector}, on %n pages, first at {address}",
                        level["pages"],
                        **values,
                    )
                return {"level": number, "reason": reason, "detail": detail, "url": level["url"]}
        return None

    def _judge(self, number: int, now: dict) -> Said | str:
        """What a level (or one page of it) found less than the job's last good runs that read
        it with the same selector: nothing where they found links, or no match where links
        matched ("" when not). A level that never found anything is no change: some listings
        start empty."""
        earlier = [
            summary["levels"][str(number)]
            for summary in self.before
            if summary.get("levels", {}).get(str(number), {}).get("selector") == now["selector"]
        ]
        found = next((level["found"] for level in earlier if level["found"]), 0)
        matching = next((level["matching"] for level in earlier if level["matching"]), 0)
        if not now["found"] and found:
            if number == 0:
                return said("the listing found nothing (it found {found} before)", found=found)
            return said("level {level} found nothing (it found {found} before)", level=number, found=found)
        if now["found"] and not now["matching"] and matching:
            values = {"found": now["found"], "matched": matching}
            if number == 0:
                return said(
                    "the listing: the pattern no longer matches (none of {found} links; {matched} matched before)",
                    **values,
                )
            return said(
                "level {level}: the pattern no longer matches (none of {found} links; {matched} matched before)",
                level=number,
                **values,
            )
        return ""


def weekly(job_id: int) -> str:
    """The weekly test's schedule (cron): Sundays, at an hour and minute of the job's own, so
    that the tests of many jobs do not come at once (the same each week: the job's id chooses)."""
    chosen = random.Random(job_id)
    return f"{chosen.randrange(60)} {chosen.randrange(24)} * * 0"


def test_due(job: Job, tested: float | None, now: float) -> bool:
    """Whether the job's weekly test has come round since its last test (or since it was made):
    an enabled website job that asks for it and has an address of its own, not waiting for its
    person (a challenge)."""
    if job.id is None or not job.enabled or job.source != "site" or job.last_status == "challenge":
        return False
    if str(job.options.get("weekly_test", 0)) != "1" or not SOURCES[job.source].address_of(job.options):
        return False
    start = datetime.datetime.fromtimestamp(tested or job.created, zone(job.timezone))
    return croniter(weekly(job.id), start).get_next(float) <= now
