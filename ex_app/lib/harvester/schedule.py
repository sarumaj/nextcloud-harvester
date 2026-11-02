"""A job's schedule (cron, in the job's time zone) in words, and its next runs, for the job's form.

The next runs come from croniter, as the executor's do; the words cover the usual forms
(every n minutes, hourly, daily, on weekdays, monthly) and fall back to field by field.
"""

import datetime
import functools
import zoneinfo

from croniter import croniter

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]  # fmt: skip
NAMES = {name[:3].lower(): str(index) for index, name in enumerate(DAYS)} | {
    name[:3].lower(): str(index) for index, name in enumerate(MONTHS) if name
}


def _numbers(field: str) -> list[int] | None:
    """The values of a field of plain numbers or names (5, mon, 1,15), else None."""
    values = []
    for part in field.lower().split(","):
        part = NAMES.get(part, part)
        if not part.isdigit():
            return None
        values.append(int(part))
    return values


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _times(minute: str, hour: str) -> str | None:
    """'at 06:00' or 'at 06:00 and 18:30' when minute and hour are plain, else None."""
    minutes, hours = _numbers(minute), _numbers(hour)
    if minutes is None or hours is None:
        return None
    return "at " + _join([f"{h:02d}:{m:02d}" for h in hours for m in minutes])


def _days(day: str, month: str, weekday: str) -> str:
    """When in the month and week: 'every day', 'on Monday', 'on day 1 of every month', ..."""
    weekday = "*" if weekday == "?" else weekday
    day = "*" if day == "?" else day
    parts = []
    if weekday == "1-5" or weekday.lower() == "mon-fri":
        parts.append("on weekdays")
    elif weekday in ("0,6", "6,0", "sat,sun", "sun,sat"):
        parts.append("on weekends")
    elif weekday != "*":
        days = _numbers(weekday)
        parts.append("on " + _join([DAYS[d % 7] for d in days]) if days is not None else f"on weekdays {weekday}")
    if day.upper() == "L":
        parts.append("on the last day")
    elif day != "*":
        days = _numbers(day)
        parts.append(("on day " + _join([str(d) for d in days])) if days is not None else f"on days {day}")
    if month != "*":
        months = _numbers(month)
        parts.append(("in " + _join([MONTHS[m] for m in months])) if months is not None else f"in months {month}")
    elif day != "*":
        parts.append("of every month")
    return " ".join(parts) or "every day"


@functools.cache
def _zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def checked_zone(name: str) -> str:
    """The time zone's IANA name (Europe/Berlin, UTC); ValueError when there is no such zone."""
    if name != "UTC" and name not in _zones():
        raise ValueError(f"unknown time zone {name!r} (an IANA name, e.g. Europe/Berlin)")
    return name


def zone(name: str) -> datetime.tzinfo:
    """The time zone of that name; UTC for a name this system does not know (a job's zone is
    checked when it is saved, so only a system without it gets here)."""
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return datetime.UTC


def describe(expression: str) -> str:
    """The schedule in words (its times are the job's time zone's, which the form names); ValueError
    when it is not a cron expression."""
    if not croniter.is_valid(expression):
        raise ValueError("not a cron expression: minute hour day month weekday, e.g. 0 6 * * *")
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("five fields, please: minute hour day month weekday")
    minute, hour, day, month, weekday = fields
    when = _days(day, month, weekday)
    if minute.startswith("*/") and hour == "*":
        return f"every {minute[2:]} minutes, {when}"
    if minute == "*" and hour == "*":
        return f"every minute, {when}"
    if hour == "*" and _numbers(minute) is not None:
        return f"every hour at minute {_join(minute.split(','))}, {when}"
    if hour.startswith("*/") and _numbers(minute) is not None:
        return f"every {hour[2:]} hours at minute {_join(minute.split(','))}, {when}"
    times = _times(minute, hour)
    if times:
        return f"{when} {times}"
    return f"minute {minute}, hour {hour}, {when}"


def next_runs(
    expression: str, count: int = 3, now: datetime.datetime | None = None, timezone: str = "UTC"
) -> list[float]:
    """The next runs as timestamps, as the executor finds them (the schedule's hours in the
    time zone)."""
    start = (now or datetime.datetime.now(datetime.UTC)).astimezone(zone(checked_zone(timezone)))
    runs = croniter(expression, start)
    return [runs.get_next(float) for _ in range(count)]
