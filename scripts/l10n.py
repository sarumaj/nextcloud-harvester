#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The texts Harvester shows people, and its translations in l10n/<language>.json and .js.

Nextcloud reads both files of the person's language, as it does a PHP app's: AppAPI adds the .js
to the app's page and to the admin settings (t() in the browser), and its notifier translates
the app's notifications with the .json. The texts are:

- the page's (ex_app/js): t('…') and n('…', '…', count), in single quotes;
- what the page shows of the server's (the API's lists, translated on the page): the sources'
  titles, descriptions and account fields, their options' titles and help, the templates'
  titles, descriptions and parameters, as the app has them when it runs;
- the admin's settings (settings.form): their titles, help and placeholders that are words;
- the API's errors given as plain strings (HTTPException(status, "…")), and the subjects and
  messages of the notifications (self.notify(nc, "…", values, "…"));
- the server's texts with values, sent as templates the page translates (harvester.l10n):
  said("…", name=value) and said_n("…", "…", count) with their texts as plain strings (the
  schedule in words, why a job waits, the progress log, a run's outcome, the recorder's
  warnings).

Not translated: the server's errors with values in them (but those made with said()), the
progress log's rarer lines and a run's errors, recipes and their keys, the API's identifiers. Run
it with the app's virtual environment:

    .venv/bin/python scripts/l10n.py            # what each language lacks or no longer uses
    .venv/bin/python scripts/l10n.py --missing de    # the texts de lacks, as JSON to translate
    .venv/bin/python scripts/l10n.py --write    # each .json in the texts' order, unused texts
                                                # dropped, and its .js made from it

It exits with 1 when a language lacks a text or a translation's placeholders differ.
"""

import argparse
import ast
import json
import pathlib
import re
import sys
from collections.abc import Iterator

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = ROOT / "ex_app" / "lib"
L10N = ROOT / "l10n"
APP = "harvester"
# t('…') and n('…', '…' in the page's code (not window.t('harvester', …), Nextcloud's own).
LITERAL = r"'((?:[^'\\\n]|\\.)*)'"
T_CALL = re.compile(r"(?<![\w$.])t\(\s*" + LITERAL)
N_CALL = re.compile(r"(?<![\w$.])n\(\s*" + LITERAL + r"\s*,\s*" + LITERAL)
PLACEHOLDER = re.compile(r"{(\w+)}")
ESCAPES = {"n": "\n", "t": "\t"}


def unquote(literal: str) -> str:
    """A JavaScript string literal's text: \\' is ', \\n a new line."""
    return re.sub(r"\\(.)", lambda match: ESCAPES.get(match[1], match[1]), literal)


def plural_key(one: str, many: str) -> str:
    """The key of n()'s texts in Nextcloud's translations."""
    return f"_{one}_::_{many}_"


def page_texts() -> dict[str, str]:
    """The page's texts by key, with where each is (a plural's key holds both of its texts)."""
    texts: dict[str, str] = {}
    for path in sorted((ROOT / "ex_app" / "js").glob("*.js")):
        code = path.read_text(encoding="utf-8")
        found = [(match.start(), unquote(match[1])) for match in T_CALL.finditer(code)]
        found += [(match.start(), plural_key(unquote(match[1]), unquote(match[2]))) for match in N_CALL.finditer(code)]
        for start, key in sorted(found):
            texts.setdefault(key, f"{path.relative_to(ROOT)}:{code.count(chr(10), 0, start) + 1}")
    return texts


def source_texts() -> Iterator[tuple[object, str]]:
    """The sources' titles, descriptions and account fields, and their options' titles and help,
    as the API lists them for the page (with where each is)."""
    # pylint: disable=import-outside-toplevel,import-error
    from harvester import watch
    from harvester.runner import COMMON_OPTIONS
    from harvester.sources import SOURCES

    for source in SOURCES.values():
        where = f"source {source.name}"
        yield source.title, where
        yield source.description, where
        for title, help_ in source.credential_help.values():
            yield title, where
            yield help_, where
        for key, option in (source.options | COMMON_OPTIONS | watch.options_of(source.name)).items():
            if key != "home":  # "Through the proxy": the page's own text, with the admin's name
                yield option.title, f"{where}, option {key}"
            yield option.description, f"{where}, option {key}"


