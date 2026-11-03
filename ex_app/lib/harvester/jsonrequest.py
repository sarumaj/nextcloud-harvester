# SPDX-License-Identifier: AGPL-3.0-or-later
"""The request of a JSON listing (harvester.jsonlist) beyond a plain GET: a POST with a body (a
form's fields, JSON, a GraphQL query and its variables), the headers it needs, and pages counted
in its body or chained by a cursor the answer gives.

    "links": {"kind": "json", "url": "https://shop.example/graphql", "method": "POST",
              "content_type": "application/json",
              "body": {"query": "query($after: String) { ... }", "variables": {"after": null}},
              "headers": {"x-requested-with": "XMLHttpRequest",
                          "x-csrf-token": {"from": "meta", "name": "csrf-token"}},
              "cursor": {"from": "data.invoices.pageInfo.endCursor", "to": "variables.after",
                         "more": "data.invoices.pageInfo.hasNextPage"},
              "items": "data.invoices.nodes", "file": "pdf", "text": "title"}

body is JSON (an object or a list) or a form's text (a=1&b=2). A string in it that is a single
value's placeholder ("{year}", "{choice}") takes the walk's value, with its type (a number stays
one); other braces are the body's own (a GraphQL query has many).

headers: a name with its text, for what does not change (x-requested-with, Apollo's), or with
where a run reads it: {"from": "cookie" | "meta" | "storage", "name": ..., "prefix": "Bearer "}:
a cookie of the job (the browser's, or the HTTP client's), a <meta name> of the page the listing
is read from, or a key of that page's localStorage (the browser only). A token that changes (a
CSRF token, a bearer token) is never kept as text: what the recipe holds is where to find it,
which stays right as the token changes, and a recipe shared or exported carries no secret.

page counts pages as for a GET ({"param", "first", "step"}), with "in": "body" in the body: the
param a field's path in a JSON body (variables.page) or a form's field. cursor chains them: the
next cursor is read from the answer (from: a path) and sent as the next request's parameter or
variable (to: a query parameter, or with "in": "body" a path in the body), while "more" (a path to
a true or false, optional) is true; without to, the value read is the next page's address (an
answer's "next": "https://.../items?page=2"). A cursor seen before, or none, ends the listing.
"""

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from . import jsonlist

METHODS = ("GET", "POST")
HEADER_SOURCES = ("cookie", "meta", "storage")
PLACES = ("query", "body")
# A body at most (its text, characters), the headers at most, a literal header's text at most.
BODY_MAX = 64 * 1024
HEADERS_MAX = 10
HEADER_TEXT_MAX = 1000
HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
# Headers a recipe never sets: the client's own (the cookies are the job's, the length the
# body's), and those of the connection.
FORBIDDEN = frozenset(
    {"cookie", "host", "content-length", "content-type", "connection", "transfer-encoding", "te", "upgrade"}
    | {"keep-alive", "trailer", "expect", "proxy-authorization", "proxy-connection", "origin", "referer"}
)
# A header whose value is a secret that changes: read at run time, never written in the recipe.
SECRET = re.compile(r"csrf|xsrf|auth|token|secret|session|api-?key|signature|bearer", re.I)
# The headers a page sent that the recorder keeps as they are: they tell the site the request is
# a script's (some answer JSON only then) or name a GraphQL client.
LITERAL = (
    "x-requested-with",
    "accept",
    "apollo-require-preflight",
    "x-apollo-operation-name",
    "apollographql-client-name",
    "apollographql-client-version",
)
# The names a cursor goes by in a request (the next page's), and in an answer, best first.
CURSOR_PARAMS = ("after", "cursor", "nextCursor", "next_cursor", "pageToken", "page_token", "continuation")
ANSWER_CURSORS = (
    "endCursor",
    "nextCursor",
    "next_cursor",
    "nextPageToken",
    "next_page_token",
    "continuation",
    "cursor",
)
# The request's name for an answer's cursor, when the request had none yet (its first page).
CURSOR_TO = {
    "endCursor": "after",
    "nextCursor": "cursor",
    "next_cursor": "cursor",
    "nextPageToken": "pageToken",
    "next_page_token": "page_token",
    "continuation": "continuation",
    "cursor": "cursor",
}
MORE_FIELDS = ("hasNextPage", "has_next_page", "hasMore", "has_more", "more")
PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


