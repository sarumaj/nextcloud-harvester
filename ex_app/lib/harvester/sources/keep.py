# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pages kept as files (harvester.sources.site, recipe: "save" and "capture"), in the job's
browser: content that a site offers as no file of its own.

Saved pages ("save"): a receipt, an order's confirmation, a payslip or an article that exists only
as a web page. Each page the file links lead to (an address, or what an element clicked shows) is
opened, settled (the network quiet, scrolled to its end so that lazy images load, as many rounds
as the level's "scroll" or SAVE_SCROLL) and saved as:

- pdf: Chromium's print (A4, with backgrounds), as "Save as PDF" makes it;
- html: one HTML file with its stylesheets, images, fonts and frames inside (harvester.mhtml:
  Chromium's MHTML snapshot, with each resource a data: URI), which every browser opens and which
  keeps the page as it was shown, after its scripts and without them;
- markdown: the main content as Markdown (harvester.pagetext, Readability's way).

A recipe's own "save" saves the start page itself (where the steps end, or a page sent to an
on-demand job). The file's name may hold {title}, the page's title (and {disposition}, its title
with the extension); the extension of what it is saved as is added where the name lacks it.

Captured files ("capture": media types, like application/pdf or image/*): the files of those
types each page loads (its answers, the frames' and popups' too), those it embeds without loading
them (<embed>, <object>, a frame's address: fetched with the browser's cookies), and blob:
addresses (read in the page). At most CAPTURE_FILES of a page, each at most CAPTURE_MOST and the
job's largest file; every one is checked as a download is (download.copy_checked) when it is
placed. A page whose files were all placed is marked (CAPTURED) and not opened again.
"""

import base64
import contextlib
import dataclasses
import fnmatch
import functools
import hashlib
import io
import mimetypes
import posixpath
import tempfile
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

from playwright.sync_api import Error as PlaywrightError

from .. import mhtml, pagetext, patterns
from . import clicks, download
from .base import MARK, ChallengeError, Item, Refused, SourceError, WebPage, nap
from .tabs import SHORT_IDLE, scroll_to_load, settle

# What a page is saved as: the extension, the type and how the file starts.
SAVES = {
    "pdf": (".pdf", "application/pdf", b"%PDF-"),
    "html": (".html", "text/html", b"<!DOCTYPE html>"),
    "markdown": (".md", "text/markdown", b"# "),
}
# The rounds a page to save is scrolled to its end when its level has no "scroll" of its own.
SAVE_SCROLL = 3
# A saved page at most (bytes), however large the job's largest file.
SAVE_MOST = 200 * download.MB
# Captured files: at most this many of a page, each at most this large (and the job's largest).
CAPTURE_FILES = 20
CAPTURE_MOST = 64 * download.MB
# A captured file is kept in memory up to this size, on the disk beyond (until it is placed).
SPOOL = 1024 * 1024
# The mark of a page whose captured files were all placed: not opened again.
CAPTURED = MARK + "captured:"
TOP = "() => window.scrollTo(0, 0)"
# The addresses a page embeds without the browser loading them (a PDF in an <embed>, which a
# headless browser shows no viewer for), and frames' that it did not load (lazy ones).
EMBEDDED = """() => [...document.querySelectorAll('embed[src], object[data], iframe[src], frame[src]')]
  .map((e) => e.src || e.data || '').filter((u) => /^https?:/i.test(u))"""
# The page's blob: addresses (an image, a link, a viewer's document) read in the page: their type,
# size and, for those that may be wanted (of a wanted type, or none given) and not too large,
# their bytes (base64).
BLOBS = """async ([types, most, count]) => {
  const wanted = (type) => !type || type === 'application/octet-stream' || types.some((t) =>
    t === type || (t.endsWith('/*') && type.startsWith(t.slice(0, -1))))
  const urls = new Set()
  for (const e of document.querySelectorAll('[src^="blob:"], [href^="blob:"], object[data^="blob:"]')) {
    urls.add(e.getAttribute('src') || e.getAttribute('href') || e.getAttribute('data'))
  }
  const found = []
  for (const url of [...urls].slice(0, count)) {
    try {
      const blob = await (await fetch(url)).blob()
      let data = null
      if (wanted(blob.type) && blob.size <= most) {
        data = await new Promise((done) => {
          const reader = new FileReader()
          reader.onload = () => done(String(reader.result).split(',', 2)[1] || '')
          reader.onerror = () => done(null)
          reader.readAsDataURL(blob)
        })
      }
      found.push({ url, type: blob.type, size: blob.size, data })
    } catch (error) {}
  }
  return found
}"""
# What a file starts with, for one that comes without a type (a blob's, an octet stream).
MAGIC = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
)


@dataclasses.dataclass(frozen=True)
class Opened:
    """A page that is open already: the start page a recipe's own "save" saves."""

    page: Any


@dataclasses.dataclass(frozen=True)
class Captured:
    """A file a page loaded: its content (a temporary file), type, name, address and what tells it
    apart across runs (its stable address; a blob's or data: URI's content's hash)."""

    file: Any = dataclasses.field(repr=False)
    content_type: str
    name: str
    address: str
    key: str


def saved_as(spec: dict) -> str:
    """What the pages of the file links (or a recipe's own "save") are saved as; "" for none."""
    kind = spec.get("save") if isinstance(spec, dict) else None
    return kind if kind in SAVES else ""


def problems(recipe: dict, settings: dict) -> list[str]:
    """The job's file types that refuse what its pages are saved as (a PDF for a job of images)."""
    kind = saved_as(recipe) or saved_as(recipe.get("links") or {})
    types = download.file_types(settings)
    if not kind or "any" in types or download.accepts(types, SAVES[kind][1], SAVES[kind][2]):
        return []
    return [f"the pages are saved as {kind}, which the job's file types ({', '.join(types)}) refuse"]


def with_extension(path: str, kind: str) -> str:
    """The path with the extension of what its page is saved as, unless it ends with it."""
    extension = SAVES[kind][0] if kind in SAVES else ""
    return path + extension if extension and not path.lower().endswith(extension) else path


def wanted(content_type: str, types: list[str]) -> bool:
    """Whether a file's type is one of the types to capture (image/* is any image's)."""
    kind = content_type.split(";")[0].strip().lower()
    return bool(kind) and any(fnmatch.fnmatchcase(kind, pattern.lower()) for pattern in types)


def sniffed(content_type: str, start: bytes) -> str:
    """A file's type: the one it came with, else (none, or an octet stream) by how it starts."""
    kind = content_type.split(";")[0].strip().lower()
    if kind and kind != "application/octet-stream":
        return kind
    return next((name for magic, name in MAGIC if start.startswith(magic)), kind)


@contextlib.contextmanager
def opened(driver: Any, where: Any, spec: dict, timeout: float) -> Iterator[Any]:
    """The page to keep, open and settled: one open already (Opened), what an element clicked
    shows (a click link's trigger: clicks.land, then back to the listing), or an address opened
    in a tab of its own (closed afterwards). A page that asks for a person is ChallengeError, a
    sign-in WebPage (signed out), an error SourceError."""
    if isinstance(where, Opened):
        page, done = where.page, None
    elif isinstance(where, tuple):
        listing, selector, index, text = where
        target = clicks.Target(listing, selector, index, text, driver.address(listing), spec, "")
        landing = clicks.land(target, timeout)
        page, done = landing.page, functools.partial(clicks.back, landing, driver.expand)
    else:
        page = driver.open(where, deep=True)
        done = functools.partial(driver.done, page)
    try:
        _check(driver, page)
        settle(page)
        scroll_to_load(page, int(spec.get("scroll") or SAVE_SCROLL))
        with contextlib.suppress(PlaywrightError):
            page.evaluate(TOP)
            settle(page, SHORT_IDLE)
        yield page
    finally:
        if done is not None:
            done()


def _check(driver: Any, page: Any) -> None:
    found = driver.challenge(page)
    if found:
        raise ChallengeError(*found)
    why = driver.trouble(page)
    if why == "a sign-in page":
        raise WebPage(f"{page.url} is a sign-in page, not the page to keep (signed out?)", login=True)
    if why.startswith("answered"):
        raise SourceError(f"{page.url} {why}")


def render(page: Any, kind: str) -> tuple[bytes, str]:
    """The page saved as kind (SAVES), and its title."""
    title = " ".join(str(page.title() or "").split())
    if kind == "pdf":
        return page.pdf(format="A4", print_background=True), title
    if kind == "html":
        session = page.context.new_cdp_session(page)
        try:
            snapshot = str(session.send("Page.captureSnapshot", {"format": "mhtml"})["data"])
        finally:
            with contextlib.suppress(PlaywrightError):
                session.detach()
        if len(snapshot) > SAVE_MOST:
            raise Refused(f"the page {page.url} is more than {SAVE_MOST // download.MB} MB as HTML")
        return mhtml.single_html(snapshot.encode("utf-8"), f"Saved by Harvester from {page.url}"), title
    text, found = pagetext.markdown(page.content(), page.url)
    return text.encode("utf-8"), title or found


def save(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    driver: Any, where: Any, dest: BinaryIO, settings: dict, spec: dict, kind: str, timeout: float
) -> tuple[str, str]:
    """Saves the page (opened) as kind into dest, checked as a download (its size, the job's
    types); the file's name ({disposition}: its title with the extension) and the page's title."""
    with opened(driver, where, spec, timeout) as page:
        data, title = render(page, kind)
        url = page.url
    if len(data) > SAVE_MOST:
        raise Refused(f"the page {url} is more than {SAVE_MOST // download.MB} MB as {kind}")
    download.copy_checked(io.BytesIO(data), dest, settings, SAVES[kind][1], what=f"the page {url} as {kind}")
    stem = download.safe_name(title)[:150] or posixpath.basename(url.split("?")[0].rstrip("/")) or "page"
    return download.safe_name(stem + SAVES[kind][0]) or "page" + SAVES[kind][0], title


@contextlib.contextmanager
def listening(context: Any) -> Iterator[list]:
    """The answers the context's pages get meanwhile (the page's, its frames', its popups')."""
    answers: list = []

    def on_response(response: Any) -> None:
        answers.append(response)

    context.on("response", on_response)
    try:
        yield answers
    finally:
        context.remove_listener("response", on_response)


def _spooled(data: bytes) -> Any:
    spooled = tempfile.SpooledTemporaryFile(max_size=SPOOL)  # pylint: disable=consider-using-with
    spooled.write(data)
    spooled.seek(0)
    return spooled


def _name(name: str, kind: str, number: int) -> str:
    """A captured file's name: the one it came with, else file-<n>; with its type's extension where
    it has none."""
    name = download.safe_name(name) or f"file-{number}"
    if not posixpath.splitext(name)[1]:
        name += mimetypes.guess_extension(kind) or ""
    return name


class Capture:  # pylint: disable=too-few-public-methods
    """The files of the wanted types a page loaded, embeds or holds as blobs (files())."""

    def __init__(self, page: Any, types: list[str], settings: dict, fetch: Callable[[str, BinaryIO], str]):
        self.page = page
        self.types = types
        self.most = min(CAPTURE_MOST, download.largest(settings))
        # fetch(url, dest) writes the file at an address, checked, and gives its type.
        self.fetch = fetch
        self.found: list[Captured] = []
        # The addresses taken (or tried), those the page loaded, and the files' contents' hashes.
        self.addresses: set[str] = set()
        self.loaded: set[str] = set()
        self.hashes: set[str] = set()

    def _keep(self, data: bytes, content_type: str, name: str, address: str) -> None:
        digest = hashlib.sha256(data).hexdigest()
        kind = sniffed(content_type, data[:16])
        if digest in self.hashes or not wanted(kind, self.types) or len(self.found) >= CAPTURE_FILES:
            return
        self.hashes.add(digest)
        stable = address if address.lower().startswith(("http://", "https://")) else ""
        key = patterns.stable_url(stable) if stable else "sha256:" + digest[:24]
        name = _name(name, kind, len(self.found) + 1)
        self.found.append(Captured(_spooled(data), kind, name, address, key))

    def _fetched(self, url: str) -> None:
        """A file the page embeds (or that its answer gave no body of), fetched by its address."""
        spooled = tempfile.SpooledTemporaryFile(max_size=SPOOL)  # pylint: disable=consider-using-with
        with spooled:
            try:
                kind = self.fetch(url, spooled)
            except SourceError:
                return  # a page, not a file; refused; not there: not captured
            if spooled.tell() <= self.most:
                spooled.seek(0)
                self._keep(spooled.read(), kind, download.disposition_name({}, url), url)

    def answers(self, answers: list) -> None:
        """The answers of the wanted types (their bodies, else fetched again by address)."""
        for response in answers:
            with contextlib.suppress(PlaywrightError):
                url, headers = response.url, response.headers
                kind = headers.get("content-type", "")
                if url in self.addresses or not wanted(kind, self.types) or len(self.found) >= CAPTURE_FILES:
                    continue
                self.addresses.add(url)
                length = headers.get("content-length", "")
                if length.isdigit() and int(length) > self.most:
                    continue
                try:
                    body = response.body()
                except PlaywrightError:
                    # A file the browser did not keep (a PDF it took for a download): by its address.
                    self._fetched(url)
                    continue
                if len(body) <= self.most:
                    self._keep(body, kind, download.disposition_name(headers, url), url)

    def files(self, answers: list) -> list[Captured]:
        """The page's files of the wanted types: its answers', then what it embeds without them,
        then its blobs'."""
        self.loaded = {response.url for response in answers}
        self.answers(answers)
        with contextlib.suppress(PlaywrightError):
            for url in self.page.evaluate(EMBEDDED):
                if url not in self.addresses | self.loaded and len(self.found) < CAPTURE_FILES:
                    self.addresses.add(url)
                    self._fetched(url)
        with contextlib.suppress(PlaywrightError):
            left = CAPTURE_FILES - len(self.found)
            for blob in self.page.evaluate(BLOBS, [self.types, self.most, max(left, 0)]) if left > 0 else []:
                if blob.get("data"):
                    self._keep(base64.b64decode(blob["data"]), str(blob.get("type") or ""), "", str(blob["url"]))
        return self.found


def capture(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    driver: Any, where: Any, settings: dict, spec: dict, timeout: float, fetch: Callable[[str, BinaryIO], str]
) -> tuple[list[Captured], str, str]:
    """The files of the types spec captures that the page (opened) loads, embeds or holds as
    blobs; the page's title and address."""
    with listening(driver.context) as answers:
        with opened(driver, where, spec, timeout) as page:
            title = " ".join(str(page.title() or "").split())
            url = page.url
            found = Capture(page, list(spec.get("capture") or []), settings, fetch).files(list(answers))
    return found, title, url


def write(found: Captured, dest: BinaryIO, settings: dict) -> str:
    """Writes a captured file to dest, checked as a download; its name."""
    try:
        download.copy_checked(found.file, dest, settings, found.content_type, what=f"{found.name} ({found.address})")
    finally:
        found.file.close()
    return found.name


def captured_item(entry: Item, found: Captured, title: str, page: str, template: str) -> Item:
    """The item of a file captured on the page of the file link entry: keyed by the link's key and
    the file's; its values the link's, with the file's name ({filename}, {stem}, {ext},
    {disposition}) and the page's title."""
    stem, ext = posixpath.splitext(found.name)
    values = entry.values | {"filename": found.name, "stem": stem, "ext": ext.lstrip(".")}
    values |= {"disposition": found.name, "title": title}
    key = f"{entry.key.removeprefix(CAPTURED)}#{found.key}"
    try:
        path = patterns.render_path(template, values)
    except (KeyError, ValueError) as error:
        problem = SourceError(str(error).strip("'\""))
        return Item(key=key, path="", url=found.address, values=values, problem=problem, referer=page)
    return Item(key=key, path=path, url=found.address, values=values, referer=page, trigger=found)


def fetcher(opener: download.Opener, settings: dict) -> Callable[[str, BinaryIO], str]:
    """fetch for a Capture: a file at an address, through the driver's download (the browser's
    cookies), checked as a download; its type."""

    def fetch(url: str, dest: BinaryIO) -> str:
        told: dict[str, str] = {}
        download.fetch(
            opener,
            url,
            dest,
            settings,
            nap,
            told=lambda response: told.update(kind=response.headers.get("content-type", "")),
        )
        return told.get("kind", "")

    return fetch
