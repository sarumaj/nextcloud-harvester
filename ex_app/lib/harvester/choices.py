# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recipe that goes through choices (harvester.recipe: the step "each"): every account or
contract at a bank, every year of an archive, every child at a school portal.

    {"action": "each", "selector": "select#account", "values": "options"}
    {"action": "each", "selector": "button.contract", "option": "ul.menu li", "values": "options",
     "pattern": "^Contract"}
    {"action": "each", "values": ["2024", "2025", "2026"]}

The step is a choice point in the steps: what comes after it (the steps after it, then the walk of
the listing from start) is done once for each value, the value chosen first. "values": "options"
takes the choices the page offers: a <select>'s options (those that can be chosen, without an
empty placeholder), or a custom dropdown's ("option": its entries' element, shown by a click on
"selector"); a list of texts is a fixed list instead (a <select>'s option by its value, else its
text; without "selector" nothing is chosen on the page, the value is only used). "pattern" keeps
the choices whose text it matches.

Each choice's text is {choice} (its value {choice_value}) in the file names, tags, the file links'
key, the listing's address and a JSON listing's, and in the steps after it (a value filled in, an
address opened). Files are told apart per choice (the same address may give each account's own).
For the next choice the browser goes back to the address the page had at the step and chooses
again there, then does the steps after it again. A step that fails for one choice (an option that
is gone) leaves that choice out, as a gap of the run.
"""

import re

# The choices a step goes through at most, and the length of a fixed value.
CHOICES_MAX = 100
VALUE_MAX = 200


def each_index(steps: list) -> int | None:
    """The place of the step "each" among the steps, None without one."""
    return next(
        (index for index, step in enumerate(steps) if isinstance(step, dict) and step.get("action") == "each"),
        None,
    )


def _values_problem(step: dict) -> str:
    """What is wrong with the step's values, or ""."""
    values = step.get("values", "options")
    if values == "options":
        return "" if step.get("selector") else 'the options of what? (a "selector", or a list of values)'
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= CHOICES_MAX
        or not all(isinstance(value, str) and value.strip() and len(value) <= VALUE_MAX for value in values)
    ):
        return f'the values are "options" or a list of 1 to {CHOICES_MAX} texts'
    return ""


def step_problem(step: dict) -> str:
    """What is wrong with a step "each", or ""."""
    problem = _values_problem(step)
    if problem:
        return problem
    for key in ("selector", "option", "pattern"):
        if key in step and not isinstance(step[key], str):
            return f"the {key} is text"
    if step.get("option") and not step.get("selector"):
        return "a dropdown's entries (option) need the element that shows them (selector)"
    try:
        re.compile(str(step.get("pattern") or ""))
    except re.error as error:
        return f"the pattern is not a regular expression ({error})"
    if step.get("optional"):
        return "a choice is never optional"
    return ""


def check(steps: list, signing: int) -> list[str]:
    """The problems of the steps' choices: one step "each" at most, after the sign-in (signing:
    how many steps sign in, recipe.sign_in_steps)."""
    places = [index for index, step in enumerate(steps) if isinstance(step, dict) and step.get("action") == "each"]
    if len(places) > 1:
        return [f"step {places[1] + 1}: a recipe goes through one list of choices (one step each)"]
    if places and places[0] < signing:
        return [f"step {places[0] + 1}: the choices come after the sign-in"]
    return []


def label(step: dict) -> str:
    """The step in a few words, for a log line."""
    if step.get("values", "options") == "options":
        return f"for each option of {step.get('selector', '')}"
    return f"for each of {', '.join(step['values'][:5])}{', …' if len(step['values']) > 5 else ''}"


def filled(step: dict, choice: str, value: str) -> dict:
    """A step after the choice with {choice} and {choice_value} filled in its value and address."""
    found = {}
    for key in ("value", "url"):
        if isinstance(step.get(key), str) and "{choice" in step[key]:
            found[key] = step[key].replace("{choice}", choice).replace("{choice_value}", value)
    return step | found


def uses_choice(text: str, choice: str, value: str) -> str:
    """An address with the choice's text and value in place of {choice} and {choice_value}."""
    return text.replace("{choice}", choice).replace("{choice_value}", value)
