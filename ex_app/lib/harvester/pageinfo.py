# SPDX-License-Identifier: AGPL-3.0-or-later
"""Values of a page a website's walk opens (harvester.sources.walk): built-in ones every such page
gives, and those a recipe's level picked on the page its links open ("page_values").

The built-in values: {page_title} (og:title, else <title>, without a " | Site" or " - Site" that
repeats the site's name), {site_name} (og:site_name, else application-name, else the host),
{page_date} (article:published_time, og:updated_time, JSON-LD's datePublished or the first <time
datetime>, as YYYY-MM-DD with {page_year}, {page_month} and {page_day}) and {page_lang} (<html
lang>). They are read in one go: in the browser one evaluate (PAGE_INFO), over HTTP from the page
already parsed (soup_info), both giving the same raw form that values() reads.

A level's "page_values" are of the same form as its "values", without "closest" (the page holds
one each): {"name", "selector", "attribute", "regex", "type"}; "many": true reads every element
the selector finds, a list (a post's tag chips: a tag per entry in a job's Tags).
"""

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from . import patterns

# The values every page a walk opens gives (and page_date's parts).
BUILT_IN = ("page_title", "site_name", "page_date", "page_lang", "page_year", "page_month", "page_day")
# The <meta> a page's values come from (property, name or itemprop, in lower case).
META = ("og:title", "og:site_name", "application-name", "article:published_time", "og:updated_time")
# What separates a title from the site's name at its end ("Post | Site", "Post - Site").
TITLE_SEPARATORS = (" | ", " - ", " – ", " — ", " · ", " :: ", " » ")
# The JSON-LD scripts read, and how much of each (characters), how deep its search goes.
LD_SCRIPTS = 5
LD_TEXT = 100_000
LD_DEPTH = 6
# The elements a value with many reads at most.
MANY = patterns.MAX_ENTRIES
# A language's tag as <html lang> holds it (de, pl-PL, zh-Hant-TW).
LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")

# The page's raw values (values() reads them), and the texts of the level's page_values (specs):
# each one's element (document.querySelector, or all of them with many), its attribute or text;
# null where there is none, or a selector the page cannot read.
PAGE_INFO = (
    """(specs) => {
  const wanted = """
    + json.dumps(list(META))
    + """
  const meta = {}
  for (const m of document.querySelectorAll('meta[content]')) {
    const key = (m.getAttribute('property') || m.getAttribute('name') || m.getAttribute('itemprop') || '').toLowerCase()
    if (wanted.includes(key) && !(key in meta)) meta[key] = m.getAttribute('content') || ''
  }
  const ld = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .slice(0, """
    + str(LD_SCRIPTS)
    + """).map((s) => (s.textContent || '').slice(0, """
    + str(LD_TEXT)
    + """))
  const time = document.querySelector('time[datetime]')
  const text = (el, s) => s.attribute ? el.getAttribute(s.attribute) : (el.innerText || el.textContent || '').trim()
  const picked = specs.map((s) => {
    try {
      if (s.many) return [...document.querySelectorAll(s.selector)].slice(0, """
    + str(MANY)
    + """).map((el) => text(el, s) || '')
      const el = document.querySelector(s.selector)
      return el ? text(el, s) : null
    } catch (error) { return null }
  })
  return { title: document.title || '', meta, ld, time: time ? time.getAttribute('datetime') : '',
    lang: document.documentElement.getAttribute('lang') || '', picked }
}"""
)


def soup_info(soup: Any, specs: list[dict]) -> dict:
    """The page's raw values as PAGE_INFO gives them, from a page read over HTTP (BeautifulSoup)."""
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta", attrs={"content": True}):
        key = str(tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in META and key not in meta:
            meta[key] = str(tag.get("content") or "")
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})[:LD_SCRIPTS]
    time = soup.find("time", attrs={"datetime": True})
    html = soup.find("html")
    title = soup.find("title")
    return {
        "title": title.get_text(strip=True) if title else "",
        "meta": meta,
        "ld": [script.get_text()[:LD_TEXT] for script in scripts],
        "time": str(time.get("datetime") or "") if time else "",
        "lang": str(html.get("lang") or "") if html else "",
        "picked": [_soup_text(soup, spec) for spec in specs],
    }


