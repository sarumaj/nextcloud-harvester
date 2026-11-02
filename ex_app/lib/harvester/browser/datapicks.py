"""The recorder's picks of data beside the links: values from the page near a level's links
("Pick a value"), the page's answers in JSON (the Network panel) and one of them as the file links
(harvester.jsonlist), and a code typed into a field that comes by e-mail instead (a step
"mail-code"). A part of the Recorder (recorder.py), which keeps the picks and shows them.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable

import regex
from playwright.async_api import Page

from .. import jsonlist, patterns
from ..sources import near

EXAMPLES = 8
# How long a person's pattern may take (s): on the links of a page, all together; on a page's
# address. A pattern that backtracks without end ((a|aa)+$) would otherwise hold the app: it runs
# with the regex module's time limit, in a thread that leaves the event loop free (concurrent).
PATTERN_TIMEOUT = 2.0
# The page's answers in JSON the Network panel lists (the newest), and the largest one whose body
# it keeps to propose a listing from (a listing is far smaller; the app's memory is shared).
REQUESTS_KEPT = 30
REQUEST_BODY = 2 * 1024 * 1024
# What a value of the page may change of its spec (the page's message), and what a JSON listing may.
VALUE_KEYS = ("name", "regex", "type", "attribute", "closest", "selector", "field")
JSON_KEYS = ("url", "items", "file", "text", "pattern", "page")
# The page's messages DataPicks.handle_data answers.
DATA_MESSAGES = frozenset({"value", "value-add", "value-remove", "json-pick", "json-edit"})


def search_all(pattern: str, texts: list[str]) -> list:
    """The pattern's match in each text (None for none or an empty text), within PATTERN_TIMEOUT
    for all of them (else TimeoutError)."""
    compiled = regex.compile(pattern)
    deadline = time.monotonic() + PATTERN_TIMEOUT
    found = []
    for text in texts:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the pattern took too long")
        found.append(compiled.search(text, timeout=left, concurrent=True) if text else None)
    return found


def examples_of(found: list, matches: list, specs: list[dict], texts: list) -> list[dict]:
    """The first EXAMPLES links the pattern matched, with their values: the pattern's groups and
    those of the page (specs: the level's values, texts: what each found beside each link)."""
    examples = []
    for index, ((href, text), match) in enumerate(zip(found, matches)):
        if match and len(examples) < EXAMPLES:
            values = patterns.group_values(match)
            if specs and index < len(texts):
                values |= patterns.page_values(specs, texts[index], timed_search, values)
            examples.append({"url": href, "text": text, "values": values})
    return examples


def timed_search(pattern: str, text: str):
    """A person's pattern searched in a text of the page, within PATTERN_TIMEOUT (else
    TimeoutError), in a thread that leaves the event loop free."""
    return regex.search(pattern, text, timeout=PATTERN_TIMEOUT, concurrent=True)


class DataPicks:  # pylint: disable=too-few-public-methods
    """The recorder's picks of values and JSON listings (mixed into Recorder,
    whose state and methods these are)."""

    send: Callable[[dict], Awaitable[None]]
    start_url: str
    follow: list[dict]
    links: dict
    proposals: dict[str, dict]
    value_for: tuple[str, int] | None
    requests: list[dict]
    _page: Page | None
    _context: object
    _json: tuple[str, object] | None

    def _remember(self) -> None:
        """Keeps the picks as they are, for Undo (the recorder's)."""

    async def _update(self) -> None:
        """Sends the recipe and the proposals to the page (the recorder's)."""

    async def _describe(self, key: str) -> None:
        """Shows what a level's pick matches (the recorder's)."""

    async def handle_data(self, kind: str, message: dict) -> None:
        """The page's messages about values and JSON listings (DATA_MESSAGES); a code by e-mail is
        an edit of the steps (steps.StepList)."""
        if kind in ("value", "value-add", "value-remove"):
            await self._edit_value(kind, message)
        elif kind == "json-pick":
            await self._json_pick(message.get("id"))
        elif kind == "json-edit":
            await self._json_edit(message.get("changes"))

    def _value_target(self, message: dict) -> tuple[str, int] | None:
        """The level a message names for its values: ("links", 0), or ("follow", level) of one
        picked; None for another."""
        target, level = message.get("target"), message.get("level", 0)
        if target == "links" and self.links:
            return "links", 0
        if target == "follow" and isinstance(level, int) and 0 <= level < len(self.follow):
            return "follow", level
        return None

    def _spec(self, target: str, level: int) -> dict:
        """The pick of the level (the file links, or a level of links to follow)."""
        return self.links if target == "links" else self.follow[level]

    async def _pick_value(self, event: dict) -> None:
        """A value picked near a level's links (recorder.js: where it is from the link, its text):
        its pattern, type and name proposed from what it reads near each of the level's links."""
        if not self.value_for or self._page is None:
            return
        target, level = self.value_for
        spec = self._spec(target, level)
        if spec.get("kind") == "json":
            return
        if event.get("none"):
            message = "That text is near none of the level's links: pick one in a link's row or card (or in the link)."
            await self.send({"type": "error", "message": message})
            return
        where = {key: str(event.get(key) or "") for key in ("closest", "selector", "attribute")}
        found = await self._page.eval_on_selector_all(spec["selector"], near.VALUES, [where])
        texts = [entry[2][0] for entry in found if entry[2][0]]
        proposal = patterns.propose_value(texts or [str(event.get("text") or "")])
        values = spec.setdefault("values", [])
        if len(values) >= patterns.MAX_VALUES:
            await self.send({"type": "error", "message": f"A level takes at most {patterns.MAX_VALUES} values."})
            return
        self._remember()
        taken = {str(value.get("name")) for value in values}
        name, number = proposal["name"], 2
        while name in taken:
            name, number = f"{proposal['name']}{number}", number + 1
        values.append({**proposal, "name": name, **where})
        await self._describe("links" if target == "links" else f"follow{level}")

    async def _edit_value(self, kind: str, message: dict) -> None:
        """A value of a level changed, added (a JSON item's field) or removed."""
        chosen = self._value_target(message)
        if not chosen:
            return
        spec = self._spec(*chosen)
        values = spec.setdefault("values", [])
        index = message.get("index")
        self._remember()
        if kind == "value-add":
            name = re.sub(r"\W", "_", str(message.get("field") or "").rsplit(".", maxsplit=1)[-1].split("[")[0])[:30]
            name = name or "value"
            name = name if patterns.VALUE_NAME.match(name) else f"v_{name}"[:30]
            values.append({"name": name, "field": str(message.get("field") or ""), "regex": "", "type": "text"})
        elif not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(values):
            return
        elif kind == "value-remove":
            del values[index]
        else:
            changes = message.get("changes") if isinstance(message.get("changes"), dict) else {}
            values[index] |= {key: str(changes[key]) for key in VALUE_KEYS if isinstance(changes.get(key), str)}
        if not values:
            spec.pop("values", None)
        await self._describe("links" if chosen[0] == "links" else f"follow{chosen[1]}")

    async def _response(self, page: Page, response) -> None:
        """An answer of the page: one in JSON to an XHR or fetch() GET of the recorder's own pages
        (not the test's) goes into the Network panel, its body kept while small enough."""
        try:
            request = response.request
            if (
                page.context != self._context
                or request.method != "GET"
                or request.resource_type not in ("xhr", "fetch")
            ):
                return
            if "json" not in (response.headers.get("content-type") or ""):
                return
            announced = response.headers.get("content-length", "")
            body = None if announced.isdigit() and int(announced) > REQUEST_BODY else await response.body()
        except Exception:  # pylint: disable=broad-exception-caught
            return  # the page went on (navigated, closed): its answers with it
        number = (self.requests[-1]["id"] + 1) if self.requests else 1
        size = len(body) if body is not None else int(announced)
        entry = {"id": number, "url": response.url, "size": size, "status": response.status}
        self.requests = (self.requests + [entry | {"body": body if body and len(body) <= REQUEST_BODY else None}])[
            -REQUESTS_KEPT:
        ]
        await self.send({"type": "requests", "items": [{key: item[key] for key in entry} for item in self.requests]})

    async def _json_pick(self, number: object) -> None:
        """A JSON answer of the Network panel chosen as the listing: file links of kind json,
        proposed from it (jsonlist.propose), for the person to check and change."""
        chosen = next((item for item in self.requests if item["id"] == number), None)
        if chosen is None or chosen["body"] is None:
            await self.send(
                {"type": "error", "message": "That answer is not kept (too large, or gone): reload the page."}
            )
            return
        try:
            data = await asyncio.to_thread(jsonlist.parse, chosen["body"])
        except ValueError as error:
            await self.send({"type": "error", "message": f"That answer is no JSON: {error}"})
            return
        self._remember()
        self._json = (chosen["url"], data)
        if self._page is not None and not self.follow:
            self.start_url = self.start_url or self._page.url
        self.links = jsonlist.propose(chosen["url"], data)
        self.proposals["links"] = {"level": len(self.follow)}
        await self._describe("links")

    async def _json_edit(self, changes: object) -> None:
        """The person's changes to the JSON listing (its address, items, fields, pages, pattern)."""
        if not isinstance(changes, dict) or self.links.get("kind") != "json":
            return
        self._remember()
        for key in JSON_KEYS:
            if key == "page" and key in changes:
                page = changes["page"]
                if isinstance(page, dict) and page.get("param"):
                    self.links["page"] = {
                        "param": str(page["param"]),
                        "first": max(0, int(page.get("first", 1) or 0)),
                        "step": max(1, int(page.get("step", 1) or 1)),
                    }
                else:
                    self.links.pop("page", None)
            elif isinstance(changes.get(key), str):
                self.links[key] = changes[key].strip()
        await self._describe("links")

    async def _examples(self, page: Page, spec: dict, found: list, matches: list) -> tuple[list[dict], str]:
        """The examples of a level's links on the page (found: their addresses and texts, matches:
        the pattern's), with the values of the page beside each where the level has values; and
        what went wrong with a value's pattern (then the examples without the page's values)."""
        specs = spec.get("values") or []
        # The values' texts beside the links: the job's own script.
        texts = (
            [entry[2] for entry in await page.eval_on_selector_all(spec["selector"], near.VALUES, specs)]
            if specs
            else []
        )
        try:
            return await asyncio.to_thread(examples_of, found, matches, specs, texts), ""
        except (TimeoutError, regex.error) as error:
            return await asyncio.to_thread(examples_of, found, matches, [], []), f"a value's pattern: {error}"

    async def _describe_json(self, proposal: dict) -> None:
        """What the JSON listing gives with the answer it was chosen from: its items, the first
        file links with their values, the address of its next page."""
        spec = self.links
        if self._json is None:
            proposal.update(error="", examples=[], matching=0, found=0, kind="json", chosen=False)
            await self._update()
            return
        url, data = self._json
        page = jsonlist.JsonPage(url, data)
        try:
            entries = jsonlist.entries(page, spec)
            matches = await asyncio.to_thread(
                search_all, str(spec.get("pattern") or ""), [href for href, _, _ in entries]
            )
            found = [(href, text) for href, text, _ in entries]
            texts = [values for _, _, values in entries]
            examples = await asyncio.to_thread(examples_of, found, matches, spec.get("values") or [], texts)
        except (TimeoutError, regex.error) as error:
            proposal.update(error=f"the pattern: {error}", examples=[], matching=0, found=0, kind="json")
            await self._update()
            return
        items = jsonlist.items(data, str(spec.get("items") or ""))
        following = jsonlist.page_url(str(spec.get("url") or url), spec.get("page"), 2) if spec.get("page") else ""
        proposal.update(
            error="" if entries else "No item of the JSON has a file's address in that field.",
            examples=examples,
            matching=sum(1 for match in matches if match),
            found=len(entries),
            items=len(items),
            sample=json.dumps(items[0], ensure_ascii=False)[:600] if items else "",
            following=following,
            kind="json",
            chosen=True,
        )
        await self._update()
