"""The spools the broker serves, kept equal to what the registry and the checkouts say.

One object owns the open spools. Everything about which checkouts are served is
decided here, under the rules of docs/protocol.md §4, and every amount an agent
could drive up is bounded here:

- a linked working tree is served for the first repository whose claim to it
  holds (`checkouts`), and only while it holds;
- at most `MAX_LINKED_SPOOLS` linked working trees of one repository are served
  at once, first come first served, because each served spool keeps three
  descriptors open. One that is being served keeps its place; main working
  trees do not count. The limit is per repository, so the working trees of one
  repository cannot take the places of another's, and a working tree over the
  limit is not opened at all;
- listing the spools for new requests takes at most `SCAN_BUDGET_SECONDS` per
  scan (plus one spool). Spools that were not reached keep their last listing
  and are read first next time, so a spool an agent has filled can delay how
  soon requests are noticed but cannot stall the broker's waits. Only the
  listing is bounded in time; finding and opening the checkouts (`refresh`) is
  bounded in the number of records, working trees and path components instead;
- a spool that stops being served while a request from it is in flight stays
  open until that request has its result, and the reason is kept in `lost`.
"""

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tart_taste_test import checkouts
from tart_taste_test.notices import Notices
from tart_taste_test.registry import RegistryError, Repository, State, load
from tart_taste_test.spool import Spool, SpoolError

MAX_LINKED_SPOOLS = 16
SCAN_BUDGET_SECONDS = 0.5
NOT_SERVED = (
    "the checkout stopped being served while this request ran (the repository was "
    "unregistered, or its worktree record was removed)"
)
SPOOL_GONE = "the checkout's spool was removed or replaced while this request ran"


@dataclass(frozen=True)
class _Held:
    repository: Repository
    spool: Spool
    linked: checkouts.Linked | None


class ServedSpools:
    def __init__(self, state: State, notices: Notices) -> None:
        self._state = state
        self._notices = notices
        self._held: dict[Path, _Held] = {}
        # The spool of the request in flight, and why it stopped being served.
        self.in_use: Spool | None = None
        self.lost: str | None = None
        # Spools no longer served but still needed by the request in flight.
        self._retired: list[Spool] = []
        # Per served root: its last listing, and when (a counter) it was read.
        self._listed: dict[Path, tuple[int, list[str]]] = {}
        self._reads = 0

    def get(self, root: Path) -> tuple[Repository, Spool] | None:
        held = self._held.get(root)
        return None if held is None else (held.repository, held.spool)

    def __iter__(self) -> Iterator[tuple[Repository, Spool]]:
        return iter([(held.repository, held.spool) for held in self._held.values()])

    # -- which spools ----------------------------------------------------------

    def _retire(self, root: Path, why: str) -> None:
        """Stop serving a spool. One a request is using stays open until that request ends."""
        spool = self._held.pop(root).spool
        # A listing belongs to the spool it was read from, not to the path.
        self._listed.pop(root, None)
        if spool is self.in_use:
            self.lost = self.lost or why
            self._retired.append(spool)
        else:
            spool.close()

    def refresh(self) -> None:
        """Serve the spool of every registered checkout that has one, and only those."""
        try:
            repositories = load(self._state)
        except RegistryError as error:
            self._notices.warn_once(str(error))
            return
        held = [entry.linked for entry in self._held.values() if entry.linked is not None]
        wanted = checkouts.served(repositories, held, self._notices.warn_once)
        for root in [root for root in self._held if root not in wanted]:
            self._gone(root, NOT_SERVED)
        for root, claims in wanted.items():
            try:
                self._serve(root, claims)
            except OSError as error:
                self._notices.warn_checkout(
                    claims[0][1] is not None, f"not serving {root}: {error.strerror or error}"
                )

    def _gone(self, root: Path, why: str) -> None:
        """Retire a spool and say so."""
        linked = self._held[root].linked is not None
        self._retire(root, why)
        self._notices.event_checkout(linked, f"no longer serving {root}")

    def _linked(self, repository: Repository) -> int:
        """How many linked working trees of `repository` are being served."""
        return sum(
            1
            for entry in self._held.values()
            if entry.linked is not None and entry.repository.key == repository.key
        )

    def _full(self) -> None:
        self._notices.warn_once(
            f"{MAX_LINKED_SPOOLS} linked working trees of one repository are being served, "
            "which is the most at once; others are served as those go away",
            key="linked working tree limit",
        )

    def _open(self, root: Path, claims: list[checkouts.Claim]) -> tuple[int, checkouts.Claim]:
        """Open `root` for the first claim that holds. Raises the first refusal otherwise."""
        refusal: checkouts.CheckoutError | None = None
        for claim in claims:
            linked = claim[1]
            try:
                if linked is None:
                    return checkouts.open_path(root)[0], claim
                return checkouts.open_linked(linked), claim
            except checkouts.CheckoutError as error:
                refusal = refusal or error
        raise refusal or checkouts.CheckoutError(f"{root} has no claim")

    def _serve(self, root: Path, claims: list[checkouts.Claim]) -> None:
        held = self._held.get(root)
        is_linked = claims[0][1] is not None
        if held is None and is_linked:
            # Not opened at all while every repository that claims it is at its limit.
            if all(self._linked(repository) >= MAX_LINKED_SPOOLS for repository, _ in claims):
                self._full()
                return
        try:
            top, (repository, linked) = self._open(root, claims)
        except checkouts.CheckoutError as error:
            if held is not None:
                self._retire(root, SPOOL_GONE)
            self._notices.warn_checkout(is_linked, f"not serving {root}: {error}")
            return
        try:
            if held is not None and held.spool.current(top):
                self._held[root] = _Held(repository, held.spool, linked)
                return
            if held is not None:
                self._gone(root, SPOOL_GONE)
            opened = Spool.open(root, top)
        except SpoolError as error:
            self._notices.warn_checkout(is_linked, f"not serving {root}: {error}")
            return
        finally:
            os.close(top)
        if opened is None:
            return
        if linked is not None and self._linked(repository) >= MAX_LINKED_SPOOLS:
            opened.close()
            self._full()
            return
        self._held[root] = _Held(repository, opened, linked)
        self._notices.event_checkout(linked is not None, f"serving {root}")

    # -- what is waiting ---------------------------------------------------------

    def unclaimed(self) -> dict[Path, list[str]]:
        """The unclaimed request IDs of every served spool, within the scan budget."""
        self._listed = {root: seen for root, seen in self._listed.items() if root in self._held}
        started = time.monotonic()
        longest_ago = sorted(self._held, key=lambda root: self._listed.get(root, (0, []))[0])
        for index, root in enumerate(longest_ago):
            if index and time.monotonic() - started >= SCAN_BUDGET_SECONDS:
                self._notices.warn_once(
                    f"listing the spools takes longer than {SCAN_BUDGET_SECONDS:g}s; new "
                    "requests are noticed later than usual (a spool with very many entries?)",
                    key="scan budget",
                )
                break
            try:
                ids = self._held[root].spool.unclaimed()
            except OSError:
                ids = []
            self._reads += 1
            self._listed[root] = (self._reads, ids)
        return {root: self._listed.get(root, (0, []))[1] for root in self._held}

    # -- the request in flight -----------------------------------------------------

    def release(self) -> None:
        """The request in flight has ended: forget it and close what only it still needed."""
        self.in_use = self.lost = None
        while self._retired:
            self._retired.pop().close()

    def close(self) -> None:
        self.release()
        for held in self._held.values():
            held.spool.close()
        self._held.clear()
