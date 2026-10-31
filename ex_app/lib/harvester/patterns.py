"""Link patterns: proposed from picked links, and the values their named groups give a file.

A pattern is a regular expression searched in a link's absolute URL. Its named groups
((?P<year>...)) become values for the download URL, the file's name and its tags, together
with the link's own: url, text, page, host, filename, stem and ext. A value of digits is a
number, so {issue:02d} works.
"""

import posixpath
import re
from collections import Counter
from string import Formatter
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


def render(template: str, values: dict) -> str:
    """Fills in the template; a value it names that is missing is an error naming it."""
    missing = [name for _, name, _, _ in Formatter().parse(template) if name and name not in values]
    if missing:
        raise KeyError(f"{template} names {', '.join(missing)}, which the link pattern does not give")
    return template.format(**values)


def render_path(template: str, values: dict) -> str:
    """The file's path from the template, without leaving the job's folder; each part without
    what Nextcloud refuses in a name (control characters, backslashes, leading or trailing
    spaces and trailing dots)."""
    path = posixpath.normpath("/" + render(template, values)).lstrip("/")
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
