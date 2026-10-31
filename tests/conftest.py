"""What every test shares: names of the test sites (*.example, RFC 2606) resolve to a public
address at once, without asking the network's DNS (browser.reachable looks names up)."""

import socket

import pytest

from harvester import browser

PUBLIC = "93.184.215.14"


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
