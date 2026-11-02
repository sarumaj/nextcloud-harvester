"""The job form's rclone wizard: rclone's own configuration questions, one at a time, for the remote
source's account, instead of a configuration written on another computer and pasted.

It drives rclone's non-interactive configuration (`rclone config create <name> <type> --all
--non-interactive` asks the first question as JSON, `rclone config update <name> --continue
--state=<state> --result=<answer>` answers it and asks the next, until the state is empty), never a
shell: the person's answers reach rclone only as the values of --result. Each wizard has a
configuration of its own in a fresh private directory, removed when it ends, and rclone runs
without the app's secrets in its environment. The finished remote is offered only when
sources.remote.parse_config() accepts it, as a run would.

The state rclone returns is kept here, not taken from the page: a state names an option by its
index, and a made-up one would set any option (a command among them).
"""

import functools
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .browser import child_env
from .sources.base import SourceError
from .sources.remote import ALLOWED_TYPES, AMBIENT_KEYS, FORBIDDEN_KEY, last_line, parse_config

# The remote's name: fixed, so that nothing the person types names a section of the configuration.
NAME = "remote"
# Seconds a step of rclone may take (OneDrive's and Shared Drives' questions ask the provider).
TIMEOUT = 60
# A wizard ends after this many seconds without an answer: long enough to run `rclone authorize`
# on another computer and sign in there.
IDLE = 900
# Wizards at a time: a person's (a new one ends their oldest), and all of them.
PER_USER = 2
TOTAL = 20
# Questions answered without asking: no advanced settings (they are where most of the files and
# commands are, and a job does not need them), and no browser here for rclone's sign-in (the
# token comes from `rclone authorize` on the person's computer), and the credentials from the
# configuration, never from the app's machine (remote.AMBIENT_KEYS).
AUTOMATIC = {"config_fs_advanced": "false", "config_is_local": "false"} | dict.fromkeys(AMBIENT_KEYS, "false")
# rclone asks this many questions at most; more means it asks the same one again and again.
MAX_STEPS = 200


