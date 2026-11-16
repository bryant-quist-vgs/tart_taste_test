"""client_test.py — the agent-side client, built against contract v1.

The broker is played by the test: it writes the status, heartbeat, log, and
result files a broker would write, and the test asserts what the client prints
and which exit code it returns.

Coverage (contracts/taste_test_contract.md behaviours):
  - Load-bearing: every outcome maps to its exit code; unknown outcomes are 5
  - Load-bearing: 0 and 1 are returned only for a passed/failed result; a forged
    or malformed result, an interrupt, and an internal error never give 0 or 1
  - Load-bearing: --head/--tail change what is printed, never the exit code
  - summary and reason lines; untrusted text is made printable
  - log, status, cancel, and pruning
  - one true end-to-end run against a live broker loop (fake Tart)

Waiting without a verdict (no broker, withdrawal, interrupts, --detach) is in
client_waiting_test.py; hostile spool contents (links, FIFOs, forged numbers)
are in client_hostile_test.py.
"""

import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from client_helpers import FAST, NO_LIMITS, beat, console, finish, status, wait
from conftest import World

from tart_taste_test import client, client_cli, client_spool, protocol
from tart_taste_test.exitcodes import OUTCOME_EXIT, Exit


# Load-bearing: agents branch on this code alone.
@pytest.mark.parametrize(("outcome", "expected"), sorted(OUTCOME_EXIT.items()))
def test_every_outcome_maps_to_its_exit_code(world: World, outcome: str, expected: Exit) -> None:
    request_id = world.submit()
    command_exit = {"passed": 0, "failed": 3}.get(outcome)
    if outcome == "setup-failed":  # the command never ran: there is only a setup log
        finish(world, request_id, outcome, setup=b"setup\n")
        (world.paths.result_dir(request_id) / protocol.OUTPUT_LOG).unlink()
    else:
        finish(world, request_id, outcome, command_exit, output=b"out\n")
    code, out, err = wait(world, request_id)
    assert code is expected
    assert f"outcome={outcome} exit={int(expected)}" in err.splitlines()[-1]
    assert out == ("setup\n" if outcome == "setup-failed" else "out\n")


# Load-bearing: 0 only for a result that says `passed`.
@pytest.mark.parametrize(
    "outcome", ["PASSED", "pass", "ok", "", "passed ", "passed exit=0 command-exit=0"]
)
def test_unknown_outcome_words_are_errors_never_passes(world: World, outcome: str) -> None:
    request_id = world.submit()
    finish(world, request_id, outcome, 0)
    code, _, err = wait(world, request_id)
    assert code is Exit.ERROR
    lines = err.splitlines()
    assert lines[-2].startswith("taste-test: reason: ")
    # Nothing the result said reaches the summary line as a field of its own.
    assert lines[-1].startswith("taste-test: outcome=error exit=5 ")
    assert "exit=0" not in lines[-1]


