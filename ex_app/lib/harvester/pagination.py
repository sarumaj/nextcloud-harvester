"""Numbered pages: a listing's pagination of numbers (‹ 1 [2] 3 … 9 ›) instead of one "next" link.

The recipe's next is then {"kind": "number", "selector": <the pagination's container>}: on each
page the walk finds the current page's number and takes the item numbered one higher, a link
(followed) or a button (clicked in the browser). The current page is the number marked as such
(aria-current, a class like active, current or selected), else the one number that is no link
or button, else the page number in the address (?page=3, /page/3), else the number the walk
counted. One implementation for both drivers: the browser hands the container's HTML over.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

NUMBER = re.compile(r"\d{1,6}")
# The classes a site marks its current page with (Bootstrap's active, WordPress's current, ...).
CURRENT_CLASS = re.compile(r"(^|[-_])(active|current|selected|is-active|is-current)([-_]|$)", re.IGNORECASE)
# The query parameters that carry a listing's page number.
PAGE_PARAMETERS = ("page", "p", "pg", "seite", "strona", "pagina", "paged", "pn")
PAGE_PATH = re.compile(r"/(?:page|seite|strona|p)/(\d{1,6})/?$", re.IGNORECASE)


@dataclass(frozen=True)
class Numbered:
    """One number of the pagination."""

    number: int
    # A link or a button that leads to its page (not the current page's plain text, not disabled).
    clickable: bool
    current: bool
    # The link's address ("" for a button, or a link that only runs a script).
    href: str


def _marked(tag: Tag) -> bool:
    current = tag.get("aria-current")
    if current is not None and str(current).lower() != "false":
        return True
    return any(CURRENT_CLASS.search(name) for name in tag.get("class") or [])


def _link(tag: Tag) -> tuple[bool, str]:
    """Whether the element leads somewhere (a link or a button, not disabled), and its address."""
    if tag.get("disabled") is not None or str(tag.get("aria-disabled", "")).lower() == "true":
        return False, ""
    href = str(tag.get("href") or "")
    if tag.name == "a" and href:
        return True, "" if href.startswith(("#", "javascript:")) else href
    return tag.name == "button" or tag.get("role") == "button" or tag.get("onclick") is not None, ""


def numbers(html: str) -> list[Numbered]:
    """The pagination's numbers in the container's HTML, in their order."""
    soup = BeautifulSoup(html or "", "html.parser")
    root = next((child for child in soup.children if isinstance(child, Tag)), None)
    if root is None:
        return []
    found = []
    for tag in [root, *root.find_all(True)]:
        # Spaced: a box of numbers (<a>1</a><a>2</a>) is no number 12.
        text = tag.get_text(" ", strip=True)
        if not NUMBER.fullmatch(text) or any(child.get_text(" ", strip=True) == text for child in tag.find_all(True)):
            continue
        # The number's element and its ancestors up to the container say what it is.
        clickable, href, current = False, "", False
        node: Tag | None = tag
        while isinstance(node, Tag):
            current = current or _marked(node)
            if not clickable:
                clickable, href = _link(node)
            if node is root:
                break
            node = node.parent
        found.append(Numbered(int(text), clickable, current, href))
    return found


def in_address(url: str) -> int | None:
    """The page number the address carries (?page=3, /page/3), if any."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for name in PAGE_PARAMETERS:
        value = (query.get(name) or [""])[0]
        if NUMBER.fullmatch(value):
            return int(value)
    match = PAGE_PATH.search(parsed.path)
    return int(match[1]) if match else None


def current_page(found: list[Numbered], url: str, counted: int) -> int:
    """The current page's number: marked, the one plain number, in the address, or counted."""
    marked = [item.number for item in found if item.current]
    if marked:
        return marked[0]
    plain = {item.number for item in found if not item.clickable}
    if len(plain) == 1:
        return plain.pop()
    return in_address(url) or counted


def following(html: str, url: str, counted: int) -> tuple[int, str] | None:
    """The next page's number and its link's address ("" for a button: click it), or None at
    the last page. counted is the walk's own count of the listing's pages (1 for the first)."""
    found = numbers(html)
    wanted = current_page(found, url, counted) + 1
    for item in found:
        if item.number == wanted and item.clickable:
            return wanted, item.href
    return None


# Marks the clickable element of a number in the browser, for Playwright to click it as a
# person would (the page's own handlers run, as for a click in the recorder).
MARK_NUMBER = """([selector, number]) => {
	const box = document.querySelector(selector)
	if (!box) return false
	for (const el of [box, ...box.querySelectorAll('*')]) {
		const text = (el.textContent || '').trim()
		if (text !== number || [...el.querySelectorAll('*')].some((inner) => /^\\d/.test((inner.textContent || '').trim()))) continue
		const target = el.closest('a[href], button, [role=button], [onclick]')
		if (target && box.contains(target)) {
			document.querySelectorAll('[data-harvester-next]').forEach((old) => old.removeAttribute('data-harvester-next'))
			target.setAttribute('data-harvester-next', '')
			return true
		}
	}
	return false
}"""
