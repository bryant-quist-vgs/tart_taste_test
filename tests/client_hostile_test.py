"""client_hostile_test.py — the client against a spool something else has tampered with.

Anything in the agent's sandbox can write `<checkout>/.taste-test/`. These tests
plant what such a writer could plant and assert the two things the contract
(contracts/taste_test_contract.md) makes load-bearing: the client never exits 0
or 1 without a valid result, and it never reads, writes, or waits outside
`.taste-test/`.

Coverage:
  - Load-bearing: a directory, FIFO, or symbolic link where a log or a result
    should be ends in exit 5, promptly, and prints nothing from outside
  - a FIFO where the heartbeat or a status should be cannot stall the client
  - Load-bearing: forged numbers (NaN, Infinity, 1e400, a 400-digit integer)
    never crash the client into Python's exit code 1
  - a heartbeat from the future is not a heartbeat
  - Load-bearing: nothing is created through a planted link (`.gitignore`,
    `cancel`, a linked spool or request directory)
  - a stored log larger than the log limit is read only up to the limit
"""

import io
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import World

from tart_taste_test import client, client_spool, protocol, render
from tart_taste_test.exitcodes import Exit

FAST = client.Timing(poll=0.01, ack_window=0.15, stale=15.0, broker_lost=0.15)
NO_LIMITS = client.Limits()
OUTSIDE_TEXT = "TEXT FROM OUTSIDE THE SPOOL"
PLANTS = ("directory", "fifo", "link-to-file", "link-to-device", "dangling-link")


