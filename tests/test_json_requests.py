# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSON listings beyond a GET (harvester.jsonrequest): POST requests with a form's or a JSON body,
GraphQL with its variables, pages counted in the body or chained by a cursor from the answer, and
the headers the page sent, a token read when the job runs from a cookie, a <meta> or the page's
storage. The pure parts, the walk over HTTP (httpx's MockTransport) and in Chromium (routes), the
recorder's Network panel, and the checks. No network."""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import json
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from test_browser import FREE, Driver, needs_chromium
from test_page_values import walked

from harvester import jsonrequest
from harvester.browser.recorder import Recorder
from harvester.recipe import check_recipe, uses_browser
from harvester.sources import SourceError, near
from harvester.sources import download
from harvester.sources import site as site_module

SHOP = "https://shop.example"
PDF = b"%PDF-1.7\n" + b"x" * 2000
QUERY = "query Invoices($after: String, $first: Int) { invoices(after: $after, first: $first) { nodes { title pdf } } }"
# Five invoices, two a page.
INVOICES = [{"title": f"Invoice {n}", "pdf": f"/files/{n}.pdf"} for n in range(1, 6)]
# The single-page app: its token in a <meta> and a cookie, its listing asked for with GraphQL.
APP = (
    """<html><head><meta name="csrf-token" content="meta-token-1"></head><body><ul id="list"></ul><script>
fetch('/graphql', {method: 'POST', headers: {'content-type': 'application/json', 'x-csrf-token': 'meta-token-1',
  'x-requested-with': 'XMLHttpRequest', 'authorization': 'Bearer secret-in-memory'},
  body: JSON.stringify({operationName: 'Invoices', query: '"""
    + QUERY
    + """', variables: {after: null, first: 2}})})
  .then(r => r.json()).then(answer => {
    for (const item of answer.data.invoices.nodes) {
      const li = document.createElement('li'); li.textContent = item.title; document.getElementById('list').append(li)
    }
  })</script></body></html>"""
)


class Shop:  # pylint: disable=too-few-public-methods
    """The shop's pages, its listings (a GraphQL one with a cursor, a form's with a page field, a
    REST one whose answer names the next page's address) and its files; the requests it got."""

    def __init__(self) -> None:
        self.asked: list[dict] = []

    def answer(  # pylint: disable=too-many-locals,too-many-return-statements
        self, url: str, method: str, body: bytes, headers: dict
    ) -> tuple[int, dict, bytes]:
        parts = urlsplit(url)
        self.asked.append({"url": url, "method": method, "body": body, "headers": headers})
        json_type = {"content-type": "application/json"}
        if parts.path == "/app":
            return 200, {"content-type": "text/html", "set-cookie": "XSRF-TOKEN=cookie%3Dtoken; Path=/"}, APP.encode()
        if parts.path == "/graphql" and method == "POST":
            if headers.get("x-csrf-token") != "meta-token-1":
                return 403, json_type, b'{"errors": ["csrf"]}'
            variables = json.loads(body)["variables"]
            start = int(variables["after"] or 0)
            nodes = INVOICES[start : start + variables["first"]]
            more = start + len(nodes) < len(INVOICES)
            info = {"endCursor": str(start + len(nodes)) if nodes else None, "hasNextPage": more}
            return 200, json_type, json.dumps({"data": {"invoices": {"nodes": nodes, "pageInfo": info}}}).encode()
        if parts.path == "/search" and method == "POST":
            if headers.get("x-xsrf-token") != "cookie=token":
                return 403, json_type, b"{}"
            form = dict(parse_qsl(body.decode()))
            page = int(form["page"])
            rows = INVOICES[(page - 1) * 2 : page * 2] if form.get("year") == "2026" else []
            return 200, json_type, json.dumps({"rows": rows}).encode()
        if parts.path == "/rest":
            page = int(dict(parse_qsl(parts.query)).get("page", "1"))
            following = f"{SHOP}/rest?page={page + 1}" if page < 3 else None
            return 200, json_type, json.dumps({"results": INVOICES[page - 1 : page], "next": following}).encode()
        if parts.path.startswith("/files/"):
            return 200, {"content-type": "application/pdf"}, PDF
        return 404, {"content-type": "text/plain"}, b"not found"


def transport(shop: Shop) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        status, headers, body = shop.answer(str(request.url), request.method, request.content, dict(request.headers))
        return httpx.Response(status, headers=headers, content=body)

    return httpx.MockTransport(handle)


GRAPHQL = {
    "kind": "json",
    "url": f"{SHOP}/graphql",
    "method": "POST",
    "content_type": "application/json",
    "body": {"operationName": "Invoices", "query": QUERY, "variables": {"after": None, "first": 2}},
    "headers": {"x-requested-with": "XMLHttpRequest", "x-csrf-token": {"from": "meta", "name": "csrf-token"}},
    "cursor": {"from": "data.invoices.pageInfo.endCursor", "to": "variables.after", "in": "body"},
    "items": "data.invoices.nodes",
    "file": "pdf",
    "text": "title",
}
FORM = {
    "kind": "json",
    "url": f"{SHOP}/search",
    "method": "POST",
    "body": "year={year}&page=1",
    "headers": {"x-xsrf-token": {"from": "cookie", "name": "XSRF-TOKEN"}},
    "page": {"param": "page", "first": 1, "step": 1, "in": "body"},
    "items": "rows",
    "file": "pdf",
    "text": "title",
}


def http_site(shop: Shop, recipe: dict, cookies: dict | None = None, **options) -> site_module.Site:
    client = httpx.Client(transport=transport(shop), follow_redirects=True, cookies=cookies)
    return site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0, **options}, {}, client)


