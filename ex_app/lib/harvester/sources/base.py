"""What a source is: it lists the items a site offers and downloads one of them."""

import abc
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, ClassVar

import httpx


class SourceError(Exception):
    """The site refused or broke: the run stops and the user is told."""


class AuthError(SourceError):
    """The credentials were refused: running again will not help until they change."""


@dataclass(frozen=True)
class Item:
    """One file on the site."""

    # Stable across runs: the job records it once the file is downloaded.
    key: str
    # Relative to the job's folder in Nextcloud.
    path: str
    url: str
    # What the link gives (its pattern's groups, its text, ...): the job's tags are made of them.
    values: dict = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Option:
    """A setting of a job, shown in the job's form."""

    title: str
    default: str | int
    description: str = ""
    # How the job's form shows it: text, or recipe (the recorder's button and its JSON).
    kind: str = "text"


class Source(abc.ABC):
    """A site files are fetched from."""

    name: ClassVar[str]
    title: ClassVar[str]
    # The names of the job's credentials (kept per job in Nextcloud, encrypted).
    credentials: ClassVar[tuple[str, ...]] = ()
    options: ClassVar[dict[str, Option]] = {}
    # Seconds between two downloads, so as not to hammer the site.
    pause: ClassVar[float] = 0.0

    def __init__(self, options: dict, credentials: dict[str, str], client: httpx.Client | None):
        self.settings = {key: options.get(key, option.default) for key, option in self.options.items()}
        self.secrets = credentials
        self.client = client

    @abc.abstractmethod
    def items(self, seen: Callable[[str], bool]) -> Iterator[Item]:
        """Yields the items not seen yet, in the order they should be downloaded."""

    @abc.abstractmethod
    def fetch(self, item: Item, dest: BinaryIO) -> None:
        """Writes the item's content to dest."""

    def close(self) -> None:
        """Releases what the source holds (a browser); called after every run."""
