# SPDX-License-Identifier: AGPL-3.0-or-later
"""The translations (l10n/<language>.json and .js, scripts/l10n.py): every text the app shows is
in every language, with the same placeholders ({name}, %n) and its plural's forms; no language
keeps texts the app no longer has; each .js registers what its .json holds; the extraction finds
each kind of text (the server's templates too: said() and said_n()), and the page passes t() only
texts it can find. The page's English without a language, and in German, are test_page.py's."""

# pylint: disable=missing-function-docstring

import importlib.util
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).parent.parent
_SPEC = importlib.util.spec_from_file_location("l10n", ROOT / "scripts" / "l10n.py")
assert _SPEC and _SPEC.loader
l10n = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(l10n)

TEXTS = l10n.wanted()
LANGUAGES = l10n.languages()


def test_each_kind_of_text_is_found():
    # One of each, so that a changed pattern does not quietly find nothing.
    for text in (
        "New job",  # the page's t()
        "_%n job_::_%n jobs_",  # the page's n()
        "Website (recipe)",  # a source's title
        "Impersonate a browser",  # an option's title
        "heise magazines (PDF)",  # a template's title
        "Magazine's name",  # a template's parameter
        "Jobs at once",  # the admin's settings
        "no such job",  # an API error
        "Harvester: {job} failed",  # a notification
        "{address} asks for a person ({kind}).",
        "every day at {times}",  # the server's said(): the schedule in words
        "_every minute_::_every %n minutes_",  # said_n(): a count's forms
        "{place}. in the queue: starts in a moment",  # why a job waits
        "Saved {path} ({size})",  # the progress log
        "_%n downloaded_::_%n downloaded_",  # a run's outcome
        "In this recipe, {warning}.",  # the recorder's warnings
        "Monday",  # a value that is a text of its own
    ):
        assert text in TEXTS, text
    assert "http://proxy.example:3128" not in TEXTS, "an example address is no text"


def test_the_languages():
    assert set(LANGUAGES) >= {"de", "de_DE", "pl"}


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_text_is_translated_with_its_placeholders(language):
    assert not l10n.problems(language, TEXTS)


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_text_the_app_no_longer_has(language):
    assert not l10n.unused(language, TEXTS), "scripts/l10n.py --write drops them"


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_js_registers_what_the_json_holds(language):
    data = l10n.read(language)
    assert l10n.registered(language) == (data["translations"], data["pluralForm"])
    assert (ROOT / "l10n" / f"{language}.js").read_text(encoding="utf-8") == l10n.javascript(data)


def test_a_wrong_translation_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(l10n, "L10N", tmp_path)
    translations = {
        "Delete {name}? The files it downloaded stay.": "Löschen?",
        "_%n job_::_%n jobs_": ["ein Job", "%n Jobs"],
        "New job": ["Neuer Job"],
    }
    data = {"translations": translations, "pluralForm": "nplurals=2; plural=(n != 1);"}
    (tmp_path / "xx.json").write_text(json.dumps(data), encoding="utf-8")
    found = l10n.problems("xx", {"New job": "", "Run now": ""})
    assert any("lacks 'Run now'" in problem for problem in found)
    assert any("placeholders" in problem for problem in found)
    assert any("%n differs" in problem for problem in found)
    assert any("'New job'" in problem and "forms" in problem for problem in found)


def test_the_page_translates_only_texts_it_names():
    # t() with anything but a quoted text would go unfound: the server's texts go through said().
    code = (ROOT / "ex_app" / "js" / "harvester-main.js").read_text(encoding="utf-8")
    others = re.findall(r"(?<![\w$.])[tn]\(\s*[^\s')][\w.]*", code)
    assert others == [
        "t(text",
        "n(one",
        "t(String",
        "n(template.text",
        "t(template.text",
    ], "only their definitions, said()'s t(String(text)) and rendered()'s templates (said() on the server)"


def test_a_plural_may_leave_out_the_count_where_english_does(tmp_path, monkeypatch):
    # "every minute" / "every %n minutes": the form of 1 without %n, as the English one.
    monkeypatch.setattr(l10n, "L10N", tmp_path)
    key = "_every minute_::_every %n minutes_"
    data = {"translations": {key: ["jede Minute", "alle %n Minuten"]}, "pluralForm": "nplurals=2; plural=(n != 1);"}
    (tmp_path / "xx.json").write_text(json.dumps(data), encoding="utf-8")
    assert not l10n.problems("xx", {key: ""})
    data["translations"][key] = ["jede Minute", "alle Minuten"]
    (tmp_path / "xx.json").write_text(json.dumps(data), encoding="utf-8")
    assert any("%n differs" in problem for problem in l10n.problems("xx", {key: ""}))
