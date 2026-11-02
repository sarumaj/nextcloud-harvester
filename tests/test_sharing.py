"""Jobs shared as Nextcloud shares files: who sees and does what (the owner, a sharee with each
permission, a group's member, one taken out of the group, a stranger), shares on within one's own
permissions, the owner's folder, account and browser kept theirs, the sharee search as the
user, the shares table in an earlier database; and the queue's status for everyone, which names
nobody else's jobs or sites."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi import HTTPException

import main
from harvester import browser, sharing
from harvester.executor import Executor
from harvester.store import SCHEMA, Job, Store

# The people's preferences (their accounts for jobs), by user, and their groups in Nextcloud.
PREFERENCES: dict[str, dict[str, str]] = {}
GROUPS: dict[str, list[str]] = {}
# What Nextcloud's sharee search finds (its response's shape, files_sharing's ShareesAPI).
PEOPLE = {"ben": "Ben Example", "carol": "Carol Example", "dave": "Dave Example", "anna": "Anna Example"}
TEAMS = {"family": "Family"}


class Preferences:
    """A user's preferences in Nextcloud (preferences_ex)."""

    def __init__(self, values: dict[str, str]):
        self.values = values

    async def get_value(self, key):
        return self.values.get(key)

    async def set_value(self, key, value, sensitive=False):  # pylint: disable=unused-argument
        self.values[key] = value

    async def delete(self, keys):
        for key in [keys] if isinstance(keys, str) else keys:
            self.values.pop(key, None)


class Nextcloud:
    """AppAPI's client as one user: their preferences, their groups (only their own, as
    Nextcloud answers a person) and the sharee search; the OCS calls asked are kept."""

    def __init__(self, user: str):
        self._user = user
        self.preferences_ex = Preferences(PREFERENCES.setdefault(user, {}))
        self.asked: list[tuple[str, dict | None]] = []
        outer = self

        class Users:  # pylint: disable=too-few-public-methods
            """The user's own profile."""

            async def get_user(self, user_id=""):
                assert not user_id, "asked as the user, for themselves"
                return type("UserInfo", (), {"display_name": PEOPLE.get(outer._user, outer._user)})()

        self.users = Users()

    @property
    async def user(self):
        return self._user

    async def ocs(self, method, path, params=None, **_kwargs):
        assert method == "GET"
        self.asked.append((path, params))
        if path.endswith("/groups"):
            assert path == f"/ocs/v1.php/cloud/users/{self._user}/groups", "a person reads only their own groups"
            return {"groups": GROUPS.get(self._user, [])}
        assert path == "/ocs/v2.php/apps/files_sharing/api/v1/sharees"
        search = params["search"].lower()

        def entry(kind, sharee, label):
            return {"label": label, "value": {"shareType": 0 if kind == "user" else 1, "shareWith": sharee}}

        users = [entry("user", uid, name) for uid, name in PEOPLE.items() if search in (uid + name).lower()]
        groups = [entry("group", gid, name) for gid, name in TEAMS.items() if search in (gid + name).lower()]
        exact = {"users": [u for u in users if u["value"]["shareWith"] == search], "groups": [], "remotes": []}
        return {"exact": exact, "users": [u for u in users if u not in exact["users"]], "groups": groups, "remotes": []}


class FakeExecutor:
    """The executor as the endpoints use it: the runs asked for, nothing running."""

    def __init__(self):
        self.asked: list[tuple[str, int]] = []
        self.queued: list[int] = []
        self.running: list[int] = []

    def request(self, job_id):
        self.asked.append(("run", job_id))

    def pause(self, job_id):
        self.asked.append(("pause", job_id))
        return True

    resume = stop = pause

    def paused(self, _job_id):
        return False

    def waits(self, _jobs):
        return {}

    def status(self):
        return {"runners": {"busy": 0, "total": 2}, "browsers": {"jobs": 0, "recordings": 0, "total": 3}, "queued": 0}

    def retag(self, *_args):
        pass


