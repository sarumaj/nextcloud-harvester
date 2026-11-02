"""The admin's settings (Administration settings > Additional settings > Harvester): how many jobs
run at once and how many browsers may be open. Their defaults come from the environment
(HARVESTER_RUNNERS, HARVESTER_BROWSERS, which the deployment sets); a value the admin chooses
wins, and the executor reads them each minute, so a change needs no restart.
"""

from nc_py_api.ex_app.ui.settings import SettingsField, SettingsFieldType, SettingsForm

RUNNERS = "runners"
BROWSERS = "browsers"
# The choices: each runner is a thread, each browser some 300-600 MB of the server's memory.
MAX_RUNNERS = 8
MAX_BROWSERS = 8


def form(runners: int, browsers: int) -> SettingsForm:
    """The admin's form, with the environment's values as its defaults."""
    return SettingsForm(
        id="harvester_admin",
        section_type="admin",
        section_id="additional",
        title="Harvester",
        description="Changes take effect within a minute; running jobs and open browsers finish first.",
        fields=[
            SettingsField(
                id=RUNNERS,
                title="Jobs at once",
                type=SettingsFieldType.SELECT,
                default=str(runners),
                options={str(n): str(n) for n in range(1, MAX_RUNNERS + 1)},
                description="How many jobs run side by side (never one twice, one per site at a time).",
            ),
            SettingsField(
                id=BROWSERS,
                title="Browsers at once",
                type=SettingsFieldType.SELECT,
                default=str(browsers),
                options={str(n): str(n) for n in range(1, MAX_BROWSERS + 1)},
                description=(
                    "How many browsers may be open: the website jobs and the recordings share them. "
                    "Each takes some 300-600 MB of the server's memory."
                ),
            ),
        ],
    )


def number(value: str | None, default: int, highest: int) -> int:
    """A setting's value as a number from 1 to highest; the default if unset or not a number."""
    try:
        return min(max(1, int(str(value))), highest)
    except ValueError:
        return default
