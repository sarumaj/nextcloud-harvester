"""Link patterns: proposed from picked links, and the values their named groups give a file.

A pattern is a regular expression searched in a link's absolute URL. Its named groups
((?P<year>...)) become values for the download URL, the file's name and its tags, together
with the link's own: url, text, page, host, filename, stem and ext. A value of digits is a
number, so {issue:02d} works.

A proposal leaves out what changes from visit to visit: a fragment (#page=2), the parameters
of a sign-in's or a visit's token (code, ticket, sid, a signature and its expiry, …) and of
tracking (utm_*, jsessionid): the pattern takes any or none of them. It reads a link's address
as the page wrote it (percent-encoded), an escape (%20) never part of a number.

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
from urllib.parse import unquote, urlparse, urlsplit

# A link's address in numbers and the text between them; an escape (%20 in Report%202024.pdf) is
# text, never the start of a number.
TOKEN = re.compile(r"\d+|(?:%[0-9A-Fa-f]{2}|%|[^\d%])+")
SPECIAL = re.compile(r"([.^$*+?{}\[\]\\|()])")


def escape(text: str) -> str:
    """The text as a literal in a pattern (only what a pattern would read otherwise escaped)."""
    return SPECIAL.sub(r"\\\1", text)


# Query parameters that carry a sign-in's or a visit's token (a one-time code, a ticket, a
# session, a signature and its expiry): spent after the visit, never the same in a job's run.
TOKEN_PARAMETERS = frozenset(
    "code ticket token sid session sessionid state sig signature expires jsessionid phpsessid access_token "
    "id_token auth_token".split()
)
# A file name that says nothing about the file (the same script serves them all, ?id=N telling
# them apart).
GENERIC_NAMES = re.compile(
    r"^(download|dl|get|getfile|file|files|attachment|fetch|view|open|show|index|default|document|doc)"
    r"(\.(php\d?|aspx?|jsp|cgi|pl|py|do|action|html?))?$",
    re.IGNORECASE,
)


def token_parameter(name: str) -> bool:
    """Whether a query parameter carries a token of the visit (TOKEN_PARAMETERS, X-Amz-*)."""
    name = unquote(name).lower()
    return name in TOKEN_PARAMETERS or name.startswith("x-amz-")


def volatile_parameter(name: str) -> bool:
    """Whether a query parameter changes from visit to visit (a token, or tracking: utm_*)."""
    return token_parameter(name) or unquote(name).lower().startswith("utm_")


def token_parameters(url: str) -> list[str]:
    """The names of the address's query parameters that carry a token of the visit."""
    query = urlparse(url).query
    return list(
        dict.fromkeys(part.split("=", 1)[0] for part in query.split("&") if token_parameter(part.split("=", 1)[0]))
    )


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


def _part(values: list[str], previous: str, taken: set[str], stop: str = "/?") -> tuple[str, str]:
    """The pattern of one part of the URLs (a path segment, the query or a parameter's value) at
    the same place, and the literal it ends with (the next group's hint); stop: the characters a
    group of text does not take (the part's end)."""
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
        return f"(?P<{name}>[^{stop}]+){escape(suffixes.pop())}", name
    return f"(?P<{name}>[^{stop}]+)", name


SESSION_PATH = re.compile(r";jsessionid=", re.IGNORECASE)


class _Link:  # pylint: disable=too-few-public-methods
    """A link's address as a proposal reads it: its path without a session's path parameter
    (;jsessionid=…), its query's parameters without the volatile ones, and what was left out."""

    def __init__(self, url: str):
        address, _, fragment = url.partition("#")
        # Split, not parsed: urlparse would take the last segment's ;jsessionid=… apart.
        parsed = urlsplit(address)
        self.fragment = bool(fragment)
        self.path = re.sub(r";jsessionid=[^/]*", "", parsed.path, flags=re.IGNORECASE)
        # The path's segments that carried a session (one each, at most).
        self.sessions = {index for index, part in enumerate(parsed.path.split("/")) if SESSION_PATH.search(part)}
        pairs = [part.partition("=") for part in parsed.query.split("&") if part]
        self.parameters = [(name, value) for name, _, value in pairs if not volatile_parameter(name)]
        self.dropped = len(self.parameters) < len(pairs)
        self.query = "&".join(f"{name}={value}" for name, value in self.parameters)

    @property
    def shape(self) -> tuple[int, bool]:
        """The number of the path's segments, and whether a query is left."""
        return len(self.path.split("/")), bool(self.parameters)


def _query(alike: list[_Link], previous: str, taken: set[str]) -> str:
    """The pattern of the links' queries (without the "?"): a parameter after the other, each
    value its own part, when the links have the same parameters; else the query as one part. With
    volatile parameters left out, any others may come before, between and after them."""
    loose = any(link.dropped for link in alike)
    names = {tuple(name for name, _ in link.parameters) for link in alike}
    if len(names) != 1:
        query, _ = _part([link.query for link in alike], previous, taken)
        return query + ("(?:&[^#]*)?" if loose else "")
    parts = []
    for index, name in enumerate(names.pop()):
        value, _ = _part([link.parameters[index][1] for link in alike], f"{name}=", taken, "&#")
        parts.append(f"{escape(name)}={value}")
    if not loose:
        return "&".join(parts)
    return "(?:[^&#]*&)*" + "&(?:[^&#]*&)*".join(parts) + "(?:&[^#]*)?"


