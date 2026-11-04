# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move to the private library (librarymove.py) from the Files app, and the admin's reset of a
person's library.

The Files action "Move to the private library" (registered with AppAPI's files actions menu,
version 2: one request with every node selected) posts the selected files here as the person;
the answer sends the browser to Harvester's page (redirect_handler, with ?fileIds= or without),
which opens the library for a move proposed in the last minutes, where the person picks the
library folder and starts the move, and watches it go.

The reset is an occ command AppAPI runs (harvester:library-reset <user> --confirm): its route is
outside /api, which AppAPI's proxy does not pass on (appinfo/info.xml), and it answers only a
request without a person (AppAPI's own)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..library import BadPath
from ..librarymove import FOLDER, MOST, Moves
from .common import NC, STATE

ROUTER = APIRouter()
# The Files action's name, and the page it sends the browser to (the top menu entry's).
ACTION = "harvester_library"
PAGE = "harvester"
RESET = "harvester:library-reset"


def moves() -> Moves:
    """The people's moves, set at start."""
    return STATE["moves"]


class Node(BaseModel):
    """A node of the Files action (nc_py_api's ActionFileInfo): what the move needs of it."""

    fileId: int  # pylint: disable=invalid-name
    name: str = Field("", max_length=1000)
    directory: str = Field("", max_length=4000)
    fileType: str = Field("file", max_length=10)  # pylint: disable=invalid-name


class Action(BaseModel):
    """The Files action's request (ActionFileInfoEx): the nodes selected."""

    files: Annotated[list[Node], Field(min_length=1, max_length=MOST)]


@ROUTER.post("/api/library/from-files")
async def from_files(body: Action, nc: NC) -> dict:
    """Proposes a move of the files selected in Files, and sends the browser to the page."""
    moves().propose(await nc.user, [node.model_dump() for node in body.files])
    return {"redirect_handler": PAGE}


@ROUTER.get("/api/library/moves")
async def list_moves(nc: NC) -> list[dict]:
    """The person's moves (proposed, under way, ended), the newest first."""
    return [move.view() for move in moves().of(await nc.user)]


class Start(BaseModel):
    """The library folder a move goes into."""

    folder: str = Field(FOLDER, max_length=4000)


@ROUTER.post("/api/library/moves/{move_id}/start")
async def start_move(move_id: str, body: Start, nc: NC) -> dict:
    """Starts the person's proposed move into the folder."""
    move = moves().get(await nc.user, move_id)
    if move is None:
        raise HTTPException(404, "No such move.")
    try:
        moves().start(move, body.folder)
    except BadPath as error:
        raise HTTPException(422, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, "The move has started already.") from error
    return move.view()


class OccInput(BaseModel):
    """An occ command's arguments and options, as AppAPI sends them."""

    arguments: dict | None = None
    options: dict | None = None


class Occ(BaseModel):
    """AppAPI's request to run an occ command."""

    occ: OccInput


@ROUTER.post("/occ/library-reset")
async def reset_library(body: Occ, nc: NC) -> PlainTextResponse:
    """The admin's reset of a person's library (occ): with --confirm, deletes it entirely; without,
    says what it would delete."""
    if await nc.user:
        raise HTTPException(403, "Only an admin's occ command resets a library.")
    user = str((body.occ.arguments or {}).get("user") or "").strip()
    if not user:
        return PlainTextResponse("Name the person whose library to reset.\n")
    shelf = STATE["shelf"]
    if not (body.occ.options or {}).get("confirm"):
        return PlainTextResponse(
            f"{user}'s private library holds {shelf.usage(user)} bytes. Run the command again with --confirm"
            " to delete it entirely: its files cannot be recovered, by anyone.\n"
        )
    count = shelf.reset(user)
    return PlainTextResponse(
        f"{user}'s private library is deleted ({count} files and folders); they can set it up anew.\n"
    )


def occ_command() -> dict:
    """The reset's occ command, for AppAPI (nc.occ_commands.register's arguments)."""
    return {
        "name": RESET,
        "callback_url": "/occ/library-reset",
        "description": "Deletes a person's private library in Harvester entirely (a forgotten password"
        " and recovery key)",
        "arguments": [{"name": "user", "mode": "required", "description": "The person's user ID"}],
        "options": [
            {
                "name": "confirm",
                "shortcut": None,
                "mode": "none",
                "description": "Delete it: without this, it only says what it would delete",
            }
        ],
    }
