# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harvester: fetches files from websites into Nextcloud on a schedule, and tags them.

The app's routes for its jobs and the recorder's WebSocket; the other groups are in
harvester/routes/ (Try the address, recipe files and templates, the rclone wizard)."""

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from nc_py_api import AsyncNextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, persistent_storage, run_app, set_handlers
from pydantic import BaseModel, Field

from harvester import browser, destinations, dns, l10n, places, needs, ondemand, schedule, settings, watch
from harvester.rclone_wizard import AUTHORIZE_ORIGIN, Authorization, WizardError
from harvester.browser.recorder import Recorder
from harvester.library import Locks, Shelf
from harvester.executor import (
    RUNNERS,
    Executor,
    browsers_in_use,
    credential_key,
    load_session,
    save_session,
    session_key,
)
from harvester.presets import PRESETS, make_job
from harvester.recipe import leaked_passwords
from harvester.runner import COMMON_OPTIONS
from harvester.routes import library, rclone, recipes, trying
from harvester.routes.common import NC, STATE, JobIn, checked, own_job, store, switched_on
from harvester.routes.rclone import WIZARDS
from harvester.sources.download import DOWNLOAD_OPTIONS
from harvester.sources import SOURCES
from harvester.store import Job, Store

LOGGER = logging.getLogger("harvester")

# A recording ends after this many seconds without a message from the page.
RECORDER_IDLE = 900


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Opens the database, starts the executor and registers the app's page."""
    # First: the app's environment (its secrets) out of the reach of its browsers and helpers.
    if not browser.undumpable():
        LOGGER.warning("the app's process stays dumpable: its helpers could read its environment")
    STATE["store"] = Store(os.path.join(persistent_storage(), "harvester.db"))
    STATE["shelf"] = Shelf(STATE["store"], os.path.join(persistent_storage(), "library"))
    STATE["locks"] = Locks(STATE["store"])
    places.SHELF[:] = [STATE["shelf"]]
    STATE["executor"] = Executor(STATE["store"])
    STATE["executor"].start()
    set_handlers(app, enabled_handler)
    yield
    WIZARDS.close()


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)
for _group in (trying, recipes, rclone, library):
    APP.include_router(_group.ROUTER)


def allowed_origin(origin: str) -> bool:
    """Whether a page of this origin may open the recorder: Nextcloud's own (NEXTCLOUD_URL), or one
    of HARVESTER_ORIGINS (comma-separated, for a Nextcloud reached under more names). No origin:
    not a browser, which HaRP authenticated all the same."""
    if not origin:
        return True
    allowed = {f"{urlparse(url).scheme}://{urlparse(url).netloc}" for url in [os.environ.get("NEXTCLOUD_URL", "")]}
    allowed |= {item.strip().rstrip("/") for item in os.environ.get("HARVESTER_ORIGINS", "").split(",") if item.strip()}
    return origin.rstrip("/") in allowed


