# SPDX-License-Identifier: AGPL-3.0-or-later
"""The walk of a website's listing (harvester.sources.site): from the start page through the
recipe's levels of links to the file links, page after page, and the files not known yet.

Known files end it once the site went through completely (stop_at_seen); a link whose values
disagree with the levels above it is another listing's; a next page read already ends its
listing (a loop); a page that should list something and shows a sign-in, an error or another
page is a gap, which keeps the run from counting as a complete pass. The file links may be a
JSON listing instead of a page's (near.json_pages), and a level's links may carry values read
from the page around them (patterns.page_values).

A level that is clicked ("click": true) has elements without an address (a button, a row, a
card): each is told apart by its text (the level's pattern is searched in it) and its place,
known by the listing's address and its text, and its next level is read where its click lands
(clicks.land), after which the walk returns to the listing (Driver.done: clicks.back).

A recipe that saves its start page ("save" of its own) yields that page alone, open (keep.Opened).
File links that capture what their pages load ("capture") yield the files of each page instead of
the page (keep.capture), a page whose files were all placed marked as done (keep.CAPTURED); the
recipe's test (listing_only) lists the pages without opening them.

A walk of one choice (harvester.choices) starts with its values ({choice}), its listing's address
filled in with them; its files are keyed by the choice too, and known only by their keys.
"""

import dataclasses
from collections.abc import Callable, Generator, Iterator
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .. import choices, jsonlist, pagewatch, patterns, selectors
from .. import recipe as recipes
from ..l10n import said, said_n
from . import keep, near
from .base import ChallengeError, Item, SourceError, pattern_search
from .clicks import Target

# The seen key a run records once it has gone through the whole site (not stopped by
# max_items): from then on, a listing of known files ends the run (stop_at_seen).
FULL_PASS = "harvester:full-pass"
# The pages of file links a run names in the log one by one (what they matched); the rest are
# counted together.
PAGES_TOLD = 20


class StartEmpty(SourceError):
    """The start page has none of the recipe's links: signed out, or the site changed."""


