"""Harvester: fetches files from websites into Nextcloud on a schedule, and tags them."""

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse
from typing import Annotated, Any, Literal

from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from nc_py_api import AsyncNextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, anc_app, persistent_storage, run_app, set_handlers
from pydantic import BaseModel, Field, field_validator

from harvester import browser, schedule, settings, sharing
from harvester.rclone_wizard import AUTHORIZE_ORIGIN, Authorization, WizardError, Wizards, remote_types
from harvester.browser.recorder import Recorder
from harvester.executor import (
    ADDRESS,
    RUNNERS,
    Executor,
    browsers_in_use,
    connect_nextcloud,
    credential_key,
    load_session,
    save_session,
    session_key,
)
from harvester.presets import PRESETS, make_job
from harvester.runner import COMMON_OPTIONS
from harvester.sources.download import DOWNLOAD_OPTIONS
from harvester.sources import SOURCES, Option, SourceError
from harvester.store import Job, Store

LOGGER = logging.getLogger("harvester")

STATE: dict = {}
# The job form's rclone wizards (rclone_wizard.py), in memory: one ends with the app.
WIZARDS = Wizards()
# A recording ends after this many seconds without a message from the page.
RECORDER_IDLE = 900


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Opens the database, starts the executor and registers the app's page."""
    # First: the app's environment (its secrets) out of the reach of its browsers and helpers.
    if not browser.undumpable():
        LOGGER.warning("the app's process stays dumpable: its helpers could read its environment")
    STATE["store"] = Store(os.path.join(persistent_storage(), "harvester.db"))
    STATE["executor"] = Executor(STATE["store"])
    STATE["executor"].start()
    set_handlers(app, enabled_handler)
    yield
    WIZARDS.close()


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)
NC = Annotated[AsyncNextcloudApp, Depends(anc_app)]


def allowed_origin(origin: str) -> bool:
    """Whether a page of this origin may open the recorder: Nextcloud's own (NEXTCLOUD_URL), or one
    of HARVESTER_ORIGINS (comma-separated, for a Nextcloud reached under more names). No origin:
    not a browser, which HaRP authenticated all the same."""
    if not origin:
        return True
    allowed = {f"{urlparse(url).scheme}://{urlparse(url).netloc}" for url in [os.environ.get("NEXTCLOUD_URL", "")]}
    allowed |= {item.strip().rstrip("/") for item in os.environ.get("HARVESTER_ORIGINS", "").split(",") if item.strip()}
    return origin.rstrip("/") in allowed


def store() -> Store:
    """The database, opened at start."""
    return STATE["store"]


class JobIn(BaseModel):
    """A job as the page sends it."""

    name: str = Field(min_length=1, max_length=200)
    source: str = Field(max_length=40)
    target: str = Field(min_length=1, max_length=1000)
    schedule: str = Field("0 6 * * *", max_length=100)
    # Bounded: a recipe is a few kilobytes; nothing a person types needs more.
    options: dict[Annotated[str, Field(max_length=60)], Annotated[str, Field(max_length=100_000)] | int] = Field(
        {}, max_length=40
    )
    tags: list[Annotated[str, Field(max_length=200)]] = Field([], max_length=50)
    enabled: bool = True
    # The schedule's time zone (the browser's, as the page sends it); UTC when none is sent.
    timezone: str = Field("UTC", max_length=64)

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
        """A cron expression (minute hour day month weekday)."""
        if not croniter.is_valid(value):
            raise ValueError("not a cron expression")
        return value

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


# A user's groups (Nextcloud's), and people's display names, kept a while: asked for on every
# poll of the page otherwise. A user taken out of a group loses its shares within GROUPS_KEPT.
GROUPS_KEPT = 60
NAMES_KEPT = 600
_GROUPS: dict[str, tuple[float, set[str]]] = {}
_NAMES: dict[str, tuple[float, str]] = {}


async def as_user(user: str) -> AsyncNextcloudApp:
    """A client of the app's acting as the user (AppAPI's impersonation, as the runs do): for
    what is the job owner's while another person works on the job (its account, their name)."""
    nc = connect_nextcloud()
    await nc.set_user(user)
    return nc


async def groups_of(nc: AsyncNextcloudApp, user: str) -> set[str]:
    """The user's groups in Nextcloud (asked as the user: a person may read their own), kept
    GROUPS_KEPT seconds; none when Nextcloud cannot tell (no group's share counts then)."""
    kept = _GROUPS.get(user)
    if kept and time.monotonic() - kept[0] < GROUPS_KEPT:
        return kept[1]
    try:
        asking = nc if user == await nc.user else await as_user(user)
        found = await asking.ocs("GET", f"/ocs/v1.php/cloud/users/{quote(user, safe='')}/groups")
        groups = {str(group) for group in (found or {}).get("groups", [])}
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.warning("the groups of %s could not be read", user, exc_info=True)
        return set()
    _GROUPS[user] = (time.monotonic(), groups)
    return groups


