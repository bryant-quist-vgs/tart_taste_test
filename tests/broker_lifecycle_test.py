"""broker_lifecycle_test.py — what happens to a request that is interrupted.

Built against contract v1 (contracts/taste_test_broker_contract.md). Each test
runs a real `Broker` on the fake Tart with a guest command that would run for
half a minute, interrupts it one way, and checks the same things: the request
gets a result that is not a verdict, no VM and no staged file is left, and the
broker does what it should next (exits, or keeps serving).

Coverage:
  - Load-bearing: SIGTERM and SIGINT while a request runs: result `error`, the
    VM stopped and deleted, exit 0, the previous signal handlers restored
  - `request_stop` from another thread does the same; when idle it just returns
  - Load-bearing: a stop that arrives while a VM is being deleted lets the
    deletion finish (with a control: without the shield the VM is left behind)
  - a stop that arrives during a scan claims nothing
  - cancel during the command and during environment setup: `cancelled`
  - the repository is unregistered while its request runs: `error`, and the
    broker keeps serving the others
  - the spool is moved away while its request runs: `error`, written to the
    directory the request came from, never to whatever replaced it
"""

import contextlib
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import DEFAULT_CONFIG, World

from tart_taste_test import broker, process, protocol, registry, served
from tart_taste_test.broker import Broker

SLOW = {"run_tests.sh": "echo started; sleep 30\n"}
LIMIT = 60.0


@pytest.fixture(autouse=True)
def fast_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "HEARTBEAT_INTERVAL_SECONDS", 0.1)


def state_of(world: World, request_id: str, root: Path | None = None) -> tuple[str, str]:
    """The request's (state, detail), or two empty strings before it has any."""
    paths = world.paths if root is None else type(world.paths)(root)
    try:
        data = (paths.result_dir(request_id) / protocol.STATUS_FILE).read_bytes()
        status = protocol.parse_status(data)
    except OSError, protocol.ProtocolError:
        return "", ""
    return status.state, status.detail


def wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + LIMIT
    while not condition():
        assert time.monotonic() < deadline, "the condition was never met"
        time.sleep(0.02)


def running(world: World, request_id: str) -> Callable[[], bool]:
    return lambda: state_of(world, request_id)[0] == protocol.STATE_RUNNING


class Serving:
    """A broker serving in a thread of its own until the block ends."""

    def __init__(self, world: World, once: bool = True) -> None:
        self.broker = world.broker()
        self.codes: list[int] = []
        self._thread = threading.Thread(
            target=lambda: self.codes.append(self.broker.serve(once=once)), daemon=True
        )

    def __enter__(self) -> Serving:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._thread.join(LIMIT)
        assert not self._thread.is_alive(), "the broker did not return"
        assert self.codes == [0]


def nothing_left(world: World) -> None:
    assert [n for n in world.vm_names() if "-run-" in n or "-build-" in n] == []
    assert list(world.state.staging.iterdir()) == []


# Load-bearing: a signal ends the request as `error` and still deletes its VM.
@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_signal_while_a_request_runs_cleans_up_and_exits_0(world: World, number: int) -> None:
    world.write(SLOW)
    request_id = world.submit()
    before = [signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)]

    def interrupt() -> None:
        wait_until(running(world, request_id))
        os.kill(os.getpid(), number)

    sender = threading.Thread(target=interrupt, daemon=True)
    sender.start()
    began = time.monotonic()
    assert world.broker().serve() == 0  # in the main thread, where signals arrive
    sender.join(LIMIT)

    assert time.monotonic() - began < 25  # it did not wait for the command to end
    result = world.result(request_id)
    assert (result["outcome"], result["reason"]) == ("error", broker.STOPPED)
    assert result["command_exit"] is None
    nothing_left(world)
    assert any(event.endswith(" stopped") for event in world.events)
    assert [signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)] == before
    assert state_of(world, request_id) == (protocol.STATE_FINISHED, "error")


def test_request_stop_from_another_thread_does_the_same(world: World) -> None:
    world.write(SLOW)
    request_id = world.submit()
    with Serving(world, once=False) as serving:
        wait_until(running(world, request_id))
        serving.broker.request_stop()
    assert world.result(request_id)["reason"] == broker.STOPPED
    nothing_left(world)
    [line] = world.audit()
    assert (line["id"], line["outcome"]) == (request_id, "error")


def test_request_stop_while_idle_returns_promptly(world: World) -> None:
    with Serving(world, once=False) as serving:
        wait_until(lambda: any("ready" in event for event in world.events))
        serving.broker.request_stop()
    assert world.audit() == []


