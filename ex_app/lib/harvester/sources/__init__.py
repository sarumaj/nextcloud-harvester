"""The sources a job can fetch from, by name."""

from .base import AuthError, Item, Option, Source, SourceError
from .site import FULL_PASS, Site

SOURCES: dict[str, type[Source]] = {source.name: source for source in (Site,)}

__all__ = ["FULL_PASS", "SOURCES", "AuthError", "Item", "Option", "Source", "SourceError"]
