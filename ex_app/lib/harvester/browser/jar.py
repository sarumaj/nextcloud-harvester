"""The cookies panel of the browser views (the recorder, Continue in the browser): the browser
context's cookies to look at, change, delete, add and take over.

The listing never carries a value: a cookie's value is often the sign-in itself, and goes to the
page only when the person asks to see that one (cookie-show) or to take them into the job's
account (cookies-export). Nothing here logs a value either. What the person adds or changes is
checked first: a name and value without control characters, sizes a browser keeps, and a domain
of the sites the browser is at or has cookies of (the page's host, a parent or a subdomain of
one): the panel is no way to plant cookies for arbitrary sites.

The messages from the page: {type: cookies, all} (the listing: the page's site's cookies, or all
of them), {type: cookie-show, name, domain, path}, {type: cookie-set, cookie, original} (add, or
change the original: a value of null keeps its value), {type: cookie-delete, name, domain, path},
{type: cookies-delete-site, domain} (all of a domain), {type: cookies-export, all} (cookies.txt of
the page's site, or of all); to the page: {type: cookies, host, all, groups: [{domain, cookies}]},
{type: cookie-value, name, domain, path, value}, {type: cookies-export, text, count}, {type:
cookies-error, message}.
"""

import re
import time
from urllib.parse import urlparse

from .. import cookies as cookie_jar

# What a browser keeps of a cookie: about 4 kB of name and value, a path of some length, and an
# expiry 400 days ahead at most (Chromium's limit since 2022).
MAX_SIZE = 4096
MAX_PATH = 1024
MAX_DAYS = 400
SAME_SITE = ("Strict", "Lax", "None")
# Characters a cookie's name or value never holds (control characters, and the header's
# separators); a name takes no "=" or white space either.
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
NAME_BAD = re.compile(r"[\s;,=]")
VALUE_BAD = re.compile(r"[;,]")
COMMANDS = ("cookies", "cookie-show", "cookie-set", "cookie-delete", "cookies-delete-site", "cookies-export")


class Refused(ValueError):
    """A cookie the panel does not set: what is wrong with it (never its value)."""


def host_of(url: str) -> str:
    """The page's host, "" for a page without one (about:blank)."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def bare(domain: str) -> str:
    """A cookie's domain without the leading dot that makes it its subdomains' too."""
    return domain.lower().lstrip(".")


def sent_to(cookie: dict, host: str) -> bool:
    """Whether the browser sends the cookie to the host: a host's own cookie to it alone, a
    domain's (".example.com") to it and its subdomains."""
    domain = str(cookie.get("domain", "")).lower()
    if not host or not domain:
        return False
    if domain.startswith("."):
        return host == domain[1:] or host.endswith(domain)
    return host == domain


def shown(cookie: dict) -> dict:
    """A cookie as the listing shows it: everything but its value (its length instead)."""
    return {
        "name": cookie["name"],
        "domain": cookie["domain"],
        "path": cookie.get("path", "/"),
        "expires": cookie.get("expires", -1) if (cookie.get("expires") or -1) > 0 else None,
        "secure": bool(cookie.get("secure")),
        "httpOnly": bool(cookie.get("httpOnly")),
        "sameSite": cookie.get("sameSite", "Lax"),
        "size": len(str(cookie.get("value", ""))),
    }


def listing(cookies: list[dict], host: str, everything: bool) -> list[dict]:
    """The cookies grouped by their domain (sorted, the page's own first), without values: those
    the page's host gets, or all."""
    groups: dict[str, list[dict]] = {}
    for cookie in cookies:
        if everything or sent_to(cookie, host):
            groups.setdefault(bare(cookie["domain"]), []).append(shown(cookie))

    def order(domain: str) -> tuple:
        # The page's host first, then its parents, then the rest by name.
        return (domain != host, not host.endswith("." + domain), domain)

    return [
        {"domain": domain, "cookies": sorted(groups[domain], key=lambda c: (c["name"], c["path"]))}
        for domain in sorted(groups, key=order)
    ]


def known_hosts(cookies: list[dict], urls: list[str]) -> set[str]:
    """The sites the browser is at (its pages' and frames' hosts) or has cookies of."""
    return {host for host in map(host_of, urls) if host} | {bare(c["domain"]) for c in cookies if c.get("domain")}


def domain_allowed(domain: str, hosts: set[str]) -> bool:
    """Whether a cookie may be set for the domain: one of the hosts, a parent of one (not a bare
    top-level domain) or a subdomain of one."""
    name = bare(domain)
    if not name or "." not in name or not re.fullmatch(r"[a-z0-9.-]+", name) or ".." in name:
        return False
    return any(name == host or host.endswith("." + name) or name.endswith("." + host) for host in hosts)


