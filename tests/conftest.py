# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every test shares: names of the test sites (*.example, RFC 2606) resolve to a public
address at once, without asking the network's DNS (browser.reachable looks names up)."""

import socket

import pytest

from harvester import browser

PUBLIC = "93.184.215.14"


def loopback_works() -> bool:
    """Whether connections on 127.0.0.1 work here: a sandbox may forbid them, CI and Docker do
    not (a listening socket, and a connection to it)."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        try:
            socket.create_connection(listener.getsockname(), timeout=2).close()
            return True
        except OSError:
            return False


# The tests that connect to servers of their own on 127.0.0.1 (the browser's guard).
needs_loopback = pytest.mark.skipif(
    not loopback_works(), reason="connections on 127.0.0.1 are not possible here (a sandbox): run them in CI or Docker"
)


@pytest.fixture(autouse=True)
def test_names_resolve_at_once(monkeypatch):
    """*.example and *.example.org/com resolve to a public address; others as they do."""
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        name = str(host).lower()
        if name.endswith((".example", ".example.org", ".example.com", ".test")):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    browser._resolves_public.cache_clear()  # pylint: disable=protected-access
    yield
    browser._resolves_public.cache_clear()  # pylint: disable=protected-access
