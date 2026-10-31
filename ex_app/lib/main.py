"""Harvester: fetches files from websites into Nextcloud on a schedule, and tags them."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, nc_app, persistent_storage, run_app, set_handlers
from pydantic import BaseModel, Field, field_validator

from harvester import browser
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
NC = Annotated[NextcloudApp, Depends(nc_app)]


def store() -> Store:
    """The database, opened at start."""
    return STATE["store"]


class JobIn(BaseModel):
    """A job as the page sends it."""

    name: str = Field(min_length=1, max_length=200)
    source: str
    target: str = Field(min_length=1)
    schedule: str = "0 6 * * *"
    options: dict[str, str | int] = {}
    tags: list[str] = []
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


def own_job(job_id: int, nc: NextcloudApp) -> Job:
    """The user's job, or 404 (also for another user's)."""
    job = store().job(job_id)
    if job is None or job.user != nc.user:
        raise HTTPException(404, "no such job")
    return job


# Options that count something and must be at least 1 (0 would fetch nothing, ever).
AT_LEAST_ONE = ("max_items", "max_pages", "max_entries")


def checked(body: JobIn) -> JobIn:
    """The job, if its source accepts its options (a recipe that can run, for instance)."""
    for name in AT_LEAST_ONE:
        if name in body.options:
            try:
                if int(body.options[name]) < 1:
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise HTTPException(422, f"{name} must be a number of at least 1") from error
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
            "options": {key: vars(option) for key, option in (source.options | COMMON_OPTIONS).items()},
        }
        for source in SOURCES.values()
    ]


@APP.get("/api/presets")
def list_presets() -> list[dict]:
    """The templates of jobs for known sites, with their parameters."""
    return [
        {"id": preset_id, "title": p["title"], "description": p["description"], "parameters": p.get("parameters", {})}
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


@APP.get("/api/jobs")
def list_jobs(nc: NC) -> list[dict]:
    """The user's jobs, with how their last run went."""
    running = STATE["executor"].running
    return [vars(job) | {"running": job.id == running} for job in store().jobs(nc.user)]


@APP.post("/api/jobs")
def create_job(body: JobIn, nc: NC) -> dict:
    """Adds a job."""
    return vars(store().save(Job(user=nc.user, **checked(body).model_dump())))


@APP.put("/api/jobs/{job_id}")
def update_job(job_id: int, body: JobIn, nc: NC) -> dict:
    """Changes a job's settings."""
    job = own_job(job_id, nc)
    saved = store().save(Job(id=job.id, user=job.user, created=job.created, **checked(body).model_dump()))
    # Another source or recipe lists other files: "known files end the run" waits for its first
    # complete pass again.
    if saved.source != job.source or saved.options != job.options:
        store().forget(job.id or 0, FULL_PASS)
    return vars(saved)


@APP.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, nc: NC) -> dict:
    """Deletes a job and its own account; the files it downloaded stay."""
    job = own_job(job_id, nc)
    nc.preferences_ex.delete(
        [credential_key(job, name) for name in SOURCES[job.source].credentials] + [session_key(job)]
    )
    store().delete(job.id or 0)
    return {}


@APP.post("/api/jobs/{job_id}/run")
def run_job(job_id: int, nc: NC) -> dict:
    """Runs a job now (after the one running)."""
    STATE["executor"].request(own_job(job_id, nc).id)
    return {}


@APP.get("/api/jobs/{job_id}/credentials")
def get_credentials(job_id: int, nc: NC) -> dict[str, bool]:
    """Which of the job's credentials the user has set (never their values)."""
    job = own_job(job_id, nc)
    return {
        name: bool(nc.preferences_ex.get_value(credential_key(job, name))) for name in SOURCES[job.source].credentials
    }


@APP.put("/api/jobs/{job_id}/credentials")
def set_credentials(job_id: int, body: dict[str, str], nc: NC) -> dict:
    """Stores the credentials given (an empty one is left as it was), encrypted by Nextcloud."""
    job = own_job(job_id, nc)
    for name in SOURCES[job.source].credentials:
        if body.get(name):
            nc.preferences_ex.set_value(credential_key(job, name), body[name], sensitive=True)
    return {}


@APP.websocket("/ws/record")
async def record(websocket: WebSocket, nc: NC) -> None:
    """A recording: the browser's screen to the page, the person's input to the browser.

    With ?job=<id> (the user's job) the browser starts with the job's kept session at the page
    that asked for a person; {type: save-session} keeps the browser's session for the job,
    clears the challenge and runs the job again."""
    await websocket.accept()
    job = None
    if websocket.query_params.get("job"):
        try:
            job = own_job(int(websocket.query_params["job"]), nc)
        except (ValueError, HTTPException):
            await websocket.send_json({"type": "error", "message": "No such job."})
            await websocket.close()
            return
    if not browser.LOCK.acquire(blocking=False):  # pylint: disable=consider-using-with
        await websocket.send_json({"type": "error", "message": "The browser is busy (a job or another recording)."})
        await websocket.close()
        return
    recorder = Recorder(websocket.send_json)
    try:
        if job:
            kept = await asyncio.to_thread(load_session, nc, job)
            try:
                start = json.loads(str(job.options.get("recipe") or "{}") or "{}").get("start", "")
            except (ValueError, AttributeError):
                start = ""  # a job of another source, or a recipe being edited: the page alone
            await recorder.start(kept, job.challenge_url or start)
            await recorder.handle({"type": "mode", "mode": "browse"})
        else:
            await recorder.start()
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


async def save_answer(job: Job, session: dict, nc: NextcloudApp) -> None:
    """Keeps the session the user left the browser with, and runs the job again with it."""
    await asyncio.to_thread(save_session, nc, job, session)
    store().finish(job.id or 0, "ok", "continued in the browser")
    STATE["executor"].request(job.id)


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    """Adds the app's entry to the top menu, with its page's script."""
    if enabled:
        nc.ui.resources.set_script("top_menu", "harvester", "js/harvester-main")
        nc.ui.top_menu.register("harvester", "Harvester", "img/app.svg")
    else:
        nc.ui.top_menu.unregister("harvester")
    return ""


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level="info")
