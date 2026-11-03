# SPDX-License-Identifier: AGPL-3.0-or-later
"""Steps of a recipe that need the job's person while its run waits: a code from the phone (an
SMS, an authenticator Harvester was not given, a code read out by a call), typed on Harvester's
page into the step's field (the step "ask-code"); an approval in an app (a bank's), which the
page shows it got (the step "approve"); and what the person does in the page itself (a passkey,
a sign-in only a person gets through: the step "handover").

    {"action": "ask-code", "selector": "#sms", "label": "the bank's SMS", "wait": 600}
    {"action": "approve", "until": {"selector": "#accounts"}, "label": "Confirm in the bank's app"}
    {"action": "handover", "label": "Sign in with your passkey", "until": {"url": "/dashboard"}}

A run that comes to a code or an approval asks its person on the desk (Desk): the job shows
Needs you on the page, the person is told (a notification), and the run waits up to the step's
wait (WAIT by default, from WAIT_MIN to WAIT_MAX seconds) for the code, or until the page shows
"until" (an address's pattern, an element shown, or both), the page polled meanwhile; a stop ends
the wait at once. The code goes from the page's request (POST /api/jobs/<id>/code, the job's
person only, while it waits) through the desk into the field, and nowhere else: it is never
logged, stored or kept after the step took it. A handover ends the run as a page asking for a
person does (Continue in the browser: the person does it in the job's browser with the session
the run left, and Done runs the job again, its kept session past the sign-in). An approval or a
handover whose "until" the page shows already asks nothing. The three belong to the sign-in,
which a kept session skips (recipe.sign_in_steps). The steps' play is harvester.sources.attend.

The desk is the executor's (one per app); the run's thread knows it (serve), as it knows its stop
(sources.base.watch_stop). Without one (a weekly recipe test, nobody to ask) such a step asks for
a person as a challenge does. The recorder's recipe test serves one of its own, on its page.
"""

import dataclasses
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from .l10n import said

# The steps of a person, and what each asks for.
ACTIONS = {"ask-code": "code", "approve": "approve", "handover": "handover"}
# Those of them without an element of their own.
WITHOUT_ELEMENT = ("approve", "handover")
# How long a run waits for its person by default, and the bounds of a step's own wait (s).
WAIT = 600
WAIT_MIN = 30
WAIT_MAX = 1800
# A step's label (what the person is asked about), and a code, at most this long.
LABEL_MAX = 100
CODE_MAX = 64
# How often a waiting step looks whether its answer came (s).
POLL = 1.0


@dataclasses.dataclass
class Asked:
    """What a run asks its person now: a code (kind "code") or an approval ("approve") about
    label, until a time (epoch s)."""

    kind: str
    label: str
    seconds: int
    until: float
    # The person's answer until the step takes it; never shown (repr) or listed (Desk.asking).
    code: str | None = dataclasses.field(default=None, repr=False)

    def take(self) -> str | None:
        """The code, if it came; it is forgotten here."""
        code, self.code = self.code, None
        return code


class Desk:
    """The runs that wait for their person now, by job: what each asks, and the answers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._asked: dict[int, Asked] = {}

    def open(self, job_id: int, kind: str, label: str, seconds: int) -> Asked:
        """The job's run asks its person (one question at a time: a new one replaces the last)."""
        asked = Asked(kind, label, seconds, time.time() + seconds)
        with self._lock:
            self._asked[job_id] = asked
        return asked

    def close(self, job_id: int, asked: Asked) -> None:
        """The question is over (answered, timed out, stopped): any answer left is dropped."""
        asked.code = None
        with self._lock:
            if self._asked.get(job_id) is asked:
                del self._asked[job_id]

    def asking(self, job_id: int) -> dict | None:
        """What the job's run asks now, for its page (never an answer), or None."""
        with self._lock:
            asked = self._asked.get(job_id)
            return None if asked is None else {"kind": asked.kind, "label": asked.label, "until": asked.until}

    def waiting(self) -> list[int]:
        """The jobs whose runs wait for their person."""
        with self._lock:
            return sorted(self._asked)

    def answer(self, job_id: int, code: str) -> bool:
        """The person's code for their job's run, if it waits for one now."""
        with self._lock:
            asked = self._asked.get(job_id)
            if asked is None or asked.kind != "code" or not code:
                return False
            asked.code = code
            return True