@pytest.fixture(name="app")
def fixture_app(tmp_path, monkeypatch):
    """Anna's job (a feed with an account), the app's state fresh, and nobody's groups kept."""
    PREFERENCES.clear()
    GROUPS.clear()
    GROUPS.update({"carol": ["family"], "dave": []})
    monkeypatch.setattr(main, "_GROUPS", {})
    monkeypatch.setattr(main, "_NAMES", {})
    store = Store(str(tmp_path / "h.db"))
    monkeypatch.setitem(main.STATE, "store", store)
    monkeypatch.setitem(main.STATE, "executor", FakeExecutor())

    async def as_user(user):
        return Nextcloud(user)

    monkeypatch.setattr(main, "as_user", as_user)
    job = store.save(
        Job(
            user="anna",
            name="Podcast",
            source="feed",
            target="/Anna/Podcast",
            schedule="0 6 * * *",
            options={"url": "https://feed.example/rss"},
        )
    )
    PREFERENCES.setdefault("anna", {})[f"credential.job{job.id}.password"] = "anna's secret"
    return store, job


def run(awaitable):
    return asyncio.run(awaitable)


def status_of(call) -> int:
    """The HTTP status an endpoint's call ends with (200 when it goes through)."""
    try:
        run(call)
    except HTTPException as error:
        return error.status_code
    return 200


def body(job: Job, **changes) -> main.JobIn:
    values = {"name": job.name, "source": job.source, "target": job.target, "options": dict(job.options)}
    return main.JobIn(**(values | changes))


def share(store: Store, job: Job, kind: str, sharee: str, granted: int, by: str = "anna") -> None:
    store.share(job.id or 0, {"kind": kind, "sharee": sharee, "label": sharee, "permissions": granted, "by": by})


def test_who_may_do_what(app):
    store, job = app
    job_id = job.id or 0
    share(store, job, "user", "ben", sharing.VIEW | sharing.RUN)
    share(store, job, "group", "family", sharing.VIEW | sharing.EDIT)
    calls = {
        "view": lambda nc: main.progress_log(nc, job_id),
        "run": lambda nc: main.run_job(job_id, nc),
        "pause": lambda nc: main.pause_job(job_id, nc),
        "stop": lambda nc: main.stop_job(job_id, nc),
        "edit": lambda nc: main.update_job(
            job_id, body(job, name="Renamed", options={"url": "https://feed.example/rss"}), nc
        ),
        "credentials": lambda nc: main.get_credentials(job_id, nc),
        "shares": lambda nc: main.get_shares(job_id, nc),
    }
    expected = {
        # The owner: everything.
        "anna": {name: 200 for name in calls},
        # View and Run: no edits, no account, no shares to manage.
        "ben": {"view": 200, "run": 200, "pause": 200, "stop": 200, "edit": 403, "credentials": 403, "shares": 403},
        # Edit through the group Family: no runs.
        "carol": {"view": 200, "run": 403, "pause": 403, "stop": 403, "edit": 200, "credentials": 403, "shares": 403},
        # Not in Family (any more), nothing shared with her: as if there were no such job.
        "dave": {name: 404 for name in calls},
        "eve": {name: 404 for name in calls},
    }
    for user, outcomes in expected.items():
        got = {name: status_of(call(Nextcloud(user))) for name, call in calls.items()}
        assert got == outcomes, user
    assert status_of(main.get_shot(job_id, Nextcloud("eve"))) == 404
    assert status_of(main.delete_job(job_id, Nextcloud("ben"))) == 403
    assert store.job(job_id) is not None
    # A refusal says what is missing, not more.
    with pytest.raises(HTTPException) as refused:
        run(main.run_job(job_id, Nextcloud("carol")))
    assert "(run)" in refused.value.detail


