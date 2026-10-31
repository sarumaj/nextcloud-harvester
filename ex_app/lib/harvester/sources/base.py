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


class ChallengeError(SourceError):
    """The site asks for a person (a CAPTCHA, a "verify you are human" page): the job waits for its
    user to answer it in the recorder's browser, whose session the next run then uses."""

    def __init__(self, url: str, kind: str):
        super().__init__(f"{url} asks for a person ({kind}): continue in the browser")
        self.url = url
        self.kind = kind


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
    # What the source fetches, for the card that starts a job of it.
    description: ClassVar[str] = ""
    # The names of the job's credentials (kept per job in Nextcloud, encrypted). The page asks
    # for "password" and "token" hidden, and for "config" (several lines) in a text area.
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

    def restore(self, session: dict) -> None:
        """Starts from the session kept after the last run (or the user's answer to a challenge)."""

    def session(self) -> dict | None:
        """The browser session to keep for the next run (cookies, local storage), or None."""

    def close(self) -> None:
        """Releases what the source holds (a browser); called after every run."""
