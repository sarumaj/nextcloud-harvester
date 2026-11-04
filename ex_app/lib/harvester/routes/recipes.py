# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipe files (export, import), a recipe's steps as Chrome's Recorder's recordings and back
(chromerecorder.py), and the person's own templates."""

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from .. import chromerecorder, recipe_file
from .. import recipe as recipes
from .common import NC, JobIn, checked, store

ROUTER = APIRouter()


class RecipeText(BaseModel):
    """A recipe file as the page read it (recipe_file.py), checked by the server."""

    text: str = Field(max_length=recipe_file.MAX_BYTES)


@ROUTER.post("/api/recipes/export")
async def export_recipe(body: JobIn, _nc: NC) -> dict:
    """The recipe file of the job as the form holds it (checked as Save checks it): its source,
    options, tags and schedule; never its account, session or folder."""
    return recipe_file.export(checked(body).model_dump())


@ROUTER.post("/api/recipes/import")
async def import_recipe(body: RecipeText, _nc: NC) -> dict:
    """The job a recipe file describes, for the form (nothing is saved): 422 with why when the
    file is refused, by its schema or by the checks of a saved job."""
    try:
        job = recipe_file.parse(body.text)
        checked(JobIn(**(job | {"target": "/"})))
    except ValueError as error:
        # pydantic's ValidationError is a ValueError: its problems by field.
        text = recipe_file.problems(error) if isinstance(error, ValidationError) else str(error)
        raise HTTPException(422, text) from error
    return job


class ChromeImport(BaseModel):
    """A recording of Chrome's Recorder, and the recipe (JSON, as the form holds it) whose steps
    it replaces."""

    text: str = Field(max_length=chromerecorder.MAX_BYTES)
    recipe: str = Field("", max_length=recipe_file.MAX_BYTES)


def _recipe(text: str) -> dict:
    """The form's recipe (422 for one that is no JSON object)."""
    try:
        recipe = json.loads(text or "{}")
    except ValueError as error:
        raise HTTPException(422, f"the recipe is not JSON: {error}") from error
    if not isinstance(recipe, dict):
        raise HTTPException(422, "the recipe is not an object")
    return recipe


@ROUTER.post("/api/recipes/chrome/import")
async def import_chrome(body: ChromeImport, _nc: NC) -> dict:
    """The recipe with its steps (and window) from the recording, what was left out or changed
    (warnings) and what the recipe's check finds (problems); nothing is saved."""
    recipe = _recipe(body.recipe)
    try:
        steps, viewport, warnings = chromerecorder.import_text(body.text)
    except chromerecorder.RecordingError as error:
        raise HTTPException(422, str(error)) from error
    recipe["steps"] = steps
    if viewport is not None:
        recipe["viewport"] = viewport
    return {"recipe": recipe, "warnings": warnings, "problems": recipes.check_recipe(recipe), "count": len(steps)}


class ChromeExport(BaseModel):
    """The recipe (JSON, as the form holds it) and the recording's title (the job's name)."""

    recipe: str = Field(max_length=recipe_file.MAX_BYTES)
    title: str = Field("", max_length=200)


@ROUTER.post("/api/recipes/chrome/export")
async def export_chrome(body: ChromeExport, _nc: NC) -> dict:
    """The recipe's steps as a recording of Chrome's Recorder, and what Chrome's replay cannot do
    of them (warnings)."""
    flow, warnings = chromerecorder.export_flow(_recipe(body.recipe), body.title)
    return {"recording": flow, "warnings": warnings}


@ROUTER.get("/api/templates")
async def list_templates(nc: NC) -> list[dict]:
    """The user's own templates (recipe files saved from a job's form)."""
    return store().templates(await nc.user)


@ROUTER.post("/api/templates")
async def save_template(body: JobIn, nc: NC) -> dict:
    """Keeps the job as the form holds it as one of the user's templates (its recipe file: no
    account, session or folder)."""
    return store().add_template(await nc.user, body.name, recipe_file.export(checked(body).model_dump()))


@ROUTER.delete("/api/templates/{template_id}")
async def delete_template(template_id: int, nc: NC) -> dict:
    """Removes one of the user's templates."""
    if not store().delete_template(await nc.user, template_id):
        raise HTTPException(404, "no such template")
    return {}
