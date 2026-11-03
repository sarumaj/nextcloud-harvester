# SPDX-License-Identifier: AGPL-3.0-or-later
"""Steps of a recipe that need the job's person while its run waits: a code from the phone (an
SMS, an authenticator Harvester was not given, a code read out by a call), typed on Harvester's
page into the step's field (the step "ask-code").

    {"action": "ask-code", "selector": "#sms", "label": "the bank's SMS", "wait": 600}

A run that comes to such a step asks its person on the desk (Desk): the job shows Needs you on
the page, the person is told (a notification), and the run waits up to the step's wait (WAIT by
default, from WAIT_MIN to WAIT_MAX seconds) for the code, the page polled meanwhile; a stop ends
the wait at once. The code goes from the page's request (POST /api/jobs/<id>/code, the job's
person only, while it waits) through the desk into the field, and nowhere else: it is never
logged, stored or kept after the step took it. The step's play is harvester.sources.attend.

The desk is the executor's (one per app); the run's thread knows it (serve), as it knows its stop
(sources.base.watch_stop). Without one (a weekly recipe test, nobody to ask) such a step asks for
a person as a challenge does. The recorder's recipe test serves one of its own, on its page.
"""

import dataclasses
import threading
import time
from collections.abc import Callable
from typing import Any

from .l10n import said

# The steps that ask the person, and what each asks for.
ACTIONS = {"ask-code": "code"}
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
    """What a run asks its person now: a code (kind "code") about label, until a time (epoch s)."""

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
        log("info", said("Waiting up to {minutes} min for you: enter the code from {label}", minutes=minutes,
                         label=asked.label))  # fmt: skip
        notify(nc, "Harvester: {job} needs you", values, "Enter the code from {label} on Harvester's page within "
               "{minutes} minutes: the job waits for it.")  # fmt: skip

    return tell


def step_label(step: dict) -> str:
    """A step of a person in a few words, for a log line ("" for another step)."""
    if step.get("action") == "ask-code":
        return f"type the code from {step.get('label') or 'the phone'} ({step.get('selector', '')})"
    return ""


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
    return ""