def is_form(spec: dict) -> bool:
    """Whether the body is a form's text (a=1&b=2) rather than JSON."""
    return isinstance(spec.get("body"), str)


def content_type(spec: dict) -> str:
    """The body's type: the recorded one, else JSON's or a form's."""
    given = str(spec.get("content_type") or "")
    return given or ("application/x-www-form-urlencoded" if is_form(spec) else "application/json")


def set_path(data: Any, path: str, value: Any) -> Any:
    """data with the value at the path (jsonlist's dotted form), the objects on the way made where
    missing; a copy, data unchanged."""
    data = json.loads(json.dumps(data))
    steps = jsonlist.steps(path)
    if not steps:
        return value
    current = data
    for index, step in enumerate(steps):
        last = index == len(steps) - 1
        if isinstance(step, int) and isinstance(current, list) and -len(current) <= step < len(current):
            if last:
                current[step] = value
            else:
                current = current[step]
        elif isinstance(step, str) and step != "*" and isinstance(current, dict):
            if last:
                current[step] = value
            else:
                current = current.setdefault(step, {})
        else:
            raise ValueError(f"{path} leads to no field of the body")
    return data


def _filled(data: Any, values: dict) -> Any:
    """The JSON body with each string that is a single placeholder of a value given the value."""
    if isinstance(data, dict):
        return {key: _filled(value, values) for key, value in data.items()}
    if isinstance(data, list):
        return [_filled(value, values) for value in data]
    if isinstance(data, str) and (found := PLACEHOLDER.match(data)) and found.group(1) in values:
        return values[found.group(1)]
    return data


def _page_value(page: dict, number: int) -> int:
    return int(page.get("first", 1)) + (number - 1) * int(page.get("step", 1))


def _in_body(part: dict | None, spec: dict) -> bool:
    """Whether a page or cursor goes into the body (else into the address's query)."""
    return bool(part) and spec.get("method") == "POST" and (part or {}).get("in") == "body"


def body(spec: dict, values: dict, number: int, cursor: str | None) -> bytes | None:
    """The body of the listing's page number (1: the first), with the walk's values, the page's
    number or offset, and the cursor (None on the first page: as recorded); None for a GET."""
    if spec.get("method") != "POST":
        return None
    page, chain = spec.get("page"), spec.get("cursor")
    if is_form(spec):
        fields = [
            (key, str(values.get(m.group(1), value)) if (m := PLACEHOLDER.match(value)) else value)
            for key, value in parse_qsl(spec["body"], keep_blank_values=True)
        ]
        changes = {}
        if _in_body(page, spec):
            changes[str(page["param"])] = str(_page_value(page, number))  # type: ignore[index]
        if _in_body(chain, spec) and cursor is not None and (chain or {}).get("to"):
            changes[str(chain["to"])] = cursor  # type: ignore[index]
        fields = [(key, value) for key, value in fields if key not in changes] + list(changes.items())
        return urlencode(fields).encode()
    data = _filled(spec.get("body"), values)
    if _in_body(page, spec):
        data = set_path(data, str(page["param"]), _page_value(page, number))  # type: ignore[index]
    if _in_body(chain, spec) and cursor is not None and (chain or {}).get("to"):
        data = set_path(data, str(chain["to"]), cursor)  # type: ignore[index]
    return json.dumps(data, ensure_ascii=False).encode()


def address(url: str, spec: dict, number: int, cursor: str | None) -> str:
    """The address of the listing's page number: its page parameter or cursor in the query (one
    in the body leaves it as it is); a cursor without "to" is the next page's address itself."""
    page, chain = spec.get("page"), spec.get("cursor")
    if page and not _in_body(page, spec):
        return jsonlist.page_url(url, page, number)
    if not chain or cursor is None or _in_body(chain, spec):
        return url
    if not chain.get("to"):
        return urljoin(url, cursor)
    parts = urlsplit(url)
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key != chain["to"]]
    return urlunsplit(parts._replace(query=urlencode(query + [(str(chain["to"]), cursor)])))