# The desk and the job of the run in this thread (serve), and how it tells its person.
_RUN = threading.local()


def serve(desk: Desk | None, job_id: int = 0, tell: Callable[[Asked], None] | None = None) -> None:
    """The desk of the run in this thread, its job and how it tells its person it asks (None:
    none, a run without a person to ask)."""
    _RUN.served = (desk, job_id, tell or (lambda asked: None)) if desk is not None else None


def serving() -> tuple[Desk, int, Callable[[Asked], None]] | None:
    """This thread's desk, job and teller, or None."""
    return getattr(_RUN, "served", None)


def teller(name: str, log: Callable[[str, str], Any], notify: Callable[..., None], nc: Any) -> Callable[[Asked], None]:
    """How a job's run tells its person what it waits for: a line of the progress log and a
    notification (the executor's notify, with the run's client nc), naming the step's label."""

    def tell(asked: Asked) -> None:
        minutes = max(1, round(asked.seconds / 60))
        values = {"job": name, "label": asked.label, "minutes": str(minutes)}
        if asked.kind == "code":
            log("info", said("Waiting up to {minutes} min for you: enter the code from {label}", minutes=minutes,
                             label=asked.label))  # fmt: skip
            notify(nc, "Harvester: {job} needs you", values, "Enter the code from {label} on Harvester's page "
                   "within {minutes} minutes: the job waits for it.")  # fmt: skip
        else:
            log("info", said("Waiting up to {minutes} min for your approval: {label}", minutes=minutes,
                             label=asked.label))  # fmt: skip
            notify(nc, "Harvester: {job} needs you", values, "{label}: approve it within {minutes} minutes, "
                   "the job goes on once the site shows it went through.")  # fmt: skip

    return tell


def step_label(step: dict) -> str:
    """A step of a person in a few words, for a log line ("" for another step)."""
    action, text = step.get("action"), step.get("label") or ""
    if action == "ask-code":
        return f"type the code from {text or 'the phone'} ({step.get('selector', '')})"
    return {"approve": f"wait for your approval ({text})", "handover": f"hand over to you ({text})"}.get(
        str(action), ""
    )


def step_wait(step: dict) -> int:
    """How long the step waits for its person (s)."""
    wait = step.get("wait", WAIT)
    return wait if isinstance(wait, int) and not isinstance(wait, bool) and WAIT_MIN <= wait <= WAIT_MAX else WAIT


def step_problem(step: dict) -> str:
    """What is wrong with a step of a person in particular, or ""."""
    text = step.get("label")
    if not isinstance(text, str) or not text.strip() or len(text) > LABEL_MAX:
        return f"the label (what you are asked about, like “SMS code”) is text of 1 to {LABEL_MAX} characters"
    wait = step.get("wait", WAIT)
    if not isinstance(wait, int) or isinstance(wait, bool) or not WAIT_MIN <= wait <= WAIT_MAX:
        return f"the wait for you from {WAIT_MIN} to {WAIT_MAX} seconds"
    if step["action"] in WITHOUT_ELEMENT and step.get("optional"):
        return f"a step {step['action']} is never optional: its until says when it is not needed"
    if step["action"] == "approve" or "until" in step:
        return until_problem(step.get("until"))
    return ""


def until_problem(until: object) -> str:
    """What is wrong with an approval's or a handover's "until", or ""."""
    if (
        not isinstance(until, dict)
        or set(until) - {"url", "selector"}
        or not all(isinstance(until.get(key, ""), str) for key in ("url", "selector"))
        or not (until.get("url") or until.get("selector"))
    ):
        return 'until is {"url": <a pattern of the address>, "selector": <an element>}: what the page shows after you'
    try:
        re.compile(until.get("url", ""))
    except re.error as error:
        return f"until's address pattern is not a regular expression ({error})"
    return ""


def recipe_problems(recipe: dict) -> list[str]:
    """What is wrong with a recipe's steps of a person as a whole: a handover needs the listing's
    address (start), where the run after it starts with the person's session."""
    steps = [step for step in recipe.get("steps") or [] if isinstance(step, dict)]
    if any(step.get("action") == "handover" for step in steps) and not recipe.get("start"):
        return ["a handover needs the listing's address (start): the run after it starts there with your session"]
    return []
