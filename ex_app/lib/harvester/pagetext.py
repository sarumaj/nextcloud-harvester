# SPDX-License-Identifier: AGPL-3.0-or-later
"""A web page's main content as Markdown (a page saved as "markdown", harvester.sources.keep).

The main content is found the way Mozilla's Readability finds it, in short: what is no content
goes first (scripts, navigation, forms, hidden elements, and blocks whose class or id reads like a
menu, a sidebar, comments, sharing or advertising); then every paragraph of some length scores
for its parent (and half for its grandparent) by its length and its commas, a container's class
or id that reads like an article adds to its score and one like a sidebar takes from it, and its
links' share of its text takes from it at the end. The best container is the content; an
<article> or <main> alone on the page is taken as it is; without a container that scores, the
body. Then it is written as Markdown: headings, paragraphs, lists, quotes, code, tables, links
and images (at their full size, harvester.images), with addresses made absolute.

No dependency of its own: BeautifulSoup, which the app reads pages with anyway.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from . import images

# What is no content, wherever it is.
DROPPED = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "nav",
    "dialog",
)
# Blocks that read like no content by their class or id, and like content.
UNLIKELY = re.compile(
    r"comment|sidebar|footer|header|menu|nav|share|social|cookie|consent|banner|advert|\bads?\b|promo|related|"
    r"popup|modal|subscribe|newsletter|breadcrumb|pagination|sponsor|skip|toolbar",
    re.IGNORECASE,
)
LIKELY = re.compile(r"article|content|main|post|entry|body|text|story|blog|receipt|invoice|order", re.IGNORECASE)
# The elements whose text scores for their containers (Readability's), and the shortest that does.
SCORED = ("p", "pre", "td", "blockquote", "li", "dd")
SHORTEST = 25
BLOCKS = {"p", "div", "section", "article", "main", "header", "footer", "aside", "figure", "figcaption"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _hidden(tag: Tag) -> bool:
    style = str(tag.get("style") or "").replace(" ", "").lower()
    return (
        tag.has_attr("hidden")
        or str(tag.get("aria-hidden") or "").lower() == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def _label(tag: Tag) -> str:
    """A block's class and id, as Readability weighs them."""
    classes = tag.get("class") or []
    return " ".join([*(classes if isinstance(classes, list) else [str(classes)]), str(tag.get("id") or "")])


def _clean(root: Tag) -> None:
    """Takes out what is no content: DROPPED, hidden elements, and blocks that read like a menu
    or a sidebar (not those that read like content too, and never the body or an article)."""
    for tag in root.find_all(DROPPED):
        tag.decompose()
    for tag in root.find_all(True):
        if tag.decomposed:
            continue
        if _hidden(tag):
            tag.decompose()
            continue
        label = _label(tag)
        if (
            tag.name not in ("body", "article", "main", "html")
            and label.strip()
            and UNLIKELY.search(label)
            and not LIKELY.search(label)
        ):
            tag.decompose()


def _text_length(tag: Tag) -> int:
    return len(tag.get_text(" ", strip=True))


def _link_density(tag: Tag) -> float:
    """The share of a block's text in its links."""
    total = _text_length(tag)
    if not total:
        return 0.0
    return sum(_text_length(link) for link in tag.find_all("a")) / total


def _weight(tag: Tag) -> int:
    label = _label(tag)
    return (25 if LIKELY.search(label) else 0) - (25 if UNLIKELY.search(label) else 0)


