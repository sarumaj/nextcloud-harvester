"""Recipes: how to get from a site's front door to its files, as the recorder makes them.

    {
      "steps": [{"action": "goto", "url": ...}, {"action": "fill", "selector": ...,
                 "credential": "username"}, {"action": "click", "selector": ...}, ...],
      "start": "https://... the first listing",
      "follow": [{"selector": "a.year", "pattern": "/archiv/(?P<year>\\\\d{4})$"}, ...],
      "links": {"selector": "a.issue", "pattern": "/archiv/(?P<year>\\\\d{4})/(?P<issue>\\\\d+)$"},
      "next": {"selector": "a.next", "level": 0}
    }

steps (optional) sign in or prepare in the browser; without them the pages are fetched without
one. start is the listing (after the steps); each entry of follow is a level of links to follow
before the file links (a year, then its issues); next is the link to a listing's next page, at
that level (0: start's). A next of {"kind": "number", "selector": <the pagination's container>,
"level": 0} is a pagination of numbers instead (harvester.pagination): the item numbered one
higher than the current page, a link or a button ("buttons": true: only the browser clicks them).

A step {"action": "scroll", "rounds": 20} scrolls the page to its end until no more comes (a
listing that loads more as it is scrolled), at most rounds times (SCROLL_ROUNDS without).

A step inside a frame (a consent banner's, a sign-in's) has "frame": its path from the page
down, one entry per frame: {"selector": <the frame's element in its parent>, "url": <its
address without query and fragment>, "name": <its name, if it has one>}. The selector finds it
first, the address or the name when the selector finds nothing (a page that changed its
frames' order, say); a query that changes from visit to visit matters to neither. A step
without it acts on the page itself, as recipes always did.

A step with "optional": true is best effort (a banner that shows only on the first visit): the
job waits for its element a short time only and goes on without it (harvester.sources.site).

A fill step's value may hold dates (dated()): {today}, {today-30d:%d.%m.%Y}, {year}, {month:02d},
filled in at each replay in the job's time zone; other braces are typed as they are.

The steps are of two kinds (sign_in_steps()): the sign-in, up to the last fill of a credential
and the click or key that sends it, which a kept session skips; and the steps from there to the
listing (a tab, "show all", a filter), which every replay does.

The file links may have "key": a template of their values ({year}-{issue}) that tells files
apart across runs instead of their address. Their "click": true makes them elements that start
a download when clicked (a button, a form, a script), which only the browser can do. A level
(an entry of follow, or links) may load more of its pages before they are read: "scroll":
rounds of scrolling to the end, "load_more": the element of a "Load more" button, clicked until
it is gone (harvester.sources.site).
"""

import calendar
import datetime
import re
import string
from urllib.parse import urlsplit

from .browser import allowed_url

ACTIONS = {"goto", "click", "fill", "press", "scroll"}
# A scroll step's rounds: by default, and at most.
SCROLL_ROUNDS = 20
SCROLL_ROUNDS_MAX = 100
# A level's "Load more" button is clicked at most this many times per page.
LOAD_MORE_MAX = 50
NEXT_KINDS = ("link", "number")
# The actions that act on an element, and so may do it inside a frame.
ON_ELEMENTS = ("click", "fill", "press")
# What a fill step may fill in from the job's account.
CREDENTIALS = ("username", "password", "totp")
# The values every link gives (patterns.link_values), besides its pattern's groups.
LINK_VALUES = ("url", "text", "page", "host", "filename", "stem", "ext")
# The keys a press step may press (Playwright's names), besides a single character, optionally
# after modifiers ("Control+A").
KEYS = frozenset(
    {"Enter", "Tab", "Escape", "Backspace", "Delete", "Insert", "Home", "End", "PageUp", "PageDown", "Space"}
    | {"ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backquote", "Minus", "Equal", "Backslash"}
    | {f"F{number}" for number in range(1, 13)}
    | {f"Key{letter}" for letter in string.ascii_uppercase}
    | {f"Digit{digit}" for digit in range(10)}
)
MODIFIERS = frozenset({"Shift", "Control", "Alt", "Meta", "ControlOrMeta"})
# A date in a fill step's value: {today}, {year}, {month}, {day}, shifted (-30d, +1w, -1m, -1y;
# without a unit, the name's own) and formatted (strftime for today, a number's format else).
DATED = re.compile(r"\{(today|year|month|day)([+-]\d{1,4}[dwmy]?)?(?::([^{}]*))?\}")
# Something that reads as such a date but is none (a typo: {today-3x}, {month:%Y}).
DATED_LIKE = re.compile(r"\{(?:today|year|month|day)\b[^{}]*\}")


