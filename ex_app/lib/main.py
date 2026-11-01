"""Harvester: fetches files from websites into Nextcloud on a schedule, and tags them."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import Annotated

from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from nc_py_api import AsyncNextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, anc_app, persistent_storage, run_app, set_handlers
from pydantic import BaseModel, Field, field_validator

from harvester import browser, schedule
from harvester.browser.recorder import Recorder
from harvester.executor import Executor, credential_key, load_session, save_session, session_key
from harvester.presets import PRESETS, make_job
from harvester.runner import COMMON_OPTIONS
from harvester.sources import FULL_PASS, SOURCES, SourceError
from harvester.store import Job, Store

STATE: dict = {}
# A recording ends after this many seconds without a message from the page.
RECORDER_IDLE = 900


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Opens the database, starts the executor and registers the app's page."""
    STATE["store"] = Store(os.path.join(persistent_storage(), "harvester.db"))
    STATE["executor"] = Executor(STATE["store"])
    STATE["executor"].start()
    set_handlers(app, enabled_handler)
    yield


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


# Options that count something and must be at least 1 (0 would fetch nothing, ever); -1 means
# no limit where UNLIMITED allows it.
AT_LEAST_ONE = ("max_items", "max_pages", "max_entries", "max_size")
UNLIMITED = ("max_items",)


def checked(body: JobIn) -> JobIn:
    """The job, if its source accepts its options (a recipe that can run, for instance)."""
    for name in AT_LEAST_ONE:
        if name in body.options:
            try:
                value = int(body.options[name])
                if value < 1 and not (value == -1 and name in UNLIMITED):
                    raise ValueError
            except (TypeError, ValueError) as error:
                hint = " (or -1: no limit)" if name in UNLIMITED else ""
                raise HTTPException(422, f"{name} must be a number of at least 1{hint}") from error
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
            "options": {key: vars(option) for key, option in (source.options | COMMON_OPTIONS).items()},
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
def explain_schedule(expression: Annotated[str, Query(max_length=100)]) -> dict:
    """The schedule in words and its next runs (timestamps), for the job's form as it is typed."""
    try:
        return {"description": schedule.describe(expression), "next": schedule.next_runs(expression)}
    except ValueError as error:
        return {"error": str(error)}


@APP.get("/api/jobs")
async def list_jobs(nc: NC) -> list[dict]:
    """The user's jobs, with how their last run went."""
    running = STATE["executor"].running
    return [vars(job) | {"running": job.id == running} for job in store().jobs(await nc.user)]


@APP.get("/api/log")
async def progress_log(nc: NC, job: int | None = None, after: int = 0) -> dict:
    """The user's progress log after the line after: of one job of theirs, or of all; and which
    of their jobs runs now."""
    if job is not None:
        await own_job(job, nc)
    user = await nc.user
    running = STATE["executor"].running
    running_job = store().job(running) if running is not None else None
    return {
        "lines": store().log_lines(user, job, max(after, 0)),
        "running": running if running_job and running_job.user == user else None,
    }


@APP.post("/api/jobs")
async def create_job(body: JobIn, nc: NC) -> dict:
    """Adds a job."""
    return vars(store().save(Job(user=await nc.user, **checked(body).model_dump())))


@APP.put("/api/jobs/{job_id}")
async def update_job(job_id: int, body: JobIn, nc: NC) -> dict:
    """Changes a job's settings."""
    job = await own_job(job_id, nc)
    saved = store().save(Job(id=job.id, user=job.user, created=job.created, **checked(body).model_dump()))
    # Another source or recipe lists other files: "known files end the run" waits for its first
    # complete pass again.
    if saved.source != job.source or saved.options != job.options:
        store().forget(job.id or 0, FULL_PASS)
    return vars(saved)


@APP.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int, nc: NC) -> dict:
    """Deletes a job and its own account; the files it downloaded stay."""
    job = await own_job(job_id, nc)
    await nc.preferences_ex.delete(
        [credential_key(job, name) for name in SOURCES[job.source].credentials] + [session_key(job)]
    )
    store().delete(job.id or 0)
    return {}


@APP.post("/api/jobs/{job_id}/run")
async def run_job(job_id: int, nc: NC) -> dict:
    """Runs a job now (after the one running)."""
    STATE["executor"].request((await own_job(job_id, nc)).id)
    return {}


@APP.get("/api/jobs/{job_id}/credentials")
async def get_credentials(job_id: int, nc: NC) -> dict[str, bool]:
    """Which of the job's credentials the user has set (never their values)."""
    job = await own_job(job_id, nc)
    return {
        name: bool(await nc.preferences_ex.get_value(credential_key(job, name)))
        for name in SOURCES[job.source].credentials
    }


@APP.put("/api/jobs/{job_id}/credentials")
async def set_credentials(
    job_id: int, body: dict[Annotated[str, Field(max_length=40)], Annotated[str, Field(max_length=65_536)]], nc: NC
) -> dict:
    """Stores the credentials given (an empty one is left as it was), encrypted by Nextcloud."""
    job = await own_job(job_id, nc)
    for name in SOURCES[job.source].credentials:
        if body.get(name):
            await nc.preferences_ex.set_value(credential_key(job, name), body[name], sensitive=True)
    return {}


@APP.websocket("/ws/record")
async def record(websocket: WebSocket, nc: NC) -> None:
    """A recording: the browser's screen to the page, the person's input to the browser.

    With ?job=<id> (the user's job) the browser starts with the job's kept session at the page
    that asked for a person; {type: save-session} keeps the browser's session for the job,
    clears the challenge and runs the job again."""
    await websocket.accept()
    allowed, job = await admit(websocket, nc)
    if not allowed:
        return
    recorder = Recorder(websocket.send_json)
    # The job's (or the form's) HTTP/1.1 only: known before the browser starts.
    http1 = websocket.query_params.get("http1") == "1" or bool(job and int(job.options.get("http1", 0) or 0))
    try:
        if job:
            await recorder.start(await load_session(nc, job), job.challenge_url or start_of(job), http1=http1)
            await recorder.handle({"type": "mode", "mode": "browse"})
        else:
            await recorder.start(http1=http1)
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
        closing.add_done_callback(lambda _: browser.LOCK.release())
        await asyncio.shield(closing)


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
            job = await own_job(int(websocket.query_params["job"]), nc)
        except (ValueError, HTTPException):
            refusal = "No such job."
    if not refusal and not browser.LOCK.acquire(blocking=False):  # pylint: disable=consider-using-with
        refusal = "The browser is busy (a job or another recording)."
    if refusal:
        await websocket.send_json({"type": "error", "message": refusal})
        await websocket.close(code=1008)
        return False, None
    return True, job


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
    """Adds the app's entry to the top menu, with its page's script."""
    if enabled:
        await nc.ui.resources.set_script("top_menu", "harvester", "js/harvester-main")
        await nc.ui.top_menu.register("harvester", "Harvester", "img/app.svg")
    else:
        await nc.ui.top_menu.unregister("harvester")
    return ""


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level="info")
