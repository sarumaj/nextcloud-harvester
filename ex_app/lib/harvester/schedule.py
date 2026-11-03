# SPDX-License-Identifier: AGPL-3.0-or-later
"""A job's schedule (cron, in the job's time zone) in words, and its next runs, for the job's form.

The next runs come from croniter, as the executor's do; the words cover the usual forms
(every n minutes, hourly, daily, on weekdays, monthly) and fall back to field by field. They are
said() texts: English here, translated on the page, each form a whole phrase.
"""

import datetime
import functools
import zoneinfo

from croniter import croniter

from .l10n import Said, listed, said, said_n

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]  # fmt: skip
NAMES = {name[:3].lower(): str(index) for index, name in enumerate(DAYS)} | {
    name[:3].lower(): str(index) for index, name in enumerate(MONTHS) if name
}
# The names in words the page translates (said() of a plain string, which scripts/l10n.py finds).
DAY_WORDS = [
    said("Sunday"),
    said("Monday"),
    said("Tuesday"),
    said("Wednesday"),
    said("Thursday"),
    said("Friday"),
    said("Saturday"),
]
MONTH_WORDS = [
    said(""),
    said("January"),
    said("February"),
    said("March"),
    said("April"),
    said("May"),
    said("June"),
    said("July"),
    said("August"),
    said("September"),
    said("October"),
    said("November"),
    said("December"),
]


def _numbers(field: str) -> list[int] | None:
    """The values of a field of plain numbers or names (5, mon, 1,15), else None."""
    values = []
    for part in field.lower().split(","):
        part = NAMES.get(part, part)
        if not part.isdigit():
            return None
        values.append(int(part))
    return values


def _times(minute: str, hour: str) -> Said | None:
    """The times of day (06:00 and 18:30) when minute and hour are plain, else None."""
    minutes, hours = _numbers(minute), _numbers(hour)
    if minutes is None or hours is None:
        return None
    return listed([f"{h:02d}:{m:02d}" for h in hours for m in minutes])


def _days(  # pylint: disable=too-many-return-statements,too-many-branches  # a phrase for each form
    day: str, month: str, weekday: str, at: Said | None = None
) -> Said:
    """When in the month and week, as a whole phrase ("every day", "on Monday", "on day 1 of
    every month"), with the times of day when given (at: "every day at 06:00"); a combination of
    fields the usual forms do not cover names them as they are."""
    weekday = "*" if weekday == "?" else weekday
    day = "*" if day == "?" else day
    weekdays, days, months = _numbers(weekday), _numbers(day), _numbers(month)
    if day == "*" and month == "*":
        if weekday == "*":
            return said("every day at {times}", times=at) if at else said("every day")
        if weekday == "1-5" or weekday.lower() == "mon-fri":
            return said("on weekdays at {times}", times=at) if at else said("on weekdays")
        if weekday.lower() in ("0,6", "6,0", "sat,sun", "sun,sat"):
            return said("on weekends at {times}", times=at) if at else said("on weekends")
        if weekdays is not None:
            names = listed([DAY_WORDS[d % 7] for d in weekdays])
            return said("on {days} at {times}", days=names, times=at) if at else said("on {days}", days=names)
    elif weekday == "*" and month == "*":
        if day.upper() == "L":
            if at:
                return said("on the last day of every month at {times}", times=at)
            return said("on the last day of every month")
        if days is not None:
            if at:
                return said_n(
                    "on day {days} of every month at {times}",
                    "on days {days} of every month at {times}",
                    len(days),
                    days=listed(days),
                    times=at,
                )
            return said_n("on day {days} of every month", "on days {days} of every month", len(days), days=listed(days))
    elif weekday == "*" and months is not None and all(1 <= m <= 12 for m in months):
        names = listed([MONTH_WORDS[m] for m in months])
        if day == "*":
            if at:
                return said("every day in {months} at {times}", months=names, times=at)
            return said("every day in {months}", months=names)
        if days is not None:
            if at:
                return said_n(
                    "on day {days} in {months} at {times}",
                    "on days {days} in {months} at {times}",
                    len(days),
                    days=listed(days),
                    months=names,
                    times=at,
                )
            return said_n(
                "on day {days} in {months}", "on days {days} in {months}", len(days), days=listed(days), months=names
            )
    # The rare forms: the fields as they are.
    fields = {"day": day, "month": month, "weekday": weekday}
    if at:
        return said("days {day}, months {month}, days of the week {weekday}, at {times}", **fields, times=at)
    return said("days {day}, months {month}, days of the week {weekday}", **fields)


@functools.cache
def _zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def checked_zone(name: str) -> str:
    """The time zone's IANA name (Europe/Berlin, UTC); ValueError when there is no such zone."""
    if name != "UTC" and name not in _zones():
        raise ValueError(said("unknown time zone “{zone}” (an IANA name, e.g. Europe/Berlin)", zone=name))
    return name


def zone(name: str) -> datetime.tzinfo:
    """The time zone of that name; UTC for a name this system does not know (a job's zone is
    checked when it is saved, so only a system without it gets here)."""
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return datetime.UTC


def describe(expression: str) -> Said:  # pylint: disable=too-many-return-statements  # a sentence for each form
    """The schedule in words (its times are the job's time zone's, which the form names); ValueError
    (a said() text) when it is not a cron expression."""
    if not croniter.is_valid(expression):
        raise ValueError(said("not a cron expression: minute hour day month weekday, e.g. 0 6 * * *"))
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(said("five fields, please: minute hour day month weekday"))
    minute, hour, day, month, weekday = fields
    times = _times(minute, hour)
    if times:
        return _days(day, month, weekday, times)
    when = _days(day, month, weekday)
    every_day = when.template["text"] == "every day"
    if minute.startswith("*/") and hour == "*" and minute[2:].isdigit():
        if every_day:
            return said_n("every minute", "every %n minutes", int(minute[2:]))
        return said_n("every minute, {when}", "every %n minutes, {when}", int(minute[2:]), when=when)
    if minute == "*" and hour == "*":
        return said("every minute") if every_day else said("every minute, {when}", when=when)
    if hour == "*" and _numbers(minute) is not None:
        minutes = listed(minute.split(","))
        if every_day:
            return said("every hour at minute {minutes}", minutes=minutes)
        return said("every hour at minute {minutes}, {when}", minutes=minutes, when=when)
    if hour.startswith("*/") and hour[2:].isdigit() and _numbers(minute) is not None:
        minutes = listed(minute.split(","))
        if every_day:
            return said_n(
                "every hour at minute {minutes}", "every %n hours at minute {minutes}", int(hour[2:]), minutes=minutes
            )
        return said_n(
            "every hour at minute {minutes}, {when}",
            "every %n hours at minute {minutes}, {when}",
            int(hour[2:]),
            minutes=minutes,
            when=when,
        )
    return said("minute {minute}, hour {hour}, {when}", minute=minute, hour=hour, when=when)


def next_runs(
    expression: str, count: int = 3, now: datetime.datetime | None = None, timezone: str = "UTC"
) -> list[float]:
    """The next runs as timestamps, as the executor finds them (the schedule's hours in the
    time zone)."""
    start = (now or datetime.datetime.now(datetime.UTC)).astimezone(zone(checked_zone(timezone)))
    runs = croniter(expression, start)
    return [runs.get_next(float) for _ in range(count)]