@APP.get("/api/sources")
def list_sources() -> list[dict]:
    """The sources with their options and credentials, for the job form."""
    return [
        {
            "name": source.name,
            "title": source.title,
            "description": source.description,
            "credentials": list(source.credentials),
            # The option a fetch of an on-demand job replaces ("": no on-demand jobs).
            "address": source.address_option,
            "credential_help": {name: list(help_) for name, help_ in source.credential_help.items()},
            "options": {
                # "Through the proxy" in the admin's name for it (the page translates the default's).
                key: vars(option)
                | ({"title": f"Through {browser.PROXY_NAME}", "proxy": browser.PROXY_NAME} if key == "home" else {})
                for key, option in (source.options | COMMON_OPTIONS | watch.options_of(source.name)).items()
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
    for the job's form as it is typed. The words in English, and as a template the page
    translates (description_l10n, error_l10n: harvester.l10n)."""
    try:
        description = schedule.describe(expression)
        return {
            "description": description,
            "description_l10n": l10n.template_of(description),
            "next": schedule.next_runs(expression, timezone=schedule.checked_zone(timezone)),
        }
    except ValueError as error:
        reason = error.args[0] if error.args else ""
        return {"error": str(error), "error_l10n": l10n.template_of(reason)}


@APP.get("/api/jobs")
async def list_jobs(nc: NC, brief: bool = False) -> list[dict]:
    """The user's jobs, with how their last run went, which run or wait for their turn, and
    why they wait. brief: without their options (a recipe may be 100 KB), with the job's own
    address instead (Source.address_of), as the jobs page lists them; GET /api/jobs/{id} has a
    job's options. Without brief (API clients, Talk AI) each job has its options as before."""
    executor = STATE["executor"]
    jobs = store().jobs(await nc.user)
    # Queued first: a job taken in between is then running too (and shown so), not neither.
    queued = executor.queued
    running = executor.running
    fetched = store().fetches([job.id or 0 for job in jobs])
    waits = executor.waits(jobs, fetched)
    fetches = fetches_of(jobs, fetched)
    return [
        (listed(job) if brief else vars(job))
        | {
            "running": job.id in running,
            "queued": job.id in queued,
            "paused": executor.paused(job.id or 0),
            # What its run asks its person now (a code from the phone), or None.
            "asking": executor.desk.asking(job.id or 0),
            "waiting": waits.get(job.id or 0, ""),
            "waiting_l10n": l10n.template_of(waits.get(job.id or 0)),
            "fetches": fetches.get(job.id or 0, []),
        }
        for job in jobs
    ]


def listed(job: Job) -> dict:
    """A job as the jobs page lists it: without its options, with its own address."""
    source = SOURCES.get(job.source)
    return {key: value for key, value in vars(job).items() if key != "options"} | {
        "address": source.address_of(job.options) if source else ""
    }


@APP.get("/api/jobs/{job_id}")
async def get_job(job_id: int, nc: NC) -> dict:
    """One of the user's jobs with its options (its form)."""
    return vars(await own_job(job_id, nc))


def fetches_of(jobs: list[Job], fetched: list[dict] | None = None) -> dict[int, list[dict]]:
    """The addresses waiting for the jobs (fetched: store.fetches of them, if read already), by
    job, oldest first; the one each runs now is marked running."""
    executor = STATE["executor"]
    found: dict[int, list[dict]] = {}
    for fetch in store().fetches([job.id or 0 for job in jobs]) if fetched is None else fetched:
        running = executor.fetching(fetch["job"]) == fetch["id"]
        found.setdefault(fetch["job"], []).append(
            {"id": fetch["id"], "url": fetch["url"], "at": fetch["at"], "running": running}
        )
    return found


@APP.get("/api/status")
def queue_status() -> dict:
    """What everyone's jobs and recordings hold now (runners, browsers, the queue), without
    whose: so a person sees why their job waits."""
    return STATE["executor"].status()


@APP.get("/api/log")
async def progress_log(nc: NC, job: int | None = None, after: int = 0) -> dict:
    """The user's progress log after the line after: of one job of theirs, or of all; which of
    their jobs run now, which wait for their turn and why, the queue's status and the addresses
    their on-demand jobs wait to fetch."""
    if job is not None:
        await own_job(job, nc)
    user = await nc.user
    jobs = store().jobs(user)
    mine = {listed.id for listed in jobs}
    executor = STATE["executor"]
    queued = executor.queued
    fetched = store().fetches([job.id or 0 for job in jobs])
    waits = executor.waits(jobs, fetched)
    return {
        "lines": store().log_lines(user, job, max(after, 0)),
        "running": [job_id for job_id in executor.running if job_id in mine],
        "queued": [job_id for job_id in queued if job_id in mine],
        "asking": [job_id for job_id in executor.desk.waiting() if job_id in mine],
        "waiting": {str(job_id): reason for job_id, reason in waits.items()},
        # The reasons' templates, which the page translates (harvester.l10n).
        "waiting_l10n": {str(job_id): l10n.template_of(reason) for job_id, reason in waits.items()},
        "status": executor.status(),
        # The on-demand jobs' waiting addresses (their ids): the page draws the list again
        # when they change.
        "fetches": {
            str(job_id): [fetch["id"] for fetch in found] for job_id, found in fetches_of(jobs, fetched).items()
        },
    }


def warn_of_leaks(job: Job) -> None:
    """Logs what a website job's recipe says of a password written into it (its step and field,
    never the value): a recipe recorded before the recorder knew a field that showed its password.
    The form says it too, as the recipe is shown."""
    try:
        recipe = json.loads(str(job.options.get("recipe") or "{}")) if job.source == "site" else {}
    except ValueError:
        return
    for warning in leaked_passwords(recipe):
        LOGGER.warning("job %s saved: %s", job.id, warning)


@APP.post("/api/jobs")
async def create_job(body: JobIn, nc: NC) -> dict:
    """Adds a job."""
    saved = store().save(Job(user=await nc.user, **checked(body).model_dump()))
    warn_of_leaks(saved)
    weekly_test_from_now(None, saved)
    return vars(saved)


@APP.put("/api/jobs/{job_id}")
async def update_job(job_id: int, body: JobIn, nc: NC) -> dict:
    """Changes a job's settings."""
    job = await own_job(job_id, nc)
    saved = store().save(Job(id=job.id, user=job.user, created=job.created, **checked(body).model_dump()))
    warn_of_leaks(saved)
    forget_changed(job, saved)
    if sign_in_changed(job, saved):
        await nc.preferences_ex.delete(session_key(saved))
    weekly_test_from_now(job, saved)
    # On a schedule now: the addresses sent to it are not fetched (the one running ends as it is).
    if not ondemand.on_demand(saved):
        for fetch in store().fetches([saved.id or 0]):
            if STATE["executor"].fetching(saved.id or 0) != fetch["id"]:
                store().drop_fetch(fetch["id"])
    # Other tags: the files it saved get them (and lose those it gave and has no more) at once.
    if saved.tags != job.tags:
        STATE["executor"].retag(saved.id or 0, job.tags)
    return vars(saved)


# The options whose change makes a file refused by type or size worth trying again.
REFUSING = tuple(DOWNLOAD_OPTIONS)


def sign_in_changed(job: Job, saved: Job) -> bool:
    """Whether the job's kept session is no longer the one its runs should start from: another
    source, or a recipe with other steps or another site to start at. Other changes of the recipe
    (its levels, patterns, pages) keep it: it may be a person's answer to a challenge."""
    if saved.source != job.source:
        return True

    def signing(options: dict) -> tuple:
        try:
            recipe = json.loads(str(options.get("recipe") or "{}"))
        except ValueError:
            return (str(options.get("recipe")),)
        if not isinstance(recipe, dict):
            return (str(options.get("recipe")),)
        return recipe.get("steps") or [], urlparse(str(recipe.get("start") or "")).netloc.lower()

    return "recipe" in SOURCES[saved.source].options and signing(saved.options) != signing(job.options)


def forget_changed(job: Job, saved: Job) -> None:
    """What the job's listing knew, when the options it lists by change: another source, recipe,
    address, folder or patterns lists other files, so "known files end the run" waits for its
    first complete pass again (and mail's handled messages are read again). Its file names, files
    per run, schedule or tags change nothing there. Other types or sizes try again the files they
    refused. Another Save to (or vault) forgets where its files are (not that it fetched them)."""

    def changed(names) -> bool:
        return any(saved.options.get(name) != job.options.get(name) for name in names)

    if saved.source != job.source or changed(SOURCES[saved.source].listing_options):
        store().forget_marks(job.id or 0)
        # What its runs found is no measure of another listing ("the site changed").
        store().forget_summaries(job.id or 0)
    if saved.source != job.source or changed(REFUSING):
        store().forget_refused(job.id or 0)
    # Its files go elsewhere now: those saved stay where they are, and are not fetched again.
    if destinations.place_of(saved.options) != destinations.place_of(job.options):
        store().forget_places(job.id or 0)


def weekly_test_from_now(job: Job | None, saved: Job) -> None:
    """A job's weekly test (watch.py), just turned on, counts from now: not at once for a job
    made long ago."""
    if switched_on(saved, "weekly_test") and not (job and switched_on(job, "weekly_test")):
        store().set_tested(saved.id or 0, time.time())


@APP.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int, nc: NC) -> dict:
    """Deletes a job and its own account; the files it downloaded stay."""
    job = await own_job(job_id, nc)
    await nc.preferences_ex.delete(
        [credential_key(job, name) for name in SOURCES[job.source].credentials] + [session_key(job)]
    )
    store().delete(job.id or 0)
    # Its run, if one goes on, ends at once: it would only write for a job that is gone.
    STATE["executor"].stop(job.id or 0)
    return {}


@APP.get("/api/jobs/{job_id}/shot")
async def get_shot(job_id: int, nc: NC) -> dict:
    """What the user's job's browser showed at its last failed step or challenge: the picture (a
    JPEG, base64), the page's address and title, what went wrong and when."""
    shot = store().shot((await own_job(job_id, nc)).id or 0)
    if not shot:
        raise HTTPException(404, "The job's browser has shown nothing to keep yet.")
    return {key: shot[key] for key in ("at", "url", "title", "what")} | {
        "image": base64.b64encode(shot["image"]).decode()
    }


@APP.post("/api/jobs/{job_id}/run")
async def run_job(job_id: int, nc: NC) -> dict:
    """Runs a job now (after the one running); an on-demand job with its own address."""
    job = await own_job(job_id, nc)
    if ondemand.on_demand(job) and not SOURCES[job.source].address_of(job.options):
        raise HTTPException(409, "The job has no address of its own: fetch one with it.")
    STATE["executor"].request(job.id)
    return {}


@APP.post("/api/jobs/{job_id}/test")
async def test_job(job_id: int, nc: NC) -> dict:
    """Tests the website job's recipe as its weekly test does (after the one running): its walk,
    nothing downloaded; what it finds is in the progress log, and a changed site warns."""
    job = await own_job(job_id, nc)
    if job.source != "site" or not SOURCES[job.source].address_of(job.options):
        raise HTTPException(409, "Only a website job with a listing of its own has a recipe to test.")
    STATE["executor"].request_test(job.id or 0)
    return {}


class FetchIn(BaseModel):
    """An address sent to an on-demand job."""

    url: str = Field(min_length=1, max_length=ondemand.MAX_URL)


async def account_set(nc: AsyncNextcloudApp, job: Job) -> bool:
    """Whether the user set any of the job's account."""
    for name in SOURCES[job.source].credentials:
        if await nc.preferences_ex.get_value(credential_key(job, name)):
            return True
    return False


@APP.post("/api/jobs/{job_id}/fetch")
async def fetch_url(job_id: int, body: FetchIn, nc: NC) -> dict:
    """Queues the address for the user's on-demand job: its run fetches that address in place
    of the job's own (whose options stay). Checked first, as the job's source checks its own
    (422 with why). An address waiting already keeps its place. Returns the fetch (id, url, at)
    and whether it is new."""
    job = await own_job(job_id, nc)
    url = body.url.strip()
    problem = ondemand.fetch_problem(job, url, await account_set(nc, job))
    if problem:
        raise HTTPException(422, problem)
    fetch, new = STATE["executor"].fetch(job.id or 0, url)
    return {key: fetch[key] for key in ("id", "url", "at")} | {"new": new}


@APP.delete("/api/jobs/{job_id}/fetch/{fetch_id}")
async def cancel_fetch(job_id: int, fetch_id: int, nc: NC) -> dict:
    """Takes a waiting address out of the job's queue; one being fetched is stopped with Stop."""
    job = await own_job(job_id, nc)
    if STATE["executor"].fetching(job.id or 0) == fetch_id:
        raise HTTPException(409, "The address is being fetched: Stop ends that.")
    if not any(fetch["id"] == fetch_id for fetch in store().fetches([job.id or 0])) or not store().drop_fetch(fetch_id):
        raise HTTPException(404, "The address is not waiting (fetched already?).")
    return {}


class CodeIn(BaseModel):
    """A code the person read on their phone, for their job's step "ask-code"."""

    code: str = Field(min_length=1, max_length=needs.CODE_MAX)


@APP.post("/api/jobs/{job_id}/code")
async def enter_code(job_id: int, body: CodeIn, nc: NC) -> dict:
    """Hands the code to the user's job, while its run waits for one (harvester.needs): to the
    step's field, and nowhere else (never logged nor kept)."""
    job = await own_job(job_id, nc)
    code = body.code.strip()
    if not code or not code.isprintable():
        raise HTTPException(422, "A code is letters, digits and signs on one line.")
    if not STATE["executor"].desk.answer(job.id or 0, code):
        raise HTTPException(409, "The job is not waiting for a code.")
    return {}


@APP.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: int, nc: NC) -> dict:
    """Pauses the user's running job before its next file."""
    if not STATE["executor"].pause((await own_job(job_id, nc)).id):
        raise HTTPException(409, "The job is not running.")
    return {}


@APP.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: int, nc: NC) -> dict:
    """Lets the user's paused job go on."""
    if not STATE["executor"].resume((await own_job(job_id, nc)).id):
        raise HTTPException(409, "The job is not running.")
    return {}


