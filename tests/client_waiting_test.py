"""client_waiting_test.py — the client while it waits, built against contract v1.

What the client does when no verdict arrives in the ordinary way: no broker,
a broker that goes silent, a wait that runs out, an interrupt, a failure of its
own, and `--detach`. The broker is played by the test (client_helpers.py).

Coverage (contracts/taste_test_contract.md behaviours):
  - Load-bearing: no broker -> exit 6 and the request is withdrawn, never left:
    after the acknowledgement window, a short --wait-timeout, or an interrupt
  - Load-bearing: withdrawal loses to a claim, and the claimed request is kept
  - Load-bearing: an interrupt, and an internal error, never give 0 or 1
  - a standard error that cannot be written does not change the exit code
  - a live broker that does not take the request is reported as busy, not missing
  - acknowledged then silent broker -> 5; wait timeout -> 8; --detach -> 8
"""

import io
import os
import threading
import time

import pytest
from client_helpers import FAST, NO_LIMITS, beat, console, finish, status, wait
from conftest import World

from tart_taste_test import client, client_spool, exitcodes, protocol
from tart_taste_test.exitcodes import Exit


# Load-bearing: a request must not sit unserved and run later.
def test_no_broker_exits_6_and_withdraws_the_request(world: World) -> None:
    con, out, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 5.0, False, FAST)
    assert code is Exit.NOT_RUN
    assert list(world.paths.requests.iterdir()) == []
    lines = err.getvalue().splitlines()
    assert "no broker is serving this checkout" in lines[-2]
    assert lines[-1].startswith("taste-test: outcome=not-run exit=6 id=")
    assert out.getvalue() == b""


# Load-bearing: an unacknowledged request does not outlive a short --wait-timeout.
def test_wait_timeout_before_any_acknowledgement_withdraws_the_request(world: World) -> None:
    patient = client.Timing(poll=0.01, ack_window=60.0, stale=15.0, broker_lost=60.0)
    con, _, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 0.05, False, patient)
    assert code is Exit.NOT_RUN
    assert list(world.paths.requests.iterdir()) == []
    lines = err.getvalue().splitlines()
    assert "--wait-timeout" in lines[-2] and "withdrawn" in lines[-2]
    assert lines[-1].startswith("taste-test: outcome=not-run exit=6 id=")


def test_wait_timeout_with_a_live_broker_blames_the_timeout_not_the_queue(world: World) -> None:
    beat(world)
    patient = client.Timing(poll=0.01, ack_window=60.0, stale=15.0, broker_lost=60.0)
    con, _, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 0.05, False, patient)
    assert code is Exit.NOT_RUN
    reason = err.getvalue().splitlines()[-2]
    assert "--wait-timeout ran out before the broker serving this checkout took" in reason
    assert "requests waiting" not in reason and "register" not in reason


# Load-bearing: the rename makes withdrawal and claim mutually exclusive.
def test_withdrawal_loses_to_a_claim_and_the_claimed_request_is_kept(world: World) -> None:
    request_id = world.submit()
    home = world.paths.request_dir(request_id)
    os.rename(home / protocol.REQUEST_FILE, home / protocol.CLAIMED_FILE)  # the broker's claim
    assert client_spool.withdraw(world.paths, request_id) is False
    assert (home / protocol.CLAIMED_FILE).is_file()
    assert (home / protocol.SOURCE_FILE).is_file()

    # The client keeps waiting for a claimed request instead of calling it not-run.
    code, _, err = wait(world, request_id, wait_timeout=0.4)
    assert code is Exit.PENDING
    assert (home / protocol.CLAIMED_FILE).is_file()
    assert "no broker" not in err

    finish(world, request_id, "failed", 2, output=b"out\n")
    code, out, _ = wait(world, request_id)
    assert code is Exit.FAILED and out == "out\n"


def test_withdrawing_twice_or_without_a_spool_takes_nothing_back(world: World) -> None:
    assert client_spool.withdraw(world.paths, "20261115T183000Z-3f9a1c2b4d5e") is False
    request_id = world.submit()
    assert client_spool.withdraw(world.paths, request_id) is True
    assert not world.paths.request_dir(request_id).exists()
    assert client_spool.withdraw(world.paths, request_id) is False


def test_acknowledgement_arriving_with_the_withdrawal_is_reported_as_such(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = client_spool.withdraw

    def late_acknowledgement(paths: client_spool.SpoolPaths, request_id: str) -> bool:
        status(world, request_id, "queued", "next")
        return real(paths, request_id)

    monkeypatch.setattr(client, "withdraw", late_acknowledgement)
    con, _, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 5.0, False, FAST)
    assert code is Exit.NOT_RUN
    assert "acknowledged the request at the moment it was withdrawn" in err.getvalue()
    assert "no broker is serving" not in err.getvalue()


def interrupt_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("tart_taste_test.client.time.sleep", interrupted)


def test_interrupt_before_acknowledgement_withdraws_and_exits_6(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupt_the_wait(monkeypatch)
    con, _, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 5.0, False, FAST)
    assert code is Exit.NOT_RUN
    assert list(world.paths.requests.iterdir()) == []
    lines = err.getvalue().splitlines()
    assert "interrupted before a broker acknowledged" in lines[-2]
    assert lines[-1].startswith("taste-test: outcome=not-run exit=6 id=")


