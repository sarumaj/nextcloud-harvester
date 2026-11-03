# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's picks of links (a level of links to follow, the file links): what a pick
matches on the page (its outlines, examples and values), and picks of elements without an
address (a button, a row or card a script opens, an <a> without href or with javascript:),
which a job clicks (harvester.sources.clicks). A part of the Recorder (recorder.py), which keeps
the picks and shows them.

A pick whose elements all lack an address becomes one that is clicked ("click": true), its
pattern proposed on the elements' texts (text_pattern); the page's {type: click, target, level,
click} switches a pick between the two, its pattern proposed anew from the page.
"""

import asyncio
import os
import re

from .. import patterns
from .datapicks import DataPicks, search_all
from .engine import main_world

# The links' outlines: the pattern as JavaScript reads it (patterns.js_regex) or, where it cannot
# (or for elements that are clicked: the pattern is on their texts), the matches Python found
# (one per element, in their order).
HIGHLIGHT = (
    "([selector, pattern, hits]) => window.__harvester ? window.__harvester.highlight(selector, pattern, hits) : null"
)
# A pick's elements: the address a job can open (an http(s) link's that goes elsewhere than the
# page; else none: an element the job clicks), and the text it tells them by (sources.clicks).
MATCHES = """els => els.map(e => {
  const href = typeof e.href === 'string' ? e.href : ''
  const opens = /^https?:/i.test(href) && !(e.getAttribute('href') || '').trim().startsWith('#')
  const text = e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('title') || ''
  return [opens ? href : '', text.trim().slice(0, 80)]
})"""


def _shared(texts: list[str]) -> str:
    """The texts' common beginning up to its last space (a whole word or words), or ""."""
    common = os.path.commonprefix(texts)
    return common[: common.rfind(" ") + 1] if " " in common else ""


def text_pattern(texts: list[str]) -> str:
    """A pattern on the texts of elements that are clicked: their common beginning and end (whole
    words) around what differs between them, a value (label); else any text ("."). Without "$":
    a row's text may go on in further lines."""
    filled = sorted({re.sub(r"[ \t]+", " ", text).strip() for text in texts if text and text.strip()})
    if len(filled) < 2:
        return "."
    first = [text.splitlines()[0] for text in filled]
    prefix = _shared(first)
    suffix = _shared([text[::-1] for text in first])[::-1]
    if len(prefix) + len(suffix) >= min(len(text) for text in first):
        suffix = ""
    if not prefix and not suffix:
        return "."
    return (
        "^" + re.escape(prefix) + "(?P<label>.+?)" + re.escape(suffix)
        if suffix
        else f"^{re.escape(prefix)}(?P<label>.+)"
    )


def link_proposal(matches: list, target: str) -> tuple[dict, bool]:
    """The proposal for a pick's elements (their addresses and texts, MATCHES): a pattern on their
    addresses (patterns.propose), or, when none has one, on their texts for elements that are
    clicked (and the file links named as the site sends them, {disposition}); and whether they
    are clicked."""
    hrefs, texts = [href for href, _ in matches], [text for _, text in matches]
    if not matches or any(hrefs):
        return patterns.propose(hrefs, texts), False
    pattern = text_pattern(texts)
    proposal = {
        "pattern": pattern,
        "groups": list(re.compile(pattern).groupindex),
        "path": "{disposition}" if target == "links" else "{filename}",
        "tags": [],
        "matches": sum(1 for text in texts if text and re.search(pattern, text)),
    }
    return proposal, True


class LinkPicks(DataPicks):  # pylint: disable=too-few-public-methods
    """The recorder's picks of links (mixed into Recorder, whose state and methods these are)."""

    async def _describe(self, key: str) -> None:
        """Outlines what the level's pattern matches on the page (on the links' addresses, or the
        texts of elements that are clicked) and lists examples with their values."""
        spec = self.links if key == "links" else self.follow[int(key.removeprefix("follow"))]
        page = self._page
        assert page is not None
        proposal = self.proposals.setdefault(key, {})
        if spec.get("kind") == "json":
            await self._describe_json(proposal)
            return
        try:
            pattern = re.compile(spec.get("pattern", ""))
        except re.error as error:
            proposal.update(error=f"not a regular expression: {error}", examples=[], matching=0)
            await self._update()
            return
        clicked = bool(spec.get("click"))
        found = await page.eval_on_selector_all(spec["selector"], MATCHES)
        try:
            matches = await asyncio.to_thread(
                search_all, pattern.pattern, [text if clicked else href for href, text in found]
            )
        except TimeoutError:
            proposal.update(
                error="the pattern takes too long on this page's links (it backtracks): make it simpler",
                examples=[],
                matching=0,
            )
            await self._update()
            return
        examples, problem = await self._examples(page, spec, found, matches)
        matching = sum(1 for match in matches if match)
        proposal.update(error=problem, examples=examples, matching=matching, found=len(found), pattern=pattern.pattern)
        proposal["groups"] = list(pattern.groupindex)
        proposal["click"] = clicked
        # JavaScript outlines by the pattern where it reads it as Python does, else by Python's
        # matches (a page that changed meanwhile may then be outlined slightly off).
        hits = [bool(match) for match in matches]
        try:
            await page.evaluate(
                HIGHLIGHT,
                [spec["selector"], None if clicked else patterns.js_regex(pattern.pattern), hits],
                **main_world(page),
            )
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # the page navigated meanwhile
        await self._update()

    async def _set_click(self, message: dict) -> None:
        """A pick switched to elements the job clicks, or back to links with addresses ({type:
        click, target, level, click}): its pattern proposed anew from the page's elements (on
        their texts, or their addresses) where the page has them, else kept."""
        chosen = self._value_target(message)
        if not chosen or self._page is None:
            return
        spec = self._spec(*chosen)
        wanted = message.get("click") is True
        if spec.get("kind") == "json" or wanted == bool(spec.get("click")):
            return
        self._remember()
        try:
            found = await self._page.eval_on_selector_all(spec["selector"], MATCHES)
        except Exception:  # pylint: disable=broad-exception-caught
            found = []  # a selector the page cannot read: the pattern stays
        key = "links" if chosen[0] == "links" else f"follow{chosen[1]}"
        if wanted:
            spec["click"] = True
            proposal, _ = link_proposal([("", text) for _, text in found], chosen[0])
        else:
            spec.pop("click", None)
            proposal = patterns.propose([href for href, _ in found], [text for _, text in found])
        usable = any(text for _, text in found) if wanted else any(href for href, _ in found)
        if usable:
            spec["pattern"] = proposal["pattern"]
            level = self.proposals.get(key, {}).get("level", chosen[1])
            self.proposals[key] = {**proposal, "level": level}
        await self._describe(key)
