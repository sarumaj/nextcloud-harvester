# SPDX-License-Identifier: AGPL-3.0-or-later
"""The job form's Try the address (POST /api/try): what a job would fetch, listed once."""

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import tryout
from ..sources import SOURCES
from ..store import Job
from .common import NC, STATE, JobIn, checked, own_job, store

ROUTER = APIRouter()


class TryIn(BaseModel):
    """A try of a job's listing from its form: the job as the form holds it (as Save sends it),
    the saved job it edits (its history, account and kept session), and the account's fields
    as typed (empty: the saved job's stay)."""

    job: JobIn
    id: int | None = None
    account: dict[Annotated[str, Field(max_length=40)], Annotated[str, Field(max_length=65_536)]] = Field(
        {}, max_length=10
    )


@ROUTER.post("/api/try")
async def try_listing(body: TryIn, nc: NC) -> dict:
    """Lists what the job as the form holds it would fetch, once, downloading nothing (tryout.py):
    checked as Save checks it (422), not for website jobs (their recorder tests the recipe), one
    try of the person's at a time (409). In a runner's place (Executor.trial) for at most
    tryout.LIMIT seconds: what it found by then comes back, cut there. The account typed goes to
    the source only, never into a log."""
    if body.job.source == "site":
        raise HTTPException(422, "A website job is tried with Test the recipe in its recorder.")
    form = checked(body.job)
    source_class = SOURCES[form.source]
    # An on-demand job's own address may be empty: there is nothing to try then.
    if source_class.address_option and not source_class.address_of(form.options):
        raise HTTPException(422, "Type the address to try first.")
    user = await nc.user
    saved = await own_job(body.id, nc) if body.id is not None else None
    # The saved job's history, account and session only while it is of the same source.
    job_id = saved.id if saved is not None and saved.source == form.source else None
    job = Job(id=job_id, user=user, **form.model_dump())
    account = {name: value for name, value in body.account.items() if name in source_class.credentials and value}
    if not STATE["executor"].enter_trial(user):
        raise HTTPException(409, "A try of yours is still going on: wait for its end.")
    known = (lambda key: store().seen(job_id, key)) if job_id is not None else None
    return await tryout.attempt(STATE["executor"], job, account, known)