class WizardError(Exception):
    """A step the wizard cannot take, with the HTTP status that says why."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def rclone_binary() -> str:
    """rclone as the remote source finds it, or WizardError."""
    binary = os.environ.get("HARVESTER_RCLONE") or shutil.which("rclone") or ""
    if not binary:
        raise WizardError("rclone is not installed", 503)
    return binary


@functools.lru_cache(maxsize=4)
def _descriptions(binary: str) -> dict[str, str]:
    """rclone's description of each remote type ({} when rclone does not tell)."""
    try:
        result = subprocess.run(
            [binary, "config", "providers"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=True,
            env=child_env(),
        )
        return {str(item["Name"]): str(item.get("Description") or "") for item in json.loads(result.stdout)}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return {}


def remote_types() -> list[dict[str, str]]:
    """The types the remote source allows, with rclone's descriptions, for the wizard's first step."""
    descriptions = _descriptions(rclone_binary())
    return [{"name": kind, "description": descriptions.get(kind) or kind} for kind in sorted(ALLOWED_TYPES)]


def authorize_command(text: str) -> str:
    """The `rclone authorize ...` line of rclone's question for a token, or ""."""
    found = re.search(r"^\s*(rclone authorize .+?)\s*$", text, re.MULTILINE)
    return found[1] if found else ""


def pasted_token(text: str) -> str:
    """The token as rclone wants it from what `rclone authorize` printed: its JSON on one line,
    without the "Paste the following ... ---> ... <---End paste" around it."""
    found = re.search(r"--->(.*?)<---", text, re.DOTALL)
    token = (found[1] if found else text).strip()
    try:
        return json.dumps(json.loads(token), separators=(",", ":"))
    except ValueError:
        return token


def provider_matches(providers: str, provider: str) -> bool:
    """Whether an option is one of the provider's, as rclone's fs.MatchProvider tells: a list of
    providers, or "!" and the providers it is not for; empty, of every provider."""
    if not providers or not provider:
        return True
    negate = providers.startswith("!")
    return (provider in providers.lstrip("!").split(",")) != negate


def as_text(value) -> str:
    """An option's default or example as the form shows it and rclone reads it back."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


@dataclass
class Step:
    """A question rclone asked, the configuration before it was answered, and the answer."""

    state: str
    option: dict
    error: str
    before: str
    answer: str = ""

    @property
    def name(self) -> str:
        """The option the question sets."""
        return str(self.option.get("Name") or "")

    def question(self) -> dict:
        """The question as the page shows it."""
        option = self.option
        default = option["DefaultStr"] if "DefaultStr" in option else as_text(option.get("Default"))
        return {
            "name": self.name,
            "help": str(option.get("Help") or ""),
            "type": str(option.get("Type") or "string"),
            "default": str(default),
            "examples": [
                {"value": as_text(example.get("Value")), "help": str(example.get("Help") or "")}
                for example in option.get("Examples") or []
            ],
            "required": bool(option.get("Required")),
            "password": bool(option.get("IsPassword")),
            "exclusive": bool(option.get("Exclusive")),
        }


@dataclass
class Session:  # pylint: disable=too-many-instance-attributes
    """One person's wizard: its own configuration file, the questions answered and the current one."""

    user: str
    kind: str
    binary: str
    run: Callable[..., subprocess.CompletedProcess]
    id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    used: float = field(default_factory=time.monotonic)
    history: list[Step] = field(default_factory=list)
    current: Step | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.dir = tempfile.mkdtemp(prefix="harvester-rclone-wizard-")
        self.path = os.path.join(self.dir, "rclone.conf")
        self._write("")

    def _write(self, text: str) -> None:
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)

    def _read(self) -> str:
        try:
            with open(self.path, encoding="utf-8") as file:
                return file.read()
        except OSError:
            return ""

    def _rclone(self, *arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        command = [
            self.binary,
            f"--config={self.path}",
            f"--cache-dir={os.path.join(self.dir, 'cache')}",
            "--ask-password=false",
            *arguments,
        ]
        try:
            result = self.run(
                command, input=stdin, capture_output=True, text=True, timeout=TIMEOUT, check=False, env=child_env()
            )
        except subprocess.TimeoutExpired as error:
            raise WizardError(f"rclone did not answer within {TIMEOUT} seconds", 504) from error
        if result.returncode != 0:
            raise WizardError(f"rclone: {last_line(result.stderr)}")
        return result

    def _ask(self, *arguments: str) -> dict:
        """rclone's next question (or the end) after a configuration command."""
        result = self._rclone("config", *arguments, "--non-interactive", "--all")
        try:
            data = json.loads(result.stdout)
        except ValueError as error:
            raise WizardError(f"rclone answered unreadably: {last_line(result.stdout)}") from error
        if not isinstance(data, dict):
            raise WizardError("rclone answered unreadably")
        return data

    def _continue(self, state: str, answer: str) -> dict:
        # One argument each, "--flag=value": an answer that starts with a dash is a value too.
        return self._ask("update", NAME, "--continue", f"--state={state}", f"--result={answer}")

    def start(self) -> dict:
        """Creates the remote and asks rclone's first question."""
        return self._advance(self._ask("create", NAME, self.kind))

    def _advance(self, data: dict) -> dict:
        """Answers what is not asked (_automatic), up to the next question for the person or the end."""
        for _ in range(MAX_STEPS):
            state = str(data.get("State") or "")
            if not state:
                self.current = None
                return self.finished()
            option = data.get("Option") if isinstance(data.get("Option"), dict) else {}
            step = Step(state, option or {}, str(data.get("Error") or ""), self._read())
            automatic = self._automatic(step)
            if automatic is None:
                self.current = step
                return self.view()
            data = self._continue(state, automatic)
        raise WizardError("rclone keeps asking: start again")

    def _automatic(self, step: Step) -> str | None:
        """The answer to a question the person is not asked, or None."""
        if step.name in AUTOMATIC:
            return AUTOMATIC[step.name]
        if FORBIDDEN_KEY.search(step.name):
            return ""
        # rclone's --all asks the options of every provider of an S3 or Koofr remote: those of
        # another provider than the one chosen keep their defaults.
        provider = next((before.answer for before in reversed(self.history) if before.name == "provider"), "")
        if not provider_matches(str(step.option.get("Provider") or ""), provider):
            return step.question()["default"]
        return None

    def answer(self, state: str, result: str) -> dict:
        """Answers the current question (state: the one the page shows) and asks the next."""
        step = self.current
        if step is None:
            raise WizardError("the configuration is finished", 409)
        if state != step.state:
            raise WizardError("this question was answered already: reload the wizard", 409)
        if FORBIDDEN_KEY.search(step.name) or step.name in AUTOMATIC:
            raise WizardError(f"{step.name} may not be set here (a file or a command of the app)")
        question = step.question()
        if step.name == "config_token":
            result = pasted_token(result)
        if re.search(r"[\r\n\0]", result):
            raise WizardError("the answer spans several lines: rclone's settings are one line each")
        if question["required"] and not result:
            raise WizardError(f"{step.name} is required")
        if question["type"] == "bool" and result not in ("true", "false"):
            raise WizardError(f"{step.name} is true or false")
        if question["exclusive"] and result not in [example["value"] for example in question["examples"]]:
            raise WizardError(f"{step.name} is one of the values offered")
        sent = self._obscure(result) if question["password"] and result else result
        data = self._continue(step.state, sent)
        # A password is not kept to be shown again when the person goes back.
        step.answer = "" if question["password"] else result
        self.history.append(step)
        return self._advance(data)

    def _obscure(self, password: str) -> str:
        # rclone keeps passwords obscured, and reads them so; its non-interactive answers are
        # stored as given (1.75), so the wizard obscures them as `rclone config` would. Through
        # stdin: rclone's command line is visible to the container's other processes.
        return self._rclone("obscure", "-", stdin=password + "\n").stdout.strip()

    def back(self) -> dict:
        """The question before (or the last, from the end), with the configuration as it was."""
        if not self.history:
            raise WizardError("this is the first question", 409)
        step = self.history.pop()
        self._write(step.before)
        self.current = step
        return self.view()

    def finished(self) -> dict:
        """The end: the configuration, if the remote source accepts it, or why not."""
        text = self._read().strip() + "\n"
        view: dict = {"session": self.id, "done": True, "first": not self.history}
        try:
            name, settings = parse_config(text)
            if name != NAME or settings.get("type") != self.kind:
                raise SourceError("rclone wrote another remote than the one asked for")
        except SourceError as error:
            return view | {"error": f"The remote source would refuse this configuration: {error}"}
        return view | {"config": text}

    def view(self) -> dict:
        """The current question, for the page."""
        if self.current is None:
            return self.finished()
        step = self.current
        question = step.question()
        return {
            "session": self.id,
            "done": False,
            "first": not self.history,
            "state": step.state,
            "question": question,
            "value": step.answer or question["default"],
            "error": step.error,
            "authorize": authorize_command(question["help"]) if step.name == "config_token" else "",
            "type": self.kind,
        }

    def close(self) -> None:
        """Removes the wizard's configuration."""
        shutil.rmtree(self.dir, ignore_errors=True)


class Wizards:
    """The wizards open now, each its user's alone, ended after IDLE seconds without a step."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.run = run
        self.clock = clock

    def _expire(self) -> None:
        now = self.clock()
        with self.lock:
            # One busy with a step is not idle.
            gone = [s for s in self.sessions.values() if now - s.used > IDLE and not s.lock.locked()]
            for session in gone:
                del self.sessions[session.id]
        for session in gone:
            session.close()

    def start(self, user: str, kind: str) -> dict:
        """A new wizard of the user's for a remote of this type, at its first question."""
        if kind not in ALLOWED_TYPES:
            raise WizardError(f"remotes of type {kind or '(none)'} are not allowed")
        self._expire()
        binary = rclone_binary()
        with self.lock:
            mine = sorted((s for s in self.sessions.values() if s.user == user), key=lambda s: s.used)
            ended = mine[: max(0, len(mine) - PER_USER + 1)]
            for session in ended:
                del self.sessions[session.id]
            if len(self.sessions) >= TOTAL:
                raise WizardError("too many rclone wizards are open: try again in a few minutes", 429)
            session = Session(user, kind, binary, self.run, used=self.clock())
            self.sessions[session.id] = session
        for old in ended:
            old.close()
        try:
            return self._step(session, session.start)
        except WizardError:
            self.cancel(user, session.id)
            raise

    def _own(self, user: str, session_id: str) -> Session:
        self._expire()
        with self.lock:
            session = self.sessions.get(session_id)
        # Another user's wizard is no different from one that does not exist.
        if session is None or session.user != user:
            raise WizardError("no such rclone wizard (it ends after a few idle minutes): start again", 404)
        return session

    def _step(self, session: Session, action: Callable[[], dict]) -> dict:
        if not session.lock.acquire(blocking=False):  # pylint: disable=consider-using-with
            raise WizardError("the wizard is busy with the previous answer", 409)
        try:
            return action()
        finally:
            session.used = self.clock()
            session.lock.release()

    def answer(self, user: str, session_id: str, state: str, result: str) -> dict:
        """The user's answer to their wizard's question, and the next question or the end."""
        session = self._own(user, session_id)
        return self._step(session, lambda: session.answer(state, result))

    def back(self, user: str, session_id: str) -> dict:
        """The user's wizard one question back."""
        session = self._own(user, session_id)
        return self._step(session, session.back)

    def cancel(self, user: str, session_id: str) -> None:
        """Ends the user's wizard and removes its configuration."""
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None or session.user != user:
                return
            del self.sessions[session_id]
        session.close()

    def close(self) -> None:
        """Ends every wizard (the app stops)."""
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close()