class Walk:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """One walk of the site's listing with its driver, yielding the items seen() does not know."""

    def __init__(self, site: Any, driver: Any, seen: Callable[[str], bool], values: dict | None = None):
        self.site = site
        # The values the walk starts with: a choice's (choose.each), whose files are its own.
        self.values = values or {}
        self.choice = str(self.values.get("choice", ""))
        self.driver = driver
        self.seen = seen
        self.recipe: dict = site.recipe
        self.follow: list[dict] = self.recipe.get("follow") or []
        self.after: dict = self.recipe.get("next") or {}
        self.key_template = str((self.recipe.get("links") or {}).get("key") or "")
        # The types of the files the file links' pages load that are kept (keep.capture).
        self.capture = bool((self.recipe.get("links") or {}).get("capture")) and not site.listing_only
        self.stop_at_seen = bool(int(site.settings["stop_at_seen"]) and seen(FULL_PASS))
        # The links taken so far (by their stable address), and the listing pages read per level
        # (their address and links: a next page among them is a loop).
        self.visited: set[str] = set()
        self.listed: dict[int, set] = {}
        self.opened = 0
        # What the level's links on the page read last were: found, and matching (matching()).
        self.counted = (0, 0)

    def items(self) -> Iterator[Item]:
        """The walk from the recipe's start (None: the page its steps ended on); a recipe that
        saves its start page, that page."""
        if self.recipe.get("save"):
            yield from self.start_page()
            return
        if "watch" in self.recipe:
            pagewatch.look(self)
            return
        start = self.recipe.get("start") or None
        if start and self.choice:
            start = choices.uses_choice(start, self.choice, str(self.values.get("choice_value", "")))
        yield from self.listing(start, 0, dict(self.values), {"found": False, "new": False})

    def start_page(self) -> Iterator[Item]:
        """The start page to save, as the item of its address, open while it is saved."""
        page = self.paced(self.driver.open, self.recipe.get("start") or None)
        try:
            url = self.driver.address(page)
            event = {"level": 0, "page": 1, "url": url, "selector": "(the page itself)", "kind": "links"}
            self.site.observe(event | {"found": 1, "matching": 1})
            made = dataclasses.replace(self.site.item(url, "", 1, {}), trigger=keep.Opened(page))
            if not self.known(made):
                yield made
        finally:
            self.driver.done(page)

    def captured(self, entry: Item) -> Iterator[Item]:
        """The files of the wanted types the page of a file link loads (keep.capture), those not
        known; the page is marked as done once each of them was placed (or skipped)."""
        if entry.problem is not None:
            yield entry
            return
        found, title, url = self.driver.capture(entry.trigger or entry.url, self.site.settings, self.recipe["links"])
        if not found:
            self.site.report("info", said("{url}: none of the files it loads is of the types to capture", url=url))
            return
        made = [keep.captured_item(entry, file, title, url, str(self.site.settings["path"])) for file in found]
        for item in made:
            if not self.seen(item.key):
                yield item
        # Back here once the run took each of them (a run that ends before does not come back).
        if all(self.seen(item.key) for item in made):
            self.site.remember(entry.key)

    def listing(self, url: str | None, level: int, values: dict, outcome: dict) -> Iterator[Item]:
        """The listing at the level: its pages, or the JSON listing that holds the file links."""
        if level == len(self.follow) and self.recipe["links"].get("kind") == "json":
            return self.json_files(url.address if isinstance(url, Target) else url, level, values, outcome)
        return self.walk(url, level, values, outcome)

    def json_files(self, url: str | None, level: int, values: dict, outcome: dict) -> Iterator[Item]:
        """A JSON listing's pages, read one after another while its files go on (url: the page it
        is read from, None: where the steps ended)."""
        pages = near.json_pages(self.recipe["links"], self.site.settings, self.driver, values, url)
        for number, page in enumerate(pages, 1):
            if (yield from self.files(page, number, values, outcome, level)):
                return

    def known(self, item: Item) -> bool:
        """Whether the item is known; a key recorded before keys were made stable (or before the
        recipe had a key template) still counts: the link as it was. A page to capture files of
        is known once it is done."""
        if item.key.startswith(keep.CAPTURED) or self.choice:
            return self.seen(item.key)
        link = item.values["url"]
        return self.seen(item.key) or self.seen(patterns.stable_url(link)) or self.seen(link)

    def paced(self, make: Callable[..., Any], *args: Any) -> Any:
        """Opens a listing page (make), after the site's pause unless it is the first."""
        if self.opened:
            self.site.sleep(self.site.page_pause)
        self.opened += 1
        return make(*args)

    def matching(self, page: Any, spec: dict, where: tuple[int, int], values: dict) -> list[tuple[str, str, dict]]:
        """The links of the level's spec on the page (where: its level and page number) that its
        pattern matches and that were not taken yet, with their groups; a clicked level's pattern
        is searched in its elements' texts, and each one's place among them kept (_click)."""
        clicked = bool(spec.get("click")) and spec.get("kind") != "json"
        links, event = self.candidates(page, spec, clicked, where[0])
        found = [entry for index, link in enumerate(links) if (entry := self.take(spec, link, values, clicked, index))]
        kind = "follow" if spec is not self.recipe["links"] else "links"
        event |= {"level": where[0], "page": where[1], "kind": kind} | ({"choice": self.choice} if self.choice else {})
        self.counted = (len(links), len(found))
        what = self.site.observe(event | {"found": len(links), "matching": len(found)})
        # A JSON listing has no page to picture.
        if what and not isinstance(page, jsonlist.JsonPage):
            self.site.notice(page, what)
        return found

    def candidates(self, page: Any, spec: dict, clicked: bool, level: int) -> tuple[list, dict]:
        """The level's links on the page (address, text, the values' texts where it has values;
        Site.listed: a selector's alternative if need be), and where they were read; a click
        link's address is the page's own, as it has none."""
        if clicked:
            address = self.driver.address(page)
            texts = self.driver.targets(page, spec["selector"])
            return [(address, text) for text in texts], {"url": address, "selector": spec["selector"]}
        what = said("file links") if spec is self.recipe["links"] else said("level {level}", level=level + 1)
        return self.site.listed(self.driver, page, spec, what)

    def take(self, spec: dict, link: tuple, values: dict, clicked: bool, index: int) -> tuple | None:
        """A link (address, text, the values' texts) as the level takes it, with its values
        (a click link's place among the elements as _click); None when its pattern does not
        match, its values belong to another listing, or it was taken already."""
        url, text, *texts = link
        groups = self.groups(spec, text if clicked else url, values)
        taken = f"{patterns.stable_url(url)}#{text}" if clicked else patterns.stable_url(url)
        if groups is None or taken in self.visited:
            return None
        self.visited.add(taken)
        if spec.get("values"):
            # A date's year, month and day where the link's pattern gives none.
            groups |= patterns.page_values(spec["values"], texts[0], pattern_search, groups)
        return url, text, groups | ({"_click": index} if clicked else {})

    @staticmethod
    def groups(spec: dict, text: str, values: dict) -> dict | None:
        """The values the level's pattern takes from a link (its address, or a click link's
        text), or None when it does not match, or when they disagree with the levels' above it
        (another year's issue shown on every year's page): that link is the other listing's,
        taken there with its values."""
        # A person's pattern, within its time limit (a slow one fails the run).
        match = pattern_search(spec.get("pattern") or "", text) if text else None
        if not match:
            return None
        groups = patterns.group_values(match)
        if any(name in values and values[name] != value for name, value in groups.items()):
            return None
        return groups

    def blocked(self, page: Any, level: int, number: int) -> None:
        """A page without the links it should have may be asking for a person instead; the start
        page without any is a signed-out session or a changed site, not an empty listing. A
        deeper page that shows a sign-in, an error or another page is a gap of the pass."""
        json = isinstance(page, jsonlist.JsonPage)
        found = None if json else self.driver.challenge(page)
        if found:
            raise ChallengeError(*found)
        if level == 0 and number == 1:
            raise StartEmpty(said("the start page has none of the recipe's links: signed out, or the site changed?"))
        why = "" if json else self.driver.trouble(page)
        if why:
            self.site.gaps += 1
            values = {"number": number, "address": self.driver.address(page), "why": str(why)}
            if level == 0:
                line = said("the listing (page {number}, {address}) gave nothing: {why}", **values)
            else:
                line = said(
                    "a level {level} page (page {number}, {address}) gave nothing: {why}", level=level, **values
                )
            self.site.report("info", line)

    def item_of(self, link: str, text: str, number: int, values: dict, page: Any) -> Item:
        """The file of a link (Site.item) with its listing as Referer; a click link's with what
        clicks it; keyed by the recipe's key template if it has one."""
        index = values.pop("_click", None)
        referer = page.url if isinstance(page, jsonlist.JsonPage) else self.driver.address(page)
        made = self.site.item(link, text, number, values, referer)
        if index is not None:
            trigger = (page, self.recipe["links"]["selector"], index, text)
            made = dataclasses.replace(made, trigger=trigger, key=f"{patterns.stable_url(link)}#{text}")
        if self.key_template:
            try:
                made = dataclasses.replace(made, key="key:" + patterns.render(self.key_template, made.values))
            except (KeyError, ValueError, IndexError):
                pass  # a value the link lacks: its address tells it, as without a template
        if self.choice:
            # Each choice's files are its own (an account's statement may have another's address).
            made = dataclasses.replace(made, key=f"choice:{self.choice}#{made.key}")
        # A page to capture files of is no file: its key marks it as done (captured).
        return dataclasses.replace(made, key=keep.CAPTURED + made.key) if self.capture else made

    def files(self, page: Any, number: int, values: dict, outcome: dict, level: int) -> Generator[Item, None, bool]:
        """The file links of a listing's page; True when the listing ends there (no file links,
        or only known ones while known files end the run)."""
        found = self.matching(page, self.recipe["links"], (level, number), values)
        made = [self.item_of(link, text, number, values | groups, page) for link, text, groups in found]
        self.site.ahead([entry.key for entry in made])
        new = [entry for entry in made if not self.known(entry)]
        self.page_line(page, number, len(new))
        if not found:
            self.blocked(page, level, number)
        outcome["found"] |= bool(found)
        outcome["new"] |= bool(new)
        for entry in new:
            yield from self.captured(entry) if self.capture else (entry,)
        if found and self.stop_at_seen and not new:
            self.site.report(
                "info",
                said("page {number}: only known files: the listing ends here (Stop at known files)", number=number),
            )
            return True
        return not found

    def page_line(self, page: Any, number: int, new: int) -> None:
        """The log's line of a page of file links: how many of its links match and how many of
        those are new; PAGES_TOLD a run, then one line that the rest are not named."""
        told = self.site.told.get("pages", 0)
        self.site.told["pages"] = told + 1
        if told > PAGES_TOLD:
            return
        if told == PAGES_TOLD:
            self.site.report("info", said("Further listing pages are not named one by one"))
            return
        found, matching = self.counted
        address = page.url if isinstance(page, jsonlist.JsonPage) else self.driver.address(page)
        values = {"number": number, "address": address, "matching": matching, "found": found, "new": new}
        self.site.report(
            "info", said("page {number} ({address}): {matching} of {found} links match, {new} new", **values)
        )

    def known_listing(self, link: str, level: int, values: dict, outcome: dict) -> Generator[Item, None, bool]:
        """A followed link's listing; True when it holds files, all known."""
        child = {"found": False, "new": False}
        yield from self.listing(link, level + 1, values, child)
        outcome["found"] |= child["found"]
        outcome["new"] |= child["new"]
        return child["found"] and not child["new"]

    def children(self, links: list, level: int, values: dict, outcome: dict) -> Generator[Item, None, bool]:
        """The links to follow of a page; True when the listing ends there. While known files end
        the run, the first known one ends it only if the page's last is known too (the newest
        come first); else the page lists the newest last, and its links are followed from the
        end back to a known one, and on to the next page."""
        for index, (link, _, groups) in enumerate(links):
            if not (yield from self.known_listing(link, level, values | groups, outcome)) or not self.stop_at_seen:
                continue
            rest = links[index + 1 :]
            if not rest:
                return True
            last, _, last_groups = rest[-1]
            if (yield from self.known_listing(last, level, values | last_groups, outcome)):
                return True
            for link_, _, groups_ in reversed(rest[:-1]):
                if (yield from self.known_listing(link_, level, values | groups_, outcome)):
                    break
            return False
        return False

    def repeated(self, page: Any, level: int, spec: dict) -> bool:
        """Whether the page was read already at its level (a next page that links back, a last
        page linking to itself): its listing ends there, said in the log."""
        seen_here = self.listed.setdefault(level, set())
        # By its links (with its address while it has none): a page of a script's buttons keeps
        # its address, and the first page may come back under another one (/list?p=1 for /list).
        links = tuple(map(tuple, self.driver.links(page, spec["selector"])))
        marks = {("links", links) if links else ("address", patterns.stable_url(self.driver.address(page)))}
        if marks & seen_here:
            self.site.report(
                "info",
                said(
                    "the next page {address} was read already (a loop): its listing ends",
                    address=self.driver.address(page),
                ),
            )
            return True
        seen_here |= marks
        return False

    def following(self, page: Any, level: int, number: int) -> Any:
        """The listing's next page after this one at the level, or None (no next page there)."""
        if not self.after.get("selector") or int(self.after.get("level", 0)) != level:
            return None
        awaits = self.awaits(level)
        if self.after.get("kind") == "number":
            return self.paced(self.driver.numbered, page, self.after["selector"], number, awaits)
        return self.paced(self.site.next_page, self.driver, page, self.after, awaits)

    def awaits(self, level: int) -> tuple[str, ...]:
        """The selectors a listing page of the level shows once it is there (its links and their
        alternatives, the element it waits for): the browser waits for them rather than for the
        network to go quiet (tabs.listing_shown)."""
        spec = self.follow[level] if level < len(self.follow) else self.recipe["links"]
        if spec.get("kind") == "json":
            return ()
        wait = spec.get("wait_for") if isinstance(spec.get("wait_for"), dict) else {}
        return tuple(selectors.candidates(spec)) + ((str(wait["selector"]),) if wait.get("selector") else ())

    def clicked(self, page: Any, spec: dict, level: int, link: tuple) -> tuple:
        """A link of a clicked level to follow as children() takes it: its element to click (a
        Target, with the next level's selector to wait for where it lands), text and values."""
        url, text, groups = link
        index = groups.pop("_click")
        after = self.follow[level + 1] if level + 1 < len(self.follow) else self.recipe["links"]
        return Target(page, spec["selector"], index, text, url, spec, str(after.get("selector") or "")), text, groups

    def once(self, what: str) -> bool:
        """Whether a line of the walk (what: its kind and level) is said for the first time in
        this run (a level's wait, its Pages limit): its listings after the first say it no more."""
        told = what not in self.site.told
        self.site.told[what] = 1
        return told

    def limited(self, level: int) -> None:
        """The Pages limit ended a listing of the level with a next page still there: said once a
        run."""
        if self.once(f"pages {level}"):
            pages = int(self.site.settings["max_pages"])
            self.site.report(
                "info",
                said_n(
                    "Read %n page (Pages): older pages are not read this run",
                    "Read %n pages (Pages): older pages are not read this run",
                    pages,
                ),
            )

    def open_listing(self, url: Any, level: int) -> Any:
        """The listing's page: its address opened, or what an element clicked shows (a Target);
        None when that click failed (a gap of the pass, said in the log)."""
        if not isinstance(url, Target):
            return self.paced(self.driver.open, url, level > 0, self.awaits(level))
        try:
            return self.paced(self.driver.open_clicked, url)
        except (SourceError, PlaywrightError) as error:
            if isinstance(error, ChallengeError):
                raise
            self.site.gaps += 1
            why = (str(error) or type(error).__name__).splitlines()[0]
            self.site.report(
                "info",
                said("level {level}: clicking “{element}” failed: {why}", level=level, element=url.text, why=why),
            )
            return None

    def walk(self, url: str | None, level: int, values: dict, outcome: dict) -> Iterator[Item]:
        """A listing at the level, page after page; outcome tells whether it found files and
        whether any was new."""
        spec = self.follow[level] if level < len(self.follow) else self.recipe["links"]
        page = self.open_listing(url, level)
        if page is None:
            return
        try:
            for number in range(1, int(self.site.settings["max_pages"]) + 1):
                if spec.get("wait_for") and number == 1 and self.once(f"wait {level}"):
                    wait = spec["wait_for"]
                    seconds = wait.get("timeout") or recipes.WAIT_FOR_TIMEOUT
                    self.site.report(
                        "info",
                        said(
                            "waiting for {selector} to show, at most {seconds} s",
                            selector=wait["selector"],
                            seconds=f"{seconds:g}",
                        ),
                    )
                self.driver.expand(page, spec)
                if self.repeated(page, level, spec) and number > 1:
                    return
                if level < len(self.follow):
                    links = self.matching(page, spec, (level, number), values)
                    if not links:
                        self.blocked(page, level, number)
                    if spec.get("click"):
                        links = [self.clicked(page, spec, level, link) for link in links]
                    if (yield from self.children(links, level, values, outcome)):
                        return
                elif (yield from self.files(page, number, values, outcome, level)):
                    return
                later = self.following(page, level, number)
                if later is None:
                    return
                page = later
            self.limited(level)
        finally:
            if page is not None:
                self.driver.done(page)