def test_the_list_shows_shared_jobs_with_their_owner_and_permissions(app):
    store, job = app
    share(store, job, "user", "ben", sharing.VIEW | sharing.RUN)
    mine = store.save(Job(user="ben", name="Bens", source="feed", target="/B", schedule="0 6 * * *"))
    listed = run(main.list_jobs(Nextcloud("ben")))
    assert [item["id"] for item in listed] == [job.id, mine.id]
    shared = listed[0]
    assert shared["owner"] == "anna" and shared["owner_name"] == "Anna Example" and shared["mine"] is False
    assert shared["permissions"] == ["view", "run"]
    assert "shares" not in shared, "the shares are the owner's to see (and a resharer's)"
    assert listed[1]["mine"] and listed[1]["permissions"] == list(sharing.NAMES)
    owners = run(main.list_jobs(Nextcloud("anna")))
    assert [(s["sharee"], s["permissions"]) for s in owners[0]["shares"]] == [("ben", ["view", "run"])]
    assert run(main.list_jobs(Nextcloud("eve"))) == []
    # The progress log: the shared job's lines for Ben, none for a stranger.
    store.log(job.id or 0, "info", "Downloading episode 1")
    assert [line["message"] for line in run(main.progress_log(Nextcloud("ben")))["lines"]] == ["Downloading episode 1"]
    assert run(main.progress_log(Nextcloud("eve")))["lines"] == []


def test_a_member_taken_out_of_the_group_loses_the_job(app, monkeypatch):
    store, job = app
    share(store, job, "group", "family", sharing.VIEW | sharing.RUN)
    carol = Nextcloud("carol")
    assert status_of(main.run_job(job.id or 0, carol)) == 200
    GROUPS["carol"] = []
    # Kept a minute: within it, the old membership still counts; after it, it does not.
    assert status_of(main.run_job(job.id or 0, carol)) == 200
    monkeypatch.setattr(main, "GROUPS_KEPT", 0)
    assert status_of(main.run_job(job.id or 0, carol)) == 404
    assert run(main.list_jobs(carol)) == []


def test_groups_are_asked_for_only_when_a_job_is_shared_with_a_group(app):
    store, job = app
    share(store, job, "user", "ben", sharing.VIEW)
    ben = Nextcloud("ben")
    run(main.job_access(job.id or 0, ben))
    assert not ben.asked, "no group's share: no question to Nextcloud"
    share(store, job, "group", "family", sharing.VIEW)
    run(main.job_access(job.id or 0, ben))
    run(main.job_access(job.id or 0, ben))
    assert [path for path, _ in ben.asked] == ["/ocs/v1.php/cloud/users/ben/groups"], "asked once, then kept"


def test_sharing_on_gives_at_most_ones_own_permissions(app):
    store, job = app
    job_id = job.id or 0
    share(store, job, "user", "ben", sharing.VIEW | sharing.RUN | sharing.RESHARE)
    ben, carol = Nextcloud("ben"), Nextcloud("carol")
    more = main.ShareIn(kind="user", sharee="carol", permissions=["view", "edit"])
    assert status_of(main.put_share(job_id, more, ben)) == 403
    shares = run(main.put_share(job_id, main.ShareIn(kind="user", sharee="carol", permissions=["run"]), ben))
    assert [(s["sharee"], s["permissions"], s["by"], s["label"]) for s in shares] == [
        ("carol", ["view", "run"], "ben", "Carol Example")
    ], "Ben sees the share he made, with Nextcloud's name for Carol"
    assert run(main.job_access(job_id, carol))[1] == sharing.VIEW | sharing.RUN
    assert len(run(main.get_shares(job_id, Nextcloud("anna")))) == 2, "the owner sees every share"
    # Ben may not change or end the owner's shares, nor share with the owner.
    ben_own = main.ShareIn(kind="user", sharee="ben", permissions=["view", "run", "reshare"])
    assert status_of(main.put_share(job_id, ben_own, ben)) == 422
    anna_share = main.ShareIn(kind="user", sharee="anna", permissions=["view"])
    assert status_of(main.put_share(job_id, anna_share, ben)) == 422
    run(main.put_share(job_id, main.ShareIn(kind="user", sharee="dave", permissions=[]), Nextcloud("anna")))
    assert status_of(main.delete_share(job_id, "user", "dave", ben)) == 403
    # The owner takes Run back from Ben: Carol's share from him loses it too; without his
    # share, hers gives nothing.
    share(store, job, "user", "ben", sharing.VIEW | sharing.RESHARE)
    assert run(main.job_access(job_id, carol))[1] == sharing.VIEW
    run(main.delete_share(job_id, "user", "ben", Nextcloud("anna")))
    assert status_of(main.job_access(job_id, carol)) == 404
    # Carol, with no access left: as if there were no such job.
    assert status_of(main.put_share(job_id, main.ShareIn(kind="user", sharee="ben", permissions=[]), carol)) == 404


