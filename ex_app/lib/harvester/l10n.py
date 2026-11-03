# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server's texts with values in them, sent to the page as templates it translates.

said("Fetching {url}", url=address) is the English text ("Fetching https://…"), a str as any
other for the logs, the database's message columns and API clients that do not translate, and
carries its template ({"text": "Fetching {url}", "params": {"url": "https://…"}}), which the
page translates with Nextcloud's t() and fills (rendered() in harvester-main.js). said_n() is a
count's text (n() on the page, %n the count), listed() a list of values, joined in the person's
language. A value may be another said(): its own words, translated too. scripts/l10n.py collects the texts of said()
and said_n() written as plain strings; a text made otherwise (an f-string) cannot be translated.
"""

import json
from collections.abc import Awaitable, Callable


class Said(str):
    """An English text, made from its template (template), which the page translates."""

    template: dict

    def __new__(cls, english: str, template: dict) -> "Said":
        made = super().__new__(cls, english)
        made.template = template
        return made


def _value(value: object) -> object:
    """A template's value: another text's template, else a number or text as it is."""
    if isinstance(value, Said):
        return value.template
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return str(value)


def _fill(text: str, params: dict[str, object]) -> str:
    """The text with its placeholders filled in once (a value's own braces stay as they are)."""
    out, rest = [], text
    while "{" in rest:
        before, _, after = rest.partition("{")
        name, closed, tail = after.partition("}")
        out.append(before)
        if closed and name in params:
            out.append(str(params[name]))
            rest = tail
        else:
            out.append("{")
            rest = after
    return "".join(out) + rest


def said(text: str, **params: object) -> Said:
    """A text with placeholders ({name}) and their values."""
    template: dict = {"text": text}
    if params:
        template["params"] = {name: _value(value) for name, value in params.items()}
    return Said(_fill(text, params), template)


def said_n(one: str, many: str, count: int, **params: object) -> Said:
    """A count's text (%n), one or many in English, the language's plural form on the page."""
    template: dict = {"text": one, "plural": many, "count": count}
    if params:
        template["params"] = {name: _value(value) for name, value in params.items()}
    return Said(_fill((one if count == 1 else many).replace("%n", str(count)), params), template)


def listed(items: list, kind: str = "and") -> Said:
    """Values in a list: "a, b and c" (kind "and"), or "a, b, c" (kind "unit", a run's figures),
    joined in the person's language on the page (Intl.ListFormat)."""
    words = [str(item) for item in items]
    if kind == "unit" or len(words) < 2:
        english = ", ".join(words)
    else:
        english = ", ".join(words[:-1]) + " and " + words[-1]
    return Said(english, {"list": [_value(item) for item in items], "kind": kind})


def template_of(text: object) -> dict | None:
    """The text's template, if it has one (a plain string has none: the page shows it as it is)."""
    return text.template if isinstance(text, Said) else None


def stored(text: object) -> str | None:
    """The text's template as the database keeps it (JSON), None for a plain string."""
    template = template_of(text)
    return json.dumps(template, ensure_ascii=False) if template is not None else None


def loaded(value: str | None) -> dict | None:
    """A template as the database kept it, None for none (a line older than templates)."""
    if not value:
        return None
    try:
        template = json.loads(value)
    except ValueError:
        return None
    return template if isinstance(template, dict) else None


def with_templates(message: dict) -> dict:
    """A message for the page with each said() value's template beside it (<key>_l10n), and a
    list's ([…] of templates and None for plain strings)."""
    added: dict = {}
    for key, value in message.items():
        if isinstance(value, Said):
            added[f"{key}_l10n"] = value.template
        elif isinstance(value, list) and any(isinstance(item, Said) for item in value):
            added[f"{key}_l10n"] = [template_of(item) for item in value]
    return message | added


def sending(send: Callable[[dict], Awaitable[None]]) -> Callable[[dict], Awaitable[None]]:
    """A WebSocket's send_json that gives the page the templates of the message's texts
    (with_templates): the recorder's warnings and errors."""

    async def send_with_templates(message: dict) -> None:
        await send(with_templates(message))

    return send_with_templates