# Load-bearing: clean-up is never interrupted. The stop arrives while the run VM
# of a finished command is being deleted; the deletion must still complete, and
# the verdict the command earned is kept.
@pytest.mark.parametrize("shielded", [True, False], ids=["shielded", "control-no-shield"])
def test_stop_while_a_vm_is_being_deleted_lets_the_deletion_finish(
    world: World, monkeypatch: pytest.MonkeyPatch, shielded: bool
) -> None:
    if not shielded:
        # The control: the same stop, with the shield taken away, does interrupt
        # the deletion. So the shielded run passes because of the shield.
        monkeypatch.setattr(Broker, "_shield", lambda self: contextlib.nullcontext())
    world.run()  # build the environment image first, at full speed
    world.switch("slow-stop", str(process.POLL_SECONDS * 4))
    before = len(world.calls("stop"))
    request_id = world.submit()

    def deleting() -> bool:
        # Stops of the run VM: before it is cloned (no such VM yet), when the
        # command has ended, and then as the first step of deleting it.
        return len(world.calls("stop")) - before >= 3

    with Serving(world, once=False) as serving:
        wait_until(deleting)
        serving.broker.request_stop()

    left = [name for name in world.vm_names() if "-run-" in name]
    result = world.result(request_id)
    if shielded:
        assert left == []
        assert result["outcome"] == "passed"
    else:
        assert len(left) == 1
        assert (result["outcome"], result["reason"]) == ("error", broker.STOPPED)


def test_stop_that_arrives_during_a_scan_claims_nothing(world: World) -> None:
    request_id = world.submit()
    serving = world.broker()
    scan = serving.tick

    def tick_then_stop(force: bool = False) -> None:
        scan(force)
        serving.request_stop()

    serving.tick = tick_then_stop  # type: ignore[method-assign]
    assert serving.serve() == 0
    home = world.paths.request_dir(request_id)
    assert (home / protocol.REQUEST_FILE).exists() and (home / protocol.SOURCE_FILE).exists()
    assert world.audit() == [] and world.calls("clone") == []
    assert state_of(world, request_id) == (protocol.STATE_QUEUED, "next")


def test_cancel_during_the_command(world: World) -> None:
    world.write(SLOW)
    request_id = world.submit()
    with Serving(world):
        wait_until(running(world, request_id))
        (world.paths.request_dir(request_id) / protocol.CANCEL_FILE).touch()
    result = world.result(request_id)
    assert (result["outcome"], result["command_exit"]) == ("cancelled", None)
    assert world.output(request_id) == "started\n"  # what it printed first is kept
    nothing_left(world)


def test_cancel_during_environment_setup(world: World) -> None:
    slow_setup = DEFAULT_CONFIG.replace("mkdir -p deps && echo built > deps/marker", "sleep 30")
    world.commit({".taste-test.toml": slow_setup})
    request_id = world.submit()

    def building() -> bool:
        return any("-build-" in call[-1] for call in world.calls("run")) and any(
            "sleep 30" in " ".join(call) for call in world.calls("exec")
        )

    with Serving(world):
        wait_until(building)
        (world.paths.request_dir(request_id) / protocol.CANCEL_FILE).touch()
    assert world.result(request_id)["outcome"] == "cancelled"
    assert world.vm_names() == []  # no unfinished build, and no environment image either
    nothing_left(world)


def test_repository_unregistered_while_its_request_runs(world: World) -> None:
    world.write(SLOW)
    request_id = world.submit()
    with Serving(world):
        wait_until(running(world, request_id))
        registry.save(world.state, [])
    result = world.result(request_id)
    assert (result["outcome"], result["reason"]) == ("error", served.NOT_SERVED)
    nothing_left(world)
    assert any("no longer serving" in event for event in world.events)


def test_spool_moved_away_while_its_request_runs(world: World, tmp_path: Path) -> None:
    world.write(SLOW)
    request_id = world.submit()
    moved = tmp_path / "moved-away"
    with Serving(world):
        wait_until(running(world, request_id))
        world.paths.base.rename(moved)
        world.write({"run_tests.sh": "echo quick\n"})
        again = world.submit()  # the agent starts over with a new spool at the same path
    # The result went to the directories the request came from...
    old_results = moved / protocol.SPOOL_NAME / protocol.RESULTS_NAME / request_id
    result = protocol.parse_result((old_results / protocol.RESULT_FILE).read_bytes())
    assert (result.outcome, result.reason) == ("error", served.SPOOL_GONE)
    # ...and the new spool is served like any other.
    assert not world.paths.result_dir(request_id).exists()
    assert world.result(again)["outcome"] == "passed"
    assert sorted(line["outcome"] for line in world.audit()) == ["error", "passed"]
    nothing_left(world)