async def display_name(user: str) -> str:
    """The user's display name in Nextcloud (their own profile, asked as them), kept NAMES_KEPT
    seconds; their id when Nextcloud cannot tell."""
    kept = _NAMES.get(user)
    if kept and time.monotonic() - kept[0] < NAMES_KEPT:
        return kept[1]
    try:
        name = (await (await as_user(user)).users.get_user()).display_name or user
    except Exception:  # pylint: disable=broad-exception-caught
        LOGGER.warning("the display name of %s could not be read", user, exc_info=True)
        return user
    _NAMES[user] = (time.monotonic(), name)
    return name


async def job_access(job_id: int, nc: AsyncNextcloudApp, need: int = sharing.VIEW) -> tuple[Job, int]:
    """The job and what the user may do with it (sharing's bits; all for its owner). 404 when
    they may not see it (as for a job that does not exist: its existence is not told), 403
    when they see it but lack the permission needed."""
    user = await nc.user
    job = store().job(job_id)
    allowed = 0
    if job is not None:
        allowed = await sharing.permissions(store(), job, user, lambda someone: groups_of(nc, someone))
    if job is None or not allowed:
        raise HTTPException(404, "no such job")
    if need & ~allowed:
        what = ", ".join(sharing.names(need & ~allowed))
        raise HTTPException(403, f"The job is shared with you without that ({what}): ask its owner.")
    return job, allowed


async def visible_jobs(nc: AsyncNextcloudApp) -> list[tuple[Job, int]]:
    """The user's jobs and those shared with them, with what they may do with each."""
    user = await nc.user
    mine = [(job, sharing.ALL) for job in store().jobs(user)]
    shared = []
    for job in store().shared_with(user, await groups_of(nc, user)):
        allowed = await sharing.permissions(store(), job, user, lambda someone: groups_of(nc, someone))
        if allowed:
            shared.append((job, allowed))
    return sorted(mine + shared, key=lambda item: item[0].id or 0)


