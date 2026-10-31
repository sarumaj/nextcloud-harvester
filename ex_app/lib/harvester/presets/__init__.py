"""Templates of jobs for known sites (presets/<id>.json), with a few parameters.

A template has a title, a description, its parameters ({name: {title, default, check (a
regular expression the value must match), description}}) and the job, in whose strings
${name} stands for a parameter's value. Its recipe is an object here; the job gets it as JSON.
"""

import json
import re
from pathlib import Path
from string import Template

PRESETS = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in Path(__file__).parent.glob("*.json")}


def _fill(value, parameters: dict[str, str]):
    if isinstance(value, str):
        return Template(value).safe_substitute(parameters)
    if isinstance(value, list):
        return [_fill(item, parameters) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, parameters) for key, item in value.items()}
    return value


def make_job(preset_id: str, given: dict[str, str]) -> dict:
    """The template's job with these parameters (the defaults for those not given)."""
    preset = PRESETS[preset_id]
    parameters = {}
    for name, spec in preset.get("parameters", {}).items():
        value = str(given.get(name) or spec["default"]).strip()
        if not re.fullmatch(spec.get("check", ".*"), value):
            raise ValueError(f"{spec['title']}: {value} is not allowed")
        parameters[name] = value
    job = _fill(preset["job"], parameters)
    # A website's recipe is an object here; the job keeps it as JSON (other sources have none).
    if isinstance(job.get("options", {}).get("recipe"), dict):
        job["options"]["recipe"] = json.dumps(job["options"]["recipe"], indent=2)
    return job
