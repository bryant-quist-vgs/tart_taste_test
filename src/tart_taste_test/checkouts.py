"""Where a repository's checkouts are, found without trusting what an agent wrote.

The broker serves the main working tree the operator registered and the working
trees linked to it. Linked working trees are recorded in files under
`<repository>/.git/worktrees/`, which an agent can write, so a record is only a
claim. This module turns a claim into an open directory under the rules of
docs/protocol.md §4:

- a checkout root is opened one path component at a time from `/`, and no
  component may be a symbolic link, so the directory that is opened is the one
  the path names and cannot be swapped for another on the way;
- a linked working tree must be on a volume the repository's own path crosses.
  A directory on any other volume is looked at once (its device number is
  read) and then refused: it is not opened, and nothing below it is touched.
  That keeps the broker from walking into network and automounted file systems
  an agent could name to make it wait;
- a linked working tree must point back: its `.git` must be a regular file that
  names the record, compared as text. An agent cannot forge that in a directory
  it cannot write, so a record cannot aim the broker at someone else's checkout.

Nothing here runs Git or resolves an agent-chosen path through links.
"""

import contextlib
import os
import stat
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from tart_taste_test.registry import Repository

MAX_WORKTREES = 256
MAX_PATH_PARTS = 64
SMALL_FILE_BYTES = 4096
GITDIR_PREFIX = "gitdir: "
WORKTREE_RECORDS = ".git/worktrees"
# How the broker opens anything in a directory an agent can write (also used by
# `spool`): never through a symbolic link, and never blocking on a FIFO.
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class CheckoutError(Exception):
    """A checkout root cannot be opened under the rules, or does not point back."""


@dataclass(frozen=True)
class Linked:
    """One worktree record: where it says the working tree is, and what must hold."""

    root: Path
    # What the working tree's `.git` file must name: the record's own directory.
    back_reference: str
    # Devices of the directories on the repository's own path.
    devices: frozenset[int]