def _soup_text(soup: Any, spec: dict) -> str | list | None:
    """A page value's text (or texts, with many) over HTTP, as PAGE_INFO reads it."""

    def text(element: Any) -> str:
        if spec.get("attribute"):
            value = element.get(spec["attribute"])
            return " ".join(value) if isinstance(value, list) else str(value or "")
        return element.get_text(" ", strip=True)

    try:
        if spec.get("many"):
            return [text(element) for element in soup.select(str(spec.get("selector") or ""))[:MANY]]
        found = soup.select_one(str(spec.get("selector") or ""))
    except Exception:  # pylint: disable=broad-exception-caught
        return None  # soupsieve's SelectorSyntaxError and its kin: the value is missing
    return text(found) if found is not None else None


def _published(data: Any, depth: int = 0) -> str:
    """JSON-LD's first datePublished, in its objects, lists and @graph."""
    if depth > LD_DEPTH:
        return ""
    if isinstance(data, dict):
        if isinstance(data.get("datePublished"), str):
            return data["datePublished"]
        data = list(data.values())
    if isinstance(data, list):
        for entry in data:
            found = _published(entry, depth + 1)
            if found:
                return found
    return ""


def _ld_date(scripts: list) -> str:
    for script in scripts:
        try:
            found = _published(json.loads(str(script)))
        except ValueError:
            continue
        if found:
            return found
    return ""


def site_name(raw: dict, url: str) -> str:
    """The site's name: og:site_name, else application-name, else the address's host."""
    meta = raw.get("meta") or {}
    named = str(meta.get("og:site_name") or meta.get("application-name") or "").strip()
    return named or (urlsplit(url).hostname or "")


def page_title(raw: dict, site: str) -> str:
    """The page's title: og:title, else <title>, without a separator and the site's name at its
    end."""
    title = re.sub(r"\s+", " ", str((raw.get("meta") or {}).get("og:title") or raw.get("title") or "")).strip()
    for separator in TITLE_SEPARATORS:
        head, found, tail = title.rpartition(separator)
        if found and head.strip() and site and tail.strip().casefold() == site.casefold():
            return head.strip()
    return title


def built_in(raw: dict, url: str) -> dict:
    """The built-in values of a page (BUILT_IN) from its raw values; one the page lacks is
    missing."""
    found: dict = {}
    site = site_name(raw, url)
    title = patterns.clean_text(page_title(raw, site))
    if title:
        found["page_title"] = title
    if patterns.clean_text(site):
        found["site_name"] = patterns.clean_text(site)
    meta = raw.get("meta") or {}
    for text in (
        meta.get("article:published_time"),
        meta.get("og:updated_time"),
        _ld_date(raw.get("ld") or []),
        raw.get("time"),
    ):
        date = patterns.parse_date(str(text or ""))
        if date:
            year, month, day = date[1]
            found["page_date"] = f"{year:04d}-{month:02d}" + (f"-{day:02d}" if day else "")
            found |= {"page_year": year, "page_month": month} | ({"page_day": day} if day else {})
            break
    lang = str(raw.get("lang") or "").strip()
    if LANGUAGE.fullmatch(lang):
        found["page_lang"] = lang
    return found


def values(raw: dict, url: str, specs: list[dict], search: Callable[[str, str], Any], given: dict) -> dict:
    """A page's values: the built-in ones and those of the level's page_values (specs), read
    from what the page gave (raw: PAGE_INFO's, soup_info's); given: the walk's values so far (a
    date's year does not replace one)."""
    if not raw:
        return {}
    picked = raw.get("picked") or []
    return built_in(raw, url) | patterns.page_values(specs, picked, search, given)