# The pure parts.


def test_a_graphql_request_is_proposed_with_its_cursor_and_its_secrets_left_out():
    answer = {"data": {"invoices": {"nodes": INVOICES[:2], "pageInfo": {"endCursor": "2", "hasNextPage": True}}}}
    body = {"operationName": "Invoices", "query": QUERY, "variables": {"after": "2", "first": 2}}
    sent = {"method": "POST", "body": json.dumps(body), "content_type": "application/json; charset=utf-8"}
    proposed = jsonrequest.propose(f"{SHOP}/graphql", answer, sent, 2)
    assert proposed == {
        "method": "POST",
        "content_type": "application/json",
        # The cursor of a later page goes back to none: the walk starts at the first.
        "body": body | {"variables": {"after": None, "first": 2}},
        "cursor": {
            "from": "data.invoices.pageInfo.endCursor",
            "more": "data.invoices.pageInfo.hasNextPage",
            "to": "variables.after",
            "in": "body",
        },
    }
    assert jsonrequest.short_body(sent) == "GraphQL Invoices"
    headers, dropped = jsonrequest.header_plan(
        {"x-csrf-token": "t1", "x-requested-with": "XMLHttpRequest", "authorization": "Bearer b1", "user-agent": "x"},
        {"cookie": [("XSRF-TOKEN", "t0")], "meta": [("csrf-token", "t1")], "storage": []},
    )
    assert headers == {"x-csrf-token": {"from": "meta", "name": "csrf-token"}, "x-requested-with": "XMLHttpRequest"}
    assert dropped == ["authorization"], "a token found nowhere is left out, never written down"
    stored, _ = jsonrequest.header_plan({"authorization": "Bearer b1"}, {"storage": [("auth", "b1")]})
    assert stored == {"authorization": {"from": "storage", "name": "auth", "prefix": "Bearer "}}


def test_pages_in_a_body_and_cursors_in_an_address_are_proposed():
    form = jsonrequest.propose(f"{SHOP}/search", {"rows": []}, {"method": "POST", "body": "q=x&page=2"}, 2)
    assert form["body"] == "q=x&page=2" and form["page"] == {"param": "page", "first": 2, "step": 1, "in": "body"}
    offset = {"method": "POST", "body": json.dumps({"filter": {"skip": 20, "first": 20}})}
    assert jsonrequest.propose(f"{SHOP}/api", {"rows": []}, offset, 20)["page"] == {
        "param": "filter.skip",
        "first": 20,
        "step": 20,
        "in": "body",
    }, "GraphQL's first is a count, not the offset"
    later = jsonrequest.propose(f"{SHOP}/api?cursor=c2&q=1", {"items": [], "nextCursor": "c3"}, {"method": "GET"}, 1)
    assert later == {"cursor": {"from": "nextCursor", "to": "cursor"}, "url": f"{SHOP}/api?q=1"}
    rest = jsonrequest.propose(f"{SHOP}/rest", {"results": [], "next": f"{SHOP}/rest?page=2"}, {"method": "GET"}, 1)
    assert rest == {"cursor": {"from": "next"}}


def test_a_body_takes_the_page_the_cursor_and_the_walks_values():
    assert json.loads(jsonrequest.body(GRAPHQL, {}, 3, "4") or b"")["variables"] == {"after": "4", "first": 2}
    assert jsonrequest.body(FORM, {"year": 2026}, 2, None) == b"year=2026&page=2"
    numbered = {"method": "POST", "body": {"year": "{year}", "q": "{not a value}"}, "page": FORM["page"]}
    assert json.loads(jsonrequest.body(numbered, {"year": 2026}, 2, None) or b"") == {
        "year": 2026,
        "q": "{not a value}",
        "page": 2,
    }, "a placeholder alone takes the value with its type; other braces stay"
    assert jsonrequest.body({"url": "x"}, {}, 1, None) is None
    chained = {"cursor": {"from": "next"}}
    assert jsonrequest.address(f"{SHOP}/rest", chained, 2, "/rest?page=2") == f"{SHOP}/rest?page=2"
    query = {"cursor": {"from": "nextCursor", "to": "cursor"}}
    assert jsonrequest.address(f"{SHOP}/api?q=1", query, 2, "c3") == f"{SHOP}/api?q=1&cursor=c3"
    assert jsonrequest.next_cursor({"a": {"next": "c", "more": False}}, {"from": "a.next", "more": "a.more"}) is None


