# SPDX-License-Identifier: AGPL-3.0-or-later
"""The website source's data beside the links (harvester.sources.site): the texts of a level's
values near each link (in the browser, VALUES; over HTTP, soup_value), the pages of a JSON
listing (json_pages, read_json), and the code of a step "mail-code" (mail_code).
"""

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from .. import browser, images, jsonlist, jsonrequest, patterns
from . import download, mail
from .base import SourceError, check_stop

# A level's links with the texts of its values (harvester.recipe: "values"): each value's element
# (the link's closest match of closest, then the match of selector in it), its attribute or its
# text; with many, those of every match of selector in it (a card's tag chips); null where there
# is none, or a selector the page cannot read. Last, an image's attributes
# (harvester.images.INFO).
VALUES = (
    """(els, specs) => els.map(e => [e.href || '', (e.innerText || '').trim(), specs.map(s => {
  try {
    const text = (el) => s.attribute ? el.getAttribute(s.attribute) : (el.innerText || el.textContent || '').trim()
    let at = s.closest ? e.closest(s.closest) : e
    if (at && s.many) return [...at.querySelectorAll(s.selector)].slice(0, """
    + str(patterns.MAX_ENTRIES)
    + """).map((el) => text(el) || '')
    if (at && s.selector) at = at.querySelector(s.selector)
    if (!at) return null
    return text(at)
  } catch (error) { return null }
}), ("""
    + images.INFO
    + ")(e)])"
)

# What a page holds under a name (Driver.page_value): a <meta name>'s content, a localStorage key's
# value; null without one.
PAGE_VALUE = """([kind, name]) => {
  if (kind === 'meta') {
    const found = [...document.querySelectorAll('meta[name]')].find(m => m.getAttribute('name') === name)
    return found ? found.getAttribute('content') : null
  }
  try { return window.localStorage.getItem(name) } catch (error) { return null }
}"""


def soup_value(link: Any, spec: dict) -> str | list | None:
    """A value's text for a link of a page read over HTTP, as VALUES finds it in the browser: the
    link's closest match of closest, the match of selector in it (every match, with many), its
    attribute or its text; None where there is none (or a selector BeautifulSoup cannot read)."""
    try:
        at = link.css.closest(spec["closest"]) if spec.get("closest") else link
        if at is not None and spec.get("many"):
            return [_text(element, spec) for element in at.select(spec["selector"])[: patterns.MAX_ENTRIES]]
        if at is not None and spec.get("selector"):
            at = at.select_one(spec["selector"])
    except Exception:  # pylint: disable=broad-exception-caught
        return None  # soupsieve's SelectorSyntaxError and its kin: the value is missing
    if at is None:
        return None
    return _text(at, spec)


def _text(element: Any, spec: dict) -> str:
    """An element's attribute that the value reads, else its text."""
    if spec.get("attribute"):
        value = element.get(spec["attribute"])
        return " ".join(value) if isinstance(value, list) else str(value or "")
    return element.get_text(" ", strip=True)


def mail_code(page, step: dict, secrets: dict[str, str], began: float) -> str:
    """The code of a step "mail-code" (harvester.sources.mail.code_from_mail): from a message that
    came after the step began, waited for in the page's pauses, so a run or a test stopped
    meanwhile ends there."""

    def pause(seconds: float) -> None:
        check_stop()
        page.wait_for_timeout(seconds * 1000)

    return mail.code_from_mail(step, secrets, began, pause)


def json_pages(
    spec: dict, settings: dict, driver, values: dict, where: str | None = None
) -> Iterator[jsonlist.JsonPage]:
    """The pages of the JSON listing (spec: the recipe's file links of kind json), its address
    made of the walk's values, up to the job's pages (settings' max_pages) while it is counted or
    chained (harvester.jsonrequest); one without either. driver reads each (Driver.read_json),
    with the method, body and headers of a POST; where is the page the listing is read from (None:
    where the steps ended), the Referer and the place of a header's <meta> or storage."""
    try:
        first = patterns.render(str(spec["url"]), patterns.link_values(str(spec["url"]), "", 1) | values)
    except (KeyError, ValueError, IndexError) as error:
        raise SourceError(f"the JSON listing's address: {str(error).strip(chr(39))}") from error
    headers = _sent_headers(spec, driver, first, where)
    pages = int(settings["max_pages"]) if jsonrequest.paged(spec) else 1
    cursor: str | None = None
    cursors: set[str] = set()
    for number in range(1, pages + 1):
        url = jsonrequest.address(first, spec, number, cursor)
        if not browser.allowed_url(url):
            raise SourceError(f"the JSON listing's next page {url} may not be opened{browser.dns_refusal(url)}")
        try:
            content = jsonrequest.body(spec, values, number, cursor)
        except ValueError as error:
            raise SourceError(f"the JSON listing's body: {error}") from error
        data = driver.read_json(url, {"method": spec.get("method") or "GET", "content": content, "headers": headers})
        yield jsonlist.JsonPage(url, data)
        if not spec.get("cursor"):
            continue
        # The next cursor from this answer; none, or one seen before (a loop), is the last page.
        cursor = jsonrequest.next_cursor(data, spec["cursor"])
        if cursor is None or cursor in cursors:
            return
        cursors.add(cursor)


def _sent_headers(spec: dict, driver, url: str, where: str | None) -> dict[str, str]:
    """All the JSON listing's headers: its own (json_headers), the page it is read from as the
    Referer, and a POST's Origin and Content-Type, as a browser's script sends them."""
    referer = where or driver.here()
    headers = json_headers(spec, driver, url, where) | ({"Referer": referer} if referer else {})
    if spec.get("method") == "POST":
        parts = urlsplit(referer or url)
        headers |= {"Origin": f"{parts.scheme}://{parts.netloc}", "Content-Type": jsonrequest.content_type(spec)}
    return headers


def json_headers(spec: dict, driver, url: str, where: str | None) -> dict[str, str]:
    """The JSON listing's headers (harvester.jsonrequest): the recipe's texts, and the tokens read
    now from the job's cookies for the listing's address, or from the page the listing is read from
    (its <meta>, its storage); SourceError naming one that is not there (signed out?)."""
    found: dict[str, str] = {}
    page = None
    try:
        for name, source in (spec.get("headers") or {}).items():
            if isinstance(source, str):
                found[name] = source
                continue
            if source["from"] == "cookie":
                value = driver.cookie_value(url, source["name"])
            else:
                page = page if page is not None else driver.open(where)
                value = driver.page_value(page, source["from"], source["name"])
            if not value:
                raise SourceError(
                    f"the JSON listing's header {name}: no {source['from']} {source['name']} (signed out?)"
                )
            found[name] = str(source.get("prefix") or "") + str(value)
    finally:
        if page is not None:
            driver.done(page)
    return found


def read_json(opener: download.Opener, url: str) -> Any:
    """A JSON listing's page, asked for as a file is (opener: the driver's download: the job's
    cookies, the guarded client), read up to download.MAX_PAGE; SourceError when it is no JSON."""
    with opener(url) as response:
        if response.status >= 400:
            raise SourceError(f"the JSON listing {url} answered {response.status}")
        body = bytearray()
        for chunk in response.chunks:
            body += chunk
            if len(body) > download.MAX_PAGE:
                raise SourceError(f"the JSON listing {url} is larger than {download.MAX_PAGE // 1048576} MB")
    try:
        return jsonlist.parse(bytes(body))
    except ValueError as error:
        raise SourceError(f"the JSON listing {url} is not JSON ({str(error).splitlines()[0]}): signed out?") from error
