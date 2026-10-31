"""The sources a job can fetch from, by name."""

from .base import AuthError, ChallengeError, Item, Option, Source, SourceError
from .feed import Feed
from .gallery import Gallery
from .mail import Mail
from .media import Media
from .releases import Releases
from .remote import Remote
from .site import FULL_PASS, Site

SOURCES: dict[str, type[Source]] = {
    source.name: source for source in (Site, Mail, Feed, Media, Gallery, Remote, Releases)
}

__all__ = ["FULL_PASS", "SOURCES", "AuthError", "ChallengeError", "Item", "Option", "Source", "SourceError"]
