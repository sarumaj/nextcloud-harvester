"""Link patterns: proposed from picked links, and the values their named groups give a file.

A pattern is a regular expression searched in a link's absolute URL. Its named groups
((?P<year>...)) become values for the download URL, the file's name and its tags, together
with the link's own: url, text, page, host, filename, stem and ext. A value of digits is a
number, so {issue:02d} works.

A level may also take values from the page around each link (page_values): the link's text or
an attribute, or an element near it (its row or card, then an element in that), narrowed by a
pattern and typed (a date gives YYYY-MM-DD and its year, month and day). propose_value proposes
one for texts a person picked, recognising dates as English, German and Polish pages write them.
"""

import datetime
import posixpath
import re
from collections import Counter
from collections.abc import Callable
from string import Formatter
from typing import Any
from urllib.parse import unquote, urlparse

TOKEN = re.compile(r"\d+|\D+")
SPECIAL = re.compile(r"([.^$*+?{}\[\]\\|()])")


def escape(text: str) -> str:
    """The text as a literal in a pattern (only what a pattern would read otherwise escaped)."""
    return SPECIAL.sub(r"\\\1", text)


def is_year(value: str) -> bool:
    """A year of this or the last century, which differs between a site's listings."""
    return value.isdigit() and len(value) == 4 and 1900 <= int(value) <= 2099


def link_values(url: str, text: str, page: int) -> dict:
    """The values every link gives, whatever the pattern."""
    parsed = urlparse(url)
    filename = posixpath.basename(unquote(parsed.path)) or "index"
    stem, ext = posixpath.splitext(filename)
    text = re.sub(r"[\\/:*?\"<>|\s]+", " ", text).strip()[:100] or stem
    return {
        "url": url,
        "text": text,
        "page": page,
        "host": parsed.hostname or "",
        "filename": filename,
        "stem": stem,
        "ext": ext,
    }


def group_values(match: re.Match) -> dict:
    """The named groups that matched, a number where they are digits."""
    return {
        name: int(value) if value.isdigit() else value for name, value in match.groupdict().items() if value is not None
    }


def render(template: str, values: dict, giver: str = "the link pattern") -> str:
    """Fills in the template; a value it names that is missing is an error naming it, and what
    should have given it (giver: the link pattern, the message, this release)."""
    missing = [name for _, name, _, _ in Formatter().parse(template) if name and name not in values]
    if missing:
        raise KeyError(f"{template} names {', '.join(missing)}, which {giver} does not give")
    return template.format(**values)


def render_path(template: str, values: dict, giver: str = "the link pattern") -> str:
    """The file's path from the template, without leaving the job's folder; each part without
    what Nextcloud refuses in a name (control characters, backslashes, leading or trailing
    spaces and trailing dots)."""
    path = posixpath.normpath("/" + render(template, values, giver)).lstrip("/")
    parts = []
    for part in path.split("/"):
        part = re.sub(r"[\x00-\x1f\x7f\\]+", " ", part).strip().rstrip(".").strip()
        if part and part != ".":
            parts.append(part)
    return "/".join(parts)


# Nextcloud's longest tag name.
MAX_TAG = 64


def render_tags(templates: list[str], values: dict) -> list[str]:
    """The tags from their templates; one naming a value the file lacks is left out, a long one
    (an email's subject, say) shortened to what Nextcloud keeps."""
    tags = []
    for template in templates:
        try:
            tag = render(template, values).strip()[:MAX_TAG].strip()
        except (KeyError, ValueError):
            continue
        if tag:
            tags.append(tag)
    return list(dict.fromkeys(tags))


def _name(values: list[str], previous: str, taken: set[str]) -> str:
    """A name for a group, from its values and the literal before it."""
    if all(is_year(value) for value in values):
        name = "year"
    elif previous == "year" and all(value.isdigit() and int(value) <= 53 for value in values):
        name = "issue"
    else:
        words = [word for word in re.findall(r"[a-z]+", previous.lower()) if len(word) > 1]
        name = words[-1] if words and previous != "year" else ("number" if values[0].isdigit() else "name")
    unique, index = name, 2
    while unique in taken:
        unique, index = f"{name}{index}", index + 1
    taken.add(unique)
    return unique


