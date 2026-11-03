# SPDX-License-Identifier: AGPL-3.0-or-later
"""Images picked as file links (an <img>, a <picture>): the address of the image at its full size.

A page often shows a placeholder in src (a grey pixel, a data: URI) until a script loads the real
image from a lazy-loading attribute (data-src, data-original, ...), and offers the image in
several sizes (srcset, a <picture>'s <source>s). The file is the largest of them: a candidate of
srcset by its width (640w), else by its density (2x); an attribute that names the full-size image
(data-full, data-zoom-image, ...) before that, the lazy-loading ones after it, and src last,
unless it is a placeholder.

INFO (JavaScript) reads an element's attributes in the browser, soup_info the same over HTTP;
best_image chooses, in Python for both.
"""

import re
from urllib.parse import urljoin, urlsplit

# An element's attributes as best_image reads them: the image's own, and its <picture>'s sources
# (null: no image). The text it is told by is its alt (or title).
INFO = """(e) => {
  const tag = (e.tagName || '').toLowerCase()
  const img = tag === 'img' ? e : tag === 'picture' ? e.querySelector('img') : null
  if (!img && tag !== 'picture') return null
  const parent = img && img.parentElement
  const box = tag === 'picture' ? e : parent && parent.tagName.toLowerCase() === 'picture' ? parent : null
  const attrs = (el) => el ? Object.fromEntries([...el.attributes].map((a) => [a.name, a.value])) : {}
  return { img: attrs(img), sources: box ? [...box.querySelectorAll('source')].map(attrs) : [], base: document.baseURI }
}"""
# Attributes that name the image at its full size (galleries, zoom), taken before srcset.
FULL = (
    "data-full",
    "data-full-src",
    "data-fullsrc",
    "data-zoom-image",
    "data-zoom-src",
    "data-large-file",
    "data-orig-file",
    "data-hi-res-src",
    "data-highres",
)
# Lazy loading's attributes: a set of candidates, and a single address, taken after srcset.
LAZY_SETS = ("data-srcset", "data-lazy-srcset", "data-original-set")
LAZY = ("data-src", "data-original", "data-lazy-src", "data-lazy", "data-url", "data-echo", "data-original-src")
# A placeholder's address: a data: URI, nothing, or a file named like one (blank.gif, 1x1.png,
# lazy-placeholder.svg; not lazy-3.png, an image of its own).
PLACEHOLDER = re.compile(
    r"(?:^|/)(?:(?:blank|spacer|transparent|pixel|empty|lazy|loading|loader|grey|gray|1x1)"
    r"(?:[_-]?(?:image|img|pixel|\d+x\d+))?|[^/]*placeholder[^/]*)\.(?:gif|png|svg|jpe?g|webp)$",
    re.IGNORECASE,
)
DESCRIPTOR = re.compile(r"^(\d+(?:\.\d+)?)([wx])$", re.IGNORECASE)
# The links of a selector in the browser: each one's address, text and (an image's) INFO.
LINKS = "els => els.map(e => [e.href || '', (e.innerText || '').trim(), (" + INFO + ")(e)])"


def parse_srcset(text: str) -> list[tuple[str, float, str]]:
    """The candidates of a srcset as the HTML standard reads it: (address, size, "w" or "x");
    one without a descriptor is 1x. An address may hold commas (w_200,h_100); a candidate ends at
    a comma after its address and descriptors."""
    found: list[tuple[str, float, str]] = []
    position, length = 0, len(text)
    while position < length:
        while position < length and (text[position].isspace() or text[position] == ","):
            position += 1
        start = position
        while position < length and not text[position].isspace():
            position += 1
        url = text[start:position]
        descriptors = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            start = position
            depth = 0
            while position < length and (text[position] != "," or depth):
                depth += {"(": 1, ")": -1}.get(text[position], 0)
                position += 1
            descriptors = text[start:position].strip()
        if not url:
            continue
        size, unit = 1.0, "x"
        for descriptor in descriptors.split():
            match = DESCRIPTOR.match(descriptor)
            if match:
                size, unit = float(match[1]), match[2].lower()
        found.append((url, size, unit))
    return found