def only_owner(job: Job, user: str, what: str) -> None:
    """403 unless the user owns the job: what (its account, its browser) is the owner's alone."""
    if job.user != user:
        raise HTTPException(403, f"Only the job's owner may {what}.")


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
    form does) and its source accepts them (a recipe that can run, for instance)."""
    options = SOURCES[body.source].options | COMMON_OPTIONS
    for name, option in options.items():
        problem = option_problem(option, body.options[name]) if name in body.options else ""
        if problem:
            title = f"Through {browser.PROXY_NAME}" if name == "home" else option.title
            raise HTTPException(422, f"{title} {problem}")
    try:
        SOURCES[body.source](body.options, {}, None).close()
    except SourceError as error:
        raise HTTPException(422, str(error)) from error
    return body


@APP.get("/api/sources")
def list_sources() -> list[dict]:
    """The sources with their options and credentials, for the job form."""
    return [
        {
            "name": source.name,
            "title": source.title,
            "description": source.description,
            "credentials": list(source.credentials),
            "credential_help": {name: list(help_) for name, help_ in source.credential_help.items()},
            "options": {
                key: vars(option) | ({"title": f"Through {browser.PROXY_NAME}"} if key == "home" else {})
                for key, option in (source.options | COMMON_OPTIONS).items()
                if key != "home" or browser.PROXY
            },
        }
        for source in SOURCES.values()
    ]


@APP.get("/api/presets")
def list_presets() -> list[dict]:
    """The templates of jobs for known sites, with their parameters."""
    return [
        {
            "id": preset_id,
            "title": p["title"],
            "source": p.get("source") or p["job"].get("source", "site"),
            "description": p["description"],
            "parameters": p.get("parameters", {}),
        }
        for preset_id, p in sorted(PRESETS.items())
    ]


@APP.post("/api/presets/{preset_id}")
def preset_job(preset_id: str, body: dict[str, str]) -> dict:
    """A job made from the template with these parameters, for the form (not saved)."""
    if preset_id not in PRESETS:
        raise HTTPException(404, "no such template")
    try:
        return make_job(preset_id, body)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@APP.get("/api/schedule")
def explain_schedule(
    expression: Annotated[str, Query(max_length=100)], timezone: Annotated[str, Query(max_length=64)] = "UTC"
) -> dict:
    """The schedule in words and its next runs (timestamps), in the time zone (the browser's),
    for the job's form as it is typed."""
    try:
        return {
            "description": schedule.describe(expression),
            "next": schedule.next_runs(expression, timezone=schedule.checked_zone(timezone)),
        }
    except ValueError as error:
        return {"error": str(error)}


@APP.get("/api/jobs")
async def list_jobs(nc: NC) -> list[dict]:
    """The user's jobs and those shared with them, with how their last run went, which run or
    wait for their turn (and why they wait), whose they are and what the user may do with them;
    a job's shares for its owner (and those one who shares it on made)."""
    executor = STATE["executor"]
    user = await nc.user
    jobs = await visible_jobs(nc)
    # Queued first: a job taken in between is then running too (and shown so), not neither.
    queued = executor.queued
    running = executor.running
    waits = executor.waits([job for job, _ in jobs])
    listed = []
    for job, allowed in jobs:
        manages = job.user == user or allowed & sharing.RESHARE
        listed.append(
            vars(job)
            | {
                "running": job.id in running,
                "queued": job.id in queued,
                "paused": executor.paused(job.id or 0),
                "waiting": waits.get(job.id or 0, ""),
                "owner": job.user,
                "mine": job.user == user,
                "owner_name": await display_name(job.user) if job.user != user else "",
                "permissions": sharing.names(allowed),
            }
            | ({"shares": shares_out(sharing.visible_shares(store(), job, user))} if manages else {})
        )
    return listed


@APP.get("/api/status")
def queue_status() -> dict:
    """What everyone's jobs and recordings hold now (runners, browsers, the queue), without
    whose: so a person sees why their job waits."""
    return STATE["executor"].status()


@APP.get("/api/log")
async def progress_log(nc: NC, job: int | None = None, after: int = 0) -> dict:
    """The progress log after the line after, of one job the user may see, or of all; which of
    those jobs run now, which wait for their turn and why, and the queue's status."""
    jobs = [listed for listed, _ in await visible_jobs(nc)]
    if job is not None:
        await job_access(job, nc)
        jobs = [listed for listed in jobs if listed.id == job]
    ids = {listed.id for listed in jobs}
    executor = STATE["executor"]
    queued = executor.queued
    return {
        "lines": store().log_lines([listed.id or 0 for listed in jobs], max(after, 0)),
        "running": [job_id for job_id in executor.running if job_id in ids],
        "queued": [job_id for job_id in queued if job_id in ids],
        "waiting": {str(job_id): reason for job_id, reason in executor.waits(jobs).items()},
        "status": executor.status(),
    }


@APP.post("/api/jobs")
async def create_job(body: JobIn, nc: NC) -> dict:
    """Adds a job."""
    return vars(store().save(Job(user=await nc.user, **checked(body).model_dump())))


@APP.put("/api/jobs/{job_id}")
async def update_job(job_id: int, body: JobIn, nc: NC) -> dict:
    """Changes a job's settings; one it is shared with to edit changes all but its folder (the
    files land in its owner's), and may not send a job with its owner's account elsewhere."""
    job, _ = await job_access(job_id, nc, sharing.EDIT)
    changes = checked(body).model_dump()
    if job.user != await nc.user:
        changes["target"] = job.target
        moved = account_moves(job, changes["source"], changes["options"])
        if moved and await account_set(await as_user(job.user), job):
            raise HTTPException(403, f"The job signs in with its owner's account: only its owner may change {moved}.")
    saved = store().save(Job(id=job.id, user=job.user, created=job.created, **changes))
    forget_changed(job, saved)
    # Other tags: the files it saved get them (and lose those it gave and has no more) at once.
    if saved.tags != job.tags:
        STATE["executor"].retag(saved.id or 0, job.tags)
    return vars(saved)


# The options that say where a job's account goes (a mail server, its sign-in, a forge): with
# the owner's account set, only the owner changes them.
ACCOUNT_OPTIONS = ("host", "port", "auth", "token_url", "forge")


def hosts(options: dict) -> set[str]:
    """The hosts of the addresses in a job's options (its site, its recipe's pages)."""
    return {
        str(urlparse(found.group(0)).hostname).lower()
        for value in options.values()
        for found in ADDRESS.finditer(str(value))
        if urlparse(found.group(0)).hostname
    }


def sign_in_steps(options: dict) -> list:
    """A website job's recorded steps (its sign-in), or [] (another source, a broken recipe)."""
    try:
        return list(json.loads(str(options.get("recipe") or "{}") or "{}").get("steps", []))
    except (ValueError, AttributeError):
        return []


def account_moves(job: Job, source: str, options: dict) -> str:
    """What of a change (to source and options) would send the job's account elsewhere, in
    words ("" if nothing): another source, server, sign-in or forge, a site it did not go to, or
    other recorded steps (which fill in the account's fields)."""
    if source != job.source:
        return "its source"
    titles = SOURCES[source].options
    for name in ACCOUNT_OPTIONS:
        if str(options.get(name, "")) != str(job.options.get(name, "")):
            return titles[name].title if name in titles else name
    new = sorted(hosts(options) - hosts(job.options))
    if new:
        return f"the sites it goes to (new: {', '.join(new)})"
    if sign_in_steps(options) != sign_in_steps(job.options):
        return "the recipe's steps"
    return ""


async def account_set(owner: AsyncNextcloudApp, job: Job) -> bool:
    """Whether the owner set an account for the job, or it keeps a session (owner: a client
    acting as them)."""
    for key in [credential_key(job, name) for name in SOURCES[job.source].credentials] + [session_key(job)]:
        if await owner.preferences_ex.get_value(key):
            return True
    return False


# The options whose change makes a file refused by type or size worth trying again.
REFUSING = tuple(DOWNLOAD_OPTIONS)


def forget_changed(job: Job, saved: Job) -> None:
    """What the job's listing knew, when the options it lists by change: another source, recipe,
    address, folder or patterns lists other files, so "known files end the run" waits for its
    first complete pass again (and mail's handled messages are read again). Its file names, files
    per run, schedule or tags change nothing there. Other types or sizes try again the files they
    refused."""

    def changed(names) -> bool:
        return any(saved.options.get(name) != job.options.get(name) for name in names)

    if saved.source != job.source or changed(SOURCES[saved.source].listing_options):
        store().forget_marks(job.id or 0)
    if saved.source != job.source or changed(REFUSING):
        store().forget_refused(job.id or 0)


@APP.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int, nc: NC) -> dict:
    """Deletes a job, its own account (its owner's preferences) and its shares; the files it
    downloaded stay."""
    job, _ = await job_access(job_id, nc, sharing.DELETE)
    owner = nc if job.user == await nc.user else await as_user(job.user)
    await owner.preferences_ex.delete(
        [credential_key(job, name) for name in SOURCES[job.source].credentials] + [session_key(job)]
    )
    store().delete(job.id or 0)
    # Its run, if one goes on, ends at once: it would only write for a job that is gone.
    STATE["executor"].stop(job.id or 0)
    return {}