def test_interrupt_after_acknowledgement_exits_8_and_keeps_the_request(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = world.submit()
    status(world, request_id, "running", "test")
    beat(world)
    interrupt_the_wait(monkeypatch)
    code, _, err = wait(world, request_id)
    assert code is Exit.PENDING
    assert world.paths.request_dir(request_id).is_dir()
    assert f"taste-test wait {request_id}" in err
    assert err.splitlines()[-1] == f"taste-test: outcome=pending exit=8 id={request_id}"


def test_interrupt_before_submission_exits_6_and_leaves_no_request(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("tart_taste_test.snapshot.write_snapshot", interrupted)
    con, _, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 5.0, False, FAST)
    assert code is Exit.NOT_RUN
    assert list(world.paths.requests.iterdir()) == []
    lines = err.getvalue().splitlines()
    assert lines[-2].endswith("interrupted before the request was submitted; nothing was run")
    assert lines[-1] == "taste-test: outcome=not-run exit=6"


# Load-bearing: no failure to obtain a verdict exits 0 or 1.
def test_an_unexpected_failure_while_waiting_is_exit_5_with_a_summary(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("boom")

    request_id = world.submit()
    monkeypatch.setattr(client, "read_status", broken)
    code, _, err = wait(world, request_id)
    assert code is Exit.ERROR
    lines = err.splitlines()
    assert "internal error (RuntimeError: boom)" in lines[-2]
    assert lines[-1] == f"taste-test: outcome=error exit=5 id={request_id}"


class _ClosedText(io.StringIO):
    def write(self, _text: str) -> int:
        raise BrokenPipeError


# Load-bearing: `2>&1 | head` must not change the exit code (decision D9).
def test_a_standard_error_that_cannot_be_written_does_not_change_the_code(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    silenced: list[object] = []
    monkeypatch.setattr(exitcodes, "silence", silenced.append)
    request_id = world.submit()
    finish(world, request_id, "cancelled", reason="stopped", output=b"out\n")
    err = _ClosedText()
    con = client.Console(io.BytesIO(), err)
    assert client.wait(world.paths, request_id, con, NO_LIMITS, 5.0, FAST) is Exit.CANCELLED
    assert silenced == [err]  # silenced once, then left alone


def test_live_broker_that_does_not_answer_is_reported_as_busy_not_missing(world: World) -> None:
    beat(world)  # serving this checkout, but the request never gets a place
    con, _, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 5.0, False, FAST)
    assert code is Exit.NOT_RUN
    assert list(world.paths.requests.iterdir()) == []
    reason = err.getvalue().splitlines()[-2]
    assert "a broker is serving this checkout but did not take the request" in reason
    assert "no broker is serving" not in reason and "register" not in reason


def test_stale_heartbeat_alone_does_not_count_as_a_broker(world: World) -> None:
    beat(world, age=3600)
    con, _, _ = console()
    assert client.run(world.paths, "test", [], con, NO_LIMITS, 5.0, False, FAST) is Exit.NOT_RUN


def test_acknowledged_request_with_a_silent_broker_is_an_error_and_is_kept(world: World) -> None:
    request_id = world.submit()
    status(world, request_id, "running", "test")
    beat(world, age=3600)
    code, _, err = wait(world, request_id)
    assert code is Exit.ERROR
    assert "the broker stopped responding" in err
    assert err.splitlines()[-1] == f"taste-test: outcome=error exit=5 id={request_id}"
    assert world.paths.request_dir(request_id).exists()


def test_wait_timeout_exits_8_and_leaves_the_request_running(world: World) -> None:
    request_id = world.submit()
    status(world, request_id, "running", "test")
    beat(world)
    code, _, err = wait(world, request_id, wait_timeout=0.1)
    assert code is Exit.PENDING
    assert f"taste-test wait {request_id}" in err
    assert err.splitlines()[-1] == f"taste-test: outcome=pending exit=8 id={request_id}"
    assert world.paths.request_dir(request_id).exists()


def test_detach_prints_the_id_and_exits_8_once_acknowledged(world: World) -> None:
    beat(world)

    def acknowledge() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for name in os.listdir(world.paths.requests):
                status(world, name, "queued", "next")
                return
            time.sleep(0.01)

    helper = threading.Thread(target=acknowledge)
    helper.start()
    con, out, err = console()
    code = client.run(world.paths, "test", [], con, NO_LIMITS, 5.0, True, FAST)
    helper.join()
    [request_id] = os.listdir(world.paths.requests)
    assert code is Exit.PENDING  # never 0: 0 means the tests passed
    assert out.getvalue().decode() == request_id + "\n"
    assert err.getvalue().splitlines()[-1] == f"taste-test: outcome=pending exit=8 id={request_id}"


def test_detach_reports_a_request_that_has_already_finished_in_full(world: World) -> None:
    request_id = world.submit()
    finish(world, request_id, "failed", 2, output=b"out\n")
    con, out, err = console()
    code = client.await_and_report(world.paths, request_id, con, NO_LIMITS, 5.0, FAST, detach=True)
    assert code is Exit.FAILED
    assert out.getvalue() == b"out\n"
    assert "outcome=failed exit=1 command-exit=2" in err.getvalue().splitlines()[-1]
