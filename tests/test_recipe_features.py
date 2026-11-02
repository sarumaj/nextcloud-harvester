"""The recipe's numbered pages, its step "scroll to load more", the step's label in the log, and
what the job's browser saw at a failed step (kept per job, read only by its owner). Over HTTP
and in the store, no browser: test_recorder_features.py has the browser's side."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import base64
import json

import pytest
from fastapi import HTTPException
from test_runner import FakeNextcloud, FakeSource
from test_sources import source

from fake_site import SITE, Site
from harvester import executor as executor_module
from harvester import pagination
from harvester.recipe import check_recipe, step_label, steps_from_events, uses_browser
from harvester.sources import SourceError
from harvester.store import Job, Store

NUMBERED = {
    "start": f"{SITE}/pages",
    "links": {"selector": "a.file", "pattern": r"\.pdf$"},
    "next": {"kind": "number", "selector": "nav.pager", "level": 0},
}


@pytest.mark.parametrize(
    "html, url, current",
    [
        ('<nav><a href="?page=1">1</a><a href="?page=2" aria-current="page">2</a><a href="?page=3">3</a></nav>', "", 2),
        (
            '<ul><li><a href="/p/1">1</a></li><li class="active"><a href="/p/2">2</a></li>'
            '<li><a href="/p/3">3</a></li></ul>',
            "",
            2,
        ),
        ('<div><a href="?p=1">1</a><span>2</span><a href="?p=3">3</a><span>…</span><a href="?p=9">9</a></div>', "", 2),
        (
            '<div><a href="?page=1">1</a><a href="?page=2">2</a><a href="?page=3">3</a></div>',
            "https://x.example/l?page=2",
            2,
        ),
        (
            '<div><a href="/l/page/1">1</a><a href="/l/page/2">2</a><a href="/l/page/3">3</a></div>',
            "https://x.example/l/page/3",
            3,
        ),
        ('<div><a href="?a=1">1</a><a href="?a=2">2</a><a href="?a=3">3</a></div>', "https://x.example/l", 1),
        ("<div><button disabled>1</button><button>2</button></div>", "", 1),
    ],
)
def test_the_current_page_is_the_marked_number_the_plain_one_or_the_addresss(html, url, current):
    assert pagination.current_page(pagination.numbers(html), url, 1) == current


def test_the_next_number_is_a_link_to_follow_or_a_button_to_click():
    links = (
        '<nav><a href="?page=1">‹</a><a href="?page=1">1</a><a class="current" href="?page=2">2</a>'
        '<a href="?page=3">3</a></nav>'
    )
    assert pagination.following(links, "", 1) == (3, "?page=3")
    buttons = '<div><button class="is-active">1</button><button>2</button><button>3</button></div>'
    assert pagination.following(buttons, "", 1) == (2, "")
    assert pagination.following('<div><a href="?p=1">1</a><span>2</span></div>', "", 1) is None, "the last page"
    # Without a mark, the walk's own count says which page this is.
    plain = '<div><a href="#">1</a><a href="#">2</a><a href="#">3</a></div>'
    assert pagination.following(plain, "", 2) == (3, "")
    assert pagination.following("", "", 1) is None


def test_numbered_pages_are_walked_over_http_up_to_the_pages_limit():
    items = list(source(Site(), NUMBERED, stop_at_seen=0).items(lambda key: False))
    assert [item.path for item in items] == [f"p{page}-{n}.pdf" for page in (1, 2, 3, 4) for n in (1, 2)]
    limited = list(source(Site(), NUMBERED, stop_at_seen=0, max_pages=2).items(lambda key: False))
    assert len(limited) == 4


def test_numbered_pages_stop_at_known_files():
    seen = {f"{SITE}/files/p2-1.pdf", f"{SITE}/files/p2-2.pdf"}
    items = list(
        source(Site(), NUMBERED, stop_at_seen=1).items(lambda key: key in seen or key.startswith("harvester:"))
    )
    assert [item.path for item in items] == ["p1-1.pdf", "p1-2.pdf"], "page 2 is known: the listing ends there"


def test_old_recipes_run_and_check_as_before():
    old = {
        "start": f"{SITE}/list",
        "links": {"selector": "a.file", "pattern": r"\.pdf$"},
        "next": {"selector": "a.next", "level": 0},
    }
    assert check_recipe(old) == [] and not uses_browser(old)
    items = list(source(Site(), old, stop_at_seen=0).items(lambda key: False))
    assert len(items) == 6
    # A next without its element was always none.
    assert check_recipe(old | {"next": {}}) == [] and check_recipe(old | {"next": {"selector": ""}}) == []


def test_the_recipes_new_parts_are_checked():
    recipe = {"steps": [{"action": "goto", "url": f"{SITE}/feed"}, {"action": "scroll"}], "links": {"selector": "a"}}
    assert check_recipe(recipe) == []
    assert check_recipe(
        recipe | {"steps": [{"action": "goto", "url": f"{SITE}/x"}, {"action": "scroll", "rounds": 0}]}
    ) == ["step 2: rounds of scrolling from 1 to 100"]
    assert check_recipe(recipe | {"next": {"kind": "pages", "selector": "nav"}}) == ["next page: unknown kind pages"]
    assert uses_browser({"next": {"kind": "number", "selector": "nav", "buttons": True}})
    assert not uses_browser({"next": {"kind": "number", "selector": "nav"}})
    assert steps_from_events([{"action": "scroll"}]) == [{"action": "scroll"}]


def test_a_steps_label_names_its_text_or_its_element():
    assert step_label({"action": "click", "selector": "a.overlay-b:nth-of-type(6)", "text": ""}) == (
        "click a.overlay-b:nth-of-type(6)"
    )
    assert step_label({"action": "click", "selector": "a", "text": "Sign in"}) == "click “Sign in”"
    assert step_label({"action": "fill", "selector": "#u", "credential": "username"}) == "fill in username (#u)"
    assert step_label({"action": "scroll"}) == "scroll to load more"


# What the job's browser saw: kept per job, the newest only, gone with the job.

SHOT = {
    "image": b"\xff\xd8jpeg",
    "url": f"{SITE}/login",
    "title": "Login",
    "what": "step 2 (click #go) failed",
    "at": 1.0,
}


def test_the_newest_shot_is_kept_per_job_and_goes_with_it(tmp_path):
    store = Store(str(tmp_path / "db"))
    job = store.save(Job(user="u", name="n", source="site", target="/x", schedule="* * * * *"))
    first = store.log(job.id or 0, "error", "step 2 (click #go) failed: timeout")
    store.keep_shot(job.id or 0, first, SHOT)
    second = store.log(job.id or 0, "error", "again")
    store.keep_shot(job.id or 0, second, SHOT | {"url": f"{SITE}/other"})
    assert store.shot(job.id or 0)["url"] == f"{SITE}/other", "replaced by the next failure"
    lines = store.log_lines("u", job.id)
    assert [bool(line["shot"]) for line in lines] == [False, True], "only the newest line links to it"
    store.delete(job.id or 0)
    assert store.shot(job.id or 0) is None


def test_only_the_jobs_owner_reads_its_shot(tmp_path, monkeypatch):
    import main  # pylint: disable=import-outside-toplevel

    store = Store(str(tmp_path / "db"))
    monkeypatch.setitem(main.STATE, "store", store)
    job = store.save(Job(user="u", name="n", source="site", target="/x", schedule="* * * * *"))
    owner, other = FakeNextcloud(), FakeNextcloud()
    owner._user, other._user = "u", "someone else"
    with pytest.raises(HTTPException) as nothing:
        asyncio.run(main.get_shot(job.id or 0, owner))
    assert nothing.value.status_code == 404
    store.keep_shot(job.id or 0, store.log(job.id or 0, "error", "x"), SHOT)
    shot = asyncio.run(main.get_shot(job.id or 0, owner))
    assert base64.b64decode(shot["image"]) == SHOT["image"] and shot["title"] == "Login"
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.get_shot(job.id or 0, other))
    assert refused.value.status_code == 404


def test_a_failed_run_keeps_what_its_browser_saw(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "db"))
    saved = store.save(Job(user="u", name="n", source="shooting", target="/x", schedule="* * * * *"))

    class Shooting(FakeSource):
        """Fails at a step, with the page it was on."""

        name = "shooting"
        shot = SHOT

        def items(self, seen):
            raise SourceError("step 2 (click #go) failed: timeout")

    monkeypatch.setitem(executor_module.SOURCES, "shooting", Shooting)
    executor_module.Executor(store, FakeNextcloud).run(saved)
    lines = store.log_lines("u", saved.id)
    assert lines[-1]["message"].startswith("step 2 (click #go) failed") and lines[-1]["shot"]
    assert store.shot(saved.id or 0)["image"] == SHOT["image"]
    assert json.loads(json.dumps(lines))  # the log stays JSON for the page


class LongNames(Site):  # pylint: disable=too-few-public-methods
    """A listing whose file's name backtracks a careless pattern without end."""

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        if url.endswith("/long"):
            link = f'<a class="file" href="/files/{"a" * 60}b">x</a>'
            return 200, {"content-type": "text/html"}, f"<html><body>{link}</body></html>".encode()
        return super().answer(url, method, cookie)


def test_a_recipes_pattern_that_takes_too_long_fails_the_run():
    slow = {"start": f"{SITE}/long", "links": {"selector": "a.file", "pattern": r"(a|aa)+$"}}
    with pytest.raises(SourceError, match="takes too long"):
        list(source(LongNames(), slow, stop_at_seen=0).items(lambda key: False))


def test_a_step_cut_short_by_a_stop_is_no_failure(monkeypatch):
    from harvester.sources import Stopped  # pylint: disable=import-outside-toplevel
    from harvester.sources import site as site_module  # pylint: disable=import-outside-toplevel

    def replay(page, step, secrets, repaired=None):  # pylint: disable=unused-argument
        raise Stopped("stopped")

    monkeypatch.setattr(site_module, "replay", replay)
    driver = object.__new__(site_module.BrowserDriver)
    driver.main = object()
    shots: list[str] = []
    driver.keep_shot = lambda page, what: shots.append(what)
    with pytest.raises(Stopped):
        driver.sign_in([{"action": "scroll"}], {}, None, "")
    assert not shots, "no picture of a failure: there was none"
