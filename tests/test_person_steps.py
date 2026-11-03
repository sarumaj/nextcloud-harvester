# SPDX-License-Identifier: AGPL-3.0-or-later
"""Steps that need the job's person while its run waits (harvester.needs, sources.attend): a code
from the phone (ask-code), asked on the jobs page and in a notification, typed into its field in
Chromium (route-served pages, no network); its wait's end, a stop, no desk to ask on (a weekly
test); the desk that takes a code only for a job that asks for one, the API's endpoint for the
job's person only; the recorder's Steps list's "code from the phone?"; the checks of a recipe."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import threading

import pytest
from fastapi import HTTPException
from test_browser import FREE, needs_chromium
from test_on_demand import Nextcloud, status_of
from test_runner import FakeNextcloud, FakeSource

import main
from harvester import needs
from harvester import executor as executor_module
from harvester.browser import trial
from harvester.browser.recorder import Recorder
from harvester.browser.steps import StepList
from harvester.executor import Executor
from harvester.recipe import check_recipe, sign_in_steps, step_label
from harvester.sources import ChallengeError, Stopped, attend
from harvester.sources import site as site_module
from harvester.sources.base import watch_stop
from harvester.store import Job, Store

GOTO = {"action": "goto", "url": "https://bank.example/login"}
STEP = {"action": "ask-code", "selector": "#sms", "label": "the bank's SMS"}
LINKS = {"selector": "a.none", "pattern": "pdf"}
CODE_FORM = b"""<html><body><input id="sms"><button id="go"
  onclick="fetch('/done/' + document.getElementById('sms').value)">Go</button></body></html>"""


def test_the_step_is_checked_and_labelled():
    assert not check_recipe({"steps": [GOTO, STEP | {"wait": 1800}], "links": LINKS})
    assert check_recipe({"steps": [GOTO, STEP | {"label": ""}], "links": LINKS}) == [
        "step 2: the label (what you are asked about, like “SMS code”) is text of 1 to 100 characters"
    ]
    assert check_recipe({"steps": [GOTO, STEP | {"wait": 10}], "links": LINKS}) == [
        "step 2: the wait for you from 30 to 1800 seconds"
    ]
    assert check_recipe({"steps": [GOTO, {"action": "ask-code", "label": "SMS"}], "links": LINKS}) == [
        "step 2: no element"
    ]
    assert step_label(STEP) == "type the code from the bank's SMS (#sms)"
    # The code is a sign-in's: a kept session skips it, and the click that sends it.
    steps = [GOTO, {"action": "fill", "selector": "#pw", "credential": "password"}, STEP]
    assert sign_in_steps([*steps, {"action": "click", "selector": "#go"}, {"action": "click", "selector": "a"}]) == 4


def test_the_desk_takes_a_code_only_while_its_job_asks():
    desk = needs.Desk()
    assert not desk.answer(1, "123456"), "nothing asked"
    asked = desk.open(1, "code", "SMS", 600)
    assert desk.asking(1) == {"kind": "code", "label": "SMS", "until": asked.until}
    assert desk.waiting() == [1]
    assert not desk.answer(2, "123456"), "another job asks nothing"
    assert not desk.answer(1, ""), "no code"
    assert desk.answer(1, "123456")
    assert "123456" not in repr(asked) and "123456" not in json.dumps(desk.asking(1))
    assert asked.take() == "123456" and asked.take() is None, "taken once"
    desk.answer(1, "999999")
    desk.close(1, asked)
    assert asked.code is None and desk.asking(1) is None and not desk.answer(1, "1")


def test_the_teller_logs_and_notifies():
    lines: list[tuple[str, str]] = []
    sent: list[tuple] = []
    tell = needs.teller("Bank", lambda level, text: lines.append((level, text)), lambda *args: sent.append(args), "nc")
    tell(needs.Asked("code", "the bank's SMS", 600, 0))
    assert lines == [("info", "Waiting up to 10 min for you: enter the code from the bank's SMS")]
    assert sent == [
        (
            "nc",
            "Harvester: {job} needs you",
            {"job": "Bank", "label": "the bank's SMS", "minutes": "10"},
            "Enter the code from {label} on Harvester's page within {minutes} minutes: the job waits for it.",
        )
    ]


class Page:  # pylint: disable=too-few-public-methods
    """A page whose waits are noted (the step's polls)."""

    url = "https://bank.example/login"

    def __init__(self):
        self.waits = 0

    def wait_for_timeout(self, _ms: float) -> None:
        self.waits += 1


def asking(desk: needs.Desk, act) -> list[needs.Asked]:
    """Serves the desk for job 7 in this thread; act(asked) runs as the person is told."""
    told: list[needs.Asked] = []

    def tell(asked: needs.Asked) -> None:
        told.append(asked)
        act(asked)

    needs.serve(desk, 7, tell)
    return told


def test_the_wait_ends_at_its_time_without_the_code_or_its_retries():
    desk = needs.Desk()

    def late(asked: needs.Asked) -> None:
        asked.until = 0

    told = asking(desk, late)
    try:
        with pytest.raises(attend.Unanswered, match="nobody entered the code from the bank's SMS within 10 min"):
            attend.ask(Page(), STEP)
    finally:
        needs.serve(None)
    assert len(told) == 1 and desk.waiting() == [], "asked once, and no more"


def test_a_stop_ends_the_wait_at_once():
    desk = needs.Desk()
    stop = threading.Event()
    asking(desk, lambda asked: stop.set())
    watch_stop(stop)
    try:
        with pytest.raises(Stopped):
            attend.ask(Page(), STEP)
    finally:
        watch_stop(None)
        needs.serve(None)
    assert desk.waiting() == []


def test_without_a_desk_the_page_asks_for_a_person():
    needs.serve(None)
    with pytest.raises(ChallengeError) as caught:
        attend.ask(Page(), STEP)
    assert caught.value.url == "https://bank.example/login" and caught.value.kind == "the bank's SMS"


def test_the_code_typed_on_the_page_goes_to_the_step():
    desk = needs.Desk()
    page = Page()
    asking(desk, lambda asked: threading.Timer(0.05, desk.answer, (7, "482913")).start())
    try:
        assert attend.ask(page, STEP) == "482913"
    finally:
        needs.serve(None)
    assert desk.waiting() == []


@needs_chromium
def test_a_job_asks_for_the_code_and_types_it_in_the_browser(monkeypatch):
    monkeypatch.setattr(needs, "POLL", 0.1)
    requests: list[str] = []

    def serve_bank(route) -> None:
        requests.append(route.request.url)
        login = route.request.url.endswith("/login")
        route.fulfill(status=200, headers={"content-type": "text/html"}, body=CODE_FORM if login else b"done")

    monkeypatch.setattr(site_module, "guard", serve_bank)
    desk = needs.Desk()
    told = asking(desk, lambda asked: threading.Timer(0.3, desk.answer, (7, "482913")).start())
    try:
        driver = site_module.BrowserDriver([GOTO, STEP, {"action": "click", "selector": "#go"}], {})
        driver.close()
    finally:
        needs.serve(None)
    assert "https://bank.example/done/482913" in requests
    assert [asked.label for asked in told] == ["the bank's SMS"]
    assert site_module.browser.free_browsers() == FREE


class Asking(FakeSource):
    """A source whose sign-in asks its person for a code, and lists what it was given."""

    name = "asking-code"
    given: list[str] = []

    def items(self, seen):
        served = needs.serving()
        assert served is not None, "the run serves the executor's desk"
        desk, job_id, _ = served
        threading.Timer(0.2, desk.answer, (job_id, "135790")).start()
        Asking.given.append(attend.ask(Page(), STEP))
        yield from ()


def test_a_run_tells_its_person_and_takes_the_code(tmp_path, monkeypatch):
    monkeypatch.setitem(executor_module.SOURCES, "asking-code", Asking)
    monkeypatch.setattr(needs, "POLL", 0.05)
    store = Store(str(tmp_path / "db"))
    job = store.save(Job(user="u", name="Bank", source="asking-code", target="/x", schedule="* * * * *"))
    nc = FakeNextcloud()
    Executor(store, lambda: nc).run(job)
    assert Asking.given == ["135790"]
    assert nc.sent[0][:2] == (
        "Harvester: Bank needs you",
        "Enter the code from the bank's SMS on Harvester's page within 10 minutes: the job waits for it.",
    )
    lines = [line["message"] for line in store.log_lines("u")]
    assert "Waiting up to 10 min for you: enter the code from the bank's SMS" in lines
    assert not any("135790" in line for line in lines), "the code is never logged"
    assert needs.serving() is None, "the thread serves no desk after the run"


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "h.db"))
    executor = Executor(store, FakeNextcloud, runners=1)
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", executor)
    return executor, store.save(Job(user="anna", name="Bank", source="site", target="/x", schedule="0 6 * * *"))


