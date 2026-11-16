"""fairqueue_test.py — which waiting request runs next.

Coverage:
  - Load-bearing: order is first seen first, never by the agent-chosen ID
  - checkouts take turns; the one served longest ago goes first
  - a checkout has a bounded number of places; the rest are not seen yet
  - one turn per interval per checkout; other checkouts are not held up
  - a request that had its turn is never offered again while it stays in the spool
  - Load-bearing: nothing is remembered about a request that left its spool
  - staleness is judged once, when a request is first seen
"""

from pathlib import Path

import pytest

from tart_taste_test import protocol
from tart_taste_test.fairqueue import FairQueue

A, B = Path("/checkouts/a"), Path("/checkouts/b")


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def never(_root: Path, _request_id: str) -> bool:
    return False


def queue(interval: float = 0.0) -> tuple[FairQueue, Clock]:
    clock = Clock()
    return FairQueue(interval, clock), clock


def test_order_is_first_seen_not_by_id() -> None:
    q, _ = queue()
    q.observe({A: ["m"]}, never)
    q.observe({A: ["a", "m", "z"]}, never)  # "a" sorts first but was seen later
    assert q.order() == [(A, "m"), (A, "a"), (A, "z")]
    assert q.next() == (A, "m") and len(q) == 3


def test_checkouts_take_turns() -> None:
    q, _ = queue()
    q.observe({A: ["a1", "a2", "a3"]}, never)
    q.observe({A: ["a1", "a2", "a3"], B: ["b1"]}, never)
    assert q.order() == [(A, "a1"), (B, "b1"), (A, "a2"), (A, "a3")]
    q.take((A, "a1"))
    q.observe({A: ["a2", "a3"], B: ["b1"]}, never)
    assert q.next() == (B, "b1")  # A has just had a turn
    q.take((B, "b1"))
    q.observe({A: ["a2", "a3"], B: ["b2"]}, never)
    assert q.order() == [(A, "a2"), (B, "b2"), (A, "a3")]


def test_places_per_checkout_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_PENDING_REQUESTS", 2)
    q, _ = queue()
    flood = [f"r{n}" for n in range(10)]
    q.observe({A: flood, B: ["b1"]}, never)
    assert len(q) == 3 and (B, "b1") in q.order()
    q.take((A, "r0"))
    q.observe({A: flood[1:], B: ["b1"]}, never)  # a place came free
    assert [key for key in q.order() if key[0] == A] == [(A, "r1"), (A, "r2")]


def test_one_turn_per_interval_per_checkout() -> None:
    q, clock = queue(interval=5.0)
    q.observe({A: ["a1", "a2"], B: ["b1"]}, never)
    q.take((A, "a1"))
    q.observe({A: ["a2"], B: ["b1"]}, never)
    assert q.next() == (B, "b1")  # B is not held up by A's interval
    q.take((B, "b1"))
    q.observe({A: ["a2"], B: []}, never)
    assert q.next() is None and len(q) == 1  # A must wait
    clock.now += 5.0
    assert q.next() == (A, "a2")


def test_request_that_had_its_turn_is_not_offered_again() -> None:
    q, _ = queue()
    q.observe({A: ["stuck"]}, never)
    q.take((A, "stuck"))
    for _ in range(3):  # it could not be claimed, so it is still in the spool
        q.observe({A: ["stuck"]}, never)
        assert q.next() is None and len(q) == 0


# Load-bearing: the broker's memory does not grow with what agents write.
def test_nothing_is_remembered_about_requests_that_left() -> None:
    q, _ = queue()
    for batch in range(200):
        ids = [f"batch{batch}-{n}" for n in range(protocol.MAX_PENDING_REQUESTS)]
        q.observe({A: ids}, lambda _root, _id: True)
        for request_id in ids:
            q.take((A, request_id))
    q.observe({A: []}, never)
    q.observe({}, never)
    assert len(q) == 0
    assert (q._places, q._stale, q._ended, q._turns) == ({}, set(), set(), {})


def test_staleness_is_judged_once_when_first_seen() -> None:
    q, _ = queue()
    asked: list[str] = []

    def stale(_root: Path, request_id: str) -> bool:
        asked.append(request_id)
        return request_id == "old"

    q.observe({A: ["old", "new"]}, stale)
    q.observe({A: ["old", "new"]}, stale)
    assert sorted(asked) == ["new", "old"]
    assert q.take((A, "old")) is True
    assert q.take((A, "new")) is False
