# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipe files (export, import) and the person's own templates."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from .. import recipe_file
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