def _part(values: list[str], previous: str, taken: set[str]) -> tuple[str, str]:
    """The pattern of one part of the URLs (a path segment or the query) at the same place,
    and the literal it ends with (the next group's hint)."""
    # A year stays a group even when all the links share it: the next listing has another.
    if len(set(values)) == 1 and not is_year(values[0]):
        return escape(values[0]), values[0]
    tokens = [TOKEN.findall(value) for value in values]
    columns = list(zip(*tokens)) if len({len(t) for t in tokens}) == 1 else []
    # Numbers within otherwise equal text (report-2024-q1.pdf): a group for each number.
    if columns and all(len(set(c)) == 1 or all(v.isdigit() for v in c) for c in columns):
        out, hint = [], previous
        for column in columns:
            if len(set(column)) == 1 and not is_year(column[0]):
                out.append(escape(column[0]))
                hint = column[0]
                continue
            name = _name(list(column), hint, taken)
            out.append(f"(?P<{name}>\\d{{4}})" if name.startswith("year") else f"(?P<{name}>\\d+)")
            hint = name
        return "".join(out), hint
    suffixes = {posixpath.splitext(value)[1] for value in values}
    name = _name(values, previous, taken)
    if len(suffixes) == 1 and suffixes != {""}:
        return f"(?P<{name}>[^/?]+){escape(suffixes.pop())}", name
    return f"(?P<{name}>[^/?]+)", name


def propose(urls: list[str]) -> dict:
    """Proposes a pattern for links like these, with named groups for what differs between them.

    The links of the most common shape (number of path segments, with or without a query) are
    the ones described; the others are left out. Returns the pattern, its groups, a file name
    and tags made of them, and how many of the links it matches.
    """
    parsed = [urlparse(url) for url in urls if url]
    if not parsed:
        return {"pattern": "", "groups": [], "path": "{filename}", "tags": [], "matches": 0}
    shape = Counter((len(p.path.split("/")), bool(p.query)) for p in parsed).most_common(1)[0][0]
    alike = [p for p in parsed if (len(p.path.split("/")), bool(p.query)) == shape]
    taken: set[str] = set()
    segments, previous = [], ""
    for column in zip(*(p.path.split("/") for p in alike)):
        part, previous = _part(list(column), previous, taken)
        segments.append(part)
    pattern = "/".join(segments)
    if shape[1]:
        query, previous = _part([p.query for p in alike], previous, taken)
        pattern += r"\?" + query
    pattern += "$"
    groups = re.findall(r"\(\?P<(\w+)>", pattern)
    path = "{filename}"
    if not posixpath.splitext(alike[0].path)[1]:
        # Links without a file name (a page per item, say): named after the link's text.
        path = "{text}"
    if "year" in groups:
        path = "{year}/" + path
    tags = ["{year}"] if "year" in groups else []
    matches = sum(1 for url in urls if url and re.search(pattern, url))
    return {"pattern": pattern, "groups": groups, "path": path, "tags": tags, "matches": matches}


# Values from the page around a link (a level's "values", harvester.recipe): each has a name, where
# its text is (the link itself, or an element near it, or a JSON item's field), a pattern that
# narrows the text, and a type. Their names are a pattern group's names (a template names them).
VALUE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,30}$")
VALUE_TYPES = ("text", "number", "date")
MAX_VALUES = 10
# The longest text of the page a value's pattern reads: a pattern runs with a time limit, and
# the text near a link is a date or a title, not a page.
VALUE_TEXT = 1000

