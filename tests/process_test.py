"""process_test.py — host subprocesses that are never left behind.

Coverage:
  - a child runs in a session of its own and its exit status is returned
  - Load-bearing: at the time limit the child and its own children are killed
  - Load-bearing: an exception from `tick` (how the broker abandons a request)
    stops the child before it continues
  - Load-bearing: a helper that outlives its parent is killed too when the call
    is ended early, as long as the parent has not been reaped
  - `tick` is called while waiting
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tart_taste_test import process


def gone(pid: int) -> bool:
    """Whether the process has ended (it may take a moment to be reaped)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def gone_now(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def with_grandchild(tmp_path: Path) -> tuple[list[str], Path]:
    """A command that starts a long-lived child of its own and records its PID."""
    record = tmp_path / "grandchild.pid"
    script = f'sleep 60 & echo $! > "{record}"; wait'
    return ["/bin/sh", "-c", script], record


def grandchild_pid(record: Path) -> int:
    deadline = time.monotonic() + 10
    while not (record.exists() and record.read_text().strip()):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return int(record.read_text())


def test_child_leads_its_own_session_and_reports_its_status(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = "import os, sys; print(os.getsid(0) == os.getpid()); sys.exit(3)"
    with out.open("wb") as sink:
        child = process.start([sys.executable, "-c", code], stdout=sink)
    assert process.wait(child, 10.0) == 3
    assert out.read_text().strip() == "True"


# Load-bearing: nothing the broker started outlives its time limit.
def test_time_limit_kills_the_child_and_its_children(tmp_path: Path) -> None:
    argv, record = with_grandchild(tmp_path)
    child = process.start(argv)
    pid = grandchild_pid(record)
    with pytest.raises(process.TimedOut):
        process.wait(child, 0.3)
    assert child.poll() is not None
    assert gone(pid)


# Load-bearing: abandoning a request from `tick` leaves no process behind.
def test_exception_from_tick_stops_the_child_first(tmp_path: Path) -> None:
    class Abandon(BaseException):
        pass

    argv, record = with_grandchild(tmp_path)
    child = process.start(argv)
    pid = grandchild_pid(record)

    def tick() -> None:
        raise Abandon()

    with pytest.raises(Abandon):
        process.wait(child, 30.0, tick)
    assert child.poll() is not None
    assert gone(pid)


# Load-bearing: what `tart exec` leaves holding its output pipe does not survive
# the time limit, even though `tart exec` itself has already exited.
def test_stop_kills_a_helper_that_outlived_its_exited_parent() -> None:
    script = "sleep 60 >/dev/null 2>&1 & echo $!"
    child = process.start(["/bin/sh", "-c", script], stdout=subprocess.PIPE)
    assert child.stdout is not None
    pid = int(child.stdout.read())  # end of file: the shell has exited
    child.stdout.close()
    time.sleep(0.2)
    reaped_before = child.returncode is not None
    alive_before = not gone_now(pid)
    process.stop(child)
    assert not reaped_before and alive_before  # it had exited, unreaped, with its helper alive
    assert child.returncode == 0
    assert gone(pid)


def test_tick_is_called_while_waiting() -> None:
    ticks: list[float] = []
    child = process.start(["/bin/sh", "-c", "sleep 0.7"])
    assert process.wait(child, 10.0, lambda: ticks.append(time.monotonic())) == 0
    assert len(ticks) >= 2


def test_stop_is_safe_for_a_process_that_already_ended() -> None:
    child = process.start(["/bin/sh", "-c", "exit 0"])
    assert process.wait(child, 10.0) == 0
    process.stop(child)
    process.stop(child)
    assert child.returncode == 0