@APP.get("/api/jobs/{job_id}/shot")
async def get_shot(job_id: int, nc: NC) -> dict:
    """What the job's browser showed at its last failed step or challenge: the picture (a JPEG,
    base64), the page's address and title, what went wrong and when."""
    shot = store().shot((await job_access(job_id, nc))[0].id or 0)
    if not shot:
        raise HTTPException(404, "The job's browser has shown nothing to keep yet.")
    return {key: shot[key] for key in ("at", "url", "title", "what")} | {
        "image": base64.b64encode(shot["image"]).decode()
    }


@APP.post("/api/jobs/{job_id}/run")
async def run_job(job_id: int, nc: NC) -> dict:
    """Runs a job now (after the one running)."""
    STATE["executor"].request((await job_access(job_id, nc, sharing.RUN))[0].id)
    return {}


@APP.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: int, nc: NC) -> dict:
    """Pauses the running job before its next file."""
    if not STATE["executor"].pause((await job_access(job_id, nc, sharing.RUN))[0].id):
        raise HTTPException(409, "The job is not running.")
    return {}


@APP.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: int, nc: NC) -> dict:
    """Lets the paused job go on."""
    if not STATE["executor"].resume((await job_access(job_id, nc, sharing.RUN))[0].id):
        raise HTTPException(409, "The job is not running.")
    return {}


@APP.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: int, nc: NC) -> dict:
    """Ends the running job before its next file; what it saved stays."""
    if not STATE["executor"].stop((await job_access(job_id, nc, sharing.RUN))[0].id):
        raise HTTPException(409, "The job is not running.")
    return {}


@APP.get("/api/jobs/{job_id}/credentials")
async def get_credentials(job_id: int, nc: NC) -> dict[str, bool]:
    """Which of the job's credentials its owner has set (never their values); the owner's
    alone."""
    job, _ = await job_access(job_id, nc)
    only_owner(job, await nc.user, "see its account")
    return {
        name: bool(await nc.preferences_ex.get_value(credential_key(job, name)))
        for name in SOURCES[job.source].credentials
    }


