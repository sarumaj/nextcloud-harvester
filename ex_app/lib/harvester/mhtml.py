# SPDX-License-Identifier: AGPL-3.0-or-later
"""A page saved as "html" (harvester.sources.keep): one HTML file with everything in it.

Chromium takes the snapshot (CDP's Page.captureSnapshot, the MHTML of "Save page as"): the page
as it shows it, after its scripts ran, with its stylesheets, images, fonts and frames, those of
other sites too, as the browser loaded them, and without scripts. MHTML itself opens only in
Chromium's browsers, and Nextcloud shows it as none; so single_html turns it into one HTML file
that every browser opens: each resource the snapshot holds becomes a data: URI where the page or
a stylesheet refers to it (src, srcset, poster, a stylesheet's url()), a stylesheet a <style>,
and a frame its <iframe>'s srcdoc. What the snapshot lacks keeps its address (absolute: the
snapshot's are). Only the standard library and BeautifulSoup.
"""

import base64
import email
import email.policy
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# How deep frames in frames are kept (deeper ones keep their address).
FRAME_DEPTH = 5
# A stylesheet's url(...), and its @import.
CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)
CSS_IMPORT = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.IGNORECASE)
# The attributes that name a resource, by element.
SOURCES = {
    "img": ("src",),
    "source": ("src",),
    "input": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "image": ("href", "xlink:href"),
    "use": ("href", "xlink:href"),
}
ICONS = ("icon", "shortcut icon", "apple-touch-icon")
# The encoding a page or a stylesheet names.
CHARSET = re.compile(rb"""charset\s*=?\s*["']?([A-Za-z0-9_-]{2,40})""", re.IGNORECASE)


class Snapshot:  # pylint: disable=too-few-public-methods
    """The resources of an MHTML snapshot by their address and Content-ID: (type, bytes)."""

    def __init__(self, data: bytes):
        message = email.message_from_bytes(data, policy=email.policy.compat32)
        self.location = str(message.get("Snapshot-Content-Location") or "")
        self.parts: dict[str, tuple[str, bytes]] = {}
        self.first = ""
        for part in message.walk():
            if part.is_multipart():
                continue
            kind = part.get_content_type()
            body = part.get_payload(decode=True) or b""
            for name in ("Content-Location", "Content-ID"):
                value = str(part.get(name) or "").strip()
                if not value:
                    continue
                key = "cid:" + value.strip("<>") if name == "Content-ID" else value
                self.parts.setdefault(key, (kind, body))
                if kind == "text/html" and not self.first:
                    self.first = key
        if self.location not in self.parts:
            self.location = self.first

    def get(self, url: str, base: str) -> tuple[str, bytes] | None:
        """The resource the page refers to by url (relative to base), if the snapshot has it."""
        url = url.strip()
        if not url or url.startswith("data:"):
            return None
        found = self.parts.get(url)
        if found is None:
            try:
                found = self.parts.get(urljoin(base, url))
            except ValueError:
                return None
        return found


def data_uri(kind: str, body: bytes) -> str:
    """A resource as a data: URI."""
    return f"data:{kind or 'application/octet-stream'};base64,{base64.b64encode(body).decode()}"


