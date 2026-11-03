# SPDX-License-Identifier: AGPL-3.0-or-later
"""Requests that present themselves as Chrome does (its TLS handshake, HTTP/2 settings and
headers), for a job's own downloads with "Impersonate a browser" (base.IMPERSONATE): sites behind
Cloudflare and similar checks answer a plain HTTP client's TLS with 403.

CurlTransport is httpx's transport over curl_cffi: httpx keeps everything but the wire. It
follows the redirects (each hop a request of its own, so the guarded client's check runs on
every one), keeps the cookies, sends the job's headers and takes the answer in chunks; curl_cffi
only sends one request at a time, without redirects or a cookie jar of its own, through the
job's proxy (http(s) or socks, as curl takes them).
"""

from collections.abc import Iterator
from typing import Any

import httpx
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_errors

# The browser curl_cffi presents itself as: its newest Chrome.
BROWSER = "chrome"
# The request's headers curl sets itself: its own Host and length, the encodings it decodes.
CURL_HEADERS = ("host", "content-length", "accept-encoding", "connection", "transfer-encoding")
# The answer's headers of what curl already undid (a compressed body comes decoded).
DECODED = ("content-encoding", "transfer-encoding")


def curl_session() -> Any:
    """A curl_cffi session impersonating the browser."""
    return curl_requests.Session(impersonate=BROWSER)


class CurlStream(httpx.SyncByteStream):
    """An answer's body from curl_cffi, in chunks as they come."""

    def __init__(self, answer: Any):
        self.answer = answer

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self.answer.iter_content()
        except curl_errors.RequestException as error:
            raise httpx.ReadError(str(error)) from error

    def close(self) -> None:
        self.answer.close()


class CurlTransport(httpx.BaseTransport):
    """httpx's transport over a curl_cffi session that impersonates the browser."""

    def __init__(self, proxy: str = "", user_agent: str = ""):
        self.proxy = proxy
        # The app's own user agent gives the impersonation away: curl's browser's goes instead.
        self.user_agent = user_agent
        self.session = curl_session()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in CURL_HEADERS and not (key.lower() == "user-agent" and self.giveaway(value))
        }
        timeout = request.extensions.get("timeout") or {}
        try:
            answer = self.session.request(
                request.method,
                str(request.url),
                headers=headers,
                data=request.read() or None,
                allow_redirects=False,
                stream=True,
                proxy=self.proxy or None,
                timeout=(timeout.get("connect") or 30, timeout.get("read") or 120),
                discard_cookies=True,
            )
        except curl_errors.Timeout as error:
            raise httpx.TimeoutException(str(error), request=request) from error
        except curl_errors.RequestException as error:
            raise httpx.ConnectError(str(error), request=request) from error
        decoded = any(key.lower() == "content-encoding" for key, _ in answer.headers.multi_items())
        kept = [
            (key, value)
            for key, value in answer.headers.multi_items()
            if key.lower() not in DECODED and not (decoded and key.lower() == "content-length")
        ]
        return httpx.Response(answer.status_code, headers=kept, stream=CurlStream(answer), request=request)

    def giveaway(self, agent: str) -> bool:
        """Whether a user agent is the app's or httpx's own, not a browser's."""
        return agent == self.user_agent or agent.startswith("python-httpx/")

    def close(self) -> None:
        self.session.close()
