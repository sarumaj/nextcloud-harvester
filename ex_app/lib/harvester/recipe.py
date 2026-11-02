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

import re
from urllib.parse import urlsplit

from . import patterns
from .browser import VIEWPORT_MAX, VIEWPORT_MIN, allowed_url
from .patterns import token_parameters

ACTIONS = {"goto", "click", "fill", "press", "scroll", "mail-code"}
# A scroll step's rounds: by default, and at most.
SCROLL_ROUNDS = 20
SCROLL_ROUNDS_MAX = 100
NEXT_KINDS = ("link", "number")
# The actions that act on an element, and so may do it inside a frame.
ON_ELEMENTS = ("click", "fill", "press", "mail-code")
# How long a step "mail-code" waits for its message by default, and at most (s).
MAIL_CODE_WAIT = 180
MAIL_CODE_WAIT_MAX = 600
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
    """The step with the frame the event happened in, if it was in one, and the alternative
    selectors recorder.js found its element by."""
    alternatives = event.get("alternatives")
    if isinstance(alternatives, list) and alternatives:
        step = step | {"alternatives": [str(value) for value in alternatives]}
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
        elif step["action"] not in ("goto", "scroll") and not step.get("selector"):
            problems.append(f"step {number}: no element")
        elif not _alternatives_ok(step):
            problems.append(f"step {number}: the alternatives are a list of selectors")
        elif step["action"] == "mail-code" and (problem := _mail_code_problem(step)):
            problems.append(f"step {number}: {problem}")
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
    if start and not allowed_url(start):
        problems.append(f"the listing {start} may not be opened")
    if not start and not any(isinstance(s, dict) and s.get("action") == "goto" for s in steps):
        problems.append("no listing to start at")
    for level, follow in enumerate(recipe.get("follow") or [], 1):
        if not isinstance(follow, dict) or not follow.get("selector"):
            problems.append(f"level {level}: no links to follow")
        else:
            problems += _check_pattern(f"level {level}", follow.get("pattern", ""))
            problems += _check_values(f"level {level}", follow.get("values"))
    links = recipe.get("links")
    if isinstance(links, dict) and links.get("kind") == "json":
        problems += _check_json(links)
    elif not isinstance(links, dict) or not links.get("selector"):
        problems.append("no file links picked")
    else:
        problems += _check_pattern("file links", links.get("pattern", ""))
        problems += _check_values("file links", links.get("values"))
    picks = [("file links", links), ("next page", recipe.get("next"))]
    picks += [(f"level {level}", follow) for level, follow in enumerate(recipe.get("follow") or [], 1)]
    for where, spec in picks:
        if isinstance(spec, dict) and not _alternatives_ok(spec):
            problems.append(f"{where}: the alternatives are a list of selectors")
    return problems + _check_next(recipe.get("next")) + _check_viewport(recipe.get("viewport"))


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