@APP.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: int, nc: NC) -> dict:
    """Ends the user's running job before its next file; what it saved stays."""
    if not STATE["executor"].stop((await own_job(job_id, nc)).id):
        raise HTTPException(409, "The job is not running.")
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
    """Stores the credentials given (an empty one is left as it was), encrypted by Nextcloud. A
    changed account forgets the job's kept session: its cookies, or a refresh token the provider
    replaced, are the old account's and would go on being used instead."""
    job = await own_job(job_id, nc)
    changed = False
    for name in SOURCES[job.source].credentials:
        if body.get(name) and body[name] != await nc.preferences_ex.get_value(credential_key(job, name)):
            await nc.preferences_ex.set_value(credential_key(job, name), body[name], sensitive=True)
            changed = True
    if changed:
        await nc.preferences_ex.delete(session_key(job))
    return {}


@APP.websocket("/ws/record")
async def record(websocket: WebSocket, nc: NC) -> None:
    """A recording: the browser's screen to the page, the person's input to the browser.

    With ?job=<id> (the user's job) the browser starts with the job's kept session at the page
    that asked for a person (with &at=shot: at the page of its last failed step); {type:
    save-session} keeps the browser's session for the job, clears the challenge and runs the job
    again; {type: cookies-keep} (the cookies panel's "Use for this job") keeps it and goes on.

    With ?rclone=<wizard> (the user's rclone wizard, at its sign-in question) the browser shows
    the provider's sign-in for `rclone authorize` instead (sign_in).

    With ?browse=1 (a job form's Browse, for an address to take into the form) it is the browser
    alone: in the mode browse from the start, nothing recorded, at &url= if given."""
    await websocket.accept()
    if websocket.query_params.get("rclone"):
        await sign_in(websocket, nc, websocket.query_params["rclone"])
        return
    allowed, job = await admit(websocket, nc)
    if not allowed:
        return
    recorder = Recorder(l10n.sending(websocket.send_json), websocket.send_bytes)
    try:
        # The job's (or the form's) HTTP/1.1 only: known before the browser starts. Within the
        # try: whatever goes wrong from here on, the browser's slot is released.
        http1 = websocket.query_params.get("http1") == "1" or (job is not None and switched_on(job, "http1"))
        home = websocket.query_params.get("home") == "1" or (job is not None and switched_on(job, "home"))
        # Disguised as the job's runs are (its challenge answered under their fingerprint), or as
        # the form says for a recording without a job.
        stealth = switched_on(job, "stealth") if job is not None else websocket.query_params.get("stealth") == "1"
        # Cookie banners dismissed as the job's own browser does, or as the form says.
        consent = switched_on(job, "consent") if job is not None else websocket.query_params.get("consent") == "1"
        try:
            if job:
                at = job.challenge_url or start_of(job)
                if websocket.query_params.get("at") == "shot":
                    at = (store().shot(job.id or 0) or {}).get("url") or at
                await recorder.start(
                    await load_session(nc, job), at, http1=http1, home=home, stealth=stealth, consent=consent
                )
                await recorder.handle({"type": "mode", "mode": "browse"})
            elif websocket.query_params.get("browse") == "1":
                # Browsing before the first page: its navigation is never a step.
                recorder.mode = "browse"
                url = websocket.query_params.get("url", "")
                await recorder.start(url=url, http1=http1, home=home, stealth=stealth, consent=consent)
            else:
                await recorder.start(http1=http1, home=home, stealth=stealth, consent=consent)
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
                await answer(websocket, recorder, job, nc, message)
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
    recorder = Recorder(l10n.sending(websocket.send_json), websocket.send_bytes)
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
        await websocket.send_json(l10n.with_templates({"type": "error", "message": refusal}))
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
            job = await own_job(int(websocket.query_params["job"]), nc)
        except (ValueError, HTTPException):
            refusal = "No such job."
    if not refusal and not browser.SLOTS.acquire(blocking=False):  # pylint: disable=consider-using-with
        refusal = busy_browsers()
    if refusal:
        await websocket.send_json(l10n.with_templates({"type": "error", "message": refusal}))
        await websocket.close(code=1008)
        return False, None
    return True, job