def test_a_sharee_must_be_one_nextcloud_lets_the_user_share_with(app):
    _, job = app
    anna = Nextcloud("anna")
    nobody = main.ShareIn(kind="user", sharee="mallory", permissions=["view"])
    assert status_of(main.put_share(job.id or 0, nobody, anna)) == 422
    group = run(main.put_share(job.id or 0, main.ShareIn(kind="group", sharee="family", permissions=["run"]), anna))
    assert [(s["kind"], s["label"]) for s in group] == [("group", "Family")]
    path, params = anna.asked[-1]
    assert path == "/ocs/v2.php/apps/files_sharing/api/v1/sharees"
    assert params["itemType"] == "file" and params["shareType[]"] == [0, 1] and params["lookup"] == "false"


def test_the_sharee_search_is_nextclouds_as_the_user(app):
    _, job = app
    found = run(main.list_sharees(Nextcloud("ben"), "example"))
    assert {(item["kind"], item["sharee"]) for item in found} == {("user", "anna"), ("user", "carol"), ("user", "dave")}
    found = run(main.list_sharees(Nextcloud("anna"), "a", job.id))
    assert ("user", "anna") not in {(item["kind"], item["sharee"]) for item in found}, "not oneself"
    assert ("group", "family") in {(item["kind"], item["sharee"]) for item in found}
    ben = run(main.list_sharees(Nextcloud("anna"), "ben"))
    assert ben == [{"kind": "user", "sharee": "ben", "label": "Ben Example"}], "an exact match once"
    assert status_of(main.list_sharees(Nextcloud("eve"), "a", job.id)) == 404


def test_the_folder_and_the_account_stay_the_owners(app):
    store, job = app
    job_id = job.id or 0
    share(store, job, "user", "ben", sharing.VIEW | sharing.EDIT)
    ben = Nextcloud("ben")
    saved = run(main.update_job(job_id, body(job, name="Bens name", target="/Ben/Stolen"), ben))
    assert saved["name"] == "Bens name" and saved["target"] == "/Anna/Podcast", "the folder is the owner's"
    # Anna's account goes with the job: Ben may not send it to another site.
    elsewhere = body(job, options={"url": "https://evil.example/rss"})
    with pytest.raises(HTTPException) as refused:
        run(main.update_job(job_id, elsewhere, ben))
    assert refused.value.status_code == 403 and "evil.example" in refused.value.detail
    assert status_of(main.update_job(job_id, body(job, source="media"), ben)) == 403
    assert (store.job(job_id) or job).options["url"] == "https://feed.example/rss"
    # The account is hers to see and set; Ben's preferences hold nothing of it.
    assert status_of(main.set_credentials(job_id, {"password": "ben's"}, ben)) == 403
    assert PREFERENCES["anna"][f"credential.job{job_id}.password"] == "anna's secret" and not PREFERENCES["ben"]
    assert run(main.get_credentials(job_id, Nextcloud("anna")))["password"] is True
    # Without an account, another site is his to choose.
    PREFERENCES["anna"].clear()
    assert status_of(main.update_job(job_id, elsewhere, ben)) == 200