def preset_texts() -> Iterator[tuple[object, str]]:
    """The templates' titles, descriptions and parameters (their titles, help, suggestions'
    labels)."""
    from harvester.presets import PRESETS  # pylint: disable=import-outside-toplevel,import-error

    for name, preset in sorted(PRESETS.items()):
        yield preset["title"], f"template {name}"
        yield preset["description"], f"template {name}"
        for key, parameter in preset.get("parameters", {}).items():
            labels = [s.get("label") for s in parameter.get("suggestions", []) if isinstance(s, dict)]
            for text in [parameter.get("title"), parameter.get("description"), *labels]:
                yield text, f"template {name}, {key}"


def settings_texts() -> Iterator[tuple[object, str]]:
    """The admin's settings: their titles, help and the placeholders that are words (not an
    example address, which is shown as it is)."""
    from harvester import settings  # pylint: disable=import-outside-toplevel,import-error

    form = settings.form(1, 1)
    for item in [form, *form.fields]:
        placeholder = getattr(item, "placeholder", "") or ""
        for text in (item.title, item.description, "" if "://" in placeholder else placeholder):
            yield text, "admin settings"


def served_texts() -> dict[str, str]:
    """The texts the API gives the page and Nextcloud's settings, as the app makes them."""
    sys.path.insert(0, str(LIB))
    texts: dict[str, str] = {}
    for text, where in [*source_texts(), *preset_texts(), *settings_texts()]:
        if isinstance(text, str) and text.strip():
            texts.setdefault(text, where)
    return texts


# The calls whose plain strings are texts: their positions and keywords.
CALLS = {"HTTPException": ((1,), ("detail",)), "notify": ((1, 3), ("subject", "message"))}


