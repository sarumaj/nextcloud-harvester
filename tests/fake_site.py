"""A fake magazine archive like heise's, for the site source and the recorder (no network).

/login (and /sso/login, heise's): a login form with heise's field names.
/select/mag/archiv: the years (newest first) and a recent issue.
/select/mag/archiv/<year>: its issues, newest first. .../<year>/<issue>/download: the PDF;
the first request for an issue redirects to a URL with wait_sec=3 (the site asking for a
pause). /list?p=<n>: three pages of a plain listing of PDFs with a next link. /pages?page=<n>:
four pages with numbered pages (‹ 1 [2] 3 4 ›, the current one marked). /buttons: three pages
drawn by a script, numbered buttons and no addresses. /feed: 35 links, ten more each time the
page is scrolled to its end.

With require_login, the archive answers the login form until the sign-in's cookie (sid=ok,
set by /session) comes with the request. Paths in challenge show a reCAPTCHA instead.
"""

# pylint: disable=too-many-return-statements,too-many-branches

import re
from urllib.parse import parse_qs, urlparse

SITE = "https://site.example"
YEARS = {2026: 2, 2025: 3, 2024: 3}
PDF = b"%PDF-1.7\n" + b"x" * 2000
CHALLENGE = b"""<html><head><title>One moment</title></head><body>
<div class="g-recaptcha" data-sitekey="x"></div><script src="https://www.google.com/recaptcha/api.js"></script>
</body></html>"""
LOGIN = """<html><body><form method="post" action="/session">
<input name="user" id="login-user" type="text"><input name="pass" id="login-password" type="password">
<button type="submit" name="rm_login">Sign in</button></form></body></html>"""


def archive(oldest_first: bool = False) -> str:
    """The archive: the years (newest first, or oldest first as some archives list them), and the
    newest issues (which the levels must not mistake)."""
    years = "".join(
        f'<li><a class="years__link" href="/select/mag/archiv/{year}">All of {year}</a></li>'
        for year in (sorted(YEARS) if oldest_first else YEARS)
    )
    recent = '<a class="year__link" href="/select/mag/archiv/2026/2">Mag 2/2026</a>'
    return f"<html><body><nav><a href='/'>Home</a></nav>{recent}<ul>{years}</ul></body></html>"


def year_page(year: int) -> str:
    """A year's issues, newest first."""
    issues = "".join(
        f'<li><a class="year__link" href="/select/mag/archiv/{year}/{n}">Mag {n}/{year}</a></li>'
        for n in range(YEARS[year], 0, -1)
    )
    return f"<html><body><a href='/select/mag/archiv'>Archive</a><ul class='issues'>{issues}</ul></body></html>"


def listing(page: int) -> str:
    """A page of a plain listing, with a link to the next."""
    files = "".join(
        f'<li class="item"><a class="file" href="/files/{page}-{n}.pdf">File {page}.{n}</a></li>' for n in (1, 2)
    )
    more = f'<a class="pager next" href="/list?p={page + 1}">Next</a>' if page < 3 else ""
    return f"<html><body><ul>{files}</ul><a href='/about'>About</a>{more}</body></html>"


def numbered(page: int, pages: int = 4) -> str:
    """A page of a listing whose pages are numbered links, the current one marked (aria-current
    and a class), with ‹ and › besides."""
    files = "".join(f'<li><a class="file" href="/files/p{page}-{n}.pdf">File {page}.{n}</a></li>' for n in (1, 2))
    numbers = "".join(
        f'<a class="page{" active" if n == page else ""}" href="/pages?page={n}"'
        f'{" aria-current=page" if n == page else ""}>{n}</a>'
        for n in range(1, pages + 1)
    )
    pager = f'<nav class="pager"><a href="/pages?page={max(page - 1, 1)}">‹</a>{numbers}<a href="/pages">›</a></nav>'
    return f"<html><body><ul>{files}</ul>{pager}</body></html>"


BUTTONS = """<html><body><ul id="files"></ul>
<div class="pager"><button>‹</button><button>1</button><button>2</button><button>3</button></div>
<script>
function show(n) {
  document.getElementById('files').innerHTML = [1, 2].map(
    (i) => `<li><a class="file" href="/files/b${n}-${i}.pdf">B ${n}.${i}</a></li>`).join('')
  document.querySelectorAll('.pager button').forEach((b) => { b.className = b.textContent === String(n) ? 'current' : '' })
}
document.querySelectorAll('.pager button').forEach((b) => b.addEventListener('click', () => {
  if (Number(b.textContent)) show(Number(b.textContent))
}))
show(1)
</script></body></html>"""

FEED = """<html><body><ul id="feed" style="margin:0"></ul>
<script>
let shown = 0
function more() {
  for (let i = 0; i < 10 && shown < 35; i++) {
    shown++
    const li = document.createElement('li')
    li.style.height = '120px'
    li.innerHTML = `<a class="post" href="/files/post-${shown}.pdf">Post ${shown}</a>`
    document.getElementById('feed').append(li)
  }
}
more()
window.addEventListener('scroll', () => {
  if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 50) setTimeout(more, 200)
})
</script></body></html>"""


class Site:  # pylint: disable=too-few-public-methods
    """The site's state: who signed in and which downloads were asked to wait."""

    def __init__(self, require_login: bool = False):
        self.waited: set[str] = set()
        self.requests: list[str] = []
        self.require_login = require_login
        self.challenge: set[str] = set()
        self.oldest_first = False

    def answer(self, url: str, method: str = "GET", cookie: str = "") -> tuple[int, dict, bytes]:
        """Status, headers and body for a request."""
        self.requests.append(url)
        parsed = urlparse(url)
        path, html = parsed.path, {"content-type": "text/html"}
        if path in self.challenge:
            return 403, html, CHALLENGE
        if path in ("/login", "/sso/login"):
            return 200, html, LOGIN.encode()
        if path == "/session" and method == "POST":
            # Not a 302: Playwright does not intercept the redirect of an answer it made up.
            headers = html | {"set-cookie": "sid=ok; Path=/"}
            return 200, headers, b"<script>location.href = '/select/mag/archiv'</script>"
        if self.require_login and path.startswith("/select/") and "sid=ok" not in cookie:
            return 200, html, LOGIN.encode()
        if path == "/select/mag/archiv":
            return 200, html, archive(self.oldest_first).encode()
        if path == "/pages":
            return 200, html, numbered(int(parse_qs(parsed.query).get("page", ["1"])[0])).encode()
        if path == "/buttons":
            return 200, html, BUTTONS.encode()
        if path == "/feed":
            return 200, html, FEED.encode()
        if path == "/list":
            return 200, html, listing(int(parse_qs(parsed.query).get("p", ["1"])[0])).encode()
        if path.startswith("/wait"):
            return 200, html, b"<html>please wait</html>"
        match = re.fullmatch(r"/select/mag/archiv/(\d{4})(?:/(\d+)(/download)?)?", path)
        if match and int(match[1]) in YEARS:
            if not match[2]:
                return 200, html, year_page(int(match[1])).encode()
            if not match[3]:
                return 200, html, b"<html>an issue</html>"
            if path not in self.waited:
                self.waited.add(path)
                return 302, {"location": f"{SITE}/wait?wait_sec=3"}, b""
            return 200, {"content-type": "binary/octet-stream"}, PDF
        if path.startswith("/files/"):
            return 200, {"content-type": "application/pdf"}, PDF
        return 404, {"content-type": "text/plain"}, b"not found"
