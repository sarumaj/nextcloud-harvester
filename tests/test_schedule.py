# SPDX-License-Identifier: AGPL-3.0-or-later
"""A job's schedule in words and its next runs, as the form shows them while it is typed."""

# pylint: disable=missing-function-docstring

import datetime

import pytest

from harvester.l10n import Said
from harvester.schedule import describe, next_runs


@pytest.mark.parametrize(
    ("expression", "words"),
    [
        ("0 6 * * *", "every day at 06:00"),
        ("30 7 * * 1-5", "on weekdays at 07:30"),
        ("0 9 * * 0,6", "on weekends at 09:00"),
        ("15 2 * * mon,thu", "on Monday and Thursday at 02:15"),
        ("*/15 * * * *", "every 15 minutes"),
        ("*/15 * * * 1-5", "every 15 minutes, on weekdays"),
        ("0 */6 * * *", "every 6 hours at minute 0"),
        ("0 8 1,15 * *", "on days 1 and 15 of every month at 08:00"),
        ("0 6 1 1,7 *", "on day 1 in January and July at 06:00"),
        ("0 6 1-10 * 1", "days 1-10, months *, days of the week 1, at 06:00"),
        ("0 6 L * *", "on the last day of every month at 06:00"),
        ("0 6,18 * * *", "every day at 06:00 and 18:00"),
    ],
)
def test_schedules_in_words(expression, words):
    assert describe(expression) == words


def test_the_words_are_whole_templates_the_page_translates():
    # The usual forms: one template with the times as a list (joined in the person's language).
    assert describe("0 6,18 * * *").template == {
        "text": "every day at {times}",
        "params": {"times": {"list": ["06:00", "18:00"], "kind": "and"}},
    }
    # Days by name: each a text of its own; a count: n()'s forms.
    assert describe("15 2 * * mon,thu").template == {
        "text": "on {days} at {times}",
        "params": {
            "days": {"list": [{"text": "Monday"}, {"text": "Thursday"}], "kind": "and"},
            "times": {"list": ["02:15"], "kind": "and"},
        },
    }
    assert describe("*/15 * * * *").template == {
        "text": "every minute",
        "plural": "every %n minutes",
        "count": 15,
    }
    assert describe("*/1 * * * *") == "every minute"


def test_wrong_schedules_say_how():
    for expression in ("61 * * * *", "bad", "0 6 * *"):
        with pytest.raises(ValueError, match="minute hour day month weekday") as raised:
            describe(expression)
        assert isinstance(raised.value.args[0], Said), "a template the form translates"


def test_the_next_runs_are_the_executors():
    now = datetime.datetime(2026, 10, 31, 22, 0, tzinfo=datetime.UTC)
    runs = next_runs("0 6 * * 1-5", now=now)
    assert [datetime.datetime.fromtimestamp(at, datetime.UTC).strftime("%a %H:%M") for at in runs] == [
        "Mon 06:00",
        "Tue 06:00",
        "Wed 06:00",
    ]