def test_the_code_goes_only_to_the_persons_own_job_while_it_asks(app):
    executor, job = app
    job_id = job.id or 0

    def enter(code: str, user: str = "anna"):
        return main.enter_code(job_id, main.CodeIn(code=code), Nextcloud(user))

    assert status_of(enter("482913")) == 409, "not waiting"
    asked = executor.desk.open(job_id, "code", "SMS", 600)
    listed = asyncio.run(main.list_jobs(Nextcloud("anna")))[0]
    assert listed["asking"] == {"kind": "code", "label": "SMS", "until": asked.until}
    assert asyncio.run(main.progress_log(Nextcloud("anna")))["asking"] == [job_id]
    assert asyncio.run(main.progress_log(Nextcloud("ben")))["asking"] == [], "only one's own jobs"
    assert status_of(enter("482913", user="ben")) == 404, "Anna's job"
    assert status_of(enter("  ")) == 422
    assert status_of(enter("48\n2913")) == 422
    assert status_of(enter(" 482913 ")) == 200
    assert asked.take() == "482913"
    with pytest.raises(HTTPException):
        asyncio.run(enter("482913", user="ben"))


def test_the_recorders_filled_code_comes_from_the_phone_and_goes_back():
    track = StepList()
    track.load([{"action": "fill", "selector": "#sms", "alternatives": ["input[name=sms]"], "credential": "totp"}])
    assert track.edit("ask-code", {"step": 0, "label": " the bank's SMS "})
    assert track.all() == [
        {"action": "ask-code", "selector": "#sms", "alternatives": ["input[name=sms]"], "label": "the bank's SMS"}
    ]
    assert not check_recipe({"steps": track.all(), "start": "https://bank.example/x", "links": LINKS})
    track.edit("ask-code", {"step": 0, "off": True})
    assert track.all()[0] == {
        "action": "fill",
        "selector": "#sms",
        "alternatives": ["input[name=sms]"],
        "credential": "totp",
    }
    assert track.edit("ask-code", {"step": 0})
    assert track.all()[0]["label"] == "SMS code", "a label of its own when none is given"
    assert not track.edit("ask-code", {"step": 3})


def test_the_recorders_test_asks_on_its_page():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    told: list[dict] = []

    async def send(message: dict) -> None:
        told.append(message)

    try:
        trial.asker(send, loop)(needs.Asked("code", "the bank's SMS", 600, 0))
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert told == [{"type": "test-asking", "kind": "code", "label": "the bank's SMS", "seconds": 600}]
    recorder = Recorder(send)
    asked = recorder._desk.open(0, "code", "the bank's SMS", 600)
    asyncio.run(recorder._trial_message("test-code", {"code": " 135790 "}))
    assert asked.take() == "135790"