def open_path(path: Path, allowed: frozenset[int] | None = None) -> tuple[int, frozenset[int]]:
    """Open the directory `path` from `/` without following any link.

    Returns the descriptor and the devices of every directory on the way,
    `path` included. With `allowed`, a directory on any other device is refused
    without being opened: its device is read from its entry in the parent, and
    checked once more on the descriptor in case the entry was swapped between.
    """
    parts = path.parts
    if not path.is_absolute() or ".." in parts or len(parts) > MAX_PATH_PARTS:
        raise CheckoutError(f"{path} is not a plain absolute path")
    foreign = CheckoutError(f"{path} is not on a volume the repository's own path crosses")
    devices: set[int] = set()

    def unopened(error: OSError) -> CheckoutError:
        return CheckoutError(f"cannot open {path} without following a link: {error.strerror}")

    try:
        current = os.open(parts[0], DIR_FLAGS)
    except OSError as error:
        raise unopened(error) from None
    try:
        for name in (*parts[1:], None):
            device = os.fstat(current).st_dev
            if allowed is not None and device not in allowed:
                raise foreign
            devices.add(device)
            if name is None:
                break
            if allowed is not None:
                entry = os.stat(name, dir_fd=current, follow_symlinks=False)
                if entry.st_dev not in allowed:
                    raise foreign
            following = os.open(name, DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except OSError as error:
        os.close(current)
        raise unopened(error) from None
    except CheckoutError:
        os.close(current)
        raise
    return current, frozenset(devices)


def _read_small(name: str, directory: int) -> str | None:
    """The text of a small regular file in `directory`, or None. Never follows a link or blocks."""
    try:
        descriptor = os.open(name, READ_FLAGS, dir_fd=directory)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        data = os.read(descriptor, SMALL_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(data) > SMALL_FILE_BYTES or b"\x00" in data:
        return None
    return os.fsdecode(data).rstrip("\n") or None


def _lexical(base: str, text: str) -> str:
    """`text` as an absolute path, relative to `base`, normalised as text only."""
    return os.path.normpath(text if os.path.isabs(text) else os.path.join(base, text))


def _ignore(_message: str) -> None:
    return None


def linked_worktrees(
    repository: Path,
    first: Collection[str] = (),
    warn: Callable[[str], None] = _ignore,
) -> list[Linked]:
    """The working trees `repository` has records for. Each is a claim to verify.

    `repository` is the registered path, which is already resolved. The records
    named in `first` (those of working trees already served) are read by name,
    then at most `MAX_WORKTREES` more in the order the directory lists them, so
    records an agent adds can never push out a working tree that is being
    served. `warn` is told when records were left unread. The working tree's
    own side is checked by `open_linked`.
    """
    found: list[Linked] = []
    with contextlib.ExitStack() as stack:
        try:
            top, devices = open_path(repository)
            stack.callback(os.close, top)
            base = top
            for name in WORKTREE_RECORDS.split("/"):
                base = os.open(name, DIR_FLAGS, dir_fd=base)
                stack.callback(os.close, base)
            listed: list[str] = []
            with os.scandir(base) as entries:
                for entry in entries:
                    if len(listed) >= MAX_WORKTREES:
                        warn(
                            f"{repository} has more than {MAX_WORKTREES} worktree records; "
                            "working trees beyond that are served only once others are removed "
                            "(`git worktree prune`)"
                        )
                        break
                    listed.append(entry.name)
        except CheckoutError, OSError:
            return found
        names = list(dict.fromkeys(first))
        names += sorted(set(listed) - set(names))
        for name in names:
            record = f"{repository}/{WORKTREE_RECORDS}/{name}"
            try:
                inner = os.open(name, DIR_FLAGS, dir_fd=base)
            except OSError:
                continue
            try:
                forward = _read_small("gitdir", inner)
            finally:
                os.close(inner)
            if forward is None:
                continue
            dot_git = _lexical(record, forward)
            root = Path(os.path.dirname(dot_git))
            if os.path.basename(dot_git) != ".git" or root == repository:
                continue
            # Every record is kept, also when several name one root: a record
            # is only a claim, and a forged one must not hide the true one.
            found.append(Linked(root, record, devices))
    return found


Claim = tuple[Repository, Linked | None]


def served(
    repositories: list[Repository],
    held: Iterable[Linked] = (),
    warn: Callable[[str], None] = _ignore,
) -> dict[Path, list[Claim]]:
    """Every checkout to serve, with every claim to it: main working trees, then linked ones.

    A main working tree has one claim, marked `None`: the operator registered
    it, so it needs no proof and no record can take it over. A linked working
    tree has one claim per repository whose records name it, in registration
    order; a record is only a claim, so the caller serves the root for the
    first claim that `open_linked` accepts. A forged record in one repository
    therefore cannot take a working tree away from the repository it belongs
    to, and neither can a forged record beside the true one in the same
    repository. `held` are the linked working trees being served now; their
    records are read first.
    """
    wanted: dict[Path, list[Claim]] = {}
    for repository in repositories:
        wanted.setdefault(Path(repository.path), [(repository, None)])
    mains = set(wanted)
    known = [linked.back_reference.rpartition("/") for linked in held]
    for repository in repositories:
        records = f"{repository.path}/{WORKTREE_RECORDS}"
        first = [name for directory, _, name in known if directory == records and name]
        for linked in linked_worktrees(Path(repository.path), first, warn):
            if linked.root not in mains:
                wanted.setdefault(linked.root, []).append((repository, linked))
    return wanted


def open_linked(linked: Linked) -> int:
    """Open a linked working tree's root, or raise `CheckoutError`.

    The root is opened under the rules above, and its `.git` file, read through
    that same descriptor, must name the record the claim came from.
    """
    top, _ = open_path(linked.root, linked.devices)
    try:
        text = _read_small(".git", top)
        if text is None or not text.startswith(GITDIR_PREFIX):
            raise CheckoutError(f"{linked.root} has no .git file naming a worktree record")
        pointed = _lexical(str(linked.root), text[len(GITDIR_PREFIX) :])
        if pointed != linked.back_reference:
            raise CheckoutError(f"{linked.root} does not point back to {linked.back_reference}")
    except CheckoutError:
        os.close(top)
        raise
    return top