def next_cursor(data: Any, chain: dict) -> str | None:
    """The cursor of the next page in an answer (chain: the listing's "cursor"), None when there is
    none: no value, an empty one, or "more" false."""
    more = str(chain.get("more") or "")
    if more and jsonlist.field(data, more) not in ("true", "1"):
        return None
    found = jsonlist.field(data, str(chain.get("from") or ""))
    return found or None


def paged(spec: dict) -> bool:
    """Whether the listing has pages after its first: a page parameter, or a cursor."""
    return bool((spec.get("page") or {}).get("param") or (spec.get("cursor") or {}).get("from"))


def _fields(data: Any, path: str = "", depth: int = 0) -> list[tuple[str, Any, Any]]:
    """Every field of a JSON value with its path, its value and the object holding it."""
    found: list[tuple[str, Any, Any]] = []
    if depth > 6:
        return found
    if isinstance(data, dict):
        for key, value in data.items():
            name = key if re.fullmatch(r"[A-Za-z_][\w-]*", key) else f'["{key}"]'
            joined = f"{path}.{name}" if path and not name.startswith("[") else f"{path}{name}"
            found.append((joined, value, data))
            found += _fields(value, joined, depth + 1)
    elif isinstance(data, list) and data:
        found += _fields(data[0], f"{path}[0]", depth + 1)
    return found


def _last(path: str) -> str:
    return re.split(r"[.\[]", path)[-1].strip('"]')