@APP.put("/api/jobs/{job_id}/credentials")
async def set_credentials(
    job_id: int, body: dict[Annotated[str, Field(max_length=40)], Annotated[str, Field(max_length=65_536)]], nc: NC
) -> dict:
    """Stores the credentials given (an empty one is left as it was), encrypted by Nextcloud. A
    changed account forgets the job's kept session: its cookies, or a refresh token the provider
    replaced, are the old account's and would go on being used instead. The owner's alone."""
    job, _ = await job_access(job_id, nc)
    only_owner(job, await nc.user, "change its account")
    changed = False
    for name in SOURCES[job.source].credentials:
        if body.get(name) and body[name] != await nc.preferences_ex.get_value(credential_key(job, name)):
            await nc.preferences_ex.set_value(credential_key(job, name), body[name], sensitive=True)
            changed = True
    if changed:
        await nc.preferences_ex.delete(session_key(job))
    return {}


class ShareIn(BaseModel):
    """A share as the page sends it: with whom (a user or group, as Nextcloud's sharee search
    named them) and what they may do."""

    kind: Literal["user", "group"]
    sharee: str = Field(min_length=1, max_length=255)
    permissions: list[Literal["view", "run", "edit", "delete", "reshare"]] = Field([], max_length=5)


def shares_out(shares: list[dict]) -> list[dict]:
    """The shares as the page shows them: their permissions by name."""
    return [share | {"permissions": sharing.names(share["permissions"])} for share in shares]


# The sharees one search returns at most (Nextcloud's own share dialog asks for 25).
SHAREES = 25


async def search_sharees(nc: AsyncNextcloudApp, search: str) -> list[dict]:
    """The users and groups Nextcloud lets the user share with that match the search: its own
    sharee search for files, asked as the user, so the instance's sharing rules apply (only
    within one's groups, no groups, ...). Each {kind, sharee, label}; exact matches first."""
    found = await nc.ocs(
        "GET",
        "/ocs/v2.php/apps/files_sharing/api/v1/sharees",
        params={"search": search, "itemType": "file", "perPage": SHAREES, "lookup": "false", "shareType[]": [0, 1]},
    )
    found = found or {}
    results: dict[tuple[str, str], dict] = {}
    for group in (found.get("exact") or {}, found):
        for key, kind in (("users", "user"), ("groups", "group")):
            for entry in group.get(key) or []:
                value = entry.get("value") or {}
                sharee = str(value.get("shareWith", ""))
                if sharee and (kind, sharee) not in results:
                    results[(kind, sharee)] = {
                        "kind": kind,
                        "sharee": sharee,
                        "label": str(entry.get("label") or sharee),
                    }
    return list(results.values())


@APP.get("/api/sharees")
async def list_sharees(
    nc: NC, search: Annotated[str, Query(min_length=1, max_length=100)], job: int | None = None
) -> list[dict]:
    """The people and groups the user may share a job with (Nextcloud's sharee search), not the
    user themselves nor the job's owner."""
    user = await nc.user
    owner = (await job_access(job, nc))[0].user if job is not None else user
    try:
        found = await search_sharees(nc, search)
    except Exception as error:  # pylint: disable=broad-exception-caught
        LOGGER.warning("the sharee search failed: %s", error)
        raise HTTPException(502, "Nextcloud's search for people and groups failed.") from error
    return [item for item in found if not (item["kind"] == "user" and item["sharee"] in (user, owner))]


async def manage_shares(job_id: int, nc: AsyncNextcloudApp) -> tuple[Job, int, str]:
    """The job whose shares the user manages (its owner, or one it is shared with to share on),
    what they may do with it and who they are; else 403 (404 without access)."""
    job, allowed = await job_access(job_id, nc)
    user = await nc.user
    if job.user != user and not allowed & sharing.RESHARE:
        raise HTTPException(403, "The job is shared with you without that (reshare): ask its owner.")
    return job, allowed, user


@APP.get("/api/jobs/{job_id}/shares")
async def get_shares(job_id: int, nc: NC) -> list[dict]:
    """The job's shares: all of them for its owner, those they made for one who shares it on."""
    job, _, user = await manage_shares(job_id, nc)
    return shares_out(sharing.visible_shares(store(), job, user))


