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

A step {"action": "select", "selector": ..., "value": ..., "label": ...} chooses an entry of a
list (<select>): by its value, else by the text it shows. {"action": "hover", "selector": ...}
moves the mouse over an element (a menu that opens under the mouse) before the click after it.
{"action": "popup"} goes on in the tab or window the step before it opened (a sign-in's popup),
{"action": "opener"} back in the page that opened it, once the popup closed itself.

A click or a press that navigated has "wait": {"url": <a pattern of the address it led to>,
"selector": <the next step's element>}: the replay waits for that address and that element
instead of for the network to go quiet, then settles a short while. Without it, as recipes
always did, it waits for the network.
"""

import copy
import re
from urllib.parse import urlsplit

from .browser import allowed_url
from .patterns import token_parameters

ACTIONS = {"goto", "click", "fill", "press", "scroll", "select", "hover", "popup", "opener"}
# A scroll step's rounds: by default, and at most.
SCROLL_ROUNDS = 20
SCROLL_ROUNDS_MAX = 100
NEXT_KINDS = ("link", "number")
# The actions that act on an element, and so may do it inside a frame.
ON_ELEMENTS = ("click", "fill", "press", "select", "hover")
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


def _check_pattern(where: str, pattern: str) -> list[str]:
    try:
        re.compile(pattern)
    except re.error as error:
        return [f"{where}: the pattern is not a regular expression ({error})"]
    return []


def _rounds_ok(rounds: object) -> bool:
    return isinstance(rounds, int) and not isinstance(rounds, bool) and 1 <= rounds <= SCROLL_ROUNDS_MAX


def _check_next(after: object) -> list[str]:
    # A next without its element has always meant none (the walk ends after the first page).
    if not isinstance(after, dict) or not after.get("selector"):
        return [] if not after or isinstance(after, dict) else ["next page: not an object"]
    if after.get("kind", "link") not in NEXT_KINDS:
        return [f"next page: unknown kind {after.get('kind')}"]
    return []


def _frame_ok(frames: object) -> bool:
    return isinstance(frames, list) and all(
        isinstance(entry, dict)
        and all(isinstance(entry.get(key, ""), str) for key in ("selector", "url", "name"))
        and any(entry.get(key) for key in ("selector", "url", "name"))
        for entry in frames
    )


def _wait_ok(wait: object) -> bool:
    if not isinstance(wait, dict) or not all(isinstance(wait.get(key, ""), str) for key in ("url", "selector")):
        return False
    return not _check_pattern("", wait.get("url", ""))


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
        elif "wait" in step and not _wait_ok(step["wait"]):
            problems.append(f"step {number}: wait is an address's pattern (url) and an element (selector)")
        elif not _alternatives_ok(step):
            problems.append(f"step {number}: the alternatives are a list of selectors")
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
    if start and not allowed_url(start):
        problems.append(f"the listing {start} may not be opened")
    if not start and not any(isinstance(s, dict) and s.get("action") == "goto" for s in steps):
        problems.append("no listing to start at")
    for level, follow in enumerate(recipe.get("follow") or [], 1):
        if not isinstance(follow, dict) or not follow.get("selector"):
            problems.append(f"level {level}: no links to follow")
        else:
            problems += _check_pattern(f"level {level}", follow.get("pattern", ""))
    links = recipe.get("links")
    if not isinstance(links, dict) or not links.get("selector"):
        problems.append("no file links picked")
    else:
        problems += _check_pattern("file links", links.get("pattern", ""))
    picks = [("file links", links), ("next page", recipe.get("next"))]
    picks += [(f"level {level}", follow) for level, follow in enumerate(recipe.get("follow") or [], 1)]
    for where, spec in picks:
        if isinstance(spec, dict) and not _alternatives_ok(spec):
            problems.append(f"{where}: the alternatives are a list of selectors")
    return problems + _check_next(recipe.get("next"))


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
    """Whether the recipe needs the browser: it has steps to replay, or numbered pages that are
    buttons (clicked, no address to fetch)."""
    after = recipe.get("next") or {}
    numbered_buttons = isinstance(after, dict) and after.get("kind") == "number" and bool(after.get("buttons"))
    return bool(recipe.get("steps")) or numbered_buttons


def edit_step(step: dict, message: dict) -> None:
    """The step as the recorder's Steps list changes it (a message of the page): optional or not;
    a fill's account's username, password or one-time code, or a value of its own ("" credential);
    a value of its own (a fill's or a list's choice); no wait for the address it led to (wait:
    null)."""
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
