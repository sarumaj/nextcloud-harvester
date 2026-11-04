# SPDX-License-Identifier: AGPL-3.0-or-later
"""A vault job's keys (Save to: a vault, vaultlib.py): the form sends the vault's password once,
the server unlocks the vault with it as the person (over WebDAV, davstorage.py) and keeps only
the vault's keys as a sensitive preference of theirs; the password is neither kept nor logged,
and the keys never leave the server."""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import destinations
from ..davstorage import WebDavStorage
from ..vault import CorruptVault, UnsupportedVault, Vault, WrongPassword
from ..vaultlib import decoded, encoded, key_name
from .common import NC, own_job

ROUTER = APIRouter()


class VaultPassword(BaseModel):
    """The vault's password, as typed."""

    password: str = Field(min_length=1, max_length=1000)


async def vault_job(job_id: int, nc: NC):
    """The person's job that saves into a vault (409 for another)."""
    job = await own_job(job_id, nc)
    if destinations.save_to(job.options) != destinations.DIRECT:
        raise HTTPException(409, "The job does not save into a vault.")
    return job


@ROUTER.get("/api/jobs/{job_id}/vault")
async def vault_state(job_id: int, nc: NC) -> dict:
    """Whether the job keeps its vault's keys (never the keys)."""
    job = await vault_job(job_id, nc)
    return {"key": decoded(await nc.preferences_ex.get_value(key_name(job))) is not None}


@ROUTER.put("/api/jobs/{job_id}/vault")
async def unlock_vault(job_id: int, body: VaultPassword, nc: NC) -> dict:
    """Unlocks the job's vault with its password and keeps its keys for the job's runs (422 with
    why: a wrong password, no vault there, one Harvester cannot write to, a damaged one)."""
    job = await vault_job(job_id, nc)
    loop = asyncio.get_running_loop()
    storage = WebDavStorage(
        nc,
        lambda awaitable: asyncio.run_coroutine_threadsafe(awaitable, loop).result(),
        destinations.folder(str(job.options.get(destinations.VAULT) or "")),
    )
    try:
        vault = await asyncio.to_thread(Vault.unlock, storage, body.password)
    except WrongPassword as error:
        raise HTTPException(422, "The password is not the vault's.") from error
    except UnsupportedVault as error:
        if "no vault.cryptomator" in str(error):
            raise HTTPException(422, "No vault in that folder: it has no vault.cryptomator.") from error
        raise HTTPException(
            422, "Harvester cannot write to this vault: only vaults of format 8 with a password (not Hub's)."
        ) from error
    except CorruptVault as error:
        raise HTTPException(422, "The vault's key file or configuration is damaged.") from error
    await nc.preferences_ex.set_value(key_name(job), encoded(vault.key_material), sensitive=True)
    return {}


@ROUTER.delete("/api/jobs/{job_id}/vault")
async def forget_vault(job_id: int, nc: NC) -> dict:
    """Forgets the job's vault's keys: its runs need the password again."""
    job = await own_job(job_id, nc)
    await nc.preferences_ex.delete(key_name(job))
    return {}
