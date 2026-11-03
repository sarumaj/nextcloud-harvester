# SPDX-License-Identifier: AGPL-3.0-or-later
"""The play of the steps that need the job's person (harvester.needs) in the browser: a code from
the phone typed into its field (ask-code), an approval waited for until the page shows it went
through (approve), the page handed over to the person (handover).

A code or an approval asks on the desk the run's thread serves (needs.serving) and waits for the
answer in the page's own pauses (page.wait_for_timeout: the browser goes on meanwhile, and a
recipe's test in the recorder ends its wait at its stop or its end), looking each needs.POLL
seconds; the run's stop ends the wait at once (Stopped). Nobody answering in time ends the run
(Unanswered), not the step's retries or a sign-in anew, which would only ask again. A handover
ends the run as a page asking for a person does (HandedOver, a ChallengeError), with the
browser's session for the person to go on in.
"""

import time
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .. import needs
from .base import ChallengeError, SourceError, check_stop, pattern_search

# Whether the page shows an element (not only has it).
SHOWN = """s => { const e = document.querySelector(s)
  return !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) }"""


class Unanswered(SourceError):
    """The person did not answer within the step's wait: the run ends there."""


class HandedOver(ChallengeError):
    """A step hands the page over to the person: the run ends, the job waits for them (Continue
    in the browser), and the browser's session at that point goes with it (session)."""

    session: dict | None = None


def play(page: Any, target: Any, selector: str, step: dict, wait: dict) -> None:
    """Plays a step of a person (target: the page or the step's frame; wait: an optional step's
    time left): a code's field waited for, the person asked, the code typed; an approval waited
    for; a handover, unless the page shows its until already."""
    action, until = step["action"], step.get("until")
    if action == "ask-code":
        target.wait_for_selector(selector, state="visible", **wait)
        target.fill(selector, ask(page, step), **wait)
    elif not (isinstance(until, dict) and reached(page, until)):
        if action == "handover":
            raise HandedOver(page.url, str(step.get("label") or ""))
        ask(page, step, lambda: reached(page, until))


def reached(page: Any, until: dict) -> bool:
    """Whether the page shows what an approval or a handover leads to: its address matches until's
    pattern, its element is shown (both, when it has both)."""
    try:
        if until.get("url") and not pattern_search(until["url"], page.url):
            return False
        return not until.get("selector") or bool(page.evaluate(SHOWN, until["selector"]))
    except PlaywrightError:
        return False  # navigating, or a selector the page cannot read: not yet


def ask(page: Any, step: dict, done: Any = None) -> str:
    """Asks the run's person what the step needs, and waits: the code typed on the page, or done()
    (the approval went through: ""). Without a desk to ask on (a recipe's weekly test) the page
    asks for a person (ChallengeError)."""
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
            if done is not None and done():
                return ""
            if time.time() >= asked.until:
                minutes = max(1, round(seconds / 60))
                if kind == "code":
                    raise Unanswered(f"nobody entered the code from {text} within {minutes} min")
                raise Unanswered(f"the approval ({text}) did not show within {minutes} min")
            check_stop()
            page.wait_for_timeout(needs.POLL * 1000)
    finally:
        desk.close(job_id, asked)
