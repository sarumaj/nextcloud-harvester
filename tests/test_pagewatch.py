# SPDX-License-Identifier: AGPL-3.0-or-later
"""Watching a page for changes of its text (harvester.pagewatch, the recipe's "watch"): a run saves
no file; its first keeps the text, a later one that finds it changed (not only its whitespace)
logs and notifies an excerpt of the change; a selector narrows the text, and one that finds
nothing is a changed site (as a listing's that finds nothing). The site is httpx's
MockTransport, the run the executor's."""

# pylint: disable=missing-function-docstring

import json

import httpx
import pytest
from test_runner import FakeNextcloud

from harvester import executor as executor_module
from harvester import pagewatch
from harvester import recipe as recipes
from harvester.sources.site import Site
from harvester.store import Job, Store

PAGE = """<html><head><style>p {{ color: red }}</style><script>var now = {now}</script></head>
<body><h1>Prices</h1><main><p class="price">Tea: {tea}</p><p>Cake: 3 EUR</p></main>
<footer>Visited {now} times</footer></body></html>"""


class Watched:
    """The page the runs read (the test changes it between them), and the job's run."""

    def __init__(self, tmp_path, monkeypatch, watch: dict):
        self.page = PAGE.format(now=1, tea="2 EUR")

        def client(**_options) -> httpx.Client:
            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, headers={"content-type": "text/html"}, text=self.page)

            return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

        monkeypatch.setattr(executor_module, "guarded_client", client)
        self.store = Store(str(tmp_path / "db"))
        recipe = {"start": "https://shop.example/prices", "watch": watch}
        self.job = self.store.save(
            Job(
                user="u",
                name="Tea",
                source="site",
                target="/Tea",
                schedule="0 6 * * *",
                options={"recipe": json.dumps(recipe)},
            )
        )
        self.nc = FakeNextcloud()
        self.executor = executor_module.Executor(self.store, lambda: self.nc)

    def run(self) -> Job:
        self.executor.run(self.store.job(self.job.id or 0) or self.job)
        return self.store.job(self.job.id or 0) or self.job

    def log(self) -> list[str]:
        return [line["message"] for line in self.store.log_lines("u", self.job.id)]


def test_a_change_of_the_pages_text_is_logged_and_told(tmp_path, monkeypatch):
    watched = Watched(tmp_path, monkeypatch, {})
    job = watched.run()
    assert (job.last_status, job.last_message) == ("ok", "The page's text is kept to compare with")
    assert "Watching https://shop.example/prices: its text is kept, a change is told from the next run" in watched.log()
    # Its scripts change, its whitespace too: no change.
    watched.page = PAGE.format(now=1, tea="2   EUR").replace("<p>Cake", "\n   <p>Cake").replace("{ color", "{ x")
    watched.page = watched.page.replace("var now = 1", "var now = 7")
    assert watched.run().last_message == "The page is as before"
    assert not watched.nc.sent
    watched.page = PAGE.format(now=2, tea="2.50 EUR")
    assert (
        watched.run().last_message == "The page changed: -Tea: 2 EUR +Tea: 2.50 EUR -Visited 1 times +Visited 2 times"
    )
    change = "-Tea: 2 EUR\n+Tea: 2.50 EUR\n-Visited 1 times\n+Visited 2 times"
    assert f"The page changed: {change}" in watched.log()
    assert watched.nc.sent[0][:2] == ("Harvester: Tea — the page changed", f"https://shop.example/prices: {change}")


def test_a_selector_narrows_the_text_and_one_that_finds_nothing_is_a_changed_site(tmp_path, monkeypatch):
    watched = Watched(tmp_path, monkeypatch, {"selector": "main .price"})
    watched.run()
    watched.page = PAGE.format(now=2, tea="2 EUR")
    assert watched.run().last_message == "The page is as before", "the footer is not watched"
    watched.page = "<html><body><main>Closed</main></body></html>"
    job = watched.run()
    # Found before: the site changed, told once (a first run without it fails).
    assert job.last_status == "changed"
    assert (job.last_message or "").endswith("https://shop.example/prices: the watched main .price is not on the page")
    assert (
        watched.nc.sent[0][0]
        == "Harvester: Tea — the site seems to have changed: the listing found nothing (it found 1 before)"
    )


def test_texts_excerpts_and_recipes():
    assert pagewatch.text_of("<body><div>a <b>b</b>\n\n  c</div><template>x</template></body>") == "a\nb\nc"
    assert pagewatch.text_of("<body><p>a</p></body>", "#none") is None
    assert pagewatch.same("a b\nc", "a\n b  c")
    long = pagewatch.excerpt("old", "new " * 200)
    assert len(long) == pagewatch.EXCERPT and long.endswith("…")
    assert not recipes.check_recipe({"start": "https://shop.example/", "watch": {"selector": "main"}})
    assert recipes.check_recipe({"start": "https://shop.example/", "watch": {"selector": ""}})
    assert recipes.check_recipe({"start": "https://shop.example/", "watch": []})
    both = {"start": "https://shop.example/", "watch": {}, "links": {"selector": "a", "pattern": ""}}
    assert recipes.check_recipe(both) == [
        "the recipe watches its start page (watch) or has levels and file links, not both"
    ]


@pytest.mark.parametrize("html", ["<html></html>", ""])
def test_an_empty_page_has_an_empty_text(html):
    assert pagewatch.text_of(html) == ""


def test_the_recipes_test_reads_the_watched_text_and_keeps_nothing():
    page = PAGE.format(now=1, tea="2 EUR")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, headers={"content-type": "text/html"}, text=page)
        )
    )
    recipe = {"start": "https://shop.example/prices", "watch": {"selector": "main"}}
    site = Site({"recipe": json.dumps(recipe)}, {}, client)
    lines: list = []
    site.report = lambda level, text: lines.append(str(text))
    result = site.survey()
    site.close()
    assert (result["files"], result["problems"], len(result["pages"])) == (0, [], 1)
    assert lines == ["The watched text: 22 characters"]


def test_the_outcomes_excerpt_is_one_short_line():
    assert pagewatch.shortened("-a\n+b") == "-a +b"
    long = pagewatch.shortened("+" + "x" * 300)
    assert len(long) == pagewatch.NOTE_EXCERPT and long.endswith("…")