def test_a_website_jobs_sign_in_steps_stay_the_owners(app):
    store, _ = app
    recipe = {"steps": [{"action": "fill", "selector": "#p", "credential": "password"}], "start": "https://s.example/"}
    job = store.save(
        Job(
            user="anna",
            name="Site",
            source="site",
            target="/x",
            schedule="0 6 * * *",
            options={"recipe": json.dumps(recipe)},
        )
    )
    PREFERENCES["anna"][f"credential.job{job.id}.password"] = "secret"
    share(store, job, "user", "ben", sharing.VIEW | sharing.EDIT)
    moved = main.account_moves(job, "site", {"recipe": json.dumps(recipe | {"steps": []})})
    assert moved == "the recipe's steps"
    assert main.account_moves(job, "site", {"recipe": json.dumps(recipe), "path": "{year}/{text}"}) == ""
    assert main.account_moves(job, "site", {"recipe": json.dumps(recipe | {"start": "https://s.example/b"})}) == ""


def test_deleting_a_shared_job_removes_the_owners_account_and_the_shares(app):
    store, job = app
    job_id = job.id or 0
    share(store, job, "user", "ben", sharing.VIEW | sharing.DELETE)
    PREFERENCES["anna"][f"session.job{job_id}"] = "{}"
    run(main.delete_job(job_id, Nextcloud("ben")))
    assert store.job(job_id) is None and store.shares(job_id) == []
    assert not PREFERENCES["anna"], "the owner's account and session went with the job"


def test_only_the_owner_opens_the_jobs_browser(app, monkeypatch):
    store, job = app
    share(store, job, "user", "ben", sharing.ALL)

    class Socket:  # pylint: disable=too-few-public-methods
        """The recorder's WebSocket."""

        query_params = {"job": str(job.id)}
        headers: dict = {}
        sent: list = []

        async def send_json(self, message):
            self.sent.append(message)

        async def close(self, code=1000):
            pass

    monkeypatch.setattr(browser, "SLOTS", browser.Slots(1))
    allowed, _ = run(main.admit(Socket(), Nextcloud("ben")))
    assert not allowed and "Only the job's owner" in Socket.sent[-1]["message"]
    allowed, opened = run(main.admit(Socket(), Nextcloud("eve")))
    assert not allowed and opened is None and Socket.sent[-1]["message"] == "No such job."
    allowed, opened = run(main.admit(Socket(), Nextcloud("anna")))
    assert allowed and opened and opened.id == job.id
    # The one browser is Anna's now: the next is refused with what holds them, not whose.
    allowed, _ = run(main.admit(Socket(), Nextcloud("anna")))
    assert Socket.sent[-1]["message"] == (
        "No browser is free: all 1 in use (0 by jobs, 1 by recordings). Try again in a few minutes."
    )
    browser.SLOTS.release()


def test_an_earlier_database_gets_the_shares_table(tmp_path):
    path = str(tmp_path / "h.db")
    earlier = SCHEMA[: SCHEMA.index("-- Who else may see")].replace(
        "id INTEGER PRIMARY KEY AUTOINCREMENT,\n    user", "id INTEGER PRIMARY KEY,\n    user"
    )
    with sqlite3.connect(path) as db:
        db.executescript(earlier)
        db.execute(
            "INSERT INTO jobs (id, user, name, source, options, target, tags, schedule, enabled, created)"
            " VALUES (3, 'anna', 'a', 'feed', '{}', '/x', '[]', '0 6 * * *', 1, 0)"
        )
    store = Store(path)
    job = store.job(3)
    assert job is not None
    share(store, job, "user", "ben", sharing.VIEW)
    assert [j.id for j in store.shared_with("ben", set())] == [3]
    assert Store(path).shares(3)[0]["sharee"] == "ben", "opened again: the same table"
    store.delete(3)
    assert store.shares(3) == [], "the shares go with their job"