def test_the_recipe_checks_the_request_and_keeps_secrets_out():
    def problems(**changes) -> list[str]:
        return check_recipe({"start": f"{SHOP}/app", "links": GRAPHQL | changes})

    assert not problems() and not check_recipe({"start": f"{SHOP}/app", "links": FORM})
    assert problems(headers={"X-CSRF-Token": "abc"}) == [
        "file links: the header X-CSRF-Token is a secret that changes: read it from a cookie, a <meta> or the "
        "page's storage"
    ]
    assert problems(headers={"Cookie": "a=b"}) == ["file links: the header Cookie may not be set"]
    assert problems(headers={"x-a": {"from": "file", "name": "x"}}) == [
        'file links: the header x-a is text or {"from": cookie, meta or storage, "name": ...}'
    ]
    assert problems(method="PUT") == ["file links: the JSON request's method is GET or POST"]
    assert problems(method="GET") == ["file links: only a POST has a body"]
    assert problems(page={"param": "page"}) == [
        "file links: the JSON's pages are counted (page) or chained (cursor), not both"
    ]
    assert problems(cursor={"from": ""}) == ["file links: the cursor's from is a field's path"]
    assert problems(body={"q": "x" * 70_000}) == ["file links: the JSON request's body is at most 64 KB"]
    assert not uses_browser({"start": f"{SHOP}/app", "links": GRAPHQL}), "a <meta> is read over HTTP too"
    stored = GRAPHQL | {"headers": {"authorization": {"from": "storage", "name": "auth", "prefix": "Bearer "}}}
    assert uses_browser({"start": f"{SHOP}/app", "links": stored})


# The walk.


def test_a_graphql_listing_is_walked_by_its_cursor_with_the_token_of_the_page_over_http():
    shop = Shop()
    items = walked(http_site(shop, {"start": f"{SHOP}/app", "links": GRAPHQL}, max_pages=10))
    assert [item.values["text"] for item in items] == [f"Invoice {n}" for n in range(1, 6)]
    posts = [asked for asked in shop.asked if asked["url"].endswith("/graphql")]
    assert [json.loads(asked["body"])["variables"]["after"] for asked in posts] == [None, "2", "4", "5"]
    first = posts[0]["headers"]
    assert first["x-csrf-token"] == "meta-token-1" and first["x-requested-with"] == "XMLHttpRequest"
    assert first["content-type"] == "application/json" and first["origin"] == SHOP
    assert first["referer"] == f"{SHOP}/app"
    assert [asked["url"] for asked in shop.asked].count(f"{SHOP}/app") == 1, "the <meta> read once"


def test_a_forms_listing_counts_its_pages_in_the_body_with_the_cookies_token():
    shop = Shop()
    recipe = {
        "start": f"{SHOP}/years",
        "follow": [],
        "links": FORM | {"body": "year=2026&page=1"},
    }
    items = walked(http_site(shop, recipe, cookies={"XSRF-TOKEN": "cookie=token"}, max_pages=5))
    assert len(items) == 5
    bodies = [asked["body"] for asked in shop.asked if asked["url"].endswith("/search")]
    assert bodies == [f"year=2026&page={page}".encode() for page in (1, 2, 3, 4)], "ends at the empty page"
    with pytest.raises(SourceError, match="header x-xsrf-token: no cookie XSRF-TOKEN"):
        walked(http_site(Shop(), recipe))


def test_an_answers_next_address_chains_the_pages_and_ends_with_none():
    shop = Shop()
    recipe = {
        "start": f"{SHOP}/app",
        "links": {"kind": "json", "url": f"{SHOP}/rest", "items": "results", "file": "pdf", "cursor": {"from": "next"}},
    }
    assert len(walked(http_site(shop, recipe, max_pages=10))) == 3
    assert [asked["url"] for asked in shop.asked] == [f"{SHOP}/rest"] + [f"{SHOP}/rest?page={n}" for n in (2, 3)]