def _text(body: bytes) -> str:
    """A text part's content in the encoding it names (Chromium keeps the page's: a meta's charset,
    a stylesheet's @charset), else UTF-8."""
    named = CHARSET.search(body[:2048])
    encoding = named[1].decode("ascii") if named else "utf-8"
    try:
        return body.decode(encoding, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def inline_css(css: str, base: str, snapshot: Snapshot, depth: int = 0) -> str:
    """A stylesheet with the resources it refers to as data: URIs, and its @imports inlined."""

    def imported(match: re.Match) -> str:
        found = snapshot.get(match[2], base)
        if found is None or depth >= FRAME_DEPTH:
            return match[0]
        # An @import of a stylesheet the snapshot holds: its rules as they are, in its place.
        return "/* @import inlined */" + inline_css(_text(found[1]), urljoin(base, match[2]), snapshot, depth + 1)

    def url(match: re.Match) -> str:
        found = snapshot.get(match[2], base)
        if found is None:
            return match[0]
        if found[0] == "text/css":
            return match[0]
        return f'url("{data_uri(*found)}")'

    css = re.sub(r"@charset\s+['\"][^'\"]*['\"]\s*;", "", css, flags=re.IGNORECASE)
    css = CSS_IMPORT.sub(imported, css)
    return CSS_URL.sub(url, css)


def _srcset(value: str, base: str, snapshot: Snapshot) -> str:
    """A srcset with each candidate the snapshot holds as a data: URI."""
    candidates = []
    for candidate in value.split(","):
        parts = candidate.strip().split(None, 1)
        if not parts:
            continue
        found = snapshot.get(parts[0], base)
        parts[0] = data_uri(*found) if found else parts[0]
        candidates.append(" ".join(parts))
    return ", ".join(candidates)


def inline_html(html: str, base: str, snapshot: Snapshot, depth: int = 0) -> str:
    """A document of the snapshot with its resources inside it."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "noscript"]):
        tag.decompose()
    _inert(soup)
    for meta in soup.find_all("meta"):
        refused = ("content-security-policy", "content-type", "refresh")
        if str(meta.get("http-equiv") or "").lower() in refused or meta.has_attr("charset"):
            meta.decompose()
    _inline_attributes(soup, base, snapshot)
    _inline_links(soup, base, snapshot)
    for frame in soup.find_all(["iframe", "frame"]):
        source = str(frame.get("src") or "")
        found = snapshot.get(source, base)
        if found is None or found[0] != "text/html" or depth >= FRAME_DEPTH:
            continue
        frame["srcdoc"] = inline_html(_text(found[1]), _location(snapshot, source, base), snapshot, depth + 1)
        del frame["src"]
    _charset(soup)
    return str(soup)


def _inline_attributes(soup: BeautifulSoup, base: str, snapshot: Snapshot) -> None:
    """The resources the elements name (SOURCES, srcset, a style's url()) and the <style>s'."""
    for tag in soup.find_all(True):
        for name in SOURCES.get(tag.name, ()):
            found = snapshot.get(str(tag.get(name) or ""), base)
            if found:
                tag[name] = data_uri(*found)
        if tag.has_attr("srcset"):
            tag["srcset"] = _srcset(str(tag["srcset"]), base, snapshot)
        if tag.has_attr("style"):
            tag["style"] = inline_css(str(tag["style"]), base, snapshot)
    for style in soup.find_all("style"):
        style.string = inline_css(style.get_text(), base, snapshot)


def _inline_links(soup: BeautifulSoup, base: str, snapshot: Snapshot) -> None:
    """The stylesheets linked as <style>s, the icons as data: URIs."""
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        found = snapshot.get(str(link.get("href") or ""), base)
        if found is None:
            continue
        if "stylesheet" in rel:
            style = soup.new_tag("style")
            if link.get("media"):
                style["media"] = link["media"]
            style.string = inline_css(_text(found[1]), urljoin(base, str(link["href"])), snapshot)
            link.replace_with(style)
        elif rel in ICONS:
            link["href"] = data_uri(*found)


def _location(snapshot: Snapshot, source: str, base: str) -> str:
    """A frame's own address (its links and resources are relative to it), for one the snapshot
    names by its Content-ID: the part's Content-Location, where it has one."""
    if not source.startswith("cid:"):
        return urljoin(base, source)
    body = snapshot.parts.get(source)
    for key, value in snapshot.parts.items():
        if value is body and not key.startswith("cid:"):
            return key
    return base


# Attributes that hold an address a script could hide in (javascript:).
ADDRESS_ATTRIBUTES = ("href", "src", "action", "formaction", "xlink:href", "data", "poster")
# Kept pages never run anything: their own scripts are gone, and this policy stops what is left
# (frames as srcdoc inherit it).
POLICY = "script-src 'none'; object-src 'none'; base-uri 'none'"


def _inert(soup: BeautifulSoup) -> None:
    """Takes out what runs besides <script>: event handlers (onclick, onerror, …) and
    javascript: addresses. A saved page is opened from Nextcloud by the family; it shows, it
    never acts."""
    for tag in soup.find_all(True):
        for name in [name for name in tag.attrs if name.lower().startswith("on")]:
            del tag[name]
        for name in ADDRESS_ATTRIBUTES:
            value = tag.get(name)
            if isinstance(value, str) and "".join(value.split()).lower().startswith(("javascript:", "vbscript:")):
                del tag[name]


def _charset(soup: BeautifulSoup) -> None:
    """A <meta charset="utf-8"> first in the head (the file is written in UTF-8), then the
    policy that no script runs."""
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    head.insert(0, soup.new_tag("meta", attrs={"http-equiv": "Content-Security-Policy", "content": POLICY}))
    head.insert(0, soup.new_tag("meta", attrs={"charset": "utf-8"}))


def single_html(data: bytes, comment: str = "") -> bytes:
    """The MHTML snapshot as one HTML file (UTF-8) with its resources in it; comment, if given,
    goes first (where the page was saved from). ValueError for a snapshot without a page."""
    snapshot = Snapshot(data)
    page = snapshot.parts.get(snapshot.location)
    if page is None:
        raise ValueError("the snapshot holds no page")
    location = _location(snapshot, snapshot.location, snapshot.location)
    html = inline_html(_text(page[1]), location, snapshot)
    head = f"<!DOCTYPE html>\n<!-- {comment.replace('--', '- -')} -->\n" if comment else "<!DOCTYPE html>\n"
    return (head + re.sub(r"^\s*<!DOCTYPE[^>]*>\s*", "", html, flags=re.IGNORECASE)).encode("utf-8")