def bounded[T](action: Callable[[], T], seconds: float = 10.0) -> T:
    """Run `action` and fail, instead of hanging the suite, if it does not return."""
    box: list[T] = []
    worker = threading.Thread(target=lambda: box.append(action()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "the client did not return"
    assert box, "the client raised"
    return box[0]


def plant(path: Path, kind: str, outside: Path) -> None:
    """Put something that is not a regular file at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "link-to-file":
        secret = outside / "secret.txt"
        secret.write_text(OUTSIDE_TEXT + "\n")
        os.symlink(secret, path)
    elif kind == "link-to-device":
        os.symlink("/dev/zero", path)
    else:
        os.symlink(outside / "does-not-exist", path)


def result_bytes(request_id: str, outcome: str = "passed", command_exit: int | None = 0) -> bytes:
    document = protocol.result_document(
        request_id,
        outcome,
        command_exit,
        "",
        None,
        None,
        None,
        None,
        None,
        0,
        False,
        {"total": 1.0},
    )
    return protocol.encode(document)


def finished(world: World, outcome: str = "passed", command_exit: int | None = 0) -> str:
    """A request with a valid result and a one-line log."""
    request_id = world.submit()
    directory = world.paths.result_dir(request_id)
    directory.mkdir(parents=True)
    (directory / protocol.OUTPUT_LOG).write_bytes(b"real output\n")
    (directory / protocol.RESULT_FILE).write_bytes(result_bytes(request_id, outcome, command_exit))
    return request_id


def wait(world: World, request_id: str, limits: client.Limits = NO_LIMITS) -> tuple[Exit, str, str]:
    out, err = io.BytesIO(), io.StringIO()
    console = client.Console(out, err)
    code = bounded(lambda: client.wait(world.paths, request_id, console, limits, 2.0, FAST))
    return code, out.getvalue().decode("utf-8", "replace"), err.getvalue()


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    directory = tmp_path / "outside"
    directory.mkdir()
    return directory


# Load-bearing: no failure to obtain or show a verdict exits 0 or 1.
@pytest.mark.parametrize("kind", PLANTS[:-1])
@pytest.mark.parametrize("limits", [NO_LIMITS, client.Limits(None, 5)], ids=["stream", "tail"])
def test_a_log_that_is_not_a_regular_file_is_exit_5_and_shows_nothing_from_outside(
    world: World, outside: Path, kind: str, limits: client.Limits
) -> None:
    request_id = finished(world)
    plant(world.paths.result_dir(request_id) / protocol.OUTPUT_LOG, kind, outside)
    code, out, err = wait(world, request_id, limits)
    assert code is Exit.ERROR
    assert out == ""
    assert "output.log" in err.splitlines()[-2] and "cannot be read" in err.splitlines()[-2]
    assert err.splitlines()[-1] == f"taste-test: outcome=error exit=5 id={request_id}"


def test_a_log_that_is_a_dangling_link_is_exit_5_not_a_missing_log(
    world: World, outside: Path
) -> None:
    request_id = finished(world)
    plant(world.paths.result_dir(request_id) / protocol.OUTPUT_LOG, "dangling-link", outside)
    code, _, _ = wait(world, request_id)
    assert code is Exit.ERROR


@pytest.mark.parametrize("kind", PLANTS[:-1])
def test_the_log_command_refuses_a_log_that_is_not_a_regular_file(
    world: World, outside: Path, kind: str
) -> None:
    request_id = finished(world)
    plant(world.paths.result_dir(request_id) / protocol.SETUP_LOG, kind, outside)
    out = io.BytesIO()
    console = client.Console(out, io.StringIO())

    def show() -> Exit:
        try:
            return client.log(world.paths, request_id, console, NO_LIMITS, True)
        except client_spool.ClientError as error:
            return error.code

    assert bounded(show) is Exit.ERROR
    assert out.getvalue() == b""


# Load-bearing: 0 is returned only after reading a result whose outcome is passed.
@pytest.mark.parametrize("kind", PLANTS)
def test_a_result_that_is_not_a_regular_file_is_exit_5(
    world: World, outside: Path, kind: str
) -> None:
    request_id = world.submit()
    if kind == "link-to-file":  # a valid `passed` result, but outside the spool
        (outside / "secret.txt").write_bytes(result_bytes(request_id))
        os.makedirs(world.paths.result_dir(request_id))
        os.symlink(outside / "secret.txt", world.paths.result_dir(request_id) / "result.json")
    else:
        plant(world.paths.result_dir(request_id) / protocol.RESULT_FILE, kind, outside)
    code, out, err = wait(world, request_id)
    assert code is Exit.ERROR
    assert out == ""
    assert err.splitlines()[-1] == f"taste-test: outcome=error exit=5 id={request_id}"


def test_a_result_directory_that_is_a_link_is_exit_5(world: World, outside: Path) -> None:
    request_id = world.submit()
    (outside / protocol.RESULT_FILE).write_bytes(result_bytes(request_id))
    (outside / protocol.OUTPUT_LOG).write_text(OUTSIDE_TEXT)
    os.symlink(outside, world.paths.result_dir(request_id))
    code, out, _ = wait(world, request_id)
    assert code is Exit.ERROR
    assert OUTSIDE_TEXT not in out


@pytest.mark.parametrize("name", [protocol.STATUS_FILE, protocol.HEARTBEAT_FILE])
@pytest.mark.parametrize("kind", PLANTS)
def test_a_planted_status_or_heartbeat_cannot_stall_or_fool_the_client(
    world: World, outside: Path, name: str, kind: str
) -> None:
    request_id = world.submit()
    home = (
        world.paths.spool if name == protocol.HEARTBEAT_FILE else world.paths.result_dir(request_id)
    )
    plant(home / name, kind, outside)
    # Not acknowledged, no usable heartbeat: withdrawn as "no broker", and promptly.
    code, _, _ = wait(world, request_id)
    assert code is Exit.NOT_RUN
    assert not world.paths.request_dir(request_id).exists()
    out = io.BytesIO()
    console = client.Console(out, io.StringIO())
    assert bounded(lambda: client.status(world.paths, None, console, FAST)) is Exit.NOT_RUN


HOSTILE_NUMBERS = ("NaN", "Infinity", "-Infinity", "1e400", "1" + "0" * 400, "-1", '"7"', "true")


# Load-bearing: a forged number must not crash the client into exit code 1.
@pytest.mark.parametrize("number", HOSTILE_NUMBERS)
def test_forged_numbers_in_a_result_are_exit_5(world: World, number: str) -> None:
    request_id = finished(world)
    valid = result_bytes(request_id).decode()
    assert '"total": 1.0' in valid
    forged = valid.replace('"total": 1.0', f'"total": {number}')
    (world.paths.result_dir(request_id) / protocol.RESULT_FILE).write_text(forged)
    code, _, err = wait(world, request_id)
    assert code is Exit.ERROR
    assert "is not a valid result" in err


@pytest.mark.parametrize("number", HOSTILE_NUMBERS)
def test_forged_numbers_in_a_heartbeat_or_status_are_not_a_broker(
    world: World, number: str
) -> None:
    request_id = world.submit()
    beat = f'{{"schema": 1, "pid": 1, "version": "0.1.0", "updated": {number}, "pending": 0}}'
    world.paths.heartbeat.write_text(beat)
    assert client_spool.heartbeat_age(world.paths) is None or number == "-1"
    directory = world.paths.result_dir(request_id)
    directory.mkdir(parents=True)
    state = (
        f'{{"schema": 1, "id": "{request_id}", "state": "running", "detail": "", '
        f'"updated": {number}}}'
    )
    (directory / protocol.STATUS_FILE).write_text(state)
    out = io.BytesIO()
    console = client.Console(out, io.StringIO())
    assert bounded(lambda: client.status(world.paths, None, console, FAST)) is Exit.NOT_RUN
    assert out.getvalue().decode().splitlines()[0] == "broker: not serving this checkout"


def test_a_heartbeat_from_the_future_is_not_a_heartbeat(world: World) -> None:
    client_spool.ensure_spool(world.paths)

    def beat(updated: float) -> None:
        document = protocol.heartbeat_document(1, "0.1.0", updated, 0)
        world.paths.heartbeat.write_bytes(protocol.encode(document))

    beat(time.time() + 3600)
    assert client_spool.heartbeat_age(world.paths) is None
    beat(time.time() + 1)  # ordinary clock jitter is still a heartbeat
    assert client_spool.heartbeat_age(world.paths) == 0.0
    beat(time.time() - 5)
    age = client_spool.heartbeat_age(world.paths)
    assert age is not None and 4 < age < 10


# Load-bearing: nothing is created outside <checkout>/.taste-test/.
def test_gitignore_is_never_written_through_a_planted_link(world: World, outside: Path) -> None:
    world.paths.base.mkdir()
    target = outside / "created-by-the-client"
    os.symlink(target, world.paths.base / protocol.GITIGNORE_NAME)
    with pytest.raises(client_spool.ClientError) as caught:
        client_spool.ensure_spool(world.paths)
    assert caught.value.code is Exit.ERROR
    assert not target.exists()
    out, err = io.BytesIO(), io.StringIO()
    code = client.run(
        world.paths, "test", [], client.Console(out, err), NO_LIMITS, 1.0, False, FAST
    )
    assert code is Exit.ERROR
    assert err.getvalue().splitlines()[-1] == "taste-test: outcome=error exit=5"
    assert not target.exists()


def test_cancel_marker_is_never_written_through_a_planted_link(world: World, outside: Path) -> None:
    request_id = world.submit()
    target = outside / "created-by-cancel"
    os.symlink(target, world.paths.request_dir(request_id) / protocol.CANCEL_FILE)
    console = client.Console(io.BytesIO(), io.StringIO())
    with pytest.raises(client_spool.ClientError) as caught:
        client.cancel(world.paths, request_id, console)
    assert caught.value.code is Exit.ERROR
    assert not target.exists()


def test_cancel_refuses_a_request_directory_that_is_a_link(world: World, outside: Path) -> None:
    client_spool.ensure_spool(world.paths)
    request_id = "20261115T183000Z-3f9a1c2b4d5e"
    os.symlink(outside, world.paths.request_dir(request_id))
    console = client.Console(io.BytesIO(), io.StringIO())
    with pytest.raises(client_spool.ClientError):
        client.cancel(world.paths, request_id, console)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("level", ["base", "spool", "requests", "results"])
def test_every_command_refuses_a_spool_with_a_link_on_its_path(
    world: World, outside: Path, level: str
) -> None:
    request_id = finished(world)
    path: Path = getattr(world.paths, level)
    moved = outside / "moved"
    os.rename(path, moved)
    os.symlink(moved, path)
    console = client.Console(io.BytesIO(), io.StringIO())
    before = sorted(p.name for p in moved.rglob("*"))

    code, out, err = wait(world, request_id)
    assert code is Exit.ERROR and out == ""
    assert "exists and is not a directory" in err
    run = client.run(world.paths, "test", [], console, NO_LIMITS, 1.0, False, FAST)
    assert run is Exit.ERROR
    actions: tuple[Callable[[], object], ...] = (
        lambda: client.log(world.paths, request_id, console, NO_LIMITS, False),
        lambda: client.status(world.paths, None, console, FAST),
        lambda: client.status(world.paths, request_id, console, FAST),
        lambda: client.cancel(world.paths, request_id, console),
    )
    for action in actions:
        with pytest.raises(client_spool.ClientError) as caught:
            action()
        assert caught.value.code is Exit.ERROR
    assert sorted(p.name for p in moved.rglob("*")) == before  # nothing written through the link


def test_reads_never_go_through_a_link_even_without_the_up_front_check(
    world: World, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor walk is the guard; `check_spool` only gives a better message."""
    request_id = finished(world)
    moved = outside / "moved"
    os.rename(world.paths.results, moved)
    os.symlink(moved, world.paths.results)
    monkeypatch.setattr(client, "check_spool", lambda _paths: None)
    with pytest.raises(OSError):
        client_spool.open_regular(world.paths, *client_spool.RESULTS, request_id, "output.log")
    assert client_spool.known(world.paths, request_id) is True  # its request directory is real
    code, out, _ = wait(world, request_id)
    assert code is Exit.ERROR and out == ""


def test_a_log_is_read_only_up_to_the_log_limit(world: World) -> None:
    request_id = finished(world)
    log = world.paths.result_dir(request_id) / protocol.OUTPUT_LOG
    log.write_bytes(b"0123456789\n" * 10)
    with log.open("rb") as handle:
        out = io.BytesIO()
        shown = render.render_limited(handle, out, None, 3, limit=25)
    assert shown.lines == 3  # two whole lines and the cut-off start of a third
    assert out.getvalue() == b"0123456789\n0123456789\n012"
    out = io.BytesIO()
    follower = render.Follower(lambda: log.open("rb"), out, limit=25)
    follower.pump()
    follower.pump()
    assert out.getvalue() == b"0123456789\n0123456789\n012"