@APP.put("/api/jobs/{job_id}/shares")
async def put_share(job_id: int, body: ShareIn, nc: NC) -> list[dict]:
    """Shares the job with a user or group, or changes what a share allows: one who shares it on
    gives at most what they may do, and changes only the shares they made. The sharee must be
    one Nextcloud lets the user share with. Returns the shares the user manages."""
    job, allowed, user = await manage_shares(job_id, nc)
    permissions = sharing.bits(body.permissions)
    if body.kind == "user" and body.sharee in (user, job.user):
        raise HTTPException(422, "The job is yours or its owner's already.")
    if job.user != user and permissions & ~allowed:
        raise HTTPException(403, "You may share the job on with at most what you may do with it.")
    existing = {(share["kind"], share["sharee"]): share for share in store().shares(job.id or 0)}
    share = existing.get((body.kind, body.sharee))
    if share and user not in (job.user, share["by"]):
        raise HTTPException(403, "Another person shared the job with them: only they or its owner change that.")
    if share:
        label = share["label"]
    else:
        try:
            found = await search_sharees(nc, body.sharee)
        except Exception as error:  # pylint: disable=broad-exception-caught
            LOGGER.warning("the sharee search failed: %s", error)
            raise HTTPException(502, "Nextcloud's search for people and groups failed.") from error
        match = [item for item in found if (item["kind"], item["sharee"]) == (body.kind, body.sharee)]
        if not match:
            raise HTTPException(422, f"Nextcloud does not let you share with this {body.kind}: {body.sharee}.")
        label = match[0]["label"]
    store().share(
        job.id or 0,
        {"kind": body.kind, "sharee": body.sharee, "label": label, "permissions": permissions, "by": user},
    )
    return shares_out(sharing.visible_shares(store(), job, user))


@APP.delete("/api/jobs/{job_id}/shares/{kind}/{sharee}")
async def delete_share(job_id: int, kind: Literal["user", "group"], sharee: str, nc: NC) -> list[dict]:
    """Ends a share: the owner any, one who shares it on those they made. Returns the shares
    the user manages."""
    job, _, user = await manage_shares(job_id, nc)
    share = next(
        (item for item in store().shares(job.id or 0) if (item["kind"], item["sharee"]) == (kind, sharee)), None
    )
    if share is None:
        raise HTTPException(404, "The job is not shared with them.")
    if user not in (job.user, share["by"]):
        raise HTTPException(403, "Another person shared the job with them: only they or its owner end that.")
    store().unshare(job.id or 0, kind, sharee)
    return shares_out(sharing.visible_shares(store(), job, user))


class WizardStart(BaseModel):
    """The remote's type the wizard configures."""

    type: str = Field(max_length=40)


class WizardAnswer(BaseModel):
    """The answer to the question the page shows (its state, as rclone gave it)."""

    state: str = Field(max_length=400)
    result: str = Field("", max_length=65_536)


async def wizard_step(action, *arguments) -> Any:
    """A wizard's step in a thread (rclone takes a moment), its refusal as the HTTP error it is."""
    try:
        return await asyncio.to_thread(action, *arguments)
    except WizardError as error:
        raise HTTPException(error.status, str(error)) from error


@APP.get("/api/rclone/types")
async def rclone_types(_nc: NC) -> list[dict]:
    """The remote types the rclone wizard offers (those the remote source allows)."""
    return await wizard_step(remote_types)


@APP.post("/api/rclone/wizard")
async def start_wizard(body: WizardStart, nc: NC) -> dict:
    """Starts the user's rclone wizard for a remote of the type: its first question."""
    return await wizard_step(WIZARDS.start, await nc.user, body.type)


@APP.post("/api/rclone/wizard/{session}/answer")
async def answer_wizard(session: str, body: WizardAnswer, nc: NC) -> dict:
    """Answers the user's wizard's question: the next one, or the finished configuration."""
    return await wizard_step(WIZARDS.answer, await nc.user, session, body.state, body.result)


@APP.post("/api/rclone/wizard/{session}/back")
async def back_wizard(session: str, nc: NC) -> dict:
    """Takes the user's wizard one question back."""
    return await wizard_step(WIZARDS.back, await nc.user, session)


@APP.delete("/api/rclone/wizard/{session}")
async def cancel_wizard(session: str, nc: NC) -> dict:
    """Ends the user's wizard and removes its configuration."""
    WIZARDS.cancel(await nc.user, session)
    return {}


