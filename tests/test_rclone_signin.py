"""The rclone wizard's sign-in in the app's browser: `rclone authorize` faked by a script (its link
on stderr, the token on stdout as rclone prints them), the app's one sign-in at a time, and the
browser's exception for rclone's listener (that recorder's alone, that port alone, while it runs).
One test starts the real rclone's listener where rclone is installed.
"""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import base64
import json
import os
import shutil
import sys

import pytest
from test_rclone_wizard import FakeRclone

from harvester.browser.recorder import Recorder
from harvester.rclone_wizard import AUTHORIZE_ORIGIN, WizardError, Wizards, authorize_arguments


def blob(settings: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(settings).encode()).decode().rstrip("=")


SCOPE = blob({"scope": "drive.readonly"})
TOKEN_STATE = "*oauth-authorize,teamdrive,oauth,"
QUESTIONS = [
    ("*all-set,6,false", {"Name": "scope", "Type": "string"}),
    (
        TOKEN_STATE,
        {
            "Name": "config_token",
            "Help": "Execute the following on the machine with the web browser:\n\n"
            f'\trclone authorize "drive" "{SCOPE}"\n\nThen paste the result.\n',
            "Required": True,
            "Type": "string",
        },
    ),
    ("teamdrive_ok", {"Name": "config_change_team_drive", "Default": False, "Type": "bool"}),
]
TOKEN = '{"access_token":"ya29.fake","token_type":"Bearer","refresh_token":"1//fake"}'
PRINTED = "Paste the following into your remote machine --->\n" + TOKEN + "\n<---End paste\n"
LINK = "http://127.0.0.1:53682/auth?state=AbC"


def fake_rclone(tmp_path, then: str) -> str:
    """A script in rclone's place for `rclone authorize`: writes its arguments to argv.json, prints
    rclone's link, then does then (Python)."""
    script = tmp_path / "rclone"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "print('2026/11/02 10:16:40 NOTICE: Waiting for code...', file=sys.stderr)\n"
        f"print('2026/11/02 10:16:40 NOTICE: Please go to the following link: {LINK}', file=sys.stderr, flush=True)\n"
        f"{then}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture(name="at_sign_in")
def fixture_at_sign_in(monkeypatch, tmp_path):
    """A factory: wizards whose rclone is the script, and a wizard of anna's at the token question."""
    made: list[Wizards] = []

    def at_sign_in(then: str = f"print({PRINTED!r})"):
        monkeypatch.setenv("HARVESTER_RCLONE", fake_rclone(tmp_path, then))
        fake = FakeRclone(QUESTIONS)
        wizards = Wizards(run=fake.run)
        wizards.fake = fake  # type: ignore[attr-defined]
        made.append(wizards)
        view = wizards.start("anna", "drive")
        view = wizards.answer("anna", view["session"], view["state"], "drive.readonly")
        assert view["question"]["name"] == "config_token"
        return wizards, view["session"]

    yield at_sign_in
    for wizards in made:
        wizards.close()


def test_the_printed_token_answers_the_question(at_sign_in, tmp_path):
    wizards, session = at_sign_in()

    async def flow():
        authorization = wizards.authorization("anna", session)
        try:
            assert await authorization.start() == LINK
            return await authorization.token()
        finally:
            await authorization.stop()

    token = asyncio.run(flow())
    assert token == TOKEN
    arguments = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    assert arguments[2:] == ["authorize", "drive", SCOPE, "--auth-no-open-browser"]
    assert arguments[0].startswith("--config=") and wizards.authorizing is None
    view = wizards.answer("anna", session, TOKEN_STATE, token)
    assert wizards.fake.answers()[TOKEN_STATE] == TOKEN and view["question"]["name"] == "config_change_team_drive"


def test_a_failed_sign_in_says_why(at_sign_in):
    wizards, session = at_sign_in(
        "print('2026/11/02 NOTICE: Fatal error: failed to get token: invalid_grant', file=sys.stderr); sys.exit(2)"
    )

    async def flow():
        authorization = wizards.authorization("anna", session)
        try:
            await authorization.start()
            with pytest.raises(WizardError, match="invalid_grant"):
                await authorization.token()
        finally:
            await authorization.stop()

    asyncio.run(flow())
    assert wizards.authorizing is None


def test_one_sign_in_at_a_time_and_a_stopped_one_ends_rclone(at_sign_in):
    wizards, session = at_sign_in("time.sleep(60)")
    other, ben = at_sign_in("time.sleep(60)")

    async def flow():
        authorization = wizards.authorization("anna", session)
        await authorization.start()
        with pytest.raises(WizardError, match="Another sign-in is running"):
            wizards.authorization("anna", session)
        await authorization.stop()
        assert authorization.process is not None and authorization.process.returncode is not None
        assert wizards.authorizing is None
        wizards.authorization("anna", session).kill()
        # The app's wizards are one object (main.WIZARDS): another's holds its own.
        other.authorization("anna", ben).kill()

    asyncio.run(flow())


def test_the_app_stopping_ends_the_sign_in(at_sign_in):
    wizards, session = at_sign_in("time.sleep(60)")

    async def flow():
        authorization = wizards.authorization("anna", session)
        await authorization.start()
        wizards.close()
        assert await asyncio.wait_for(authorization.process.wait(), 10) is not None
        assert wizards.authorizing is None

    asyncio.run(flow())


