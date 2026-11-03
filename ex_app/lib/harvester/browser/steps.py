# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's steps: those before what is recorded now (the form's, and each edit's), the
events recorded since (made steps by recipe.steps_from_events), and the steps after them (new
steps go in after step "after" of the page's record-at, before the later ones); the Steps list's
edits, and the click or press just recorded, whose page may navigate (its address to wait for).
"""

import copy
import time

from ..choices import CHOICES_MAX
from ..needs import LABEL_MAX
from ..recipe import edit_step, steps_from_events

# The page's messages that change the steps.
EDITS = frozenset(
    {"undo", "optional", "step-delete", "step-move", "step-edit", "record-at", "mail-code", "ask-code", "each"}
)
# How long after a click or a press a navigation of the page counts as its (s): a page that
# reloads itself much later is none of the step's doing.
NAVIGATION_SECONDS = 5


class StepList:
    """One recording's steps."""

    def __init__(self) -> None:
        self.before: list[dict] = []
        self.events: list[dict] = []
        self.after: list[dict] = []
        self.navigating: dict | None = None

    def all(self, forms: bool = False) -> list[dict]:
        """The steps: those before, the recorded ones and those after; with each fill's form (the
        recorder's own, for a password recorded later to find its username) or without (the
        recipe's)."""
        return steps_from_events(self.events, self.before, self.after, forms)

    def record(self, event: dict) -> None:
        """A step's event, after the steps so far; a click or press may navigate the page."""
        self.events.append(event)
        self.navigating = event if event.get("action") in ("click", "press") else None
        if self.navigating is not None:
            self.navigating["at"] = time.monotonic()

    def navigation(self) -> dict | None:
        """The click or press a navigation of the page just now comes from, if any."""
        event = self.navigating
        return event if event is not None and time.monotonic() - event.get("at", 0) < NAVIGATION_SECONDS else None

    def load(self, steps: object) -> None:
        """The form's steps (a list of steps), to edit or record more after."""
        if isinstance(steps, list) and all(isinstance(step, dict) for step in steps):
            self.before, self.after, self.events = copy.deepcopy(steps), [], []

    def _commit(self) -> list[dict]:
        """The recorded events as steps, before an edit (which acts on steps): all of them."""
        steps = self.all(forms=True)
        point = len(steps) - len(self.after)
        self.before, self.after, self.events = steps[:point], steps[point:], []
        self.navigating = None
        return steps

    def edit(self, kind: str, message: dict) -> bool:
        """Undo (the last event, else the last step before the new ones), and the Steps list's
        edits: a step removed, moved, changed (optional, what a fill fills in, its value, its wait),
        where new steps go (record-at), a filled code that comes by e-mail (mail-code) or that the
        person reads on their phone (ask-code), and a list's choice repeated for each of its options
        (each); whether the steps changed."""
        if kind == "undo":
            if self.events:
                self.events.pop()
            elif self.before:
                self.before.pop()
            return True
        steps, point = self._commit(), len(self.before)
        if kind == "record-at":
            after = message.get("after")
            if not isinstance(after, int) or isinstance(after, bool) or not 0 <= after <= len(steps):
                return False
            self.before, self.after = steps[:after], [] if message.get("replace") else steps[after:]
            return True
        index = message.get("step")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(steps):
            return False
        if kind in CHANGES:
            steps[index] = CHANGES[kind](steps[index], message)
        elif kind == "step-delete":
            del steps[index]
            point -= 1 if index < point else 0
        elif kind == "step-move":
            other = index + (1 if message.get("by") == 1 else -1)
            if not 0 <= other < len(steps):
                return False
            steps[index], steps[other] = steps[other], steps[index]
        else:
            edit_step(steps[index], message)
        self.before, self.after = steps[:point], steps[point:]
        return True


def mail_code(step: dict, message: dict) -> dict:
    """A filled code's step as "type the code from e-mail" of a sender and subject (and the code's
    pattern), or with off back to the current one-time code; another step as it is."""
    kept = {key: step[key] for key in ("selector", "alternatives", "frame", "optional") if key in step}
    if message.get("off") and step.get("action") == "mail-code":
        return {"action": "fill", **kept, "credential": "totp"}
    if message.get("off") or step.get("action") not in ("fill", "mail-code"):
        return step
    code = {key: str(message.get(key) or "") for key in ("from", "subject")}
    return {"action": "mail-code", **kept, **code} | ({"regex": str(message["regex"])} if message.get("regex") else {})


def ask_code(step: dict, message: dict) -> dict:
    """A filled code's step (or one from e-mail) as "the code from the phone" of a label, which
    the job asks its person for when it runs (harvester.needs); with off back to the current
    one-time code; another step as it is."""
    kept = {key: step[key] for key in ("selector", "alternatives", "frame", "optional") if key in step}
    if message.get("off") and step.get("action") == "ask-code":
        return {"action": "fill", **kept, "credential": "totp"}
    if message.get("off") or step.get("action") not in ("fill", "mail-code", "ask-code"):
        return step
    text = str(message.get("label") or "").strip()[:LABEL_MAX] or "SMS code"
    return {"action": "ask-code", **kept, "label": text}


def each(step: dict, message: dict) -> dict:
    """A list's choice (a step "select") as the step "each" that goes through every option
    (harvester.choices); a step "each" with the message's values (a list of texts, or "options")
    and pattern on the options' texts. Another step as it is."""
    kept = {key: step[key] for key in ("selector", "alternatives", "frame") if key in step}
    if step.get("action") == "select":
        return {"action": "each", **kept, "values": "options"}
    if step.get("action") != "each":
        return step
    changed = dict(step)
    values = message.get("values")
    if values == "options" or (isinstance(values, list) and not values):
        changed["values"] = "options"
    elif isinstance(values, list) and all(isinstance(value, str) for value in values):
        changed["values"] = [value.strip() for value in values if value.strip()][:CHOICES_MAX] or "options"
    if isinstance(message.get("pattern"), str):
        changed["pattern"] = message["pattern"]
        if not changed["pattern"]:
            changed.pop("pattern")
    return changed


# The edits that make a step another: a code from e-mail or the phone, choices.
CHANGES = {"mail-code": mail_code, "ask-code": ask_code, "each": each}