@APP.websocket("/ws/record")
async def record(websocket: WebSocket, nc: NC) -> None:
    """A recording: the browser's screen to the page, the person's input to the browser.

    With ?job=<id> (a job of the user's own) the browser starts with the job's kept session at the page
    that asked for a person (with &at=shot: at the page of its last failed step); {type:
    save-session} keeps the browser's session for the job, clears the challenge and runs the job
    again.

    With ?rclone=<wizard> (the user's rclone wizard, at its sign-in question) the browser shows
    the provider's sign-in for `rclone authorize` instead (sign_in)."""
    await websocket.accept()
    if websocket.query_params.get("rclone"):
        await sign_in(websocket, nc, websocket.query_params["rclone"])
        return
    allowed, job = await admit(websocket, nc)
    if not allowed:
        return
    recorder = Recorder(websocket.send_json)
    try:
        # The job's (or the form's) HTTP/1.1 only: known before the browser starts. Within the
        # try: whatever goes wrong from here on, the browser's slot is released.
        http1 = websocket.query_params.get("http1") == "1" or (job is not None and switched_on(job, "http1"))
        home = websocket.query_params.get("home") == "1" or (job is not None and switched_on(job, "home"))
        try:
            if job:
                at = job.challenge_url or start_of(job)
                if websocket.query_params.get("at") == "shot":
                    at = (store().shot(job.id or 0) or {}).get("url") or at
                await recorder.start(await load_session(nc, job), at, http1=http1, home=home)
                await recorder.handle({"type": "mode", "mode": "browse"})
            else:
                await recorder.start(http1=http1, home=home)
        except Exception as error:  # pylint: disable=broad-exception-caught
            # The page says why instead of a screen that stays empty.
            LOGGER.exception("the recorder's browser did not start")
            text = (str(error) or type(error).__name__).splitlines()[0]
            await websocket.send_json({"type": "error", "message": f"The browser did not start: {text}"})
            await websocket.close(code=1011)
            return
        while True:
            message = await asyncio.wait_for(websocket.receive_json(), RECORDER_IDLE)
            try:
                if message.get("type") == "save-session" and job:
                    await save_answer(job, await recorder.session(), nc)
                    await websocket.send_json({"type": "session-saved"})
                else:
                    await recorder.handle(message)
            except Exception as error:  # pylint: disable=broad-exception-caught
                # A step the page could not do (an element gone, a timeout): the recording goes on.
                text = (str(error) or type(error).__name__).splitlines()[0]
                await websocket.send_json({"type": "error", "message": text})
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        # Shielded: even when the handler is cancelled, the browser closes and is released.
        closing = asyncio.ensure_future(recorder.close())
        closing.add_done_callback(lambda _: browser.SLOTS.release())
        await asyncio.shield(closing)


async def sign_in(websocket: WebSocket, nc: AsyncNextcloudApp, wizard: str) -> None:
    """A provider's sign-in for the user's rclone wizard in the app's browser: rclone authorize
    runs, the browser opens its link (and may open rclone's listener, AUTHORIZE_ORIGIN, alone of
    the local addresses, until rclone ends), the person signs in, and the token rclone prints
    answers the wizard's question; the page gets {type: rclone-done, view} with what comes next."""
    user = await nc.user
    authorization = await admit_sign_in(websocket, user, wizard)
    if authorization is None:
        return
    # Never through the jobs' proxy: the listener is this container's own. This browser's guard
    # alone lets it through (Chromium sends even 127.0.0.1 to its guard), while loopback is set.
    recorder = Recorder(websocket.send_json)
    recorder.loopback = AUTHORIZE_ORIGIN
    try:
        try:
            token = await signed_in(websocket, recorder, authorization)
            # The browser may no more open the listener, which ends with rclone.
            recorder.loopback = ""
            view = await asyncio.to_thread(WIZARDS.answer, user, wizard, authorization.state, token)
        except WizardError as error:
            await websocket.send_json({"type": "error", "message": str(error)})
        else:
            await websocket.send_json({"type": "rclone-done", "view": view})
        await websocket.close()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        recorder.loopback = ""
        stopping = asyncio.ensure_future(authorization.stop())
        closing = asyncio.ensure_future(recorder.close())
        closing.add_done_callback(lambda _: browser.SLOTS.release())
        await asyncio.shield(asyncio.gather(stopping, closing))


async def admit_sign_in(websocket: WebSocket, user: str, wizard: str) -> Authorization | None:
    """The app's one sign-in for the user's wizard, with a browser's slot taken; else the page is
    told why (another sign-in runs, all browsers are in use) and the socket closed."""
    refusal = ""
    authorization = None
    if not allowed_origin(websocket.headers.get("origin", "")):
        refusal = "This page may not open the browser."
    else:
        try:
            authorization = WIZARDS.authorization(user, wizard)
        except WizardError as error:
            refusal = str(error)
    if authorization and not browser.SLOTS.acquire(blocking=False):  # pylint: disable=consider-using-with
        authorization.kill()
        authorization = None
        refusal = busy_browsers()
    if authorization is None:
        await websocket.send_json({"type": "error", "message": refusal})
        await websocket.close(code=1008)
    return authorization


