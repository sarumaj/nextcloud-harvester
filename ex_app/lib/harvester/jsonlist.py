# SPDX-License-Identifier: AGPL-3.0-or-later
"""Listings in JSON: the file links of a site whose listing the page fetches as JSON (an XHR or
fetch() of a single-page app) instead of showing them as links in its HTML.

A recipe's file links of kind "json" (harvester.recipe) name the request, where its items are and
which of each item's fields are the file's address, its text and its values:

    "links": {"kind": "json", "url": "https://shop.example/api/invoices?page=1",
              "items": "data.invoices", "file": "pdf.href", "text": "title",
              "page": {"param": "page", "first": 1, "step": 1},
              "values": [{"name": "date", "field": "issued", "type": "date"}], "pattern": ""}

A path is JSONPath's dotted form, without its filters: names and [n] indexes from the answer (or
the item) down, `*` or [*] for each entry of a list or object; a leading `$` is allowed. The page
parameter is replaced in the address for each page (first, first + step, ...): a page number or
an offset. The recorder proposes all of it from an answer the page received (propose).
"""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# The items of a page read at most, and how deep an answer may nest (deeper is no listing, and
# Python's parser would recurse without end).
MAX_ITEMS = 1000
MAX_DEPTH = 64
# The query parameters a listing's pages are counted in: page numbers, and offsets (the number of
# items before the page's first).
PAGE_PARAMS = ("page", "p", "pg", "pageNumber", "page_number", "pageNo", "pageIndex", "currentPage")
OFFSET_PARAMS = ("offset", "skip", "start", "from", "first")
# The fields a file's address, its text and its date are most often in, best first.
URL_FIELDS = ("download_url", "downloadUrl", "file_url", "fileUrl", "pdf", "download", "file", "href", "url", "link")
TEXT_FIELDS = ("title", "name", "filename", "fileName", "subject", "label", "description")
DATE_FIELDS = ("date", "published", "publishedAt", "published_at", "created", "createdAt", "created_at", "issued")
TOKEN = re.compile(r"\[(\*|\d+)\]|\[[\"']([^\"']+)[\"']\]|([^.\[\]]+)")


@dataclass(frozen=True)
class JsonPage:
    """A page of a JSON listing: its address and its parsed answer."""

    url: str
    data: Any


def parse(body: bytes) -> Any:
    """The answer as JSON (ValueError when it is none, or nests deeper than MAX_DEPTH)."""
    text = body.decode("utf-8-sig", "replace")
    depth = most = 0
    quoted = escaped = False
    # The nesting is counted before parsing: json.loads recurses once per level.
    for char in text:
        if quoted:
            escaped, quoted = (False, True) if escaped else (char == "\\", char != '"')
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            most = max(most, depth)
        elif char in "]}":
            depth -= 1
    if most > MAX_DEPTH:
        raise ValueError(f"nested deeper than {MAX_DEPTH} levels")
    return json.loads(text)


def steps(path: str) -> list[str | int]:
    """The path's steps: names, indexes (int) and "*"."""
    path = path.strip().removeprefix("$").lstrip(".")
    found: list[str | int] = []
    for index, quoted, name in TOKEN.findall(path):
        if index:
            found.append("*" if index == "*" else int(index))
        else:
            found.append(quoted or name)
    return found


def select(data: Any, path: str) -> list:
    """What the path leads to in data: each match ("*" takes every entry of a list or object)."""
    current = [data]
    for step in steps(path):
        following: list = []
        for value in current:
            if step == "*":
                following += (
                    list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else []
                )
            elif isinstance(step, int):
                if isinstance(value, list) and -len(value) <= step < len(value):
                    following.append(value[step])
            elif isinstance(value, dict) and step in value:
                following.append(value[step])
        current = following
    return current


def field(item: Any, path: str) -> str | None:
    """The item's field as text (the first match; a number as written), None when it has none or
    it is no single value."""
    if not path:
        return None
    found = select(item, path)
    if not found or isinstance(found[0], (dict, list)) or found[0] is None:
        return None
    value = found[0]
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def items(data: Any, path: str) -> list:
    """The listing's items: the path's matches, a list's entries in its place (so "data.items"
    and "data.items[*]" are the same), at most MAX_ITEMS."""
    found = select(data, path) if path else [data]
    if len(found) == 1 and isinstance(found[0], list):
        found = found[0]
    return found[:MAX_ITEMS]


