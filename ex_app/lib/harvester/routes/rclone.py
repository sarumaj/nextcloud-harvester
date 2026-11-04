# SPDX-License-Identifier: AGPL-3.0-or-later
"""The job form's rclone wizard (rclone_wizard.py): a remote configured question by question."""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..rclone_wizard import WizardError, Wizards, remote_types
from .common import NC

ROUTER = APIRouter()
# The job form's rclone wizards (rclone_wizard.py), in memory: one ends with the app.
WIZARDS = Wizards()


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


@ROUTER.get("/api/rclone/types")
async def rclone_types(_nc: NC) -> list[dict]:
    """The remote types the rclone wizard offers (those the remote source allows)."""
    return await wizard_step(remote_types)


@ROUTER.post("/api/rclone/wizard")
async def start_wizard(body: WizardStart, nc: NC) -> dict:
    """Starts the user's rclone wizard for a remote of the type: its first question."""
    return await wizard_step(WIZARDS.start, await nc.user, body.type)


@ROUTER.post("/api/rclone/wizard/{session}/answer")
async def answer_wizard(session: str, body: WizardAnswer, nc: NC) -> dict:
    """Answers the user's wizard's question: the next one, or the finished configuration."""
    return await wizard_step(WIZARDS.answer, await nc.user, session, body.state, body.result)


@ROUTER.post("/api/rclone/wizard/{session}/back")
async def back_wizard(session: str, nc: NC) -> dict:
    """Takes the user's wizard one question back."""
    return await wizard_step(WIZARDS.back, await nc.user, session)


@ROUTER.delete("/api/rclone/wizard/{session}")
async def cancel_wizard(session: str, nc: NC) -> dict:
    """Ends the user's wizard and removes its configuration."""
    WIZARDS.cancel(await nc.user, session)
    return {}
