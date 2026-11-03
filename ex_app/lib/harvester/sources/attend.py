# SPDX-License-Identifier: AGPL-3.0-or-later
"""The play of the steps that need the job's person (harvester.needs) in the browser: a code from
the phone typed into its field (ask-code).

The step asks on the desk the run's thread serves (needs.serving) and waits for the answer in the
page's own pauses (page.wait_for_timeout: the browser goes on meanwhile, and a recipe's test in the
recorder ends its wait at its stop or its end), looking each needs.POLL seconds; the run's stop
ends the wait at once (Stopped). Nobody answering in time ends the run (Unanswered), not the
step's retries or a sign-in anew, which would only ask again.
"""

import time
from typing import Any

from .. import needs
from .base import ChallengeError, SourceError, check_stop


class Unanswered(SourceError):
    """The person did not answer within the step's wait: the run ends there."""


def play(page: Any, target: Any, selector: str, step: dict, wait: dict) -> None:
    """Plays a step of a person: its field waited for (target: the page or the step's frame; wait:
    an optional step's time left), then the person asked, then the code typed."""
    target.wait_for_selector(selector, state="visible", **wait)
    target.fill(selector, ask(page, step), **wait)


def ask(page: Any, step: dict) -> str:
    """Asks the run's person what the step needs, and waits: the code typed on the page. Without
    a desk to ask on (a recipe's weekly test) the page asks for a person (ChallengeError)."""
    kind, text, seconds = needs.ACTIONS[step["action"]], str(step.get("label") or ""), needs.step_wait(step)
    served = needs.serving()
    if served is None:
        raise ChallengeError(page.url, text)
    desk, job_id, tell = served
    asked = desk.open(job_id, kind, text, seconds)
    try:
        tell(asked)
        while True:
            code = asked.take()
            if code is not None:
                return code
            if time.time() >= asked.until:
                raise Unanswered(f"nobody entered the code from {text} within {max(1, round(seconds / 60))} min")
            check_stop()
            page.wait_for_timeout(needs.POLL * 1000)
    finally:
        desk.close(job_id, asked)
