"""Which waiting request runs next: first seen first, fairly across checkouts.

Agents choose their request IDs and write their own spools, so nothing here
trusts an ID for ordering, and nothing here grows with what an agent writes:

- a request's place is the order in which the broker first saw it;
- each checkout has at most `protocol.MAX_PENDING_REQUESTS` places. Further
  requests are not seen (and so not acknowledged) until earlier ones end;
- checkouts take turns: the checkout served longest ago goes first, so one
  checkout's backlog cannot hold up another's;
- a checkout gets at most one turn per `turn_interval`, which bounds how fast
  requests that end at once (refused ones) can fill the audit log;
- everything remembered is forgotten once the request is no longer in the spool.
"""

import time
from collections.abc import Callable, Mapping
from pathlib import Path

from tart_taste_test import protocol

Key = tuple[Path, str]


class FairQueue:
    def __init__(self, turn_interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._turn_interval = turn_interval
        self._clock = clock
        self._counter = 0
        # Place numbers of the requests waiting for a turn.
        self._places: dict[Key, int] = {}
        # Requests that were already too old when first seen.
        self._stale: set[Key] = set()
        # Requests that have had their turn. One that cannot be claimed or
        # answered (a hostile spool) stays in its spool; it is never retried.
        self._ended: set[Key] = set()
        # Per checkout: the counter value and the time of its latest turn.
        self._turns: dict[Path, tuple[int, float]] = {}

    def observe(
        self, unclaimed: Mapping[Path, list[str]], is_stale: Callable[[Path, str], bool]
    ) -> None:
        """Take in the unclaimed request IDs now present in each served spool."""
        present = {(root, request_id) for root, ids in unclaimed.items() for request_id in ids}
        self._places = {key: place for key, place in self._places.items() if key in present}
        self._stale &= present
        self._ended &= present
        self._turns = {root: turn for root, turn in self._turns.items() if root in unclaimed}
        for root, ids in unclaimed.items():
            held = sum(1 for other, _ in self._places if other == root)
            for request_id in ids:
                key = (root, request_id)
                if key in self._places or key in self._ended:
                    continue
                if held >= protocol.MAX_PENDING_REQUESTS:
                    break
                held += 1
                self._counter += 1
                self._places[key] = self._counter
                if is_stale(root, request_id):
                    self._stale.add(key)

    def order(self) -> list[Key]:
        """Every waiting request, in the order they will run."""
        lanes: dict[Path, list[Key]] = {}
        for key in sorted(self._places, key=lambda key: self._places[key]):
            lanes.setdefault(key[0], []).append(key)
        roots = sorted(lanes, key=lambda root: (self._turn(root), self._places[lanes[root][0]]))
        depth = max((len(lane) for lane in lanes.values()), default=0)
        return [
            lanes[root][row] for row in range(depth) for root in roots if row < len(lanes[root])
        ]

    def _turn(self, root: Path) -> int:
        return self._turns[root][0] if root in self._turns else 0

    def next(self) -> Key | None:
        """The request to run now, or None when none may run yet."""
        now = self._clock()
        for key in self.order():
            turn = self._turns.get(key[0])
            if turn is None or now - turn[1] >= self._turn_interval:
                return key
        return None

    def take(self, key: Key) -> bool:
        """Give `key` its turn. Returns whether it was already too old when first seen."""
        self._places.pop(key, None)
        self._ended.add(key)
        self._counter += 1
        self._turns[key[0]] = (self._counter, self._clock())
        return key in self._stale

    def __len__(self) -> int:
        return len(self._places)