def _counter(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def body_page(data: Any, count: int) -> dict | None:
    """The page counter of a JSON body (a GraphQL query's variables, say): a page number, else an
    offset of count items per page."""
    fields = [(path, value) for path, value, _ in _fields(data) if _counter(value)]
    # GraphQL's "first" is how many items a page holds, not an offset.
    offsets = tuple(name for name in jsonlist.OFFSET_PARAMS if name != "first")
    for names, step in ((jsonlist.PAGE_PARAMS, 1), (offsets, count)):
        for path, value in fields:
            if _last(path) in names and step:
                return {"param": path, "first": value, "step": step, "in": "body"}
    return None


def answer_cursor(data: Any) -> tuple[str, str, str]:
    """The answer's next cursor: its path, its name and the path of the "more" beside it ("" for
    none); ("", "", "") without one. A "next" that is an address is one too (its name "next")."""
    fields = _fields(data)
    ranked = sorted(
        (ANSWER_CURSORS.index(_last(path)), path, holder)
        for path, value, holder in fields
        if _last(path) in ANSWER_CURSORS and (value is None or isinstance(value, str))
    )
    if ranked:
        _, path, holder = ranked[0]
        more = next((name for name in MORE_FIELDS if isinstance(holder.get(name), bool)), "")
        parent = path.rsplit(".", 1)[0] if "." in path else ""
        return path, _last(path), (f"{parent}.{more}" if parent else more) if more else ""
    for path, value, _ in fields:
        if _last(path) == "next" and isinstance(value, str) and re.match(r"(https?://|/|\?)", value):
            return path, "next", ""
    return "", "", ""


def _posted(request: dict) -> dict:
    """A recorded POST's method, type and body (JSON parsed, else its text); {} for a GET."""
    if request.get("method") != "POST":
        return {}
    text = str(request.get("body") or "")
    kind = str(request.get("content_type") or "").split(";", maxsplit=1)[0].strip().lower()
    try:
        data: Any = json.loads(text) if text.strip() else {}
    except ValueError:
        data = text
    return {"method": "POST", "content_type": kind or "application/json", "body": data}


def _chained(url: str, found: dict, chain: dict, name: str) -> dict:
    """The cursor's place in the request: a variable of a JSON body (the one it had, set back to
    none for the first page; else beside GraphQL's variables), else the address's parameter (the
    one it had, dropped from the first page's address)."""
    data = found.get("body")
    if isinstance(data, (dict, list)):
        given = [path for path, value, _ in _fields(data) if _last(path) in CURSOR_PARAMS and not _counter(value)]
        target = given[0] if given else _cursor_variable(data, CURSOR_TO[name])
        return found | {"body": set_path(data, target, None), "cursor": chain | {"to": target, "in": "body"}}
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    target = next((param for param in CURSOR_PARAMS if param in dict(query)), CURSOR_TO[name])
    rest = urlencode([(key, value) for key, value in query if key != target])
    return found | {"cursor": chain | {"to": target}, "url": urlunsplit(parts._replace(query=rest))}


def propose(url: str, data: Any, request: dict, count: int) -> dict:
    """What a recorded request adds to the JSON listing jsonlist.propose made of its answer: the
    method, body and type of a POST; a cursor the answer gives for the next page (its request's
    parameter or variable, set back to none for the first page), else a page counter in the body;
    the url without a cursor of a later page. The headers are the recorder's (header_plan)."""
    found = _posted(request)
    path, name, more = answer_cursor(data)
    if path and name == "next":
        return found | {"cursor": {"from": path}}
    if path:
        return _chained(url, found, {"from": path} | ({"more": more} if more else {}), name)
    body_ = found.get("body")
    if isinstance(body_, (dict, list)):
        page = body_page(body_, count)
    else:
        # A form's fields are counted as an address's parameters are.
        page = jsonlist.detect_page(f"?{body_}", count) if found else None
        page = page | {"in": "body"} if page else None
    return found | ({"page": page} if page else {})


def _cursor_variable(data: Any, name: str) -> str:
    """Where a cursor not in the body yet goes: beside a GraphQL query's variables, else at the top."""
    return f"variables.{name}" if isinstance(data, dict) and isinstance(data.get("variables"), dict) else name


def header_plan(sent: dict[str, str], sources: dict[str, list[tuple[str, str]]]) -> tuple[dict, list[str]]:
    """The recipe's headers from those a page sent (sent: lower-case names and values): the
    LITERAL ones as they are; a secret (SECRET) by where its value is found (sources: kind ->
    [(name, value)], cookies, metas and the storage's entries; a "Bearer " before it kept as the
    prefix), else left out. Also the names left out that way."""
    headers: dict = {}
    dropped: list[str] = []
    for name, value in sent.items():
        if name in LITERAL and not SECRET.search(name):
            # Any type (*/*) is what every client asks for already.
            if value.strip() != "*/*":
                headers[name] = value
            continue
        if not SECRET.search(name) or name in FORBIDDEN:
            continue
        prefix = re.match(r"(?i)(bearer|token)\s+", value)
        bare = value[prefix.end() :] if prefix else value
        where = next(
            (
                {"from": kind, "name": key}
                for kind in HEADER_SOURCES
                for key, found in sources.get(kind, [])
                if found == bare
            ),
            None,
        )
        if where is None:
            dropped.append(name)
            continue
        headers[name] = where | ({"prefix": prefix.group(0)} if prefix else {})
    return headers, dropped


def short_body(request: dict) -> str:
    """A request's body in a few words for the Network panel: a GraphQL operation's name, else
    its first characters on one line."""
    text = str(request.get("body") or "")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("operationName"), str) and data["operationName"]:
        return f"GraphQL {data['operationName']}"
    if isinstance(data, dict) and isinstance(data.get("query"), str):
        found = re.search(r"\b(query|mutation)\s+(\w+)", data["query"])
        return f"GraphQL {found.group(2)}" if found else "GraphQL"
    flat = " ".join(text.split())
    return flat[:80] + ("…" if len(flat) > 80 else "")


def _header_problem(name: object, value: object) -> str:
    """What is wrong with one of the JSON request's headers, or ""."""
    if not HEADER_NAME.match(str(name)) or str(name).lower() in FORBIDDEN:
        return "may not be set"
    if isinstance(value, str):
        if SECRET.search(str(name)):
            return "is a secret that changes: read it from a cookie, a <meta> or the page's storage"
        if len(value) > HEADER_TEXT_MAX or re.search(r"[\r\n\0]", value):
            return f"is one line of at most {HEADER_TEXT_MAX} characters"
        return ""
    if not (
        isinstance(value, dict)
        and value.get("from") in HEADER_SOURCES
        and isinstance(value.get("name"), str)
        and value["name"]
        and isinstance(value.get("prefix", ""), str)
        and not set(value) - {"from", "name", "prefix"}
    ):
        return 'is text or {"from": cookie, meta or storage, "name": ...}'
    return ""


