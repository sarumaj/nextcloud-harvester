# SPDX-License-Identifier: AGPL-3.0-or-later
"""The website source's data beside the links (harvester.sources.site): the texts of a level's
values near each link (in the browser, VALUES; over HTTP, soup_value), the pages of a JSON
listing (json_pages, read_json), and the code of a step "mail-code" (mail_code).
"""

from collections.abc import Iterator
from typing import Any

from .. import jsonlist, patterns
from . import download, mail
from .base import SourceError, check_stop

# A level's links with the texts of its values (harvester.recipe: "values"): each value's element
# (the link's closest match of closest, then the match of selector in it), its attribute or its
# text; null where there is none, or a selector the page cannot read.
VALUES = """(els, specs) => els.map(e => [e.href || '', (e.innerText || '').trim(), specs.map(s => {
  try {
    let at = s.closest ? e.closest(s.closest) : e
    if (at && s.selector) at = at.querySelector(s.selector)
    if (!at) return null
    return s.attribute ? at.getAttribute(s.attribute) : (at.innerText || at.textContent || '').trim()
  } catch (error) { return null }
})])"""


def soup_value(link: Any, spec: dict) -> str | None:
    """A value's text for a link of a page read over HTTP, as VALUES finds it in the browser: the
    link's closest match of closest, the match of selector in it, its attribute or its text; None
    where there is none (or a selector BeautifulSoup cannot read)."""
    try:
        at = link.css.closest(spec["closest"]) if spec.get("closest") else link
        if at is not None and spec.get("selector"):
            at = at.select_one(spec["selector"])
    except Exception:  # pylint: disable=broad-exception-caught
        return None  # soupsieve's SelectorSyntaxError and its kin: the value is missing
    if at is None:
        return None
    if spec.get("attribute"):
        value = at.get(spec["attribute"])
        return " ".join(value) if isinstance(value, list) else value
    return at.get_text(" ", strip=True)


def mail_code(page, step: dict, secrets: dict[str, str], began: float) -> str:
    """The code of a step "mail-code" (harvester.sources.mail.code_from_mail): from a message that
    came after the step began, waited for in the page's pauses, so a run or a test stopped
    meanwhile ends there."""

    def pause(seconds: float) -> None:
        check_stop()
        page.wait_for_timeout(seconds * 1000)

    return mail.code_from_mail(step, secrets, began, pause)


def json_pages(spec: dict, settings: dict, driver, values: dict) -> Iterator[jsonlist.JsonPage]:
    """The pages of the JSON listing (spec: the recipe's file links of kind json), its address
    made of the walk's values, up to the job's pages (settings' max_pages); one without a page
    parameter. driver reads each (Driver.read_json)."""
    try:
        first = patterns.render(str(spec["url"]), patterns.link_values(str(spec["url"]), "", 1) | values)
    except (KeyError, ValueError, IndexError) as error:
        raise SourceError(f"the JSON listing's address: {str(error).strip(chr(39))}") from error
    pages = int(settings["max_pages"]) if (spec.get("page") or {}).get("param") else 1
    for number in range(1, pages + 1):
        url = jsonlist.page_url(first, spec.get("page"), number)
        yield jsonlist.JsonPage(url, driver.read_json(url))


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
