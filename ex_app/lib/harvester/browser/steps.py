# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorder's steps: those before what is recorded now (the form's, and each edit's), the
events recorded since (made steps by recipe.steps_from_events), and the steps after them (new
steps go in after step "after" of the page's record-at, before the later ones); the Steps list's
edits, and the click or press just recorded, whose page may navigate (its address to wait for).

The steps of the person's (harvester.needs) the panel adds where the recording is (person): an
approval in an app, until the page shows the address it is at now (the person approved, and the
site went on), and a handover; the Steps list changes their label, wait and until (person-edit),
its address from the page's (here) or an element picked on it (the recorder's mode until).
"""

import copy
import time

from .. import needs
from ..choices import CHOICES_MAX
from ..needs import LABEL_MAX
from ..recipe import edit_step, steps_from_events, url_pattern

# The page's messages that change the steps.
EDITS = frozenset(
    {
        "undo",
        "optional",
        "step-delete",
        "step-move",
        "step-edit",
        "record-at",
        "mail-code",
        "ask-code",
        "each",
        "person",
        "person-edit",
        "wait-add",
    }
)
# The labels of the person's steps the panel adds without one.
LABELS = {"approve": "Approve the sign-in in your app", "handover": "Sign in yourself (a passkey)"}
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
        """A step's event, after the steps so far; a click or press may navigate the page (a dialog
        it opened answered meanwhile changes nothing of that)."""
        self.events.append(event)
        if event.get("action") == "dialog":
            return
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

    def edit(self, kind: str, message: dict) -> bool:  # pylint: disable=too-many-return-statements
        """Undo (the last event, else the last step before the new ones), and the Steps list's
        edits: a step removed, moved, changed (optional, what a fill fills in, its value, its wait),
        where new steps go (record-at), a filled code that comes by e-mail (mail-code) or that the
        person reads on their phone (ask-code), a list's choice repeated for each of its options
        (each), and the person's steps added (person) and changed (person-edit); whether the steps
        changed."""
        if kind in ("undo", "person"):
            return self._undo() if kind == "undo" else self._add_person(message)
        if kind == "wait-add":
            return self._add_wait(message)
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

    def _undo(self) -> bool:
        """The last event goes, else the last step before the new ones."""
        if self.events:
            self.events.pop()
        elif self.before:
            self.before.pop()
        return True

    def _add_wait(self, message: dict) -> bool:
        """A step "wait" for the element picked on the page (selector), after the step of the
        message (-1: before the first)."""
        selector = str(message.get("selector") or "").strip()
        steps = self._commit()
        index = message.get("step")
        if not selector or not isinstance(index, int) or isinstance(index, bool) or not -1 <= index < len(steps):
            return False
        point = len(self.before)
        steps.insert(index + 1, {"action": "wait", "selector": selector, "state": "visible"})
        point += 1 if index + 1 <= point else 0
        self.before, self.after = steps[:point], steps[point:]
        return True

    def _add_person(self, message: dict) -> bool:
        """A step of the person's where the recording is (person_step); a click before an
        approval no longer waits for the address the approval led to."""
        step = person_step(message)
        if step is None:
            return False
        self._commit()
        last = self.before[-1] if self.before else {}
        wait = last.get("wait")
        if step["action"] == "approve" and isinstance(wait, dict) and wait.get("url") == step["until"]["url"]:
            last.pop("wait")
        self.before.append(step)
        return True


def person_step(message: dict) -> dict | None:
    """The step of the person's the panel adds (action approve or handover; its label, else
    LABELS'): an approval until the page's address now (address), a handover without until."""
    action = message.get("action")
    if action not in LABELS:
        return None
    label = str(message.get("label") or "").strip()[:LABEL_MAX] or LABELS[str(action)]
    step = {"action": action, "label": label}
    address = str(message.get("address") or "")
    if action == "approve":
        if not address.startswith(("http://", "https://")):
            return None
        step["until"] = {"url": url_pattern(address)}
    return step


def person_edit(step: dict, message: dict) -> dict:
    """An approval's or a handover's label, wait (s) and until (its url, a pattern, or here: the
    page's address now; its selector) as the Steps list changes them; what is wrong is left as it
    was (an approval keeps an until, a handover without one has none). Another step as it is."""
    if step.get("action") not in LABELS:
        return step
    changed = dict(step)
    label = str(message.get("label") or "").strip()[:LABEL_MAX] if "label" in message else ""
    if label:
        changed["label"] = label
    if "wait" in message:
        wait = message["wait"]
        if wait is None or wait == needs.WAIT:
            changed.pop("wait", None)
        elif isinstance(wait, int) and not isinstance(wait, bool) and needs.WAIT_MIN <= wait <= needs.WAIT_MAX:
            changed["wait"] = wait
    until = dict(step.get("until") or {})
    address = str(message.get("address") or "")
    if message.get("here") and address.startswith(("http://", "https://")):
        until["url"] = url_pattern(address)
    for key in ("url", "selector"):
        if isinstance(message.get(key), str):
            until[key] = message[key].strip()
    until = {key: value for key, value in until.items() if value}
    if until and not needs.until_problem(until):
        changed["until"] = until
    elif not until and changed["action"] == "handover":
        changed.pop("until", None)
    return changed


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


# The edits that make a step another: a code from e-mail or the phone, choices, a step of the
# person's.
CHANGES = {"mail-code": mail_code, "ask-code": ask_code, "each": each, "person-edit": person_edit}