def busy_browsers() -> l10n.Said:
    """The recorder's refusal when every browser is open: what holds them, without whose."""
    return l10n.said("No browser is free: {browsers}. Try again in a few minutes.", browsers=browsers_in_use())


def start_of(job: Job) -> str:
    """The listing a job's recipe starts at, or "" (another source, or a recipe being edited)."""
    try:
        return str(json.loads(str(job.options.get("recipe") or "{}") or "{}").get("start", ""))
    except (ValueError, AttributeError):
        return ""


async def answer(websocket: WebSocket, recorder: Recorder, job: Job | None, nc: AsyncNextcloudApp, message: dict):
    """Does what the recorder's page asks: the job's session kept, the test as the job runs (with
    its account, which only the app reads), else the recorder's own."""
    if message.get("type") in ("save-session", "cookies-keep") and job:
        await keep_session(websocket, recorder, job, nc, message["type"])
    elif message.get("type") == "test" and message.get("as_job"):
        await test_as_job(websocket, recorder, message, nc)
    else:
        await recorder.handle(message)


async def test_as_job(websocket: WebSocket, recorder: Recorder, message: dict, nc: AsyncNextcloudApp) -> None:
    """The recipe's test as the job runs: with the saved job's account, read here from the user's
    own job and handed to the recorder (the page never sees it, nor sends one)."""
    try:
        job = await own_job(int(message.get("job") or 0), nc)
    except (TypeError, ValueError, HTTPException):
        job = None
    if job is None or job.source != "site":
        refusal = "Save the job first: the test as the job runs signs in with its stored account."
        await websocket.send_json(l10n.with_templates({"type": "error", "message": refusal}))
        return
    account = {
        name: await nc.preferences_ex.get_value(credential_key(job, name)) or ""
        for name in SOURCES[job.source].credentials
    }
    await recorder.test(message, account)


