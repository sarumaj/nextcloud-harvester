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
"""

import re

from .browser import allowed_url

ACTIONS = {"goto", "click", "fill", "press", "scroll"}
# A scroll step's rounds: by default, and at most.
SCROLL_ROUNDS = 20
SCROLL_ROUNDS_MAX = 100
NEXT_KINDS = ("link", "number")


def step_label(step: dict) -> str:
    """A step in a few words, for a log line: what it opens, clicks (its text, else its element),
    fills or presses."""
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


def steps_from_events(events: list[dict]) -> list[dict]:  # pylint: disable=too-many-branches
    """Turns what the recorder saw into the recipe's steps.

    Typing into a field several times keeps the last value. A password is never kept: its step
    fills in the job's password, and the field filled before it in the same form the job's
    username, so neither is in the recipe. A one-time code is not kept either: its step fills in
    the current code from the account's secret.
    """
    steps: list[dict] = []
    for event in events:
        action = event.get("action")
        if action == "goto":
            steps.append({"action": "goto", "url": event["url"]})
        elif action == "scroll":
            steps.append({"action": "scroll"})
        elif action == "click":
            steps.append({"action": "click", "selector": event["selector"], "text": event.get("text", "")})
        elif action == "press":
            steps.append({"action": "press", "selector": event["selector"], "key": event.get("key", "Enter")})
        elif action == "fill":
            step = {"action": "fill", "selector": event["selector"], "value": event.get("value", "")}
            if event.get("otp"):
                # The code typed while recording is spent: the job fills in the current one.
                step = {"action": "fill", "selector": event["selector"], "credential": "totp"}
            elif event.get("field") == "password":
                step = {"action": "fill", "selector": event["selector"], "credential": "password"}
                for earlier in reversed(steps):
                    if earlier["action"] == "fill" and "value" in earlier and earlier.get("form") == event.get("form"):
                        earlier.pop("value")
                        earlier["credential"] = "username"
                        break
            step["form"] = event.get("form", "")
            if steps and steps[-1]["action"] == "fill" and steps[-1]["selector"] == step["selector"]:
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


def _check_steps(steps: list) -> list[str]:
    problems = []
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get("action") not in ACTIONS:
            problems.append(f"step {number}: unknown action {step.get('action') if isinstance(step, dict) else step}")
        elif step["action"] == "goto" and not allowed_url(str(step.get("url", ""))):
            problems.append(f"step {number}: the browser may not open {step.get('url')}")
        elif step["action"] == "scroll" and not _rounds_ok(step.get("rounds", SCROLL_ROUNDS)):
            problems.append(f"step {number}: rounds of scrolling from 1 to {SCROLL_ROUNDS_MAX}")
        elif step["action"] not in ("goto", "scroll") and not step.get("selector"):
            problems.append(f"step {number}: no element")
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
    return problems + _check_next(recipe.get("next"))


def uses_browser(recipe: dict) -> bool:
    """Whether the recipe needs the browser: it has steps to replay, or numbered pages that are
    buttons (clicked, no address to fetch)."""
    after = recipe.get("next") or {}
    numbered_buttons = isinstance(after, dict) and after.get("kind") == "number" and bool(after.get("buttons"))
    return bool(recipe.get("steps")) or numbered_buttons
