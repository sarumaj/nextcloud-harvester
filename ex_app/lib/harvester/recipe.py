# SPDX-License-Identifier: AGPL-3.0-or-later
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

A step that acts on an element (click, fill, press) may have "alternatives": more selectors
that found its element alone when it was recorded, tried in their order when "selector" finds
nothing (harvester.selectors: the run says which stood in, the recipe stays as it is). A level,
the file links and the next page's link may have them too, matching the same links.

A step with "optional": true is best effort (a banner that shows only on the first visit): the
job waits for its element a short time only and goes on without it (harvester.sources.site).

A step may have "pause": seconds (above 0, at most PAUSE_MAX) the replay waits before it (a page
that needs a moment after the step before), and "retry": {"times": 1 to RETRY_TIMES_MAX, "delay":
seconds from 0 to RETRY_DELAY_MAX (RETRY_DELAY without)}: when the step fails (its element not
found in time, its wait not met) it is tried again that many more times, after the delay each
time; then it fails as before (an optional one is skipped). Both waits end at once on a stop.

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

A step {"action": "select", "selector": ..., "value": ..., "label": ...} chooses an entry of a
list (<select>): by its value, else by the text it shows. {"action": "hover", "selector": ...}
moves the mouse over an element (a menu that opens under the mouse) before the click after it.
{"action": "popup"} goes on in the tab or window the step before it opened (a sign-in's popup),
{"action": "opener"} back in the page that opened it, once the popup closed itself.

A click or a press that navigated has "wait": {"url": <a pattern of the address it led to>,
"selector": <the next step's element>}: the replay waits for that address and that element
instead of for the network to go quiet, then settles a short while. Without it, as recipes
always did, it waits for the network.

A step {"action": "mail-code", "selector": <the code's field>, "from": <a pattern on the sender>,
"subject": <one on the subject>, "regex": <the code in the message>, "wait": <seconds>} types a
code the site sends by e-mail: the job waits for a new message in the account's code mailbox
(harvester.sources.mail.code_from_mail) and types the code it finds.

A level of links to follow, or the file links, may take "values" from the page around each link
(harvester.patterns.page_values): [{"name": "date", "closest": "tr", "selector": "td.date",
"attribute": "", "regex": "...", "type": "date"}, ...]: the link's closest element matching
closest (none: the link itself), then the element in it matching selector (none: that element),
its attribute or else its text, narrowed by regex, of a type (text, number, date).

The file links may be a JSON listing instead ("kind": "json", harvester.jsonlist): the request's
address, the path to its items and the fields of each item; their values have a "field" in
place of where on the page.

"viewport": {"width": 1280, "height": 800} is the browser window's size the recipe was recorded
at, which the job's browser takes too (pages lay out for it); without one, 1280 × 800.
"""

import calendar
import datetime
import copy
import math
import re
import string
from urllib.parse import urlsplit

from . import patterns
from .browser import VIEWPORT_MAX, VIEWPORT_MIN, allowed_url
from .patterns import token_parameters

ACTIONS = {"goto", "click", "fill", "press", "scroll", "select", "hover", "popup", "opener", "mail-code"}
# A scroll step's rounds: by default, and at most.
SCROLL_ROUNDS = 20
SCROLL_ROUNDS_MAX = 100
# A level's "Load more" button is clicked at most this many times per page.
LOAD_MORE_MAX = 50
NEXT_KINDS = ("link", "number")
# The actions that act on an element, and so may do it inside a frame.
ON_ELEMENTS = ("click", "fill", "press", "mail-code", "select", "hover")
# The actions that switch the tab the steps act in, and so have no element.
SWITCHES = ("popup", "opener")
# What a fill step may fill in instead of a value of its own: the job's account's.
CREDENTIALS = ("username", "password", "totp")
# A fill step's element that looks like a password's field (#pw, [type=password], a name with
# pass or pwd): a value of its own there is most likely a password written into the recipe.
PASSWORD_FIELD = re.compile(r"pass|pwd|(?<![a-z])pw(?![a-z])|current-password|new-password", re.IGNORECASE)
# A segment of an address that changes from visit to visit: a number, or a token (16 and more
# letters and digits, with a digit).
_NUMBER = re.compile(r"^\d+$")
_TOKEN = re.compile(r"^(?=[^/]*\d)[\w~.-]{16,}$")
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
# How long a step "mail-code" waits for its message by default, and at most (s).
MAIL_CODE_WAIT = 180
MAIL_CODE_WAIT_MAX = 600
# A step's pause before it (s, at most), and its retries: how many more tries at most, the delay
# between them by default and at most (s).
PAUSE_MAX = 60
RETRY_TIMES_MAX = 10
RETRY_DELAY = 2
RETRY_DELAY_MAX = 60
# What names an element's attribute a value is read from.
ATTRIBUTE = re.compile(r"[A-Za-z_:][\w:.-]{0,60}$")


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
    fills, presses or types from e-mail, and the frame it does it in."""
    action = f"type the code from e-mail ({step.get('selector', '')})" if step.get("action") == "mail-code" else ""
    return (action or _action_label(step)) + (
        f" in frame {frame_host(step) or '(no address)'}" if step.get("frame") else ""
    )


def _action_label(step: dict) -> str:
    action, selector = step.get("action"), step.get("selector", "")
    text = str(step.get("text") or "").strip()
    labels = {
        "goto": f"open {step.get('url', '')}",
        "scroll": "scroll to load more",
        "click": f"click “{text}”" if text else f"click {selector}",
        "press": f"press {step.get('key', 'Enter')} in {selector}",
        "fill": f"fill in {step.get('credential') or 'a value'} ({selector})",
        "select": f"choose “{step.get('label') or step.get('value', '')}” in {selector}",
        "hover": f"hover over {selector}",
        "popup": "go on in the popup",
        "opener": "go back to the page that opened the popup",
    }
    return labels.get(str(action), str(action))


def _framed(step: dict, event: dict) -> dict:
    """The step with the frame the event happened in, if it was in one, and the alternative
    selectors recorder.js found its element by."""
    alternatives = event.get("alternatives")
    if isinstance(alternatives, list) and alternatives:
        step = step | {"alternatives": [str(value) for value in alternatives]}
    return step | {"frame": event["frame"]} if event.get("frame") else step


def url_pattern(url: str) -> str:
    """A pattern of the address a step led to, for its replay to wait for: the scheme, host and
    path, with numbers and tokens left open (they change from visit to visit), and any query or
    fragment."""
    parts = urlsplit(url)
    segments = [
        r"\d+" if _NUMBER.match(segment) else r"[^/]+" if _TOKEN.match(segment) else re.escape(segment)
        for segment in parts.path.split("/")
    ]
    return "^" + re.escape(f"{parts.scheme}://{parts.netloc}") + "/".join(segments) + r"(?:[?#].*)?$"


def link_waits(steps: list[dict]) -> None:
    """Gives each step that waits for an address the element of the step after it to wait for
    too, when that one acts on the page's own element and is required (an optional one's element
    may never come, a frame's is not the page's); a step after it removed or moved takes its
    element along."""
    for index, step in enumerate(steps):
        wait = step.get("wait")
        if not isinstance(wait, dict) or not wait.get("url"):
            continue
        after = steps[index + 1] if index + 1 < len(steps) else {}
        if after.get("action") in ON_ELEMENTS and after.get("selector") and not after.get("frame"):
            if not after.get("optional"):
                wait["selector"] = after["selector"]
                continue
        wait.pop("selector", None)


def _same_field(one: dict, other: dict) -> bool:
    return (one.get("selector"), one.get("frame")) == (other.get("selector"), other.get("frame"))


def _repeats(steps: list[dict], step: dict) -> bool:
    """Whether the fill does again what one since the last click or address did: the same field
    with the same value or credential (a field reported when Enter submitted it, and again as it
    lost the focus)."""
    for earlier in reversed(steps):
        if earlier.get("action") in ("goto", "click", "popup", "opener"):
            return False
        if earlier.get("action") == "fill" and _same_field(earlier, step):
            return (earlier.get("value"), earlier.get("credential")) == (step.get("value"), step.get("credential"))
    return False


def _fill(event: dict, steps: list[dict]) -> dict:
    """A fill's step. A password is never kept: its step fills in the job's password, and the field
    filled before it in the same form the job's username. A one-time code is not kept either: its
    step fills in the current code; a field that only might be one (a number of 6 to 8 digits, a
    field named code or challenge) is one after a password."""
    selector = event["selector"]
    after_password = any(step.get("credential") == "password" for step in steps)
    if event.get("otp") or (event.get("code_like") and after_password):
        # The code typed while recording is spent: the job fills in the current one.
        return {"action": "fill", "selector": selector, "credential": "totp"}
    if event.get("field") != "password":
        return {"action": "fill", "selector": selector, "value": event.get("value", "")}
    for earlier in reversed(steps):
        same_form = (earlier.get("form"), earlier.get("frame")) == (event.get("form"), event.get("frame"))
        if earlier["action"] == "fill" and "value" in earlier and same_form:
            earlier.pop("value")
            earlier["credential"] = "username"
            break
    return {"action": "fill", "selector": selector, "credential": "password"}


def _step(event: dict, steps: list[dict]) -> dict | None:
    """The event's step, without its frame (None: an event that is no step)."""
    action = event.get("action")
    if action == "goto":
        return {"action": "goto", "url": event["url"]}
    if action in ("scroll", "popup", "opener"):
        return {"action": action}
    if action == "fill":
        return _fill(event, steps) | {"form": event.get("form", "")}
    if action == "select":
        return {"action": "select", "selector": event["selector"], "value": event.get("value", "")} | (
            {"label": event["label"]} if event.get("label") else {}
        )
    if action == "click":
        step = {"action": "click", "selector": event["selector"], "text": event.get("text", "")}
    elif action == "press":
        step = {"action": "press", "selector": event["selector"], "key": event.get("key", "Enter")}
    else:
        return None
    if event.get("navigated"):
        step["wait"] = {"url": url_pattern(event["navigated"])}
    return step


def steps_from_events(
    events: list[dict], before: list[dict] | None = None, after: list[dict] | None = None, forms: bool = False
) -> list[dict]:
    """Turns what the recorder saw into the recipe's steps, between the steps before and after (a
    copy of each).

    Typing into a field several times keeps the last value, and a fill that repeats one since the
    last click goes (_repeats); a password or one-time code is never kept (_fill). A click on an
    element that showed only under the mouse comes after a step "hover" over its menu; a click or
    press that navigated waits for the address it led to (link_waits). An event inside a frame
    keeps its frame's path. forms keeps each fill's form (the recorder's own steps, for a password
    recorded later to find its username), else it is left out.
    """
    steps: list[dict] = copy.deepcopy(before or [])
    for event in events:
        step = _step(event, steps)
        if step is None:
            continue
        step = _framed(step, event)
        last = steps[-1] if steps else {}
        if event.get("hover") and step["action"] == "click":
            # The menu's own selector only: the click's alternatives are the clicked element's.
            hover = {"action": "hover", "selector": event["hover"]} | (
                {"frame": event["frame"]} if event.get("frame") else {}
            )
            if last != hover:
                steps.append(hover)
        if step["action"] in ("fill", "select") and last.get("action") == step["action"] and _same_field(last, step):
            steps[-1] = step
        elif not (step["action"] == "fill" and _repeats(steps, step)):
            steps.append(step)
    steps += copy.deepcopy(after or [])
    link_waits(steps)
    if not forms:
        for step in steps:
            step.pop("form", None)
    return steps


def sign_in_steps(steps: list[dict]) -> int:
    """How many of the steps sign in: those up to the last fill of a credential and the first
    click or key after it (which sends the form), and the return from a sign-in's popup; 0
    without a credential (no sign-in, only the way to the listing). The steps after them lead
    to the listing and are always replayed."""
    last = max((index for index, step in enumerate(steps) if step.get("credential")), default=-1)
    if last < 0:
        return 0
    end = next(
        (index + 1 for index in range(last + 1, len(steps)) if steps[index].get("action") in ("click", "press")),
        last + 1,
    )
    # A sign-in in a popup ends back in the page that opened it.
    while end < len(steps) and steps[end].get("action") == "opener":
        end += 1
    return end


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


def leaked_passwords(recipe: object) -> list[str]:
    """What the recipe's steps say of a password in the recipe: a fill with a value of its own in a
    field that looks like a password's (PASSWORD_FIELD), recorded before the recorder knew it. Never
    with the value."""
    steps = recipe.get("steps") if isinstance(recipe, dict) else None
    found = []
    for number, step in enumerate(steps if isinstance(steps, list) else [], 1):
        if not isinstance(step, dict) or step.get("action") != "fill" or step.get("credential"):
            continue
        selector = str(step.get("selector") or "")
        if step.get("value") and PASSWORD_FIELD.search(selector):
            found.append(
                f"step {number} fills a value of its own into {selector}, which looks like a password field: "
                "mark it as the account's password (the recorder's Steps), and change the password if the "
                "recipe was shared"
            )
    return found


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


def _check_next(after: object, depth: int) -> list[str]:
    # A next without its element has always meant none (the walk ends after the first page).
    if not isinstance(after, dict) or not after.get("selector"):
        return [] if not after or isinstance(after, dict) else ["next page: not an object"]
    if not isinstance(after["selector"], str):
        return ["next page: the element (selector) is text"]
    if after.get("kind", "link") not in NEXT_KINDS:
        return [f"next page: unknown kind {after.get('kind')}"]
    level = after.get("level", 0)
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= depth:
        return [f"next page: the level is a number from 0 (the listing) to {depth} (the file links' pages)"]
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
        # Values read from the page count too, a date's year, month and day with them.
        for value in (level.get("values") or []) if isinstance(level, dict) else []:
            if isinstance(value, dict):
                groups |= {str(value.get("name"))} | (
                    {"year", "month", "day"} if value.get("type") == "date" else set()
                )
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


def _wait_ok(wait: object) -> bool:
    if not isinstance(wait, dict) or not all(isinstance(wait.get(key, ""), str) for key in ("url", "selector")):
        return False
    return not _check_pattern("", wait.get("url", ""))


def _mail_code_problem(step: dict) -> str:
    """What is wrong with a step "mail-code", or ""."""
    for key in ("from", "subject", "regex"):
        try:
            re.compile(str(step.get(key) or ""))
        except re.error as error:
            return f"the {key} pattern is not a regular expression ({error})"
    wait = step.get("wait", MAIL_CODE_WAIT)
    if not isinstance(wait, int) or isinstance(wait, bool) or not 10 <= wait <= MAIL_CODE_WAIT_MAX:
        return f"the wait for the e-mail from 10 to {MAIL_CODE_WAIT_MAX} seconds"
    return ""


def _seconds(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _timing_problem(step: dict) -> str:
    """What is wrong with a step's pause or retry, or ""."""
    if "pause" in step and not (_seconds(step["pause"]) and 0 < step["pause"] <= PAUSE_MAX):
        return f"the pause is a number of seconds above 0, at most {PAUSE_MAX}"
    if "retry" in step:
        retry = step["retry"]
        times = retry.get("times") if isinstance(retry, dict) else None
        delay = retry.get("delay", RETRY_DELAY) if isinstance(retry, dict) else None
        if (
            not isinstance(retry, dict)
            or set(retry) - {"times", "delay"}
            or not (isinstance(times, int) and not isinstance(times, bool) and 1 <= times <= RETRY_TIMES_MAX)
            or not (_seconds(delay) and 0 <= delay <= RETRY_DELAY_MAX)  # type: ignore[operator]
        ):
            return f'retry is {{"times": 1 to {RETRY_TIMES_MAX}, "delay": seconds from 0 to {RETRY_DELAY_MAX}}}'
    return ""


def step_pause(step: dict) -> float:
    """How long the replay waits before the step (s; 0: not at all)."""
    pause = step.get("pause")
    return float(pause) if _seconds(pause) and 0 < pause <= PAUSE_MAX else 0.0  # type: ignore[operator]


def step_retry(step: dict) -> tuple[int, float]:
    """How many more times the replay tries a step that failed, and the delay before each (s)."""
    if "retry" not in step or _timing_problem({"retry": step["retry"]}):
        return 0, 0.0
    return int(step["retry"]["times"]), float(step["retry"].get("delay", RETRY_DELAY))


def _alternatives_ok(spec: dict) -> bool:
    alternatives = spec.get("alternatives", [])
    return isinstance(alternatives, list) and all(isinstance(value, str) and value for value in alternatives)


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
        elif step["action"] not in ("goto", "scroll", *SWITCHES) and not step.get("selector"):
            problems.append(f"step {number}: no element")
        elif step["action"] != "mail-code" and "wait" in step and not _wait_ok(step["wait"]):
            problems.append(f"step {number}: wait is an address's pattern (url) and an element (selector)")
        elif not _alternatives_ok(step):
            problems.append(f"step {number}: the alternatives are a list of selectors")
        elif step["action"] == "mail-code" and (problem := _mail_code_problem(step)):
            problems.append(f"step {number}: {problem}")
        elif problem := _timing_problem(step):
            problems.append(f"step {number}: {problem}")
        else:
            problems += _check_fields(number, step)
    return problems


def _check_values(where: str, values: object, json_items: bool = False) -> list[str]:
    """The problems of a level's values from the page (or of a JSON listing's items)."""
    if values in (None, []):
        return []
    if not isinstance(values, list) or len(values) > patterns.MAX_VALUES:
        return [f"{where}: the values are a list of at most {patterns.MAX_VALUES}"]
    problems: list[str] = []
    for value in values:
        place = ("field",) if json_items else ("closest", "selector", "attribute")
        if not isinstance(value, dict) or not patterns.VALUE_NAME.match(str(value.get("name", ""))):
            problems.append(f"{where}: a value without a name of letters, digits and _")
        elif value.get("type", "text") not in patterns.VALUE_TYPES:
            problems.append(f"{where}: the value {value['name']} is of an unknown type {value.get('type')}")
        elif not all(isinstance(value.get(key, ""), str) for key in (*place, "regex")):
            problems.append(f"{where}: the value {value['name']}'s {', '.join(place)} and regex are text")
        elif json_items and not value.get("field"):
            problems.append(f"{where}: the value {value['name']} has no field")
        elif value.get("attribute") and not ATTRIBUTE.match(value["attribute"]):
            problems.append(f"{where}: the value {value['name']}'s attribute is no attribute's name")
        else:
            problems += _check_pattern(f"{where}: the value {value['name']}", value.get("regex", ""))
    return problems


def _check_json(links: dict) -> list[str]:
    """The problems of file links of kind json."""
    # The address may name the walk's values ({year}): it is checked with them filled in.
    url = re.sub(r"\{[^{}]*\}", "0", str(links.get("url") or ""))
    if not allowed_url(url):
        return [f"file links: the JSON listing {links.get('url') or '(none)'} may not be opened"]
    if not isinstance(links.get("items", ""), str) or not isinstance(links.get("text", ""), str):
        return ["file links: the JSON's items and text are paths"]
    if not links.get("file") or not isinstance(links["file"], str):
        return ["file links: no field with the files' addresses in the JSON's items"]
    page = links.get("page")
    if page is not None and not (
        isinstance(page, dict)
        and isinstance(page.get("param"), str)
        and page["param"]
        and all(
            isinstance(page.get(key, 1), int) and not isinstance(page.get(key, 1), bool) for key in ("first", "step")
        )
        and page.get("first", 1) >= 0
        and page.get("step", 1) >= 1
    ):
        return ["file links: the JSON's pages are a parameter with its first value (0 or more) and step (1 or more)"]
    return _check_pattern("file links", links.get("pattern", "")) + _check_values(
        "file links", links.get("values"), True
    )


def _check_viewport(viewport: object) -> list[str]:
    if viewport is None:
        return []
    if not isinstance(viewport, dict) or not all(
        isinstance(viewport.get(key), int) and VIEWPORT_MIN[key] <= viewport[key] <= VIEWPORT_MAX[key]
        for key in ("width", "height")
    ):
        return [
            f"the window's size: width and height from {VIEWPORT_MIN['width']} × {VIEWPORT_MIN['height']} "
            f"to {VIEWPORT_MAX['width']} × {VIEWPORT_MAX['height']}"
        ]
    return []


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
        if isinstance(spec, dict):
            problems += _check_values(f"level {level}", spec.get("values"))
    links = recipe.get("links")
    problems += _check_links(links, recipe)
    picks = [("file links", links), ("next page", recipe.get("next"))]
    picks += [(f"level {level}", spec) for level, spec in enumerate(follow, 1)]
    for where, spec in picks:
        if isinstance(spec, dict) and not _alternatives_ok(spec):
            problems.append(f"{where}: the alternatives are a list of selectors")
    return problems + _check_next(recipe.get("next"), len(follow)) + _check_viewport(recipe.get("viewport"))


def _check_links(links: object, recipe: dict) -> list[str]:
    """The file links: a JSON listing's parts, or a level's with its values, key and click."""
    if isinstance(links, dict) and links.get("kind") == "json":
        return _check_json(links)
    problems = _check_level("file links", links)
    if isinstance(links, dict):
        problems += _check_values("file links", links.get("values"))
        problems += _check_key(links, recipe)
        if not isinstance(links.get("click", False), bool):
            problems.append("file links: click is true or false")
    return problems


def levels(recipe: dict) -> list[dict]:
    """The recipe's levels in the walk's order: those to follow, then the file links."""
    return [spec for spec in [*(recipe.get("follow") or []), recipe.get("links")] if isinstance(spec, dict)]


def token_warnings(recipe: dict) -> list[str]:
    """The recipe's addresses that carry a token of the visit they were recorded in (a sign-in's
    code or ticket, a session, a signature: harvester.patterns.token_parameter), which a job's
    run will likely find spent: its steps' and its listing's."""
    warnings = []
    for number, step in enumerate(recipe.get("steps") or [], 1):
        names = token_parameters(str(step.get("url") or "")) if isinstance(step, dict) else []
        if names and step.get("action") == "goto":
            warnings.append(
                f"step {number} opens an address with {', '.join(names)}: a token of this visit, likely spent "
                "when the job runs. Record the clicks that lead there instead (undo the step)."
            )
    names = token_parameters(str(recipe.get("start") or ""))
    if names:
        warnings.append(
            f"the listing's address carries {', '.join(names)}: a token of this visit, likely spent when the job "
            "runs. Pick on the page the steps lead to, without browsing away first: the job starts where its "
            "steps end then."
        )
    return warnings


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


def edit_step(step: dict, message: dict) -> None:
    """The step as the recorder's Steps list changes it (a message of the page): optional or not;
    a fill's account's username, password or one-time code, or a value of its own ("" credential);
    a value of its own (a fill's or a list's choice); no wait for the address it led to (wait:
    null); a pause before it and its retries (_edit_timing)."""
    if "optional" in message:
        if message["optional"] is True:
            step["optional"] = True
        else:
            step.pop("optional", None)
    if step.get("action") == "fill" and "credential" in message:
        if message["credential"] in CREDENTIALS:
            step.pop("value", None)
            step["credential"] = message["credential"]
        elif not message["credential"]:
            step.pop("credential", None)
            step.setdefault("value", "")
    if "value" in message and isinstance(message["value"], str) and not step.get("credential"):
        if step.get("action") in ("fill", "select"):
            step["value"] = message["value"]
            # A list's choice by the value given now, not by the text the old one showed.
            step.pop("label", None)
    if "wait" in message and message["wait"] is None:
        step.pop("wait", None)
    _edit_timing(step, message)


def _edit_timing(step: dict, message: dict) -> None:
    """The step's pause and retry as the message sets them (null: none); a value out of bounds
    changes nothing."""
    if "pause" in message:
        if message["pause"] is None:
            step.pop("pause", None)
        elif not _timing_problem({"pause": message["pause"]}):
            step["pause"] = message["pause"]
    if "retry" in message:
        if message["retry"] is None:
            step.pop("retry", None)
        elif not _timing_problem({"retry": message["retry"]}):
            step["retry"] = dict(message["retry"])