def placeholder(url: str) -> bool:
    """Whether an image's address is no image of its own: empty, a data: or about: URI, or a file
    named like a placeholder (blank.gif, spacer.png, 1x1.gif)."""
    url = (url or "").strip()
    if not url or url.lower().startswith(("data:", "about:", "javascript:")):
        return True
    try:
        return bool(PLACEHOLDER.search(urlsplit(url).path))
    except ValueError:
        return True


def _absolute(url: str, base: str) -> str:
    """The address resolved against the page's, if it is a web address; else ""."""
    try:
        absolute = urljoin(base, url.strip())
    except ValueError:
        return ""
    return absolute if absolute.lower().startswith(("http://", "https://")) else ""


def _largest(sets: list[str]) -> str:
    """The largest candidate of the srcsets: by width where any has one, else by density."""
    candidates = [candidate for text in sets for candidate in parse_srcset(text)]
    widths = [candidate for candidate in candidates if candidate[2] == "w"]
    chosen = widths or candidates
    return max(chosen, key=lambda candidate: candidate[1])[0] if chosen else ""


def best_image(info: object, base: str = "") -> str:
    """The address of an image at its full size (INFO, soup_info), absolute; "" for an element
    that is no image, or has none but placeholders."""
    if not isinstance(info, dict):
        return ""
    img = info.get("img") if isinstance(info.get("img"), dict) else {}
    sources = [source for source in info.get("sources") or [] if isinstance(source, dict)]
    base = str(info.get("base") or base)
    sets = [str(img.get(name) or "") for name in ("srcset", *LAZY_SETS)]
    sets += [str(source.get(name) or "") for source in sources for name in ("srcset", *LAZY_SETS)]
    largest = _largest([text for text in sets if text.strip()])
    for url in [
        *(str(img.get(name) or "") for name in FULL),
        largest,
        *(str(img.get(name) or "") for name in LAZY),
        str(img.get("src") or ""),
    ]:
        if url and not placeholder(url) and (absolute := _absolute(url, base)):
            return absolute
    return ""


def alt(info: object) -> str:
    """The text an image is told by: its alt, else its title."""
    img = info.get("img") if isinstance(info, dict) and isinstance(info.get("img"), dict) else {}
    return str(img.get("alt") or img.get("title") or "").strip()


def link(href: object, text: object, info: object) -> tuple[str, str]:
    """A link as LINKS gives it, as the walk takes it: its address (an image's at its full size,
    best_image) and its text (an image's alt)."""
    address = href if isinstance(href, str) and href else best_image(info)
    return address, str(text or "") or alt(info)


def resolved(found: list) -> list[tuple[str, str]]:
    """Links as (address, text, INFO) as the walk and the recorder take them (link): (address,
    text); entries without INFO pass as they are."""
    return [link(entry[0], entry[1], entry[2] if len(entry) > 2 else None) for entry in found]


def _attributes(tag) -> dict:
    """A BeautifulSoup element's attributes as the browser gives them (a class list as text)."""
    return {key: " ".join(value) if isinstance(value, list) else str(value) for key, value in tag.attrs.items()}


def soup_info(tag) -> dict | None:
    """INFO for an element of a page read over HTTP (BeautifulSoup); None for one that is no image."""
    name = getattr(tag, "name", "")
    img = tag if name == "img" else tag.find("img") if name == "picture" else None
    if img is None and name != "picture":
        return None
    parent = img.parent if img is not None else None
    box = tag if name == "picture" else parent if parent is not None and parent.name == "picture" else None
    return {
        "img": _attributes(img) if img is not None else {},
        "sources": [_attributes(source) for source in box.find_all("source")] if box is not None else [],
    }