# Month names as dates are written in English, German and Polish (both of Polish's forms, "listopad"
# and "2 listopada"; without diacritics too, as some sites write them), and their abbreviations.
_MONTH_NAMES = (
    "january jänner januar jan styczeń stycznia styczen sty",
    "february februar feb luty lutego lut",
    "march märz maerz mär mrz marzec marca mar",
    "april kwiecień kwietnia kwiecien apr kwi",
    "may mai maj maja",
    "june juni czerwiec czerwca jun cze",
    "july juli lipiec lipca jul lip",
    "august sierpień sierpnia sierpien aug sie",
    "september wrzesień września wrzesien wrzesnia sept sep wrz",
    "october oktober październik października pazdziernik pazdziernika oct okt paź paz",
    "november listopad listopada nov lis",
    "december dezember grudzień grudnia grudzien dec dez gru",
)
MONTHS = {name: number for number, names in enumerate(_MONTH_NAMES, 1) for name in names.split()}
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
# The dates a value of type date is read from, the first that is in the text: numbers year first
# (2026-11-02, 2026/11/02), numbers day first (02.11.2026, 2/11/2026: day first, as written in
# Germany and Poland), a day and a month's name (2. November 2026, 2 listopada 2026), a month's name
# and a day (November 2, 2026), a month's name and a year alone (listopad 2026).
_DATES = (
    ("ymd", re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")),
    ("dmy", re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")),
    ("d month y", re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH})\.?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE)),
    ("month d y", re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE)),
    ("month y", re.compile(rf"\b({_MONTH})\.?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE)),
)
# The pattern proposed for each of them: it finds the date in the text around it.
DATE_PATTERNS = {
    "ymd": r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
    "dmy": r"(?P<date>\d{1,2}[./]\d{1,2}[./]\d{4})",
    "d month y": r"(?P<date>\d{1,2}\.?\s+[^\W\d_]+\.?,?\s+\d{4})",
    "month d y": r"(?P<date>[^\W\d_]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})",
    "month y": r"(?P<date>[^\W\d_]+\.?,?\s+\d{4})",
}


def clean_text(text: str, limit: int = 100) -> str:
    """Text of the page as a part of a file name or a tag: one line, without the characters a
    file name may not hold (a slash would make a folder), shortened."""
    return re.sub(r"[\\/:*?\"<>|\s]+", " ", text).strip()[:limit].strip()


def parse_date(text: str) -> tuple[str, tuple[int, int, int | None]] | None:
    """The first date in the text, in one of _DATES's forms: its form and (year, month, day); the
    day None for a month and a year alone. None when there is none (or it is no real date)."""
    for kind, pattern in _DATES:
        for match in pattern.finditer(text):
            parts = match.groups()
            if kind == "ymd":
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            elif kind == "dmy":
                day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            elif kind == "d month y":
                day, month, year = int(parts[0]), MONTHS[parts[1].lower()], int(parts[2])
            elif kind == "month d y":
                month, day, year = MONTHS[parts[0].lower()], int(parts[1]), int(parts[2])
            else:
                month, year, day = MONTHS[parts[0].lower()], int(parts[1]), 1
            try:
                datetime.date(year, month, day)
            except ValueError:
                continue
            if 1900 <= year <= 2100:
                return kind, (year, month, None if kind == "month y" else day)
    return None


def _typed(name: str, kind: str, text: str, given: dict) -> dict:
    """A value of its type: a date as YYYY-MM-DD (YYYY-MM without a day) with its year, month and
    day where the link's other values have none; a number as one; text cleaned for a file name."""
    if kind == "date":
        found = parse_date(text)
        if not found:
            return {}
        year, month, day = found[1]
        values: dict = {name: f"{year:04d}-{month:02d}" + (f"-{day:02d}" if day else "")}
        parts = {"year": year, "month": month} | ({"day": day} if day else {})
        return values | {key: value for key, value in parts.items() if key not in given and key != name}
    if kind == "number":
        digits = re.sub(r"\D", "", text)
        return {name: int(digits)} if digits else {}
    cleaned = clean_text(text)
    return {name: cleaned} if cleaned else {}


def page_values(specs: list[dict], texts: list, search: Callable[[str, str], Any], given: dict) -> dict:
    """The values a link's page gives (specs: the level's "values"; texts: what each found near the
    link, None where it found nothing): each spec's pattern searched in its text (with search, a
    person's pattern's time limit) and its value of its type. The pattern's named groups are values
    too; the value is the group of its own name, else the first group, else the whole match. A value
    whose text or pattern finds nothing is missing: a file name naming it fails, a tag is left out.
    given: the link's values so far (a date's year does not replace the address's)."""
    values: dict = {}
    for spec, text in zip(specs, texts):
        if text is None:
            continue
        text = re.sub(r"\s+", " ", str(text)).strip()[:VALUE_TEXT]
        name, pattern = str(spec.get("name", "")), str(spec.get("regex") or "")
        if pattern:
            match = search(pattern, text)
            if not match:
                continue
            groups = match.groupdict()
            for group, found in groups.items():
                if found is not None and group != name:
                    values[group] = int(found) if found.isdigit() else clean_text(found)
            text = groups.get(name) or (match.group(1) if match.re.groups and match.group(1) else match.group(0))
        values |= _typed(name, str(spec.get("type") or "text"), text or "", given | values)
    return values


def propose_value(texts: list[str]) -> dict:
    """A value for texts like these (what was picked near each link): a date when most hold one in
    the same form, a number when they are numbers, else text; its name, pattern and type."""
    filled = [re.sub(r"\s+", " ", text).strip() for text in texts if text and text.strip()]
    if not filled:
        return {"name": "title", "regex": "", "type": "text"}
    forms = Counter(found[0] for found in map(parse_date, filled) if found)
    if forms and forms.most_common(1)[0][1] * 2 >= len(filled):
        return {"name": "date", "regex": DATE_PATTERNS[forms.most_common(1)[0][0]], "type": "date"}
    if all(re.fullmatch(r"[#№]?\s*\d[\d.,\s]*", text) for text in filled):
        return {"name": "number", "regex": r"(\d[\d.,]*)", "type": "number"}
    return {"name": "title", "regex": "", "type": "text"}
