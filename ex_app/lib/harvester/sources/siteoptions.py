# SPDX-License-Identifier: AGPL-3.0-or-later
"""The website source's account fields and options (harvester.sources.site: Site), apart from
its code: what the job's form offers and explains.
"""

from . import download
from .base import CONSENT, IMPERSONATE, RECHECK, STEALTH, Option

# The account's fields a recipe may use, and what the form says of each.
CREDENTIALS = ("username", "password", "totp", "cookies", "code_mailbox")
CREDENTIAL_HELP = {
    "username": (
        "Username",
        "Filled into the recorded sign-in's username field; without recorded steps, sent to sites that ask with "
        "the browser's own sign-in box (HTTP Basic).",
    ),
    "password": ("Password", "Filled into the recorded sign-in's password field (or sent with HTTP Basic)."),
    "totp": (
        "One-time code's secret",
        "For a sign-in with an authenticator app: the secret it was given (the base32 text beside the QR code, "
        "or the QR code's otpauth:// link). The recorded code field gets the current code.",
    ),
    "cookies": (
        "Cookies",
        "A signed-in browser's cookies for the site, instead of a recorded sign-in: a cookies.txt export "
        "(Netscape format) or a Cookie header's name=value; name=value. They expire as they do in the browser.",
    ),
    "code_mailbox": (
        "Mailbox for codes",
        "For a sign-in that sends a code by e-mail (the step “type the code from e-mail”): the IMAP mailbox it "
        "arrives in, one setting per line: host = mail.example.org, user = me@example.org, password = …; also "
        "port (993), folder (INBOX), and auth = oauth2-google, oauth2-microsoft or oauth2 with token_url, "
        "client_id, client_secret, refresh_token. Without user and password, the job's Username and Password "
        "sign in. Read-only: nothing is marked as read.",
    ),
}

# The job's options: its recipe, how its pages are fetched, the files' addresses and names.
OPTIONS = {
    "recipe": Option(
        "Recipe", "", "Record it in the browser, or start from a template; the JSON can be edited.", kind="recipe"
    ),
    "browser": Option(
        "Use the browser",
        0,
        "Fetch the pages in Chromium even without steps (pages built by JavaScript).",
        kind="flag",
    ),
    "http1": Option(
        "HTTP/1.1 only",
        0,
        "For sites whose protection cuts the browser's connection (ERR_HTTP2_PROTOCOL_ERROR): the browser "
        "speaks the older HTTP/1.1 with them, also in the recorder.",
        kind="flag",
    ),
    # Images, fonts and videos left out of the job's pages (site.LIGHT): each would come through the
    # browser's routes, uncached (the routes turn Chromium's cache off), for nothing a walk reads.
    # On for new jobs, off for those saved before (they ran with them).
    "light": Option(
        "Light browser",
        1,
        "Skip images, fonts and videos while walking the site: the pages load faster and the server works "
        "less. The links of pictures are found all the same (by their addresses); a picture of a failed step "
        "shows the pages without them. Not for a disguised browser, nor for jobs that save pages or capture "
        "the files they load.",
        kind="flag",
        before=0,
    ),
    "stealth": STEALTH,
    "consent": CONSENT,
    "recheck": RECHECK,
    "download_url": Option(
        "Download URL",
        "{url}",
        "The file's address from the link's values, e.g. {url}/download.",
        kind="template",
        values=("filename", "stem", "ext", "text", "page", "host", "url"),
        suggestions=("{url}", "{url}/download", "{url}?download=1"),
    ),
    "path": Option(
        "File name",
        "{filename}",
        "Relative to the folder, from the link's values: {filename}, {stem}, {ext}, {text}, {page}, {host} and "
        "the pattern's groups, e.g. {year}/{text}; {disposition}: the name the site gives the file as it "
        "sends it.",
        kind="template",
        values=("filename", "stem", "ext", "text", "page", "host", "url", "disposition"),
        suggestions=("{filename}", "{text}{ext}", "{host}/{filename}", "{page}/{filename}"),
    ),
    **download.DOWNLOAD_OPTIONS,
    "impersonate": IMPERSONATE,
    "max_pages": Option(
        "Pages", 10, "At most this many pages of a listing.", minimum=1, suggestions=("1", "5", "10", "50")
    ),
    "stop_at_seen": Option(
        "Stop at known files",
        1,
        "Once the site went through completely, a listing of known files ends the run. It assumes the "
        "newest come first, or last: of the links to follow, the first and last are looked at, the new ones "
        "taken from the end that has them. A listing over several pages is taken to start with its newest "
        "page: for one whose first page is its oldest, turn this off.",
        kind="flag",
    ),
}