def _file_name(alike: list[_Link], groups: list[str], texts: dict[str, str], urls: list[str]) -> tuple[str, bool]:
    """The proposed file name's template, and whether the links' own name is generic (the same
    script for all: download.php?id=N), the server's name for the file ({disposition}) then
    likely the better one."""
    names = {posixpath.basename(unquote(link.path)) for link in alike}
    generic = len(names) == 1 and (
        bool(GENERIC_NAMES.match(next(iter(names)))) or (len(alike) > 1 and len({link.query for link in alike}) > 1)
    )
    if not generic:
        # Links without a file name (a page per item, say): named after the link's text.
        return ("{filename}" if posixpath.splitext(alike[0].path)[1] else "{text}"), False
    shown = [texts.get(url, "").strip() for url in urls if url in texts]
    named = [group for group in groups if not group.startswith("year")]
    if (shown and all(shown) and len(set(shown)) == len(shown)) or not named:
        return "{text}", True
    return "-".join(f"{{{group}}}" for group in named), True


def _pattern(alike: list[_Link], query: bool) -> str:
    """The pattern of links of one shape: their path's segments (a session's path parameter
    allowed where they had one), their query, a fragment if they had any."""
    fragments = any(link.fragment for link in alike)
    sessions = set().union(*(link.sessions for link in alike))
    stop = "/?" + ("#" if fragments else "") + (";" if sessions else "")
    taken: set[str] = set()
    segments, previous = [], ""
    for index, column in enumerate(zip(*(link.path.split("/") for link in alike))):
        part, previous = _part(list(column), previous, taken, stop)
        segments.append(part + ("(?:;jsessionid=[^/?#]*)?" if index in sessions else ""))
    pattern = "/".join(segments)
    if query:
        pattern += r"\?" + _query(alike, previous, taken)
    elif any(link.dropped for link in alike):
        pattern += r"(?:\?[^#]*)?"
    return pattern + ("(?:#.*)?" if fragments else "") + "$"


def propose(urls: list[str], texts: list[str] | None = None) -> dict:
    """Proposes a pattern for links like these, with named groups for what differs between them.

    The links of the most common shape (number of path segments, with or without a query) are
    the ones described; the others are left out. Fragments and volatile query parameters
    (volatile_parameter) are no part of it. Returns the pattern, its groups, a file name (from
    the links' texts, given in the same order, when the links' own name is generic: "generic"
    then) and tags made of them, and how many of the links it matches.
    """
    links = [(url, _Link(url)) for url in urls if url]
    if not links:
        return {"pattern": "", "groups": [], "path": "{filename}", "tags": [], "matches": 0}
    shape = Counter(link.shape for _, link in links).most_common(1)[0][0]
    alike = [(url, link) for url, link in links if link.shape == shape]
    pattern = _pattern([link for _, link in alike], shape[1])
    groups = re.findall(r"\(\?P<(\w+)>", pattern)
    path, generic = _file_name(
        [link for _, link in alike], groups, dict(zip(urls, texts or [])), [url for url, _ in alike]
    )
    if "year" in groups:
        path = "{year}/" + path
    tags = ["{year}"] if "year" in groups else []
    matches = sum(1 for url in urls if url and re.search(pattern, url))
    proposal = {"pattern": pattern, "groups": groups, "path": path, "tags": tags, "matches": matches}
    if generic:
        proposal["generic"] = True
    return proposal


# Inline flags at a pattern's start that JavaScript has as flags of its own (a and u change
# nothing there: its \d and \w are ASCII's).
JS_FLAGS = {"i": "i", "m": "m", "s": "s", "a": "", "u": ""}


def js_regex(pattern: str) -> dict | None:  # pylint: disable=too-many-branches
    """The pattern as JavaScript's RegExp reads it ({"source", "flags"}), for the recorder to
    outline the links it matches in the page; None when it uses what JavaScript lacks or reads
    otherwise (scoped or verbose flags, atomic groups, possessive quantifiers, conditionals, a
    class starting with "]"): the matches then come from Python."""
    flags, index = "", 0
    while leading := re.match(r"\(\?([a-zA-Z]+)\)", pattern[index:]):
        if any(flag not in JS_FLAGS for flag in leading[1]):
            return None
        flags += "".join(JS_FLAGS[flag] for flag in leading[1])
        index += leading.end()
    out, in_class = [], False
    while index < len(pattern):
        char, rest = pattern[index], pattern[index:]
        if char == "\\":
            escaped = pattern[index + 1 : index + 2]
            if escaped in ("A", "Z") and not in_class:
                if "m" in flags:
                    return None
                out.append("^" if escaped == "A" else "$")
            else:
                out.append(pattern[index : index + 2])
            index += 2
            continue
        if in_class:
            in_class = char != "]"
            out.append(char)
            index += 1
            continue
        if char == "[":
            # Python reads []…] and [^]…] as a class holding "]", JavaScript [] as an empty one.
            if re.match(r"\[\^?\]", rest):
                return None
            in_class = True
            out.append(char)
        elif rest.startswith("(?P<"):
            out.append("(?<")
            index += 4
            continue
        elif reference := re.match(r"\(\?P=(\w+)\)", rest):
            out.append(f"\\k<{reference[1]}>")
            index += reference.end()
            continue
        elif comment := re.match(r"\(\?#[^)]*\)", rest):
            index += comment.end()
            continue
        elif re.match(r"\(\?([a-zA-Z-]|>|\()", rest) or (char in "*+?}" and rest[1:2] == "+"):
            return None
        else:
            out.append(char)
        index += 1
    return {"source": "".join(out), "flags": "".join(sorted(set(flags)))}


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
