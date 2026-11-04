# SPDX-License-Identifier: AGPL-3.0-or-later
"""The kinds of step other recorders have that a recipe has too (harvester.recipe checks them,
harvester.sources.steps replays them):

- A click may be another than one with the left button: "button": "right" (a context menu) or
  "middle", and "count": 2 (a double click) or 3.
- A click or a press may answer the dialog it opens (alert, confirm, prompt, "Leave the
  page?"): "dialog": "accept" or "dismiss", and "prompt": the text a prompt is answered with;
  without, a dialog is dismissed, as it always was.
- A fill with "typed": true types its text key by key (a field that ignores a value set at once,
  a code split into boxes) instead of setting it.
- A press without an element presses its key on the page (the element that has the focus:
  Escape on a dialog of the site's).
- A step {"action": "wait", "selector": ..., "state": "visible" (or "hidden"), "text": ...,
  "timeout": seconds} waits until its element shows (with that text in it, if given) or is gone,
  WAIT_TIMEOUT seconds by default and WAIT_MAX at most: the page answered a search, a "please
  wait" went away. It fails as a step does when the time is up (an optional one goes on).

What the recorder sees of them (merged): a double click's dblclick makes the two clicks it began
with one click of count 2; a dialog the recorder answered (OK) is the answer of the click or key
before it.
"""

# A click's buttons and how many clicks it may be; what a dialog a step opens is answered with.
BUTTONS = ("left", "right", "middle")
CLICK_COUNT_MAX = 3
DIALOG_ANSWERS = ("accept", "dismiss")
# How long a step "wait" waits by default, and at most (s); the states it waits for.
WAIT_TIMEOUT = 30
WAIT_MAX = 300
WAIT_STATES = ("visible", "hidden")


def click_label(step: dict, text: str, selector: str) -> str:
    """A click in a few words (recipe.step_label): its kind, its text or else its element."""
    kind = {(1, "right"): "right-click", (2, "left"): "double-click"}.get(
        (int(step.get("count") or 1), str(step.get("button") or "left")), "click"
    )
    return f"{kind} “{text}”" if text else f"{kind} {selector}"


def wait_label(step: dict, selector: str) -> str:
    """A step "wait" in a few words (recipe.step_label)."""
    went = "go" if step.get("state") == "hidden" else "show"
    return f"wait for {selector} to {went}" + (f" with “{step['text']}”" if step.get("text") else "")


def _count_ok(value: object, highest: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= highest


def options_problem(step: dict) -> str:  # pylint: disable=too-many-return-statements
    """What is wrong with a click's button and count, a fill's typing, or the answer to a dialog
    a click or a press opens, or ""."""
    action = step["action"]
    if ("button" in step or "count" in step) and action != "click":
        return "only a click has a button and a count"
    if "button" in step and step["button"] not in BUTTONS:
        return f"the button is one of {', '.join(BUTTONS)}"
    if "count" in step and not _count_ok(step["count"], CLICK_COUNT_MAX):
        return f"the count of clicks is 1 to {CLICK_COUNT_MAX}"
    if action == "press" and step.get("frame") and not step.get("selector"):
        return "a key pressed on the page has no frame (it goes to the element in focus)"
    if "typed" in step and (action != "fill" or not isinstance(step["typed"], bool)):
        return "typed is true or false, for a fill"
    if ("dialog" in step or "prompt" in step) and action not in ("click", "press"):
        return "only a click or a press answers a dialog"
    if "dialog" in step and step["dialog"] not in DIALOG_ANSWERS:
        return "the dialog is answered accept or dismiss"
    if "prompt" in step and not isinstance(step["prompt"], str):
        return "the prompt's answer is text"
    return ""


def wait_step_problem(step: dict) -> str:
    """What is wrong with a step "wait", or ""."""
    if step.get("state", "visible") not in WAIT_STATES:
        return "the state waited for is visible or hidden"
    if not isinstance(step.get("text", ""), str):
        return "the text waited for is text"
    if not _count_ok(step.get("timeout", WAIT_TIMEOUT), WAIT_MAX):
        return f"the wait from 1 to {WAIT_MAX} seconds"
    return ""


def wait_timeout(step: dict) -> float:
    """How long a step "wait" waits (s)."""
    timeout = step.get("timeout", WAIT_TIMEOUT)
    return float(timeout) if _count_ok(timeout, WAIT_MAX) else float(WAIT_TIMEOUT)


def _kept(step: dict, key: str, value: object) -> None:
    """The step's key set to the value, or gone for none (False, "", None)."""
    if value:
        step[key] = value
    else:
        step.pop(key, None)


def edit_options(step: dict, message: dict) -> None:
    """A click's kind (click, double-click, right-click), a fill typed key by key, the answer to a
    dialog a click or a press opens (null: dismissed, as by default), a wait's state, text and
    time, as the Steps list sets them; what does not fit the step changes nothing."""
    action = step.get("action")
    if action == "click" and message.get("click") in ("click", "double", "right"):
        _kept(step, "count", 2 if message["click"] == "double" else None)
        _kept(step, "button", "right" if message["click"] == "right" else None)
    if action == "fill" and isinstance(message.get("typed"), bool):
        _kept(step, "typed", message["typed"])
    if action in ("click", "press") and (
        message.get("dialog") in DIALOG_ANSWERS or "dialog" in message and message["dialog"] is None
    ):
        _kept(step, "dialog", message["dialog"])
        if step.get("dialog") != "accept":
            step.pop("prompt", None)
    if action in ("click", "press") and isinstance(message.get("prompt"), str) and step.get("dialog") == "accept":
        _kept(step, "prompt", message["prompt"])
    if action == "wait":
        if message.get("state") in WAIT_STATES:
            step["state"] = message["state"]
        if isinstance(message.get("text"), str):
            _kept(step, "text", message["text"])
        if _count_ok(message.get("timeout"), WAIT_MAX):
            step["timeout"] = message["timeout"]


def merged(event: dict, steps: list[dict]) -> bool:
    """An event that changes the step before rather than making one: a double click (the two
    clicks it began with become one click of count 2), the dialog a click or a press opened (the
    step answers it as the person did); whether it was such an event."""
    action = event.get("action")
    last = steps[-1] if steps else {}
    if action == "dblclick":
        if (
            len(steps) >= 2
            and all(step.get("action") == "click" for step in steps[-2:])
            and steps[-1].get("selector") == steps[-2].get("selector") == event.get("selector")
            and steps[-1].get("frame") == steps[-2].get("frame")
        ):
            steps.pop()
            steps[-1]["count"] = 2
            steps[-1].pop("wait", None)
        return True
    if action == "dialog":
        if last.get("action") in ("click", "press"):
            last["dialog"] = "accept" if event.get("accept", True) else "dismiss"
            if last["dialog"] == "accept" and event.get("prompt"):
                last["prompt"] = str(event["prompt"])
        return True
    return False