async def keep_session(websocket: WebSocket, recorder: Recorder, job: Job, nc: AsyncNextcloudApp, kind: str) -> None:
    """Keeps the browser's session for the job: Done (save-session) also runs the job again and
    ends the view; the cookies panel's "Use for this job" (cookies-keep) keeps it as it is now,
    for the job's next run, and the browser stays open."""
    if kind == "save-session":
        await save_answer(job, await recorder.session(), nc)
        await websocket.send_json({"type": "session-saved"})
    else:
        await save_session(nc, job, await recorder.session())
        await websocket.send_json({"type": "cookies-kept"})


async def save_answer(job: Job, session: dict, nc: AsyncNextcloudApp) -> None:
    """Keeps the session the user left the browser with, and runs the job again with it."""
    await save_session(nc, job, session)
    store().finish(job.id or 0, "ok", l10n.said("continued in the browser"))
    STATE["executor"].request(job.id)


async def enabled_handler(enabled: bool, nc: AsyncNextcloudApp) -> str:
    """Adds the app's entry to the top menu, with its page's script, and the admin's settings."""
    if enabled:
        await nc.ui.resources.set_script("top_menu", "harvester", "js/harvester-main")
        await nc.ui.top_menu.register("harvester", "Harvester", "img/app.svg")
        await nc.ui.settings.register_form(
            settings.form(
                RUNNERS, browser.BROWSERS, settings.DEFAULT_PROXY, settings.DEFAULT_PROXY_NAME, settings.DEFAULT_GUARD
            )
        )
        await nc.ui.settings.register_form(
            settings.dns_form(settings.DEFAULT_DNS, settings.DEFAULT_DNS_FALLBACK, dns.PROXY.status)
        )
    else:
        await nc.ui.top_menu.unregister("harvester")
        await nc.ui.settings.unregister_form("harvester_admin")
        await nc.ui.settings.unregister_form("harvester_dns")
    return ""


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level="info")
