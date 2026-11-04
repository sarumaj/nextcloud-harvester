# SPDX-License-Identifier: AGPL-3.0-or-later
"""The person's Immich account (Save to: Immich, immich.py): its server (the admin's preset unless
they type another) and their API key, tested before it is kept (Immich's GET /api/users/me: the
page shows the account's email). The key is a sensitive preference of theirs, never sent back
nor logged: the page learns only whether one is kept."""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import immich
from .common import NC

ROUTER = APIRouter()


class Account(BaseModel):
    """The server's address ("": the admin's) and the API key ("": the one kept)."""

    url: str = Field("", max_length=2000)
    key: str = Field("", max_length=500)


def _tested(url: str, key: str) -> dict:
    """The account's email and name, or 422 with why (the address, a refused key, Immich's
    answer)."""
    if not url:
        raise HTTPException(422, "Enter your Immich server's address.")
    if not key:
        raise HTTPException(422, "Enter your Immich API key.")
    try:
        client = immich.Client(url, key)
    except immich.ImmichError as error:
        raise HTTPException(422, str(error)) from error
    try:
        return client.me()
    except immich.ImmichRefused as error:
        raise HTTPException(422, "Immich refused the API key.") from error
    except immich.SourceError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        client.close()


@ROUTER.get("/api/immich")
async def immich_account(nc: NC) -> dict:
    """The address the person typed ("": the admin's), the admin's, whether a key is kept, and
    whether plain http is allowed."""
    return {
        "url": await nc.preferences_ex.get_value(immich.URL_KEY) or "",
        "preset": immich.SETTINGS["url"],
        "key": bool(await nc.preferences_ex.get_value(immich.API_KEY)),
        "http": bool(immich.SETTINGS["http"]),
    }


@ROUTER.put("/api/immich")
async def set_immich_account(body: Account, nc: NC) -> dict:
    """Tests the account (the key typed, else the one kept) and keeps it: its email and name."""
    url = body.url.strip() or str(immich.SETTINGS["url"])
    key = body.key.strip() or await nc.preferences_ex.get_value(immich.API_KEY) or ""
    me = await asyncio.to_thread(_tested, url, key)
    if body.url.strip() and not immich.preset(body.url):
        await nc.preferences_ex.set_value(immich.URL_KEY, immich.base(body.url))
    else:
        await nc.preferences_ex.delete(immich.URL_KEY)
    if body.key.strip():
        await nc.preferences_ex.set_value(immich.API_KEY, key, sensitive=True)
    return me


@ROUTER.post("/api/immich/test")
async def test_immich_account(nc: NC) -> dict:
    """Tests the account kept: its email and name, or 422 with why."""
    url = await nc.preferences_ex.get_value(immich.URL_KEY) or str(immich.SETTINGS["url"])
    key = await nc.preferences_ex.get_value(immich.API_KEY) or ""
    return await asyncio.to_thread(_tested, url, key)


@ROUTER.delete("/api/immich")
async def forget_immich_account(nc: NC) -> dict:
    """Forgets the person's Immich account (its key and address)."""
    await nc.preferences_ex.delete([immich.URL_KEY, immich.API_KEY])
    return {}
