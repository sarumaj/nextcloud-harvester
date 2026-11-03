# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cookie banners dismissed on their own (the website source's option "Dismiss cookie banners",
sources.base.CONSENT): consent.js, added to every page and frame of the job's browser (its steps
and its walk) and of the recorder's (its recording and its recipe's test), answers the common
consent managers' banners (OneTrust, Cookiebot, Usercentrics, Didomi, Quantcast's TCF, TrustArc,
Borlabs, Klaro, Sourcepoint's frame, CookieYes and its CookieLawInfo, Complianz, Osano, Iubenda,
consentmanager.net, Cookie Script, Termly, Axeptio, Cookie Notice, Google's Funding Choices) with
"Reject all", else "Only necessary", and buttons of exactly such a text (in English, German or
Polish) in a dialog or a floating banner that speaks of cookies; nothing else.
It clicks without recording: recorder.js leaves the clicks it makes alone. What it clicked a job's
run puts in its progress log (Told): a line on the page where a banner was dismissed, naming the
manager (or "a cookie banner") and the button, each manager once a run.

A recipe recorded before may still click such a banner itself: with the banners dismissed, its
click on a consent manager's element or on a button reading like a consent (relaxed) is
optional, as the banner may be gone by then.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError

from ..l10n import said
from .engine import main_world

SCRIPT = (Path(__file__).parent / "consent.js").read_text(encoding="utf-8")
# The managers consent.js knows by name, and "banner" (a cookie banner of none of them).
NAMES = frozenset(re.findall(r"name: '([^']+)'", SCRIPT)) | {"banner"}
# The clicks consent.js noted in a document and nobody read yet ({name, text}), marked read.
NOTED = """() => (Array.isArray(window.__harvesterConsent) ? window.__harvesterConsent : [])
  .filter((entry) => entry && typeof entry === 'object' && !entry.read && (entry.read = true))
  .map((entry) => ({ name: String(entry.name), text: String(entry.text || '') }))"""
# What names a consent manager's element in a selector, or its frame's address.
MANAGERS = re.compile(
    r"onetrust|cybotcookiebot|cookiebot|usercentrics|didomi|qc-cmp|truste|borlabs|brlbs|klaro|cookie|consent|"
    r"sp_message|sp_choice|privacy-mgmt|cky-|cmplz|osano|iubenda|cmpbox|cmpwrapper|termly|axeptio|fc-cta|fc-consent",
    re.IGNORECASE,
)
# A button's text that reads like a banner's (the recorder's own hint for optional steps).
ANSWERS = re.compile(
    r"^(?:ok(?:ay)?|got it|agree|i agree|accept(?: all)?(?: cookies)?|allow(?: all)?(?: cookies)?|reject all|"
    r"akzeptieren|alle akzeptieren|alles akzeptieren|zustimmen|alle zulassen|einverstanden|alle ablehnen|"
    r"zgadzam się|akceptuj(?:ę)?(?: wszystkie)?|odrzuć wszystkie)(?=$|[\s!.,])",
    re.IGNORECASE,
)


def relaxed(step: dict) -> dict:
    """The step as a run with the banners dismissed plays it: a click on a consent manager's
    element (its selector, an alternative or its frame's address names one) or on a button
    reading like a banner's is optional; another step as it is."""
    if step.get("optional") or step.get("action") != "click":
        return step
    frames = [entry for entry in step.get("frame") or [] if isinstance(entry, dict)]
    named = [str(step.get("selector") or ""), *map(str, step.get("alternatives") or [])]
    named += [str(entry.get(key) or "") for entry in frames for key in ("selector", "url")]
    if any(MANAGERS.search(text) for text in named) or ANSWERS.match(str(step.get("text") or "").strip()):
        return step | {"optional": True}
    return step


class Told:  # pylint: disable=too-few-public-methods
    """A run's lines of the cookie banners its browser dismissed (report: the progress log's),
    read from a page and its frames when the run is done with the page or with the steps: one per
    manager (or for the banners of none) a run, not one per page of a walk."""

    def __init__(self, report: Callable[[str, str], None]):
        self.report = report
        self.named: set[str] = set()

    def __call__(self, page: Any) -> None:
        host = urlparse(page.url).hostname or ""
        for frame in page.frames:
            try:
                noted = frame.evaluate(NOTED, **main_world(frame))
            except PlaywrightError:
                continue  # a frame that went (or navigated) meanwhile
            for entry in noted if isinstance(noted, list) else []:
                name = str(entry.get("name")) if isinstance(entry, dict) else ""
                if name not in NAMES or name in self.named:
                    continue
                self.named.add(name)
                button = str(entry.get("text") or "")[:80]
                if name == "banner":
                    self.report(
                        "info", said("A cookie banner dismissed on {host}: “{button}”", host=host, button=button)
                    )
                else:
                    line = said("{manager}'s cookie banner dismissed on {host}: “{button}”", manager=name, host=host,
                                button=button)  # fmt: skip
                    self.report("info", line)
