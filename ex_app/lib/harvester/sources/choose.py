# SPDX-License-Identifier: AGPL-3.0-or-later
"""The choices a recipe goes through (harvester.choices: the step "each"), in the job's browser:
the options read where the steps before the step left the page, and for each, the option chosen,
the steps after it done again, and the walk (harvester.sources.site: Site._walk).

A <select>'s option is chosen by its value (select_option), a custom dropdown's by a click on the
element that shows its entries and then on the entry of its text, found again by its place and
text (clicks.element_at). From the second choice on, the page goes back to the address it had at
the step first. The calls are the Playwright page's own, so that the recipe's test (harvester
.browser.trial) goes through the choices as a job does.
"""

from collections.abc import Iterator
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .. import choices
from ..l10n import listed, said, said_n
from .base import ChallengeError, SourceError, pattern_search
from .clicks import TARGETS, element_at
from .tabs import arrived, in_frame, settle

# A <select>'s options that can be chosen (not disabled, not an empty placeholder): text and value.
SELECT_OPTIONS = (
    "el => el.options ? [...el.options].filter(o => !o.disabled && o.value !== '')"
    ".map(o => [(o.label || o.text || '').trim(), o.value]) : null"
)


class Chooser:
    """The step "each" of a driver's steps (number: its place, from 1), the steps after it with
    their numbers, and the address the page had when the steps before it were done."""

    def __init__(self, number: int, step: dict, rest: list[tuple[int, dict]]):
        self.number = number
        self.step = step
        self.rest = rest
        self.address = ""

    def target(self, page: Any, timeout: float) -> Any:
        """The page, or the frame the step acts in."""
        return in_frame(page, self.step["frame"], timeout) if self.step.get("frame") else page

    def options(self, page: Any, timeout: float) -> list[tuple[str, str]]:
        """The choices: a fixed list's values, or the options the page offers (a <select>'s, a
        dropdown's entries once shown), those whose text the step's pattern matches, at most
        choices.CHOICES_MAX."""
        self.address = page.url
        step = self.step
        values = step.get("values", "options")
        if values != "options":
            found = [(str(value), str(value)) for value in values]
        elif step.get("option"):
            target = self.target(page, timeout)
            self.shown(target, timeout)
            found = [(text, text) for text in target.eval_on_selector_all(step["option"], TARGETS) if text]
            page.keyboard.press("Escape")
        else:
            target = self.target(page, timeout)
            target.wait_for_selector(step["selector"], state="attached", timeout=timeout)
            found = target.eval_on_selector(step["selector"], SELECT_OPTIONS)
            if found is None:
                raise SourceError(f"{step['selector']} is no list (<select>): a dropdown needs its entries (option)")
        pattern = str(step.get("pattern") or "")
        found = [(text, value) for text, value in found if not pattern or pattern_search(pattern, text)]
        return list(dict.fromkeys(found))[: choices.CHOICES_MAX]

    def shown(self, target: Any, timeout: float) -> None:
        """Shows the dropdown's entries: a click on its element, unless they show already (a menu
        that Escape did not close)."""
        entry = target.query_selector(self.step["option"])
        if entry is None or not entry.is_visible():
            target.click(self.step["selector"], timeout=timeout)
        target.wait_for_selector(self.step["option"], state="visible", timeout=timeout)

    def choose(self, page: Any, choice: tuple[str, str], again: bool, timeout: float) -> None:
        """Chooses the option on the page (again: after the first, back at the step's address
        before), and waits for what it started."""
        step = self.step
        if again and self.address:
            page.goto(self.address, wait_until="domcontentloaded")
            settle(page)
        if not step.get("selector"):
            return
        text, value = choice
        target = self.target(page, timeout)
        if step.get("option"):
            self.shown(target, timeout)
            texts = target.eval_on_selector_all(step["option"], TARGETS)
            element_at(target, step["option"], texts.index(text) if text in texts else len(texts), text).click()
        else:
            try:
                target.select_option(step["selector"], value=value, timeout=timeout)
            except PlaywrightError:
                # A fixed list's value may be the text the option shows.
                target.select_option(step["selector"], label=text, timeout=timeout)
        arrived(page, step, timeout)


def each(site: Any, driver: Any, timeout: float) -> Iterator[dict]:
    """The values each walk of the site starts with: {} once without choices; else, for each
    choice, {"choice": its text, "choice_value": its value} once it was chosen and the steps after
    it were done (driver.chooser, driver.replay_rest). A choice that fails is a gap of the run,
    said in the log; the site's choices are the texts gone through (the recipe's test shows them)."""
    chooser: Chooser | None = getattr(driver, "chooser", None)
    if chooser is None:
        yield {}
        return
    page = driver.main
    found = chooser.options(page, timeout)
    site.choices = [text for text, _ in found]
    if not found:
        raise SourceError(f"step {chooser.number} ({choices.label(chooser.step)}) found no choices")
    line = said_n(
        "going through %n choice: {choices}",
        "going through %n choices: {choices}",
        len(found),
        choices=listed(site.choices),
    )
    site.report("info", line)
    for index, choice in enumerate(found):
        try:
            chooser.choose(page, choice, index > 0, timeout)
            driver.replay_rest([(number, choices.filled(step, *choice)) for number, step in chooser.rest])
        except ChallengeError:
            raise
        except (SourceError, PlaywrightError) as error:
            site.gaps += 1
            why = (str(error) or type(error).__name__).splitlines()[0]
            site.report("info", said("the choice “{choice}” failed: {why}", choice=choice[0], why=why))
            continue
        yield {"choice": choice[0], "choice_value": choice[1]}