def main_content(soup: BeautifulSoup) -> Tag:
    """The page's main content (after _clean): an <article> or <main> alone on the page, else the
    container its paragraphs score best for (Readability's way), else the body."""
    body = soup.body or soup
    for name in ("article", "main"):
        found = body.find_all(name)
        if len(found) == 1 and _text_length(found[0]) >= SHORTEST:
            return found[0]
    scores: dict[int, float] = {}
    tags: dict[int, Tag] = {}
    for paragraph in body.find_all(SCORED):
        text = paragraph.get_text(" ", strip=True)
        if len(text) < SHORTEST:
            continue
        score = 1 + text.count(",") + min(len(text) // 100, 3)
        for depth, ancestor in enumerate([paragraph.parent, paragraph.parent.parent if paragraph.parent else None]):
            if not isinstance(ancestor, Tag) or ancestor.name in ("html", "[document]"):
                continue
            if id(ancestor) not in scores:
                tags[id(ancestor)] = ancestor
                scores[id(ancestor)] = float(_weight(ancestor))
            scores[id(ancestor)] += score / (1 if depth == 0 else 2)
    if not scores:
        return body
    best = max(scores, key=lambda key: scores[key] * (1 - _link_density(tags[key])))
    return tags[best]


def _inline(node, base: str) -> str:  # pylint: disable=too-many-return-statements
    """A node's text with its inline markup as Markdown (links, images, emphasis, code)."""
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    if node.name == "br":
        return "  \n"
    if node.name in ("img", "picture"):
        info = images.soup_info(node)
        src = images.best_image(info, base)
        return f"![{images.alt(info)}]({src})" if src else ""
    inner = "".join(_inline(child, base) for child in node.children)
    if node.name == "a":
        href = str(node.get("href") or "").strip()
        address = urljoin(base, href) if href and not href.lower().startswith(("javascript:", "#")) else ""
        return f"[{inner.strip()}]({address})" if address and inner.strip() else inner
    if node.name in ("strong", "b") and inner.strip():
        return f"**{inner.strip()}**"
    if node.name in ("em", "i") and inner.strip():
        return f"*{inner.strip()}*"
    if node.name == "code" and inner.strip():
        return f"`{inner.strip()}`"
    return inner


def _cell(node: Tag, base: str) -> str:
    return re.sub(r"\s+", " ", _inline(node, base)).strip().replace("|", "\\|")


def _table(node: Tag, base: str) -> list[str]:
    """A table as Markdown's: its first row the head."""
    rows = [
        [_cell(cell, base) for cell in row.find_all(["th", "td"], recursive=False)]
        for row in node.find_all("tr")
        if row.find_parent("table") is node
    ]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    return lines + ["| " + " | ".join(row) + " |" for row in rows[1:]]


def _list(node: Tag, base: str, depth: int) -> list[str]:
    lines = []
    for number, item in enumerate(node.find_all("li", recursive=False), 1):
        mark = f"{number}." if node.name == "ol" else "-"
        nested = [child for child in item.children if isinstance(child, Tag) and child.name in ("ul", "ol")]
        text = "".join(_inline(child, base) for child in item.children if child not in nested)
        text = re.sub(r"\s+", " ", text).strip()
        lines.append("  " * depth + f"{mark} {text}")
        for child in nested:
            lines += _list(child, base, depth + 1)
    return lines


def _blocks(node: Tag, base: str) -> list[str]:  # pylint: disable=too-many-return-statements
    """A container's blocks as Markdown, each a paragraph of lines."""
    if node.name in HEADINGS:
        text = re.sub(r"\s+", " ", _inline(node, base)).strip()
        return [f"{'#' * HEADINGS[node.name]} {text}"] if text else []
    if node.name in ("ul", "ol"):
        return ["\n".join(_list(node, base, 0))]
    if node.name == "pre":
        code = node.get_text().strip("\n")
        return [f"```\n{code}\n```"] if code.strip() else []
    if node.name == "blockquote":
        inner = "\n\n".join(_blocks_of(node, base))
        return ["\n".join(f"> {line}".rstrip() for line in inner.splitlines())] if inner else []
    if node.name == "table":
        return ["\n".join(_table(node, base))] if _table(node, base) else []
    if node.name == "hr":
        return ["---"]
    return _blocks_of(node, base)


def _blocks_of(node: Tag, base: str) -> list[str]:
    """The blocks of a container's children: block elements each on their own, the inline ones
    between them joined into paragraphs."""
    blocks: list[str] = []
    inline: list[str] = []

    def flush() -> None:
        text = re.sub(r"[ \t]+", " ", "".join(inline)).strip()
        text = re.sub(r" *\n *", "\n", text)
        if text:
            blocks.append(text)
        inline.clear()

    for child in node.children:
        if isinstance(child, Tag) and (
            child.name in BLOCKS
            or child.name in HEADINGS
            or child.name in ("ul", "ol", "pre", "blockquote", "table", "hr", "dl", "dt", "dd", "li", "tr")
        ):
            flush()
            blocks += _blocks(child, base)
        else:
            inline.append(_inline(child, base))
    flush()
    return blocks


def title_of(soup: BeautifulSoup) -> str:
    """The page's title: its <title>, else its og:title, else its first heading."""
    found = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not found:
        meta = soup.find("meta", attrs={"property": "og:title"})
        found = str(meta.get("content") or "") if meta else ""
    if not found:
        heading = soup.find(["h1", "h2"])
        found = heading.get_text(" ", strip=True) if heading else ""
    return re.sub(r"\s+", " ", found).strip()


def markdown(html: str, url: str) -> tuple[str, str]:
    """The page's main content as Markdown, under its title and address; and its title."""
    soup = BeautifulSoup(html, "html.parser")
    title = title_of(soup)
    _clean(soup)
    content = main_content(soup)
    blocks = [block for block in _blocks(content, url) if block.strip()]
    # The content's own first heading may be the title again.
    if blocks and re.fullmatch(r"#{1,6} " + re.escape(title), blocks[0]):
        blocks = blocks[1:]
    head = [f"# {title}"] if title else []
    return "\n\n".join([*head, f"<{url}>", *blocks]).rstrip() + "\n", title
