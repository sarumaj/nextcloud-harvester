# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cookie banners dismissed on their own (the website source's option "Dismiss cookie banners",
sources.base.CONSENT): consent.js, added to every page and frame of the job's browser (its steps
and its walk) and of the recorder's (its recording and its recipe's test), answers the common
consent managers' banners (OneTrust, Cookiebot, Usercentrics, Didomi, Quantcast's TCF, TrustArc,
Borlabs, Klaro) with "Reject all", else "Only necessary", and buttons of exactly such a text (in
English, German or Polish) in a dialog or a floating banner that speaks of cookies; nothing else.
It clicks without recording: recorder.js leaves the clicks it makes alone.

A recipe recorded before may still click such a banner itself: with the banners dismissed, its
click on a consent manager's element or on a button reading like a consent (relaxed) is
optional, as the banner may be gone by then.
"""

import re
from pathlib import Path

SCRIPT = (Path(__file__).parent / "consent.js").read_text(encoding="utf-8")
# What names a consent manager's element in a selector, or its frame's address.
MANAGERS = re.compile(
    r"onetrust|cybotcookiebot|cookiebot|usercentrics|didomi|qc-cmp|truste|borlabs|brlbs|klaro|cookie|consent",
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
