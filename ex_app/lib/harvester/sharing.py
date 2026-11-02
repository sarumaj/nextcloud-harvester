"""Jobs shared with other people, as Nextcloud shares files: with users or groups, each share
with its permissions. A job stays its owner's: its account, its folder, its kept session and its
notifications are theirs alone, and it runs as them."""

from collections.abc import Awaitable, Callable

from .store import Job, Store

# The permissions of a share, combinable as Nextcloud's: VIEW always (the job, its log, what its
# browser saw), RUN (Run now, Pause, Resume, Stop), EDIT (its settings but the folder and the
# account), DELETE, RESHARE (share it on, with at most one's own permissions).
VIEW, RUN, EDIT, DELETE, RESHARE = 1, 2, 4, 8, 16
ALL = VIEW | RUN | EDIT | DELETE | RESHARE
NAMES = {"view": VIEW, "run": RUN, "edit": EDIT, "delete": DELETE, "reshare": RESHARE}
# How far shares made by sharees are followed back to the owner (a share on of a share on ...).
DEPTH = 5

Groups = Callable[[str], Awaitable[set[str]]]


def names(granted: int) -> list[str]:
    """The names of the permissions granted, in NAMES's order: ["view", "run"]."""
    return [name for name, bit in NAMES.items() if granted & bit]


def bits(chosen: list[str]) -> int:
    """The permissions named (unknown names are refused by the API's model), VIEW always."""
    return VIEW | sum(NAMES[name] for name in set(chosen) if name in NAMES)


async def permissions(store: Store, job: Job, user: str, groups: Groups, _seen: frozenset = frozenset()) -> int:
    """What the user may do with the job: ALL for its owner; else what its shares to them (or to
    a group of theirs) grant, each share made by a sharee limited to what that sharee may still
    do (a share on loses what its sharer lost, as the owner took it back or they left a group);
    0 for no access. The user's groups are asked for only when the job has a share to a group."""
    if user == job.user:
        return ALL
    if user in _seen or len(_seen) >= DEPTH:
        return 0
    granted = 0
    member_of: set[str] | None = None
    for share in store.shares(job.id or 0):
        if share["kind"] == "group":
            if member_of is None:
                member_of = await groups(user)
            if share["sharee"] not in member_of:
                continue
        elif share["sharee"] != user:
            continue
        given = share["permissions"] | VIEW
        if share["by"] != job.user:
            given &= await permissions(store, job, share["by"], groups, _seen | {user})
        granted |= given if given & VIEW else 0
    return granted


def visible_shares(store: Store, job: Job, user: str) -> list[dict]:
    """The job's shares the user manages: all of them for the owner, those they made for one who
    shares it on."""
    return [share for share in store.shares(job.id or 0) if user in (job.user, share["by"])]
