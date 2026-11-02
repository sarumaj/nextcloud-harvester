"""The rclone wizard: rclone's questions one at a time, with a fake rclone (its state machine as
`rclone config ... --non-interactive` answers), and once with the real one where it is installed
(HARVESTER_RCLONE or rclone on the PATH).
"""

# pylint: disable=missing-function-docstring,protected-access

import json
import os
import shutil
import subprocess

import pytest

from harvester.rclone_wizard import (
    IDLE,
    PER_USER,
    Step,
    WizardError,
    Wizards,
    authorize_command,
    pasted_token,
    provider_matches,
)
from harvester.sources.remote import parse_config

BOOL = {"Type": "bool", "Exclusive": True, "Examples": [{"Value": "true"}, {"Value": "false"}]}
# The questions of the fake's sftp, in order: rclone's state, then the option it asks for.
QUESTIONS = [
    ("*all-set,0,false", {"Name": "host", "Help": "SSH host to connect to.", "Required": True, "Type": "string"}),
    ("*all-set,3,false", {"Name": "pass", "Help": "SSH password.", "IsPassword": True, "Type": "string"}),
    ("*all-set,5,false", {"Name": "key_file", "Help": "Path to a PEM key file.", "Type": "string"}),
    ("*all-set,43,false", {"Name": "ssh", "Help": "Command to run.", "Type": "SpaceSepList"}),
    ("*all-set,14,false", {"Name": "disable_hashcheck", "Default": False, "DefaultStr": "false"} | BOOL),
    ("*all-advanced", {"Name": "config_fs_advanced", "Default": False} | BOOL),
]


class FakeRclone:
    """subprocess.run for rclone's configuration: asks QUESTIONS in turn, writes each answer that is
    not the default into the configuration (as rclone does, also those it should not have been sent)."""

    def __init__(self, questions=None):
        self.questions = questions or QUESTIONS
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def run(self, command, **kwargs):
        assert isinstance(command, list), "never a shell"
        self.calls.append(command)
        self.envs.append(kwargs.get("env") or {})
        if "obscure" in command:
            return subprocess.CompletedProcess(command, 0, f"OBSCURED-{kwargs['input'].strip().encode().hex()}\n", "")
        config = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--config="))
        if "create" in command:
            kind = command[command.index("create") + 2]
            with open(config, "w", encoding="utf-8") as file:
                file.write(f"[remote]\ntype = {kind}\n")
            return self.ask(command, 0)
        state = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--state="))
        result = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--result="))
        index = [listed for listed, _ in self.questions].index(state)
        option = self.questions[index][1]
        name = option["Name"]
        # rclone leaves out an answer that is the option's default.
        if not name.startswith("config_") and result != option.get("DefaultStr", ""):
            with open(config, "a", encoding="utf-8") as file:
                file.write(f"{name} = {result}\n")
        return self.ask(command, index + 1)

    def ask(self, command, index):
        state, option = self.questions[index] if index < len(self.questions) else ("", None)
        data = {"State": state, "Option": option, "Error": "", "Result": ""}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    def answers(self) -> dict[str, str]:
        """The answers sent, by state."""
        return {
            next(a[8:] for a in call if a.startswith("--state=")): next(
                a[9:] for a in call if a.startswith("--result=")
            )
            for call in self.calls
            if "--continue" in call
        }