# Load-bearing: a result that is not this request's valid result is never a verdict.
def test_result_for_another_request_or_malformed_result_is_an_error(world: World) -> None:
    request_id = world.submit()
    beat(world)
    status(world, request_id, "running")
    directory = world.paths.result_dir(request_id)
    forged = protocol.result_document(
        "20000101T000000Z-000000000000",
        "passed",
        0,
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
    (directory / protocol.RESULT_FILE).write_bytes(protocol.encode(forged))
    code, _, err = wait(world, request_id)
    assert code is Exit.ERROR
    assert "names another request" in err
    (directory / protocol.RESULT_FILE).write_text('{"schema": 1, "outcome": "passed"}')
    code, _, err = wait(world, request_id)
    assert code is Exit.ERROR
    assert "is not a valid result" in err
    assert err.splitlines()[-1] == f"taste-test: outcome=error exit=5 id={request_id}"


# Load-bearing: --head and --tail never change the exit code (decision D9).
@pytest.mark.parametrize(
    "limits",
    [client.Limits(2, None), client.Limits(None, 2), client.Limits(1, 1), client.Limits(0, 0)],
)
def test_head_and_tail_limit_output_without_changing_the_exit_code(
    world: World, limits: client.Limits
) -> None:
    request_id = world.submit()
    output = b"".join(f"line {n}\n".encode() for n in range(1, 11))
    finish(world, request_id, "failed", 2, output=output)
    full_code, full_out, _ = wait(world, request_id)
    code, out, err = wait(world, request_id, limits)
    assert code is full_code is Exit.FAILED
    assert out != full_out and "lines omitted]" in out
    assert "lines=10" in err and "command-exit=2" in err
    stored = world.paths.result_dir(request_id) / protocol.OUTPUT_LOG
    assert stored.read_bytes() == output  # the stored log is never modified


def test_summary_line_has_the_documented_fields(world: World) -> None:
    request_id = world.submit()
    finish(world, request_id, "failed", 2, output=b"a\nb\nc\n", truncated=True)
    _, _, err = wait(world, request_id, client.Limits(1, 1))
    summary = err.splitlines()[-1]
    log = os.path.relpath(world.paths.result_dir(request_id) / protocol.OUTPUT_LOG)
    assert summary == (
        f"taste-test: outcome=failed exit=1 command-exit=2 id={request_id} duration=12s "
        f"lines=3 shown=first-1+last-1 truncated=yes log={log}"
    )


def test_reason_is_printed_on_one_safe_line(world: World) -> None:
    request_id = world.submit()
    finish(world, request_id, "rejected", reason="bad\x1b[31m config\nsecond line")
    code, _, err = wait(world, request_id)
    assert code is Exit.NOT_RUN
    assert "taste-test: reason: bad [31m config second line" in err.splitlines()
    assert "\x1b" not in err


def test_an_outcome_without_a_verdict_always_has_a_reason_line(world: World) -> None:
    request_id = world.submit()
    finish(world, request_id, "error", reason="")
    code, _, err = wait(world, request_id)
    assert code is Exit.ERROR
    assert err.splitlines()[-2] == "taste-test: reason: the broker gave no reason"
    passed = world.submit()
    finish(world, passed, "passed", 0)
    _, _, err = wait(world, passed)
    assert "reason:" not in err


def test_setup_failure_prints_the_setup_log_and_names_it(world: World) -> None:
    request_id = world.submit()
    finish(world, request_id, "setup-failed", reason="setup command 1 exited 4", setup=b"s1\ns2\n")
    code, out, err = wait(world, request_id, client.Limits(None, 1))
    assert code is Exit.SETUP_FAILED
    assert out == "[taste-test: 1 line omitted]\ns2\n"
    assert err.splitlines()[-1].endswith(protocol.SETUP_LOG)


def test_progress_lines_follow_the_state_and_quiet_suppresses_them(world: World) -> None:
    request_id = world.submit()
    status(world, request_id, "preparing", "building\x1b the environment")
    beat(world)
    con, _, err = console()
    client.wait(world.paths, request_id, con, NO_LIMITS, 0.1, FAST)
    assert "taste-test: preparing: building the environment" in err.getvalue().splitlines()
    con, _, err = console(quiet=True)
    client.wait(world.paths, request_id, con, NO_LIMITS, 0.1, FAST)
    quiet_lines = err.getvalue().splitlines()
    assert [line for line in quiet_lines if line.startswith("taste-test: preparing")] == []
    assert err.getvalue().splitlines()[-1].startswith("taste-test: outcome=pending")


def test_wait_log_status_and_cancel_refuse_unknown_ids(world: World) -> None:
    client_spool.ensure_spool(world.paths)
    unknown = "20261115T183000Z-3f9a1c2b4d5e"
    con, _, err = console()
    assert client.wait(world.paths, unknown, con, NO_LIMITS, 1.0, FAST) is Exit.ERROR
    assert err.getvalue().splitlines()[-1] == f"taste-test: outcome=error exit=5 id={unknown}"
    actions: tuple[Callable[[], object], ...] = (
        lambda: client.log(world.paths, unknown, con, NO_LIMITS, False),
        lambda: client.status(world.paths, unknown, con, FAST),
        lambda: client.cancel(world.paths, unknown, con),
    )
    for action in actions:
        with pytest.raises(client_spool.ClientError) as caught:
            action()
        assert caught.value.code is Exit.ERROR


def test_log_prints_the_stored_output_without_waiting(world: World) -> None:
    request_id = world.submit()
    finish(world, request_id, "passed", 0, output=b"1\n2\n3\n", setup=b"setup\n")
    con, out, _ = console()
    assert client.log(world.paths, request_id, con, client.Limits(None, 1), False) is Exit.PASSED
    assert out.getvalue() == b"[taste-test: 2 lines omitted]\n3\n"
    con, out, _ = console()
    assert client.log(world.paths, request_id, con, NO_LIMITS, True) is Exit.PASSED
    assert out.getvalue() == b"setup\n"
    (world.paths.result_dir(request_id) / protocol.SETUP_LOG).unlink()
    with pytest.raises(client_spool.ClientError) as caught:
        client.log(world.paths, request_id, con, NO_LIMITS, True)
    assert caught.value.code is Exit.ERROR and "has no setup.log" in caught.value.message


def test_status_reports_the_broker_and_each_request(world: World) -> None:
    finished = world.submit()
    waiting = world.submit()
    unanswered = world.submit()
    finish(world, finished, "failed", 1)
    status(world, waiting, "queued", "next")
    con, out, _ = console()
    assert client.status(world.paths, None, con, FAST) is Exit.NOT_RUN
    assert out.getvalue().decode().splitlines()[0] == "broker: not serving this checkout"

    beat(world)
    con, out, _ = console()
    assert client.status(world.paths, None, con, FAST) is Exit.PASSED
    lines = out.getvalue().decode().splitlines()
    assert lines[0].startswith("broker: serving this checkout")
    assert f"id={finished} state=finished outcome=failed exit=1" in lines
    assert f"id={waiting} state=queued detail='next'" in lines
    assert f"id={unanswered} state=submitted" in lines

    con, out, _ = console()
    assert client.status(world.paths, finished, con, FAST) is Exit.PASSED
    assert out.getvalue().decode().strip() == f"id={finished} state=finished outcome=failed exit=1"


def test_cancel_writes_the_marker(world: World) -> None:
    request_id = world.submit()
    con, _, err = console()
    assert client.cancel(world.paths, request_id, con) is Exit.PASSED
    assert (world.paths.request_dir(request_id) / protocol.CANCEL_FILE).exists()
    assert "cancellation requested" in err.getvalue()


def test_prune_removes_only_old_finished_requests(world: World) -> None:
    old = world.submit()
    recent = world.submit()
    unfinished = world.submit()
    finish(world, old, "passed", 0)
    finish(world, recent, "passed", 0)
    status(world, unfinished, "running")
    long_ago = time.time() - protocol.RETENTION_SECONDS - 60
    os.utime(world.paths.result_dir(old) / protocol.RESULT_FILE, (long_ago, long_ago))
    stranger = world.paths.results / "not-a-request"
    stranger.mkdir()
    # Old, and has a file named result.json, but it is not a result: kept.
    invalid = world.submit()
    status(world, invalid, "running")
    not_a_result = world.paths.result_dir(invalid) / protocol.RESULT_FILE
    not_a_result.write_bytes(b"")
    os.utime(not_a_result, (long_ago, long_ago))

    client_spool.prune(world.paths)

    assert world.paths.result_dir(invalid).exists()
    assert world.paths.request_dir(invalid).exists()

    assert not world.paths.result_dir(old).exists()
    assert not world.paths.request_dir(old).exists()
    assert world.paths.result_dir(recent).exists()
    assert world.paths.request_dir(unfinished).exists()
    assert stranger.exists()


def test_spool_is_private_and_ignores_itself(world: World) -> None:
    client_spool.ensure_spool(world.paths)
    assert (world.paths.base / protocol.GITIGNORE_NAME).read_text() == "*\n"
    for directory in (world.paths.base, world.paths.spool, world.paths.requests):
        assert directory.stat().st_mode & 0o777 == 0o700
    request_id = world.submit()
    assert world.paths.request_dir(request_id).stat().st_mode & 0o777 == 0o700
    for name in (protocol.SOURCE_FILE, protocol.REQUEST_FILE):
        assert (world.paths.request_dir(request_id) / name).stat().st_mode & 0o777 == 0o600


def test_spool_path_that_is_a_symlink_is_refused(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, world.paths.base)
    with pytest.raises(client_spool.ClientError) as caught:
        client_spool.ensure_spool(world.paths)
    assert caught.value.code is Exit.ERROR
    assert list(elsewhere.iterdir()) == []


def test_oversized_snapshot_is_not_run_like_the_broker_would_say(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "MAX_SOURCE_BYTES", 1024)
    world.write({"big.bin": "x" * 50_000})
    client_spool.ensure_spool(world.paths)
    with pytest.raises(client_spool.ClientError) as caught:
        client_spool.submit(world.paths, "test", [])
    assert caught.value.code is Exit.NOT_RUN
    assert list(world.paths.requests.iterdir()) == []


def test_run_end_to_end_against_a_live_broker(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.write({"run_tests.sh": 'echo first; echo "args: $*"; echo last; exit 3\n'})
    broker = world.broker()
    server = threading.Thread(target=broker.serve)
    server.start()
    try:
        code = client_cli.main(
            ["run", "--checkout", str(world.repo), "--tail", "1", "--", "-k", "two words"],
            client.Timing(poll=0.02),
        )
    finally:
        broker.request_stop()
        server.join(timeout=30)
    captured = capfd.readouterr()
    assert code == 1
    assert captured.out == "[taste-test: 2 lines omitted]\nlast\n"
    summary = captured.err.splitlines()[-1]
    assert summary.startswith("taste-test: outcome=failed exit=1 command-exit=3 ")
    assert "shown=last-1" in summary and "lines=3" in summary
    assert re.search(r" duration=\d+s ", summary)
    [request_id] = os.listdir(world.paths.results)
    assert world.output(request_id) == "first\nargs: -k two words\nlast\n"