def frame_address(url: str) -> str:
    """A frame's address as a step keeps it: without query and fragment, which often change from
    visit to visit (a session, a version); "" for a frame without one of its own (about:blank,
    srcdoc)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def frame_host(step: dict) -> str:
    """The host of the innermost frame the step acts in ("" on the page, or for a frame without
    an address)."""
    frames = step.get("frame") or []
    if not isinstance(frames, list) or not frames or not isinstance(frames[-1], dict):
        return ""
    try:
        return urlsplit(str(frames[-1].get("url") or "")).hostname or ""
    except ValueError:
        return ""


def step_label(step: dict) -> str:
    """A step in a few words, for a log line: what it opens, clicks (its text, else its element),
    fills or presses, and the frame it does it in."""
    return _action_label(step) + (f" in frame {frame_host(step) or '(no address)'}" if step.get("frame") else "")


def _action_label(step: dict) -> str:
    action = step.get("action")
    if action == "goto":
        return f"open {step.get('url', '')}"
    if action == "scroll":
        return "scroll to load more"
    if action == "click":
        text = str(step.get("text") or "").strip()
        return f"click “{text}”" if text else f"click {step.get('selector', '')}"
    if action == "press":
        return f"press {step.get('key', 'Enter')} in {step.get('selector', '')}"
    if action == "fill":
        return f"fill in {step.get('credential') or 'a value'} ({step.get('selector', '')})"
    return str(action)


def _framed(step: dict, event: dict) -> dict:
    """The step with the frame the event happened in, if it was in one."""
    return step | {"frame": event["frame"]} if event.get("frame") else step


def steps_from_events(events: list[dict]) -> list[dict]:  # pylint: disable=too-many-branches
    """Turns what the recorder saw into the recipe's steps.

    Typing into a field several times keeps the last value. A password is never kept: its step
    fills in the job's password, and the field filled before it in the same form the job's
    username, so neither is in the recipe. A one-time code is not kept either: its step fills in
    the current code from the account's secret. An event inside a frame keeps its frame's path.
    """
    steps: list[dict] = []
    for event in events:
        action = event.get("action")
        if action == "goto":
            steps.append({"action": "goto", "url": event["url"]})
        elif action == "scroll":
            steps.append({"action": "scroll"})
        elif action == "click":
            steps.append(
                _framed({"action": "click", "selector": event["selector"], "text": event.get("text", "")}, event)
            )
        elif action == "press":
            steps.append(
                _framed({"action": "press", "selector": event["selector"], "key": event.get("key", "Enter")}, event)
            )
        elif action == "fill":
            step = {"action": "fill", "selector": event["selector"], "value": event.get("value", "")}
            if event.get("otp"):
                # The code typed while recording is spent: the job fills in the current one.
                step = {"action": "fill", "selector": event["selector"], "credential": "totp"}
            elif event.get("field") == "password":
                step = {"action": "fill", "selector": event["selector"], "credential": "password"}
                for earlier in reversed(steps):
                    same_form = (earlier.get("form"), earlier.get("frame")) == (event.get("form"), event.get("frame"))
                    if earlier["action"] == "fill" and "value" in earlier and same_form:
                        earlier.pop("value")
                        earlier["credential"] = "username"
                        break
            step = _framed(step, event) | {"form": event.get("form", "")}
            last = steps[-1] if steps else {}
            if last.get("action") == "fill" and (last["selector"], last.get("frame")) == (
                step["selector"],
                step.get("frame"),
            ):
                steps[-1] = step
            else:
                steps.append(step)
    for step in steps:
        step.pop("form", None)
    return steps


def sign_in_steps(steps: list[dict]) -> int:
    """How many of the steps sign in: those up to the last fill of a credential and the first
    click or key after it (which sends the form); 0 without a credential (no sign-in, only the
    way to the listing). The steps after them lead to the listing and are always replayed."""
    last = max((index for index, step in enumerate(steps) if step.get("credential")), default=-1)
    if last < 0:
        return 0
    for index in range(last + 1, len(steps)):
        if steps[index].get("action") in ("click", "press"):
            return index + 1
    return last + 1


def _shifted(today: datetime.date, shift: str, unit: str) -> datetime.date:
    """The date shifted by a number of days, weeks, months or years (a month's day kept where
    the month has it, else its last)."""
    amount = int(shift[:-1] if shift[-1].isalpha() else shift)
    unit = shift[-1] if shift[-1].isalpha() else unit
    if unit in "dw":
        return today + datetime.timedelta(days=amount * (7 if unit == "w" else 1))
    year, month = divmod(today.year * 12 + today.month - 1 + amount * (12 if unit == "y" else 1), 12)
    return datetime.date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))


def dated(text: str, today: datetime.date) -> str:
    """The value with its dates filled in for today (DATED); ValueError for a format that does
    not fit (a number's format on a date)."""

    def fill(match: re.Match) -> str:
        name, shift, form = match.group(1), match.group(2), match.group(3)
        units = {"today": "d", "day": "d", "month": "m", "year": "y"}
        when = _shifted(today, shift, units[name]) if shift else today
        if name == "today":
            return when.strftime(form or "%Y-%m-%d")
        return format(getattr(when, name), form or "")

    return DATED.sub(fill, text)


def _check_value(number: int, value: object) -> list[str]:
    if not isinstance(value, str):
        return [f"step {number}: the value is text"]
    wrong = [found for found in DATED_LIKE.findall(value) if not DATED.fullmatch(found)]
    try:
        dated(value, datetime.date(2024, 1, 31))
    except ValueError:
        wrong.append(value)
    if wrong:
        return [f"step {number}: {wrong[0]} is no date of the form {{today-30d:%d.%m.%Y}}, {{year}} or {{month:02d}}"]
    return []


def key_ok(key: object) -> bool:
    """Whether a press step's key is one the browser knows: a key's name or one character,
    after modifiers ("Control+Shift+KeyA", "Shift++")."""
    if not isinstance(key, str) or not key:
        return False
    if len(key) == 1:
        return True
    head, plus, last = key.rpartition("+")
    if not last and head.endswith("+"):
        head, last = head[:-1], "+"
    modifiers = head.split("+") if plus else []
    return all(modifier in MODIFIERS for modifier in modifiers) and (len(last) == 1 or last in KEYS | MODIFIERS)


def _check_pattern(where: str, pattern: object) -> list[str]:
    if not isinstance(pattern, str):
        return [f"{where}: the pattern is text"]
    try:
        re.compile(pattern)
    except re.error as error:
        return [f"{where}: the pattern is not a regular expression ({error})"]
    return []


def _rounds_ok(rounds: object) -> bool:
    return isinstance(rounds, int) and not isinstance(rounds, bool) and 1 <= rounds <= SCROLL_ROUNDS_MAX


def _check_next(after: object, levels: int) -> list[str]:
    # A next without its element has always meant none (the walk ends after the first page).
    if not isinstance(after, dict) or not after.get("selector"):
        return [] if not after or isinstance(after, dict) else ["next page: not an object"]
    if not isinstance(after["selector"], str):
        return ["next page: the element (selector) is text"]
    if after.get("kind", "link") not in NEXT_KINDS:
        return [f"next page: unknown kind {after.get('kind')}"]
    level = after.get("level", 0)
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= levels:
        return [f"next page: the level is a number from 0 (the listing) to {levels} (the file links' pages)"]
    if not isinstance(after.get("buttons", False), bool):
        return ["next page: buttons is true or false"]
    return []


def _check_level(where: str, spec: object) -> list[str]:
    """A level's element, pattern and loading of more (scroll rounds, a Load more button)."""
    if not isinstance(spec, dict) or not spec.get("selector"):
        return [f"{where}: no links to follow" if where.startswith("level") else "no file links picked"]
    if not isinstance(spec["selector"], str):
        return [f"{where}: the element (selector) is text"]
    problems = _check_pattern(where, spec.get("pattern", ""))
    if "scroll" in spec and not _rounds_ok(spec["scroll"]):
        problems.append(f"{where}: rounds of scrolling from 1 to {SCROLL_ROUNDS_MAX}")
    if "load_more" in spec and not (isinstance(spec["load_more"], str) and spec["load_more"]):
        problems.append(f"{where}: the Load more button's element (selector) is text")
    return problems


def _check_key(spec: dict, recipe: dict) -> list[str]:
    """The file links' key template: text naming only values every file link has (its
    pattern's and the levels' groups, the link's own)."""
    if "key" not in spec:
        return []
    key = spec["key"]
    if not isinstance(key, str) or not key.strip():
        return ["file links: the key is a template of the links' values, like {year}-{issue}"]
    try:
        names = [name for _, name, _, _ in string.Formatter().parse(key) if name is not None]
    except ValueError as error:
        return [f"file links: the key {key} is no template ({error})"]
    groups = set(LINK_VALUES)
    for level in [*(recipe.get("follow") or []), spec]:
        with_pattern = isinstance(level, dict) and isinstance(level.get("pattern"), str)
        try:
            groups |= set(re.compile(level["pattern"]).groupindex) if with_pattern else set()
        except re.error:
            pass  # told as the level's own problem
    unknown = [name for name in names if name.split(".")[0].split("[")[0] not in groups]
    if not names or unknown:
        return [f"file links: the key {key} names {', '.join(unknown) or 'no value'}, which no pattern gives"]
    return []


def _frame_ok(frames: object) -> bool:
    return isinstance(frames, list) and all(
        isinstance(entry, dict)
        and all(isinstance(entry.get(key, ""), str) for key in ("selector", "url", "name"))
        and any(entry.get(key) for key in ("selector", "url", "name"))
        for entry in frames
    )


def _check_fields(number: int, step: dict) -> list[str]:
    """What a step's fields hold: text where text belongs, a credential of the account, a key
    the browser knows, dates it can fill in."""
    for field in ("selector", "url", "text"):
        if field in step and not isinstance(step[field], str):
            return [f"step {number}: the {field} is text"]
    if "credential" in step and step["credential"] not in CREDENTIALS:
        return [f"step {number}: unknown credential {step['credential']} (username, password or totp)"]
    if step["action"] == "press" and not key_ok(step.get("key", "Enter")):
        return [f"step {number}: {step.get('key')} is no key (Enter, Tab, Escape, ArrowDown, a letter, Control+A, …)"]
    if "value" in step:
        return _check_value(number, step["value"])
    return []


def _check_steps(steps: list) -> list[str]:
    problems = []
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get("action") not in ACTIONS:
            problems.append(f"step {number}: unknown action {step.get('action') if isinstance(step, dict) else step}")
        elif not isinstance(step.get("optional", False), bool):
            problems.append(f"step {number}: optional is true or false")
        elif step.get("frame") and (step["action"] not in ON_ELEMENTS or not _frame_ok(step["frame"])):
            problems.append(f"step {number}: the frame is a list of frames, each with its selector, address or name")
        elif step["action"] == "goto" and not allowed_url(str(step.get("url", ""))):
            problems.append(f"step {number}: the browser may not open {step.get('url')}")
        elif step["action"] == "scroll" and not _rounds_ok(step.get("rounds", SCROLL_ROUNDS)):
            problems.append(f"step {number}: rounds of scrolling from 1 to {SCROLL_ROUNDS_MAX}")
        elif step["action"] not in ("goto", "scroll") and not step.get("selector"):
            problems.append(f"step {number}: no element")
        else:
            problems += _check_fields(number, step)
    return problems


def check_recipe(recipe: dict) -> list[str]:
    """The recipe's problems, if any (an empty list: it can run)."""
    if not isinstance(recipe, dict):
        return ["the recipe is not an object"]
    steps = recipe.get("steps") or []
    if not isinstance(steps, list):
        return ["the steps are not a list"]
    problems = _check_steps(steps)
    start = recipe.get("start", "")
    if not isinstance(start, str):
        return problems + ["the listing (start) is an address"]
    if start and not allowed_url(start):
        problems.append(f"the listing {start} may not be opened")
    if not start and not any(isinstance(s, dict) and s.get("action") == "goto" for s in steps):
        problems.append("no listing to start at")
    follow = recipe.get("follow") or []
    if not isinstance(follow, list):
        return problems + ["the levels (follow) are a list"]
    for level, spec in enumerate(follow, 1):
        problems += _check_level(f"level {level}", spec)
    links = recipe.get("links")
    problems += _check_level("file links", links)
    if isinstance(links, dict):
        problems += _check_key(links, recipe)
        if not isinstance(links.get("click", False), bool):
            problems.append("file links: click is true or false")
    return problems + _check_next(recipe.get("next"), len(follow))


def levels(recipe: dict) -> list[dict]:
    """The recipe's levels in the walk's order: those to follow, then the file links."""
    return [spec for spec in [*(recipe.get("follow") or []), recipe.get("links")] if isinstance(spec, dict)]


def uses_browser(recipe: dict) -> bool:
    """Whether the recipe needs the browser: it has steps to replay, a next page or numbered
    pages that are buttons (clicked, no address to fetch), file links that are clicked to
    download, or a level that loads more by scrolling or with a button."""
    after = recipe.get("next") or {}
    buttons = isinstance(after, dict) and bool(after.get("buttons"))
    links = recipe.get("links") or {}
    clicked = isinstance(links, dict) and bool(links.get("click"))
    loading = any("scroll" in spec or "load_more" in spec for spec in levels(recipe))
    return bool(recipe.get("steps")) or buttons or clicked or loading
