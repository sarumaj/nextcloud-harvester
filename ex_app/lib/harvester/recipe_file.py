# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipes as files: a job's recipe and settings exported as JSON, to keep or to share, and a job
made from such a file (its person chooses the folder and the account; nothing is saved until
Save). A person's own templates are such files too, kept in the app's database for them alone.

    {
      "format": "harvester-recipe", "version": 1,
      "name": "iX", "source": "site", "schedule": "0 6 * * *", "once": false,
      "tags": ["iX {year}"],
      "options": {"recipe": {"start": ..., "follow": [...], "links": {...}}, "path": "{year}/..."}
    }

Never in a file: the account (user names, passwords, tokens, cookies, rclone's configuration are
Nextcloud's preferences, not options, and a source's credentials are refused as options), the
kept session, the folder, the time zone (the importing person's) and options the source does not
have. A file is data only: its schema is checked (RecipeFile: known fields of their kinds and
sizes), then its options by the checks a saved job passes (main.checked); nothing in it runs.
"""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .destinations import WHERE
from .runner import COMMON_OPTIONS
from .sources import SOURCES
from .watch import options_of

KIND = "harvester-recipe"
# The file format's version: a file of a later version is refused (its fields may mean more).
VERSION = 1
# A recipe is a few kilobytes; a file this large is something else.
MAX_BYTES = 256_000


class RecipeFile(BaseModel):
    """A recipe file's schema: every field known, of its kind and size (as a job's form sends
    them, main.JobIn); anything else in the file is refused."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["harvester-recipe"]
    version: int
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(max_length=40)
    # Cron; "" for an on-demand job.
    schedule: str = Field("", max_length=100)
    once: bool = False
    tags: list[Annotated[str, Field(max_length=200)]] = Field([], max_length=50)
    # A website's recipe may be an object (readable, as in the templates) or its JSON text.
    options: dict[Annotated[str, Field(max_length=60)], Annotated[str, Field(max_length=100_000)] | int | dict] = Field(
        {}, max_length=40
    )

    @field_validator("version")
    @classmethod
    def known_version(cls, value: int) -> int:
        """This app's version of the format, or an earlier one."""
        if not 1 <= value <= VERSION:
            raise ValueError(f"version {value} is not one this Harvester reads (up to {VERSION})")
        return value

    @field_validator("source")
    @classmethod
    def known_source(cls, value: str) -> str:
        """Only the sources the app has."""
        if value not in SOURCES:
            raise ValueError(f"unknown source {value}")
        return value


def known_options(source: str) -> set[str]:
    """The options a job of the source has (its own, every job's, the watch's), without any of
    its account's names and where its files go."""
    names = set(SOURCES[source].options | COMMON_OPTIONS | options_of(source))
    # Where the files go is the person's, as the folder is (destinations.WHERE).
    return names - set(SOURCES[source].credentials) - set(WHERE)


def without_secrets(recipe: str) -> str | dict:
    """A website's recipe for a file: an object, its steps without a value where they fill in the
    account (the recorder keeps none there; a recipe edited by hand might); as it is when it is
    no JSON object (the import's checks say why)."""
    try:
        parsed = json.loads(recipe or "{}")
    except ValueError:
        return recipe
    if not isinstance(parsed, dict):
        return recipe
    steps = parsed.get("steps")
    if isinstance(steps, list):
        parsed["steps"] = [
            (
                {key: value for key, value in step.items() if key != "value"}
                if isinstance(step, dict) and step.get("credential")
                else step
            )
            for step in steps
        ]
    return parsed


def export(job: dict) -> dict:
    """The recipe file of a job (its fields as the form sends them, checked): its source, name,
    schedule, tags and options, without its account, session or folder."""
    options = {key: value for key, value in job["options"].items() if key in known_options(job["source"])}
    if isinstance(options.get("recipe"), str):
        options["recipe"] = without_secrets(options["recipe"])
    return {
        "format": KIND,
        "version": VERSION,
        "name": job["name"],
        "source": job["source"],
        "schedule": job["schedule"],
        "once": bool(job.get("once")),
        "tags": list(job["tags"]),
        "options": options,
    }


def problems(error: ValidationError) -> str:
    """A refused file's problems, by field, as the page shows them."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'the file'}: {item['msg']}" for item in error.errors()
    )


def parse(text: str) -> dict:
    """The job a recipe file describes, for the form (its folder empty, enabled): ValueError
    with why when the file is too large, no JSON, of another format or version, of fields the
    schema does not know or of the wrong kind, or with options its source does not have. Its
    options are checked further as a saved job's are (main.checked)."""
    if len(text.encode()) > MAX_BYTES:
        raise ValueError(f"The file is larger than a recipe ({MAX_BYTES // 1000} KB at most).")
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ValueError(f"The file is no JSON: {error}") from error
    if not isinstance(data, dict) or data.get("format") != KIND:
        raise ValueError("The file is no Harvester recipe (its format is not harvester-recipe).")
    try:
        file = RecipeFile.model_validate(data)
    except ValidationError as error:
        raise ValueError(f"The recipe file is refused: {problems(error)}") from error
    unknown = sorted(set(file.options) - known_options(file.source))
    if unknown:
        raise ValueError(
            f"The recipe file is refused: options {SOURCES[file.source].title} has not: {', '.join(unknown)}"
        )
    options: dict[str, str | int] = {}
    for key, value in file.options.items():
        if isinstance(value, dict):
            if key != "recipe":
                raise ValueError(f"The recipe file is refused: options.{key} must be text or a number")
            value = json.dumps(value, indent=2)
        options[key] = value
    return {
        "name": file.name,
        "source": file.source,
        "schedule": file.schedule,
        "once": file.once,
        "tags": file.tags,
        "options": options,
        "target": "",
        "enabled": True,
    }