# The queue's status: everyone's load, nobody's names or sites.


def test_the_status_and_the_reasons_name_nothing_of_others(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "SLOTS", browser.Slots(2))
    store = Store(str(tmp_path / "db"))
    executor = Executor(store, runners=1)

    def feed(user, name, url):
        return store.save(
            Job(user=user, name=name, source="feed", target="/x", schedule="0 6 * * *", options={"url": url})
        )

    theirs = feed("eve", "Eves secret", "https://shared.example/rss")
    queued_first = feed("eve", "Eves other", "https://private.example/rss")
    same_site = feed("anna", "Mine", "https://shared.example/other")
    behind = feed("anna", "Mine too", "https://news.example/rss")
    assert executor._claim(theirs)
    for job in (queued_first, same_site, behind):
        executor.request(job.id or 0)
    assert browser.SLOTS.acquire(blocking=False, kind="job") and browser.SLOTS.acquire(blocking=False)
    status = executor.status()
    assert status == {
        "runners": {"busy": 1, "total": 1},
        "browsers": {"jobs": 1, "recordings": 1, "total": 2},
        "queued": 3,
    }
    reasons = executor.waits([same_site, behind])
    assert reasons == {
        same_site.id: "2. in the queue: waits for its site (shared.example), used by another job",
        behind.id: "3. in the queue: waits for a runner, all 1 busy (2 jobs ahead)",
    }
    words = json.dumps([status, reasons])
    assert "private.example" not in words and "eve" not in words.lower() and "Eves" not in words
    browser.SLOTS.release("job")
    browser.SLOTS.release()


def test_a_running_job_that_waits_for_a_browser_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "SLOTS", browser.Slots(1))
    store = Store(str(tmp_path / "db"))
    executor = Executor(store, runners=2)
    job = store.save(Job(user="anna", name="Site", source="site", target="/x", schedule="0 6 * * *"))
    assert executor._claim(job)
    assert browser.SLOTS.acquire(blocking=False), "a recording holds the one browser"
    waiting = threading.Thread(target=browser.SLOTS.acquire, kwargs={"timeout": 5, "kind": "job"})
    waiting.start()
    executor.controls[job.id or 0].thread = waiting.ident or 0
    time.sleep(0.1)
    assert executor.waits([job]) == {job.id: "Waits for a browser: all 1 in use (0 by jobs, 1 by recordings)"}
    browser.SLOTS.release()
    waiting.join(2)
    assert not executor.waits([job]), "it has its browser"
    assert browser.SLOTS.taken() == {"job": 1, "recording": 0}
    browser.SLOTS.release("job")


def test_the_api_gives_the_status_and_the_reasons_of_the_users_jobs(app, monkeypatch):
    store, job = app
    executor = Executor(store, runners=1)
    monkeypatch.setitem(main.STATE, "executor", executor)
    other = store.save(Job(user="eve", name="E", source="feed", target="/x", schedule="0 6 * * *"))
    assert executor._claim(other)
    executor.request(job.id or 0)
    share(store, job, "user", "ben", sharing.VIEW)
    for user in ("anna", "ben"):
        listed = run(main.list_jobs(Nextcloud(user)))
        assert listed[0]["waiting"] == "1. in the queue: waits for a runner, all 1 busy (0 jobs ahead)", user
        log = run(main.progress_log(Nextcloud(user)))
        assert log["queued"] == [job.id] and log["waiting"] == {str(job.id): listed[0]["waiting"]}
        assert log["status"]["runners"] == {"busy": 1, "total": 1} and log["status"]["queued"] == 1
    assert main.queue_status() == executor.status()
    stranger = run(main.progress_log(Nextcloud("carol")))
    assert stranger["queued"] == [] and stranger["waiting"] == {} and stranger["status"]["queued"] == 1