class Clock:  # pylint: disable=too-few-public-methods
    """A clock the test moves."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(name="wizards")
def fixture_wizards(monkeypatch):
    monkeypatch.setenv("HARVESTER_RCLONE", "/usr/bin/rclone")
    fake = FakeRclone()
    made = Wizards(run=fake.run, clock=Clock())
    made.fake = fake  # type: ignore[attr-defined]
    yield made
    made.close()


def test_the_questions_one_at_a_time_up_to_a_checked_configuration(wizards, monkeypatch):
    monkeypatch.setenv("HARVESTER_SECRET_PROBE", "app secret")
    view = wizards.start("anna", "sftp")
    assert view["question"]["name"] == "host" and view["question"]["required"] and view["first"]
    view = wizards.answer("anna", view["session"], view["state"], "sftp.example.com")
    assert view["question"]["password"] and not view["first"]
    view = wizards.answer("anna", view["session"], view["state"], "se cret")
    # The key file, the command and the advanced settings are answered without asking.
    assert view["question"]["name"] == "disable_hashcheck" and view["value"] == "false"
    view = wizards.answer("anna", view["session"], view["state"], "false")
    assert view["done"] and not view.get("error")
    name, settings = parse_config(view["config"])
    assert name == "remote" and settings["host"] == "sftp.example.com"
    assert settings["pass"] == "OBSCURED-" + "se cret".encode().hex(), "rclone keeps passwords obscured"
    sent = wizards.fake.answers()
    assert sent["*all-set,5,false"] == "" and sent["*all-set,43,false"] == "" and sent["*all-advanced"] == "false"
    assert not any("se cret" in arg for call in wizards.fake.calls for arg in call), "a password is not an argument"
    assert all("HARVESTER_SECRET_PROBE" not in env for env in wizards.fake.envs)


def test_a_session_has_a_private_configuration_removed_at_the_end(wizards):
    view = wizards.start("anna", "sftp")
    session = wizards.sessions[view["session"]]
    assert os.stat(session.dir).st_mode & 0o077 == 0 and os.stat(session.path).st_mode & 0o077 == 0
    wizards.cancel("anna", view["session"])
    assert not os.path.exists(session.dir)


def test_only_the_remote_sources_types():
    with pytest.raises(WizardError):
        Wizards(run=FakeRclone().run).start("anna", "local")
    with pytest.raises(WizardError):
        Wizards(run=FakeRclone().run).start("anna", "alias")


def test_an_answer_that_is_a_flag_stays_a_value(wizards):
    view = wizards.start("anna", "sftp")
    view = wizards.answer("anna", view["session"], view["state"], "--config=/etc/passwd")
    assert "--result=--config=/etc/passwd" in wizards.fake.calls[-1]


def test_a_state_is_rclones_own_never_the_pages(wizards):
    view = wizards.start("anna", "sftp")
    with pytest.raises(WizardError) as refused:
        wizards.answer("anna", view["session"], "*all-set,40,false", "touch /tmp/x")
    assert refused.value.status == 409
    assert not any("--continue" in call for call in wizards.fake.calls)


def test_a_question_for_a_file_or_a_command_is_refused(wizards):
    view = wizards.start("anna", "sftp")
    session = wizards.sessions[view["session"]]
    session.current = Step("*all-set,5,false", {"Name": "key_file"}, "", "")
    with pytest.raises(WizardError, match="may not be set"):
        wizards.answer("anna", view["session"], "*all-set,5,false", "/app/secret")


@pytest.mark.parametrize(
    "answer, message",
    [("a\nkey_file = /etc/passwd", "several lines"), ("", "required")],
)
def test_answers_rclone_would_take_are_refused(wizards, answer, message):
    view = wizards.start("anna", "sftp")
    with pytest.raises(WizardError, match=message):
        wizards.answer("anna", view["session"], view["state"], answer)


def test_a_yes_or_no_question_takes_true_or_false(wizards):
    view = wizards.start("anna", "sftp")
    view = wizards.answer("anna", view["session"], view["state"], "sftp.example.com")
    view = wizards.answer("anna", view["session"], view["state"], "")
    with pytest.raises(WizardError, match="true or false"):
        wizards.answer("anna", view["session"], view["state"], "maybe")


def test_a_configuration_the_source_refuses_is_not_offered(wizards):
    view = wizards.start("anna", "sftp")
    view = wizards.answer("anna", view["session"], view["state"], "localhost")
    view = wizards.answer("anna", view["session"], view["state"], "")
    view = wizards.answer("anna", view["session"], view["state"], "false")
    assert view["done"] and "config" not in view
    assert "may not be reached" in view["error"]
    # Back to the last question, and on to the first, with the configuration as it was.
    view = wizards.back("anna", view["session"])
    assert view["question"]["name"] == "disable_hashcheck"
    view = wizards.back("anna", view["session"])
    view = wizards.back("anna", view["session"])
    assert view["question"]["name"] == "host" and view["value"] == "localhost" and view["first"]
    with open(wizards.sessions[view["session"]].path, encoding="utf-8") as file:
        assert file.read() == "[remote]\ntype = sftp\n"


def test_a_password_is_not_shown_again_going_back(wizards):
    view = wizards.start("anna", "sftp")
    view = wizards.answer("anna", view["session"], view["state"], "sftp.example.com")
    view = wizards.answer("anna", view["session"], view["state"], "se cret")
    view = wizards.back("anna", view["session"])
    assert view["question"]["name"] == "pass" and view["value"] == ""


def test_a_wizard_is_its_users_alone(wizards):
    view = wizards.start("anna", "sftp")
    for step in (
        lambda: wizards.answer("ben", view["session"], view["state"], "x"),
        lambda: wizards.back("ben", view["session"]),
    ):
        with pytest.raises(WizardError) as refused:
            step()
        assert refused.value.status == 404
    wizards.cancel("ben", view["session"])
    assert view["session"] in wizards.sessions


def test_an_idle_wizard_ends(wizards):
    view = wizards.start("anna", "sftp")
    directory = wizards.sessions[view["session"]].dir
    wizards.clock.now += IDLE + 1
    with pytest.raises(WizardError) as refused:
        wizards.answer("anna", view["session"], view["state"], "sftp.example.com")
    assert refused.value.status == 404
    assert not os.path.exists(directory)


def test_a_new_wizard_ends_the_users_oldest(wizards):
    views = []
    for _ in range(PER_USER + 1):
        wizards.clock.now += 1
        views.append(wizards.start("anna", "webdav"))
    wizards.start("ben", "webdav")
    assert views[0]["session"] not in wizards.sessions
    assert all(view["session"] in wizards.sessions for view in views[1:])
    assert len(wizards.sessions) == PER_USER + 1


def test_only_the_chosen_providers_questions(monkeypatch):
    monkeypatch.setenv("HARVESTER_RCLONE", "/usr/bin/rclone")
    questions = [
        ("*all-set,0,false", {"Name": "provider", "Type": "string", "Examples": [{"Value": "AWS"}]}),
        ("*all-set,1,false", {"Name": "ibm_api_key", "Provider": "IBMCOS", "Type": "string"}),
        ("*all-set,2,false", {"Name": "region", "Provider": "!IBMCOS,Storj", "Type": "string"}),
    ]
    wizards = Wizards(run=FakeRclone(questions).run)
    try:
        view = wizards.start("anna", "s3")
        view = wizards.answer("anna", view["session"], view["state"], "AWS")
        assert view["question"]["name"] == "region"
    finally:
        wizards.close()
    assert provider_matches("", "AWS") and provider_matches("AWS,Ceph", "Ceph") and provider_matches("!IBMCOS", "AWS")
    assert not provider_matches("IBMCOS", "AWS") and not provider_matches("!AWS,Ceph", "AWS")


def test_a_pasted_token_and_the_command_that_makes_it():
    printed = (
        'Paste the following into your remote machine --->\n{"access_token": "a",\n "expiry": "x"}\n<---End paste\n'
    )
    assert pasted_token(printed) == '{"access_token":"a","expiry":"x"}'
    assert pasted_token(" eyJabc ") == "eyJabc"
    help_ = (
        'Execute the following on the machine with the web browser:\n\n\trclone authorize "drive" "eyJz"\n\nThen paste'
    )
    assert authorize_command(help_) == 'rclone authorize "drive" "eyJz"'


RCLONE = os.environ.get("HARVESTER_RCLONE") or shutil.which("rclone")


@pytest.mark.skipif(not RCLONE, reason="rclone is not installed")
def test_the_real_rclone_configures_a_webdav_remote(monkeypatch):
    monkeypatch.setenv("HARVESTER_RCLONE", RCLONE or "")
    wizards = Wizards()
    try:
        view = wizards.start("anna", "webdav")
        answers = {"url": "https://cloud.example.org/remote.php/dav/files/anna", "vendor": "nextcloud", "user": "anna"}
        answers |= {"pass": "se cret"}
        for _ in range(20):
            if view["done"]:
                break
            question = view["question"]
            view = wizards.answer("anna", view["session"], view["state"], answers.get(question["name"], view["value"]))
        assert view["done"], view
        _, settings = parse_config(view["config"])
        assert settings["url"] == answers["url"] and settings["vendor"] == "nextcloud"
        # Stored obscured: rclone reveals the password typed.
        revealed = subprocess.run(
            [RCLONE or "", "reveal", settings["pass"]], capture_output=True, text=True, check=True
        )
        assert revealed.stdout.strip() == "se cret"
    finally:
        wizards.close()
