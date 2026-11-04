# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the app's routes share: the database and the executor (STATE, set at start), the
person's client (NC), a job as the page sends it (JobIn) and the checks a saved job passes."""

from typing import Annotated

from croniter import croniter
from fastapi import Depends, HTTPException
from nc_py_api import AsyncNextcloudApp
from nc_py_api.ex_app import anc_app
from pydantic import BaseModel, Field, field_validator

from .. import browser, ondemand, schedule, watch
from ..runner import COMMON_OPTIONS
from ..sources import SOURCES, Option, SourceError
from ..store import Job, Store

STATE: dict = {}
NC = Annotated[AsyncNextcloudApp, Depends(anc_app)]


def store() -> Store:
    """The database, opened at start."""
    return STATE["store"]


class JobIn(BaseModel):
    """A job as the page sends it."""

    name: str = Field(min_length=1, max_length=200)
    source: str = Field(max_length=40)
    target: str = Field(min_length=1, max_length=1000)
    # Cron; "" for an on-demand job, which runs when its person sends it an address.
    schedule: str = Field("0 6 * * *", max_length=100)
    # Bounded: a recipe is a few kilobytes; nothing a person types needs more.
    options: dict[Annotated[str, Field(max_length=60)], Annotated[str, Field(max_length=100_000)] | int] = Field(
        {}, max_length=40
    )
    tags: list[Annotated[str, Field(max_length=200)]] = Field([], max_length=50)
    enabled: bool = True
    # The schedule's time zone (the browser's, as the page sends it); UTC when none is sent.
    timezone: str = Field("UTC", max_length=64)
    # An on-demand job deleted after a run that went through.
    once: bool = False

    @field_validator("source")
    @classmethod
    def known_source(cls, value: str) -> str:
        """Only the sources the app has."""
        if value not in SOURCES:
            raise ValueError(f"unknown source {value}")
        return value

    @field_validator("schedule")
    @classmethod
    def valid_schedule(cls, value: str) -> str:
        """A cron expression (minute hour day month weekday), or none (on demand)."""
        if value.strip() and not croniter.is_valid(value):
            raise ValueError("not a cron expression")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        """An IANA time zone (Europe/Berlin)."""
        return schedule.checked_zone(value)

    @field_validator("target")
    @classmethod
    def absolute_target(cls, value: str) -> str:
        """A folder of the user's, from the root of their files."""
        return "/" + value.strip("/")

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        """Without blanks or repeats."""
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))


async def own_job(job_id: int, nc: AsyncNextcloudApp) -> Job:
    """The user's job, or 404 (also for another user's)."""
    job = store().job(job_id)
    if job is None or job.user != await nc.user:
        raise HTTPException(404, "no such job")
    return job


def option_problem(option: Option, value: str | int) -> str:
    """What is wrong with an option's value for its kind ("" if nothing): a flag is 0 or 1, a
    number a whole number within its bounds, a choice one of its choices (several, comma-separated,
    where it allows them)."""
    text = str(value).strip()
    if option.kind == "flag":
        return "" if text in ("0", "1") else "must be on or off (1 or 0)"
    if option.choices:
        chosen = [part.strip() for part in text.split(",")] if option.multiple else [text]
        # None chosen of several: the default.
        if (option.multiple and not text) or all(part in option.choices for part in chosen):
            return ""
        return f"must be {'one or several' if option.multiple else 'one'} of {', '.join(option.choices)}"
    return number_problem(option, text) if isinstance(option.default, int) else ""


def number_problem(option: Option, text: str) -> str:
    """What is wrong with a number option's value ("" if nothing): a whole number within its
    bounds, its words saying which."""
    lowest, highest = option.minimum, option.maximum
    # A minimum of -1 is "no limit" (Files per run): 0 would fetch nothing, ever.
    unlimited = lowest == -1
    if unlimited:
        lowest, bounds = 1, " of at least 1 (or -1: no limit)"
    elif lowest is not None and highest is not None:
        bounds = f" from {lowest} to {highest}"
    else:
        bounds = f" of at least {lowest}" if lowest is not None else ""
    try:
        number = int(text)
    except ValueError:
        return f"must be a number{bounds}"
    if unlimited and number == -1:
        return ""
    if (lowest is not None and number < lowest) or (highest is not None and number > highest):
        return f"must be a number{bounds}"
    return ""


def checked(body: JobIn) -> JobIn:
    """The job, if each option has a value of its kind (else the refusal names the option as the
    form does) and its source accepts them (a recipe that can run, for instance). An on-demand
    job's source must have an address a fetch replaces; its own may be empty."""
    if not body.schedule and not ondemand.offered(body.source):
        raise HTTPException(422, f"{SOURCES[body.source].title} has no address to send: it runs on a schedule.")
    if body.once and body.schedule:
        raise HTTPException(422, "Only an on-demand job is deleted after its run.")
    options = SOURCES[body.source].options | COMMON_OPTIONS | watch.options_of(body.source)
    for name, option in options.items():
        problem = option_problem(option, body.options[name]) if name in body.options else ""
        if problem:
            title = f"Through {browser.PROXY_NAME}" if name == "home" else option.title
            raise HTTPException(422, f"{title} {problem}")
    given = body.options if body.schedule else ondemand.checked_options(body.source, body.options)
    try:
        SOURCES[body.source](given, {}, None).close()
    except SourceError as error:
        raise HTTPException(422, str(error)) from error
    return body


def switched_on(job: Job, name: str) -> bool:
    """Whether the job's flag is on (a value that is no number, saved before the options were
    checked, is off)."""
    try:
        return bool(int(job.options.get(name, 0) or 0))
    except (TypeError, ValueError):
        return False