def entries(page: JsonPage, spec: dict) -> list[tuple[str, str, list]]:
    """The file links of a page: the file's absolute address, its text and the texts of the spec's
    values (None where an item lacks one), for each item that has an http(s) address."""
    found = []
    for item in items(page.data, str(spec.get("items") or "")):
        address = field(item, str(spec.get("file") or ""))
        if not address:
            continue
        url = urljoin(page.url, address.strip())
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        text = field(item, str(spec.get("text") or "")) or ""
        values = [field(item, str(value.get("field") or "")) for value in spec.get("values") or []]
        found.append((url, text, values))
    return found


def page_url(url: str, page: dict | None, number: int) -> str:
    """The address of the listing's page number (1: the first), the page parameter set in its
    query; the address as it is without a page parameter."""
    if not page or not page.get("param"):
        return url
    value = int(page.get("first", 1)) + (number - 1) * int(page.get("step", 1))
    parts = urlsplit(url)
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key != page["param"]]
    query.append((str(page["param"]), str(value)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def detect_page(url: str, count: int) -> dict | None:
    """The page parameter of a listing's address, if it has one: a page number (step 1) or an offset
    (step: the items of a page, count)."""
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    for name in PAGE_PARAMS:
        if name in query and query[name].isdigit():
            return {"param": name, "first": int(query[name]), "step": 1}
    for name in OFFSET_PARAMS:
        if name in query and query[name].isdigit() and count:
            return {"param": name, "first": int(query[name]), "step": count}
    return None


def _lists(data: Any, path: str = "", depth: int = 0) -> list[tuple[str, list]]:
    """Every list of objects in the answer, with its path."""
    if depth > MAX_DEPTH:
        return []
    found: list[tuple[str, list]] = []
    if isinstance(data, list):
        if data and all(isinstance(entry, dict) for entry in data[:20]):
            found.append((path, data))
        for index, entry in enumerate(data[:1]):
            found += _lists(entry, f"{path}[{index}]", depth + 1)
    elif isinstance(data, dict):
        for key, value in data.items():
            name = key if re.fullmatch(r"[A-Za-z_][\w-]*", key) else f'["{key}"]'
            joined = f"{path}.{name}" if path and not name.startswith("[") else f"{path}{name}"
            found += _lists(value, joined, depth + 1)
    return found


def _fields(item: Any, path: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    """An item's single values with their paths (objects within it too, lists' first entries)."""
    if depth > 4:
        return []
    found: list[tuple[str, Any]] = []
    if isinstance(item, dict):
        for key, value in item.items():
            found += _fields(value, f"{path}.{key}" if path else str(key), depth + 1)
    elif isinstance(item, list) and item:
        found += _fields(item[0], f"{path}[0]", depth + 1)
    elif item is not None:
        found.append((path, item))
    return found


def _best(candidates: list[tuple[str, Any]], names: tuple[str, ...]) -> str:
    """The candidate whose last name comes first in names (case aside), else the first one."""
    ranked = {name.lower(): rank for rank, name in enumerate(names)}
    last = [
        (ranked.get(re.split(r"[.\[]", path)[-1].lower(), len(names)), index)
        for index, (path, _) in enumerate(candidates)
    ]
    return candidates[min(last)[1]][0] if candidates else ""


def propose(url: str, data: Any) -> dict:
    """File links of kind json for an answer: its largest list of objects as the items, the field
    with the files' addresses (a name like url or href, an address ending in a file name first),
    their text and date, and the page parameter of the address. "file" is "" when no field holds
    addresses."""
    lists = sorted(_lists(data), key=lambda found: len(found[1]), reverse=True)
    spec: dict = {"kind": "json", "url": url, "items": "", "file": "", "text": "", "pattern": ""}
    if not lists:
        return spec
    path, entries_ = lists[0]
    fields = _fields(entries_[0])
    addresses = [
        (name, value)
        for name, value in fields
        if isinstance(value, str)
        and re.match(r"(https?://|/)[^\s]+$", value)
        and not re.search(r"\.(jpe?g|png|gif|webp|svg)$", value, re.I)
    ]
    files = [(name, value) for name, value in addresses if re.search(r"/[^/?#]+\.\w{2,5}([?#]|$)", value)]
    texts = [(name, value) for name, value in fields if isinstance(value, str) and not re.match(r"https?://|/", value)]
    dates = [
        (name, value) for name, value in fields if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value)
    ]
    spec |= {"items": path, "file": _best(files or addresses, URL_FIELDS), "text": _best(texts, TEXT_FIELDS)}
    if dates:
        spec["values"] = [{"name": "date", "field": _best(dates, DATE_FIELDS), "type": "date", "regex": ""}]
    page = detect_page(url, len(entries_))
    if page:
        spec["page"] = page
    return spec