def test_a_sign_in_only_for_the_users_wizard_at_its_token_question(at_sign_in):
    wizards, session = at_sign_in()
    with pytest.raises(WizardError) as refused:
        wizards.authorization("ben", session)
    assert refused.value.status == 404
    wizards.back("anna", session)
    with pytest.raises(WizardError, match="does not ask for a sign-in"):
        wizards.authorization("anna", session)
    assert wizards.authorizing is None


@pytest.mark.parametrize(
    "command",
    [
        'rclone authorize "onedrive" "x"',
        'rclone authorize "drive" "--config=/etc/x"',
        'rclone authorize "drive" "a;b"',
        'rclone authorize "drive" a b c',
        "rclone config file",
        f'rclone authorize "drive" "{blob({"token_url": "http://127.0.0.1:8080/token"})}"',
        f'rclone authorize "drive" "{blob({"service_account_file": "/app/secret.json"})}"',
        'rclone authorize "drive" "not base64 json"',
    ],
)
def test_only_rclones_own_sign_in_runs(command):
    with pytest.raises(WizardError):
        authorize_arguments(command, "drive")


def test_rclones_sign_in_commands():
    assert authorize_arguments(f'rclone authorize "drive" "{SCOPE}"', "drive") == ["drive", SCOPE]
    assert authorize_arguments('rclone authorize "dropbox"', "dropbox") == ["dropbox"]
    assert authorize_arguments('rclone authorize "box" "id" "secret"', "box") == ["box", "id", "secret"]


class Route:
    """A request the browser's guard lets through or aborts."""

    def __init__(self, url: str):
        self.request = type("Request", (), {"url": url})()
        self.verdict = ""

    async def continue_(self):
        self.verdict = "continued"

    async def abort(self, _reason):
        self.verdict = "aborted"


def guarded(recorder: Recorder, url: str) -> str:
    route = Route(url)
    asyncio.run(recorder._guard(route))  # type: ignore[arg-type]
    return route.verdict


async def ignore(_message):
    """A page that reads nothing."""


def test_the_browser_opens_rclones_listener_alone_and_only_while_allowed():
    signing_in, recording = Recorder(ignore), Recorder(ignore)
    signing_in.loopback = AUTHORIZE_ORIGIN
    assert guarded(signing_in, LINK) == "continued"
    assert guarded(signing_in, "http://127.0.0.1:53682/?state=AbC&code=x") == "continued"
    for elsewhere in (
        "http://127.0.0.1:53683/",
        "http://127.0.0.1/",
        "https://127.0.0.1:53682/",
        "http://localhost:53682/",
        "http://127.0.0.2:53682/",
        "http://[::1]:53682/",
        "file:///app/secret",
    ):
        assert guarded(signing_in, elsewhere) == "aborted", elsewhere
    assert guarded(recording, LINK) == "aborted", "another browser has no exception"
    signing_in.loopback = ""
    assert guarded(signing_in, LINK) == "aborted", "gone once rclone ends"


RCLONE = os.environ.get("HARVESTER_RCLONE") or shutil.which("rclone")


@pytest.mark.skipif(not RCLONE, reason="rclone is not installed")
def test_the_real_rclone_listens_until_stopped(monkeypatch):
    monkeypatch.setenv("HARVESTER_RCLONE", RCLONE or "")
    wizards = Wizards()

    async def flow():
        view = wizards.start("anna", "dropbox")
        while view["question"]["name"] != "config_token":
            view = wizards.answer("anna", view["session"], view["state"], view["value"])
        for _ in range(2):
            # Twice: the first one's listener is gone once it stopped (the port is free again).
            authorization = wizards.authorization("anna", view["session"])
            try:
                link = await authorization.start()
                assert link.startswith(f"{AUTHORIZE_ORIGIN}/auth?state=")
            finally:
                await authorization.stop()

    try:
        asyncio.run(flow())
    finally:
        wizards.close()


def test_ending_the_wizard_ends_its_sign_in_from_the_loop_or_a_worker(at_sign_in):
    async def cancelled(in_worker: bool):
        wizards, session = at_sign_in("time.sleep(60)")
        authorization = wizards.authorization("anna", session)
        await authorization.start()
        token = asyncio.ensure_future(authorization.token())
        if in_worker:
            # A wizard ended in a worker thread (a third wizard of the person's, expiry).
            await asyncio.to_thread(wizards.cancel, "anna", session)
        else:
            wizards.cancel("anna", session)
        with pytest.raises(WizardError):
            await asyncio.wait_for(token, 10)
        assert await asyncio.wait_for(authorization.process.wait(), 10) is not None, "rclone is gone"
        assert wizards.authorizing is None, "the app's sign-in is free again"

    asyncio.run(cancelled(False))
    asyncio.run(cancelled(True))


def test_ending_another_wizard_leaves_a_sign_in_alone(at_sign_in):
    wizards, session = at_sign_in("time.sleep(60)")
    other = wizards.start("ben", "drive")["session"]

    async def flow():
        authorization = wizards.authorization("anna", session)
        await authorization.start()
        wizards.cancel("ben", other)
        assert authorization.process.returncode is None and wizards.authorizing is authorization
        await authorization.stop()

    asyncio.run(flow())