async def signed_in(websocket: WebSocket, recorder: Recorder, authorization: Authorization) -> str:
    """Starts rclone and the browser at its link, and passes the person's input to the browser
    until rclone prints the token (WizardError when it fails or takes too long)."""
    try:
        link = await authorization.start()
        await recorder.start(url=link)
        await recorder.handle({"type": "mode", "mode": "browse"})
    except WizardError:
        raise
    except Exception as error:  # pylint: disable=broad-exception-caught
        LOGGER.warning("the browser for rclone's sign-in did not start: %s", error)
        raise WizardError(
            f"The browser did not start: {(str(error) or type(error).__name__).splitlines()[0]}"
        ) from error
    token = asyncio.ensure_future(authorization.token())
    try:
        while True:
            receiving = asyncio.ensure_future(websocket.receive_json())
            await asyncio.wait({receiving, token}, return_when=asyncio.FIRST_COMPLETED)
            if token.done():
                receiving.cancel()
                return token.result()
            message = receiving.result()
            try:
                await recorder.handle(message)
            except Exception as error:  # pylint: disable=broad-exception-caught
                # A step the page could not do: the sign-in goes on.
                text = (str(error) or type(error).__name__).splitlines()[0]
                await websocket.send_json({"type": "error", "message": text})
    finally:
        token.cancel()


async def admit(websocket: WebSocket, nc: AsyncNextcloudApp) -> tuple[bool, Job | None]:
    """Whether the recording may start (and the job it continues, if any), with the browser's
    lock taken; else the page is told why and the socket closed."""
    refusal = ""
    job = None
    # Only Nextcloud's own pages may open it (a browser sends the page's origin): another site
    # could otherwise drive a person's recorder with their session.
    if not allowed_origin(websocket.headers.get("origin", "")):
        refusal = "This page may not open the recorder."
    elif websocket.query_params.get("job"):
        try:
            job, _ = await job_access(int(websocket.query_params["job"]), nc)
        except (ValueError, HTTPException):
            refusal = "No such job."
        # The job's browser opens with its owner's session (signed in as them at the site), and
        # Done keeps the session it is left with for the owner's runs: theirs alone, even for
        # one who may run and edit the job.
        if job is not None and job.user != await nc.user:
            job, refusal = None, "Only the job's owner may open its browser: it holds their session at the site."
    if not refusal and not browser.SLOTS.acquire(blocking=False):  # pylint: disable=consider-using-with
        refusal = busy_browsers()
    if refusal:
        await websocket.send_json({"type": "error", "message": refusal})
        await websocket.close(code=1008)
        return False, None
    return True, job


def busy_browsers() -> str:
    """The recorder's refusal when every browser is open: what holds them, without whose."""
    return f"No browser is free: {browsers_in_use()}. Try again in a few minutes."


def switched_on(job: Job, name: str) -> bool:
    """Whether the job's flag is on (a value that is no number, saved before the options were
    checked, is off)."""
    try:
        return bool(int(job.options.get(name, 0) or 0))
    except (TypeError, ValueError):
        return False


def start_of(job: Job) -> str:
    """The listing a job's recipe starts at, or "" (another source, or a recipe being edited)."""
    try:
        return str(json.loads(str(job.options.get("recipe") or "{}") or "{}").get("start", ""))
    except (ValueError, AttributeError):
        return ""


async def save_answer(job: Job, session: dict, nc: AsyncNextcloudApp) -> None:
    """Keeps the session the user left the browser with, and runs the job again with it."""
    await save_session(nc, job, session)
    store().finish(job.id or 0, "ok", "continued in the browser")
    STATE["executor"].request(job.id)


async def enabled_handler(enabled: bool, nc: AsyncNextcloudApp) -> str:
    """Adds the app's entry to the top menu, with its page's script, and the admin's settings."""
    if enabled:
        await nc.ui.resources.set_script("top_menu", "harvester", "js/harvester-main")
        await nc.ui.top_menu.register("harvester", "Harvester", "img/app.svg")
        await nc.ui.settings.register_form(
            settings.form(RUNNERS, browser.BROWSERS, settings.DEFAULT_PROXY, settings.DEFAULT_PROXY_NAME)
        )
    else:
        await nc.ui.top_menu.unregister("harvester")
        await nc.ui.settings.unregister_form("harvester_admin")
    return ""


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level="info")
