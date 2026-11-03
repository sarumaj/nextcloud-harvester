# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sources a job can fetch from, by name."""

from .base import (
    MARK,
    REFUSED,
    AuthError,
    ChallengeError,
    Denied,
    Item,
    Option,
    Refused,
    Source,
    SourceError,
    Stopped,
    WebPage,
)
from .feed import Feed
from .gallery import Gallery
from .mail import Mail
from .media import Media
from .releases import Releases
from .remote import Remote
from .site import Site
from .walk import FULL_PASS

SOURCES: dict[str, type[Source]] = {
    source.name: source for source in (Site, Mail, Feed, Media, Gallery, Remote, Releases)
}

__all__ = [
    "FULL_PASS",
    "MARK",
    "REFUSED",
    "SOURCES",
    "AuthError",
    "ChallengeError",
    "Denied",
    "Item",
    "Option",
    "Refused",
    "Source",
    "SourceError",
    "Stopped",
    "WebPage",
]