def _check_headers(headers: object) -> list[str]:
    if headers is None:
        return []
    if not isinstance(headers, dict) or len(headers) > HEADERS_MAX:
        return [f"file links: the JSON request's headers are an object of at most {HEADERS_MAX}"]
    return [
        f"file links: the header {name} {problem}"
        for name, value in headers.items()
        if (problem := _header_problem(name, value))
    ][:1]


def _check_path(where: str, path: object) -> list[str]:
    if not isinstance(path, str) or not path.strip() or "*" in path:
        return [f"file links: the {where} is a field's path"]
    return []


def _check_cursor(spec: dict) -> list[str]:
    chain = spec.get("cursor")
    if chain is None:
        return []
    if not isinstance(chain, dict) or set(chain) - {"from", "to", "in", "more"}:
        return ['file links: the cursor is {"from": <its path in the answer>, "to": <the parameter>, "more": ...}']
    problems = _check_path("cursor's from", chain.get("from"))
    if "to" in chain:
        problems += _check_path("cursor's to", chain["to"])
    if "more" in chain:
        problems += _check_path("cursor's more", chain["more"])
    if chain.get("in", "query") not in PLACES or (chain.get("in") == "body" and spec.get("method") != "POST"):
        problems.append("file links: the cursor goes in the query, or in the body of a POST")
    if spec.get("page"):
        problems.append("file links: the JSON's pages are counted (page) or chained (cursor), not both")
    return problems


def _body_problem(spec: dict) -> str:
    """What is wrong with the JSON request's method, body or type, or ""."""
    if spec.get("method", "GET") not in METHODS:
        return f"the JSON request's method is {' or '.join(METHODS)}"
    if spec.get("method") != "POST":
        return "only a POST has a body" if "body" in spec or "content_type" in spec else ""
    data = spec.get("body", {})
    try:
        size = len(data) if isinstance(data, str) else len(json.dumps(data))
    except (TypeError, ValueError):
        return "the JSON request's body is JSON or a form's text"
    if size > BODY_MAX:
        return f"the JSON request's body is at most {BODY_MAX // 1024} KB"
    kind = spec.get("content_type", "")
    if not isinstance(kind, str) or not re.match(r"^[\w.+-]*/?[\w.+-]*(;.*)?$", kind) or "\n" in kind:
        return "the JSON request's content_type is a media type"
    return _body_page_problem(spec, data)


def _body_page_problem(spec: dict, data: Any) -> str:
    """What is wrong with the place of a POST's page counter, or ""."""
    page = spec.get("page")
    if isinstance(page, dict) and page.get("in", "query") not in PLACES:
        return "the JSON's pages go in the query, or in the body of a POST"
    if isinstance(page, dict) and page.get("in") == "body" and not is_form(spec):
        try:
            set_path(data, str(page.get("param")), 1)
        except ValueError as error:
            return f"the page's field: {error}"
    return ""


def check(spec: dict) -> list[str]:
    """The problems of a JSON listing's request: its method and body, headers, page and cursor."""
    # A request without its method or body right has no place for its pages either.
    problem = _body_problem(spec)
    if problem:
        return [f"file links: {problem}"]
    problems = _check_headers(spec.get("headers")) + _check_cursor(spec)
    page = spec.get("page")
    if isinstance(page, dict) and page.get("in") == "body" and spec.get("method") != "POST":
        problems.append("file links: the JSON's pages go in the query, or in the body of a POST")
    return problems


def uses_browser(spec: dict) -> bool:
    """Whether the request needs the browser: a header read from the page's storage."""
    headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else {}
    return any(isinstance(value, dict) and value.get("from") == "storage" for value in headers.values())