def test_a_next_address_to_a_private_one_is_refused():
    class Driver_:  # pylint: disable=too-few-public-methods,invalid-name
        """A driver whose answer names a private address as its next page."""

        def here(self) -> str:
            return ""

        def read_json(self, url: str, request: dict) -> dict:
            assert request["method"] == "GET"
            return {"items": [], "next": "http://127.0.0.1:8080/admin"} if url.startswith(SHOP) else {}

    spec = {"url": f"{SHOP}/rest", "items": "items", "file": "f", "cursor": {"from": "next"}}
    pages = near.json_pages(spec, {"max_pages": 5}, Driver_(), {})
    next(pages)
    with pytest.raises(SourceError, match="may not be opened"):
        next(pages)


@needs_chromium
def test_a_graphql_listing_is_asked_for_with_the_browsers_cookies_and_tokens(monkeypatch):
    shop = Shop()

    def fulfill(route) -> None:
        request = route.request
        status, headers, body = shop.answer(request.url, request.method, request.post_data_buffer or b"", {})
        route.fulfill(status=status, headers=headers, body=body)

    real = download.guarded_client
    monkeypatch.setattr(site_module, "guard", fulfill)
    monkeypatch.setattr(
        site_module.download, "guarded_client", lambda **options: real(transport=transport(shop), **options)
    )
    links = GRAPHQL | {
        "headers": GRAPHQL["headers"] | {"x-xsrf-token": {"from": "cookie", "name": "XSRF-TOKEN"}},
    }
    recipe = {"steps": [{"action": "goto", "url": f"{SHOP}/app"}], "links": links}
    source = site_module.Site({"recipe": json.dumps(recipe), "stop_at_seen": 0}, {}, None)
    assert len(walked(source)) == 5
    posts = [asked for asked in shop.asked if asked["url"].endswith("/graphql") and asked["headers"]]
    assert posts and all(asked["headers"]["x-csrf-token"] == "meta-token-1" for asked in posts)
    assert all("XSRF-TOKEN=cookie%3Dtoken" in asked["headers"]["cookie"] for asked in posts), "the browser's cookies"
    assert all(asked["headers"]["x-xsrf-token"] == "cookie%3Dtoken" for asked in posts)
    assert site_module.browser.free_browsers() == FREE


# The recorder.


@needs_chromium
def test_the_network_panel_lists_a_graphql_post_and_makes_it_the_listing():
    shop = Shop()

    async def scenario():
        driver = Driver()
        recorder = Recorder(driver.send)
        driver.recorder = recorder

        async def fulfill(route) -> None:
            request = route.request
            headers = await request.all_headers()
            status, answer_headers, body = shop.answer(
                request.url, request.method, request.post_data_buffer or b"", headers
            )
            await route.fulfill(status=status, headers=answer_headers, body=body)

        recorder._guard = fulfill  # type: ignore[method-assign]
        await recorder.start()
        try:
            await recorder.handle({"type": "goto", "url": f"{SHOP}/app"})
            panels: list = []
            for _ in range(100):
                panels = [message["items"] for message in driver.sent if message["type"] == "requests"]
                if panels:
                    break
                await asyncio.sleep(0.05)
            listed = panels[-1]
            await recorder.handle({"type": "json-pick", "id": listed[0]["id"]})
            chosen = driver.last()
            before = len(driver.sent)
            await recorder.handle({"type": "json-edit", "changes": {"body": "{not json"}})
            refused = [message for message in driver.sent[before:] if message["type"] == "error"]
            await recorder.handle(
                {
                    "type": "json-edit",
                    "changes": {"cursor": None, "page": {"param": "variables.first", "first": 2, "in": "body"}},
                }
            )
            edited = driver.last()
            return listed, chosen, refused, edited
        finally:
            await recorder.close()

    listed, chosen, refused, edited = asyncio.run(scenario())
    assert listed[0]["method"] == "POST" and listed[0]["summary"] == "GraphQL Invoices"
    assert set(listed[0]) == {"id", "url", "size", "status", "method", "summary"}, "no body nor header to the page"
    links = chosen["recipe"]["links"]
    assert links["method"] == "POST" and links["body"]["variables"] == {"after": None, "first": 2}
    assert links["cursor"]["from"] == "data.invoices.pageInfo.endCursor" and links["cursor"]["to"] == "variables.after"
    assert links["headers"] == {
        "x-csrf-token": {"from": "meta", "name": "csrf-token"},
        "x-requested-with": "XMLHttpRequest",
    }
    assert "secret-in-memory" not in json.dumps(chosen), "the bearer token stays out of the recipe and the page"
    proposal = chosen["proposals"]["links"]
    assert proposal["dropped"] == ["authorization"] and proposal["found"] == 2
    assert proposal["following"] == "variables.after = 2"
    assert not check_recipe(chosen["recipe"])
    assert refused and refused[0]["message"].startswith("The body is no JSON")
    page = edited["recipe"]["links"]
    assert "cursor" not in page and page["page"] == {"param": "variables.first", "first": 2, "step": 1, "in": "body"}
    assert edited["proposals"]["links"]["following"] == "variables.first = 3"