def text_of(node: ast.expr) -> str:
    """A call's argument as a text: a plain string's (one written in pieces, "a" "b", is one), else
    ""."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def code_texts() -> dict[str, str]:
    """The API's errors given as plain strings (HTTPException(status, "…")), the notifications'
    subjects and messages (self.notify(nc, subject, values, message)), and the templates the page
    translates: said("…") and said_n("…", "…", count) (harvester.l10n)."""
    texts: dict[str, str] = {}
    for path in sorted(LIB.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            if name == "said" and node.args and text_of(node.args[0]):
                texts.setdefault(text_of(node.args[0]), where)
            elif name == "said_n" and len(node.args) >= 2 and text_of(node.args[0]) and text_of(node.args[1]):
                texts.setdefault(plural_key(text_of(node.args[0]), text_of(node.args[1])), where)
            positions, keywords = CALLS.get(name, ((), ()))
            arguments = [node.args[i] for i in positions if i < len(node.args)]
            arguments += [keyword.value for keyword in node.keywords if keyword.arg in keywords]
            for argument in arguments:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str) and argument.value:
                    texts.setdefault(argument.value, f"{path.relative_to(ROOT)}:{node.lineno}")
    return texts


def wanted() -> dict[str, str]:
    """Every text the app translates, in order (the page's first), with where it is."""
    texts = page_texts()
    for found in (served_texts(), code_texts()):
        for key, where in found.items():
            texts.setdefault(key, where)
    return texts


def languages() -> list[str]:
    """The languages that have translations."""
    return sorted(path.stem for path in L10N.glob("*.json"))


def read(language: str) -> dict:
    """A language's .json: {"translations": {...}, "pluralForm": "nplurals=…; plural=…;"}."""
    return json.loads((L10N / f"{language}.json").read_text(encoding="utf-8"))


def registered(language: str) -> tuple[dict, str]:
    """What a language's .js registers: its translations and plural form."""
    code = (L10N / f"{language}.js").read_text(encoding="utf-8")
    match = re.fullmatch(r'OC\.L10N\.register\(\s*"' + APP + r'",\s*(\{.*\}),\s*(".*")\s*\);\s*', code, re.DOTALL)
    if not match:
        raise ValueError(f"l10n/{language}.js does not register {APP}'s translations")
    return json.loads(match[1]), json.loads(match[2])


def nplurals(form: str) -> int:
    """The number of plural forms a pluralForm names."""
    match = re.search(r"nplurals\s*=\s*(\d+)", form)
    if not match:
        raise ValueError(f"no nplurals in {form!r}")
    return int(match[1])


def problems(language: str, texts: dict[str, str] | None = None) -> list[str]:
    """What is wrong with a language's translations: a text it lacks, an empty or wrong-shaped
    translation, placeholders ({name}, %n) other than the text's."""
    texts = wanted() if texts is None else texts
    data = read(language)
    translations = data.get("translations", {})
    forms = nplurals(data.get("pluralForm", ""))
    found = [f"lacks {key!r}" for key in texts if key not in translations]
    for key, value in translations.items():
        plural = re.fullmatch(r"_(.*)_::_(.*)_", key, re.DOTALL)
        english = [plural[1], plural[2]] if plural else [key]
        values = value if isinstance(value, list) else [value]
        if (plural and (not isinstance(value, list) or len(value) != forms)) or (
            not plural and isinstance(value, list)
        ):
            found.append(
                f"{key!r}: {len(values) if isinstance(value, list) else 'one'} forms, not {forms if plural else 'one'}"
            )
            continue
        names = set(PLACEHOLDER.findall(english[-1]))
        for index, text in enumerate(values):
            # The first form is the one of 1 (as in English): it may leave out %n where the English
            # one does ("every minute", "jede Minute").
            optional = plural and index == 0 and "%n" not in english[0] and "%n" in english[-1]
            if not isinstance(text, str) or not text.strip():
                found.append(f"{key!r}: an empty translation")
            elif set(PLACEHOLDER.findall(text)) != names:
                found.append(f"{key!r}: placeholders {sorted(set(PLACEHOLDER.findall(text)))}, not {sorted(names)}")
            elif not optional and ("%n" in english[-1]) != ("%n" in text):
                found.append(f"{key!r}: %n differs")
    return found


def unused(language: str, texts: dict[str, str] | None = None) -> list[str]:
    """The texts a language translates that the app no longer has."""
    texts = wanted() if texts is None else texts
    return [key for key in read(language).get("translations", {}) if key not in texts]


def javascript(data: dict) -> str:
    """A language's .js, as Nextcloud's translation tool writes it."""
    table = json.dumps(data["translations"], ensure_ascii=False, indent=4).replace("\n", "\n    ")
    return f'OC.L10N.register(\n    "{APP}",\n    {table},\n    {json.dumps(data["pluralForm"])}\n);\n'


def write(language: str, texts: dict[str, str]) -> None:
    """The language's .json in the texts' order without unused texts, and its .js from it."""
    data = read(language)
    translations = data["translations"]
    data = {
        "translations": {key: translations[key] for key in texts if key in translations},
        "pluralForm": data["pluralForm"],
    }
    (L10N / f"{language}.json").write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    (L10N / f"{language}.js").write_text(javascript(data), encoding="utf-8")


def main() -> int:
    """Reports, writes, or lists a language's missing texts."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--write", action="store_true", help="order the .json files and make the .js files")
    parser.add_argument("--missing", metavar="LANGUAGE", help="print the texts LANGUAGE lacks, as JSON")
    arguments = parser.parse_args()
    texts = wanted()
    if arguments.missing:
        have = read(arguments.missing)["translations"] if (L10N / f"{arguments.missing}.json").exists() else {}
        lacking = {key: key for key in texts if key not in have}
        print(json.dumps(lacking, ensure_ascii=False, indent=4))
        return 0
    failed = False
    for language in languages():
        if arguments.write:
            write(language, texts)
        for problem in problems(language, texts):
            failed = True
            print(f"l10n/{language}.json: {problem}")
        for key in unused(language, texts):
            print(f"l10n/{language}.json: no longer used: {key!r} (--write drops it)")
    print(f"{len(texts)} texts, {len(languages())} languages")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