def checked(cookie: object, hosts: set[str], now: float) -> dict:
    """The cookie as Playwright's add_cookies takes it, once checked; Refused says what is
    wrong (never with the value)."""
    if not isinstance(cookie, dict):
        raise Refused("not a cookie")
    name, value = cookie.get("name"), cookie.get("value")
    domain, path = str(cookie.get("domain") or "").strip(), str(cookie.get("path") or "/")
    if not isinstance(name, str) or not name or CONTROL.search(name) or NAME_BAD.search(name):
        raise Refused("a name without spaces, control characters or ; , =")
    if not isinstance(value, str) or CONTROL.search(value) or VALUE_BAD.search(value):
        raise Refused(f"{name}: a value without control characters, ; or ,")
    if len(name.encode()) + len(value.encode()) > MAX_SIZE:
        raise Refused(f"{name}: name and value longer than {MAX_SIZE} bytes")
    if not path.startswith("/") or len(path) > MAX_PATH or CONTROL.search(path) or ";" in path:
        raise Refused(f"{name}: a path starts with / (at most {MAX_PATH} characters)")
    if not domain_allowed(domain, hosts):
        raise Refused(f"{name}: {domain or 'no domain'} is none of the sites this browser is at or has cookies of")
    same_site = cookie.get("sameSite") or "Lax"
    if same_site not in SAME_SITE:
        raise Refused(f"{name}: SameSite is Strict, Lax or None")
    secure = bool(cookie.get("secure"))
    if same_site == "None" and not secure:
        raise Refused(f"{name}: SameSite None needs Secure (browsers drop it otherwise)")
    result = {
        "name": name,
        "value": value,
        "domain": domain.lower(),
        "path": path,
        "secure": secure,
        "httpOnly": bool(cookie.get("httpOnly")),
        "sameSite": same_site,
    }
    expires = cookie.get("expires")
    if expires not in (None, "", -1):
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            raise Refused(f"{name}: the expiry is a time (seconds since 1970) or none (the session)")
        if not now < expires <= now + MAX_DAYS * 86400:
            raise Refused(f"{name}: the expiry is in the future, at most {MAX_DAYS} days ahead")
        result["expires"] = float(expires)
    return result


def export(cookies: list[dict]) -> str:
    """The cookies as a cookies.txt (Netscape's format, as harvester.cookies reads it)."""
    return cookie_jar.to_text(
        [
            cookie_jar.Cookie(
                name=c["name"],
                value=c.get("value", ""),
                domain=c["domain"],
                path=c.get("path") or "/",
                secure=bool(c.get("secure")),
                expires=int(c["expires"]) if (c.get("expires") or -1) > 0 else 0,
                http_only=bool(c.get("httpOnly")),
            )
            for c in cookies
        ]
    )


def same(cookie: dict, key: dict) -> bool:
    """Whether the cookie is the one the page names (its name, domain and path)."""
    return all(cookie.get(field) == key.get(field) for field in ("name", "domain", "path"))


async def handle(context, url: str, message: dict) -> list[dict]:
    """Does what the page asks of the cookies in the browser context (the page at url); the
    answers to send. A change is the context's at once (the pages' next requests carry it), and
    the listing follows it."""
    kind = message.get("type")
    host = host_of(url)
    everything = bool(message.get("all"))
    cookies = await context.cookies()
    try:
        if kind == "cookie-show":
            found = [c for c in cookies if same(c, message)]
            if not found:
                raise Refused("that cookie is gone")
            key = {field: message.get(field) for field in ("name", "domain", "path")}
            return [{"type": "cookie-value", **key, "value": found[0].get("value", "")}]
        if kind == "cookies-export":
            chosen = [c for c in cookies if everything or sent_to(c, host)]
            return [{"type": "cookies-export", "text": export(chosen), "count": len(chosen)}]
        if kind == "cookie-set":
            await _set(context, cookies, message, url)
        elif kind == "cookie-delete":
            if not any(same(c, message) for c in cookies):
                raise Refused("that cookie is gone")
            await context.clear_cookies(name=message["name"], domain=message["domain"], path=message["path"])
        elif kind == "cookies-delete-site":
            domain = bare(str(message.get("domain") or ""))
            # The domain's own and its leading-dot twin, nothing of its subdomains.
            await context.clear_cookies(domain=re.compile(rf"^\.?{re.escape(domain)}$", re.IGNORECASE))
    except Refused as refusal:
        return [{"type": "cookies-error", "message": str(refusal)}]
    groups = listing(await context.cookies(), host, everything)
    return [{"type": "cookies", "host": host, "all": everything, "groups": groups}]


async def _set(context, cookies: list[dict], message: dict, url: str) -> None:
    """Adds the cookie, or changes the original (another name, domain or path: the original goes)."""
    wanted = dict(message.get("cookie") or {}) if isinstance(message.get("cookie"), dict) else None
    original = message.get("original") if isinstance(message.get("original"), dict) else None
    before = next((c for c in cookies if original and same(c, original)), None)
    if original and before is None:
        raise Refused("that cookie is gone")
    if wanted is not None and wanted.get("value") is None and before is not None:
        # The value the page never saw stays as it is.
        wanted["value"] = before.get("value", "")
    pages = [frame.url for page in context.pages for frame in page.frames] + [url]
    cookie = checked(wanted, known_hosts(cookies, pages), time.time())
    if before is not None and not same(cookie, before):
        await context.clear_cookies(name=before["name"], domain=before["domain"], path=before["path"])
    await context.add_cookies([cookie])
