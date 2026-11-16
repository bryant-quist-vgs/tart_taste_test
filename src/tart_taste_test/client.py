"""The agent-side client: submit a request, wait for it, report the outcome.

Built against contract v1 (contracts/taste_test_contract.md).

Everything here stays inside `<checkout>/.taste-test/`. This module and the ones
it imports contain no code that can start a VM or read operator state
(docs/decisions.md D2); a test enforces the import boundary.

`run` and `wait` end the documented way whatever goes wrong: a reason line, a
summary line, and an exit code from docs/interfaces.md §2. An exception never
decides the exit code.
"""

import contextlib
import os
import time
from dataclasses import dataclass
from typing import BinaryIO, TextIO

from tart_taste_test import exitcodes, protocol
from tart_taste_test.client_spool import (
    RESULTS,
    ClientError,
    SpoolPaths,
    check_spool,
    ensure_spool,
    heartbeat_age,
    known,
    open_regular,
    prune,
    read_result,
    read_status,
    request_cancel,
    request_ids,
    submit,
    withdraw,
)
from tart_taste_test.exitcodes import (
    OUTCOMES,
    OUTCOMES_WITH_COMMAND_EXIT,
    Exit,
    exit_for_outcome,
    name_of,
)
from tart_taste_test.render import Follower, Shown, render_limited

PREFIX = "taste-test: "

# For `log`, `status`, and `cancel`, 0 means that command succeeded. It says
# nothing about a test run (docs/interfaces.md §2).
DONE = Exit.PASSED
NOT_SERVING = Exit.NOT_RUN

ASK_OPERATOR = (
    "Ask the operator to run, outside the sandbox: `taste-test-broker register "
    "--remote-url URL` in the repository's main working tree (once), then "
    "`taste-test-broker serve`"
)
NO_BROKER = (
    "no broker is serving this checkout (none is running, this repository is not "
    "registered, or a running broker does not serve this working tree: its output says "
    "why), so nothing was run. " + ASK_OPERATOR
)
BROKER_BUSY = (
    "a broker is serving this checkout but did not take the request in time: this "
    f"checkout already has {protocol.MAX_PENDING_REQUESTS} requests waiting, or the broker "
    "is slow to scan. The request was withdrawn and nothing was run. Wait for earlier "
    "requests to finish (`taste-test status`), then run it again"
)


@dataclass(frozen=True)
class Timing:
    """Polling and patience: the client's values in docs/protocol.md §2."""

    poll: float = protocol.CLIENT_POLL_SECONDS
    ack_window: float = protocol.ACK_WINDOW_SECONDS
    stale: float = protocol.HEARTBEAT_STALE_SECONDS
    broker_lost: float = protocol.BROKER_LOST_SECONDS


@dataclass(frozen=True)
class Limits:
    """Output limits requested on the command line."""

    head: int | None = None
    tail: int | None = None

    @property
    def unlimited(self) -> bool:
        return self.head is None and self.tail is None


class Console:
    """Where the client prints: output bytes to stdout, everything else to stderr."""

    def __init__(self, out: BinaryIO, err: TextIO, quiet: bool = False) -> None:
        self.out = out
        self.err = err
        self.quiet = quiet
        self._err_closed = False

    def progress(self, message: str) -> None:
        if not self.quiet:
            self.say(message)

    def say(self, message: str) -> None:
        """Print one line on standard error.

        Messages can carry text from the spool (a status detail, a reason), which
        anything in the sandbox can write, so every line is made printable first.
        A standard error that can no longer be written (`2>&1 | head`) stops the
        printing and nothing else.
        """
        if self._err_closed:
            return
        try:
            print(PREFIX + protocol.printable(message), file=self.err, flush=True)
        except OSError, ValueError:
            self._err_closed = True
            exitcodes.silence(self.err)

    def line(self, text: str) -> None:
        self.out.write(text.encode("utf-8", "replace") + b"\n")
        self.out.flush()


@dataclass(frozen=True)
class Waited:
    """How a wait ended: with a result, or with a client-side exit code and reason."""

    result: protocol.Result | None = None
    code: Exit = Exit.ERROR
    reason: str = ""


def end(console: Console, code: Exit, reason: str, request_id: str | None = None) -> Exit:
    """End `run` or `wait` without a result: the reason line, then the summary line."""
    console.say("reason: " + reason)
    fields = [f"outcome={name_of(code)}", f"exit={int(code)}"]
    if request_id is not None:
        fields.append(f"id={request_id}")
    console.say(" ".join(fields))
    return code


def failure(error: Exception) -> tuple[Exit, str]:
    """The exit code and reason for an exception that ended `run` or `wait`."""
    if isinstance(error, ClientError):
        return error.code, error.message
    return Exit.ERROR, f"internal error ({type(error).__name__}: {error}); no verdict"


def _pump(follower: Follower | None, request_id: str, final: bool) -> None:
    """Copy new output. Before the result exists an unusable log is not yet a log."""
    if follower is None:
        return
    try:
        follower.pump()
    except OSError as error:
        if final:
            raise ClientError(
                Exit.ERROR,
                f"{protocol.OUTPUT_LOG} of {request_id} cannot be read: {error.strerror or error}",
            ) from None


def wait_for_result(
    paths: SpoolPaths,
    request_id: str,
    console: Console,
    wait_timeout: float,
    timing: Timing,
    follower: Follower | None,
    detach: bool = False,
) -> Waited:
    """Poll until the request finishes, the wait times out, or no broker answers.

    A request no broker has acknowledged is never left behind: when the
    acknowledgement window or the wait timeout runs out first, it is withdrawn.
    """
    started = time.monotonic()
    acknowledged = False
    last_report = ""
    stale_since: float | None = None
    while True:
        result = read_result(paths, request_id)
        _pump(follower, request_id, final=result is not None)
        if result is not None:
            return Waited(result=result)
        status = read_status(paths, request_id)
        if status is not None:
            acknowledged = True
            report = f"{status.state}: {status.detail}" if status.detail else status.state
            if report != last_report:
                console.progress(report)
                last_report = report
            if detach:
                return Waited(
                    code=Exit.PENDING,
                    reason=f"submitted and {status.state}; collect the result with "
                    f"`taste-test wait {request_id}`",
                )
        now = time.monotonic()
        timed_out = now - started >= wait_timeout
        age = heartbeat_age(paths)
        fresh = age is not None and age <= timing.stale
        if not acknowledged:
            window_over = now - started >= timing.ack_window
            if (window_over or timed_out) and withdraw(paths, request_id):
                why = _withdrawn(paths, request_id, window_over, fresh)
                return Waited(code=Exit.NOT_RUN, reason=why)
        elif fresh:
            stale_since = None
        else:
            stale_since = now if stale_since is None else stale_since
            if now - stale_since >= timing.broker_lost:
                return Waited(
                    code=Exit.ERROR,
                    reason="the broker stopped responding; the request was left in place. "
                    f"Check `taste-test status {request_id}` once the broker is running again",
                )
        if timed_out:
            return Waited(
                code=Exit.PENDING,
                reason=f"still {status.state if status else 'waiting'} after {wait_timeout:g}s; "
                f"re-attach with `taste-test wait {request_id}`",
            )
        time.sleep(timing.poll)


def _withdrawn(paths: SpoolPaths, request_id: str, window_over: bool, serving: bool) -> str:
    """The reason line for a request the client has just withdrawn.

    `serving` says whether the broker's heartbeat is fresh: a live broker that
    did not answer is busy, and must not be reported as missing.
    """
    if read_status(paths, request_id) is not None:
        return (
            "a broker acknowledged the request at the moment it was withdrawn for lack "
            "of an answer; it was withdrawn and nothing was run. Run it again"
        )
    if not window_over:
        # The client's own time limit ended the wait, whatever the broker is doing.
        if serving:
            return (
                "--wait-timeout ran out before the broker serving this checkout took the "
                "request; it was withdrawn and nothing was run. Use a longer --wait-timeout"
            )
        return (
            "no broker acknowledged the request before --wait-timeout ran out; it was "
            "withdrawn and nothing was run. If no broker is serving this checkout: " + ASK_OPERATOR
        )
    return BROKER_BUSY if serving else NO_BROKER


def print_log(
    paths: SpoolPaths, request_id: str, name: str, console: Console, limits: Limits
) -> Shown | None:
    """Print a stored log under the output limits. None when there is no such log.

    A log that exists but is not a readable regular file is an error (exit 5).
    """
    try:
        handle = open_regular(paths, *RESULTS, request_id, name)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ClientError(
            Exit.ERROR, f"{name} of {request_id} cannot be read: {error.strerror or error}"
        ) from None
    with handle:
        try:
            return render_limited(handle, console.out, limits.head, limits.tail)
        except OSError as error:
            raise ClientError(
                Exit.ERROR, f"{name} of {request_id} cannot be read: {error.strerror or error}"
            ) from None


def summary_line(
    result: protocol.Result, code: Exit, request_id: str, shown: Shown, log: str
) -> str:
    """The summary line for a result (docs/interfaces.md §1).

    Every field but the last is a number or a fixed word. `log` is last because
    it is a path, which can contain spaces.
    """
    outcome = result.outcome if result.outcome in OUTCOMES else name_of(code)
    fields = [f"outcome={outcome}", f"exit={int(code)}"]
    if result.outcome in OUTCOMES_WITH_COMMAND_EXIT and result.command_exit is not None:
        fields.append(f"command-exit={result.command_exit}")
    fields += [
        f"id={request_id}",
        f"duration={round(result.seconds)}s",
        f"lines={shown.lines}",
        f"shown={shown.describe()}",
    ]
    if result.truncated:
        fields.append("truncated=yes")
    fields.append(f"log={log}")
    return " ".join(fields)


def report(
    paths: SpoolPaths,
    request_id: str,
    result: protocol.Result,
    console: Console,
    limits: Limits,
    streamed: Shown | None,
) -> Exit:
    """Print the output (unless already streamed), the reason, and the summary."""
    code = exit_for_outcome(result.outcome)
    setup_failed = result.outcome == "setup-failed"
    name = protocol.SETUP_LOG if setup_failed else protocol.OUTPUT_LOG
    if streamed is not None and not setup_failed:
        shown = streamed
    else:
        shown = print_log(paths, request_id, name, console, limits) or Shown(
            0, 0, limits.head, limits.tail
        )
    if result.outcome not in OUTCOMES:
        console.say(
            f"reason: the result names an outcome this client does not know "
            f"({result.outcome}); no verdict"
        )
    elif result.reason:
        console.say("reason: " + result.reason)
    elif result.outcome not in OUTCOMES_WITH_COMMAND_EXIT:
        console.say("reason: the broker gave no reason")
    log = os.path.relpath(paths.result_dir(request_id) / name)
    console.say(summary_line(result, code, request_id, shown, log))
    return code


def _interrupted(paths: SpoolPaths, request_id: str, console: Console) -> Exit:
    """End after a keyboard interrupt; an unacknowledged request is withdrawn first."""
    with contextlib.suppress(Exception):
        unanswered = (
            read_status(paths, request_id) is None and read_result(paths, request_id) is None
        )
        if unanswered and withdraw(paths, request_id):
            return end(
                console,
                Exit.NOT_RUN,
                "interrupted before a broker acknowledged the request; it was withdrawn "
                "and nothing was run",
                request_id,
            )
    return end(
        console,
        Exit.PENDING,
        f"interrupted; request {request_id} is not affected. Collect its result with "
        f"`taste-test wait {request_id}` or stop it with `taste-test cancel {request_id}`",
        request_id,
    )


def await_and_report(
    paths: SpoolPaths,
    request_id: str,
    console: Console,
    limits: Limits,
    wait_timeout: float,
    timing: Timing,
    detach: bool = False,
) -> Exit:
    """Shared tail of `run` and `wait`."""
    follower = None
    if limits.unlimited and not detach:
        follower = Follower(
            lambda: open_regular(paths, *RESULTS, request_id, protocol.OUTPUT_LOG), console.out
        )
    try:
        waited = wait_for_result(paths, request_id, console, wait_timeout, timing, follower, detach)
        if waited.result is not None:
            streamed = follower.shown() if follower is not None else None
            return report(paths, request_id, waited.result, console, limits, streamed)
    except KeyboardInterrupt:
        return _interrupted(paths, request_id, console)
    except Exception as error:
        return end(console, *failure(error), request_id)
    if detach and waited.code is Exit.PENDING:
        console.line(request_id)
    return end(console, waited.code, waited.reason, request_id)


def run(
    paths: SpoolPaths,
    command: str,
    args: list[str],
    console: Console,
    limits: Limits,
    wait_timeout: float,
    detach: bool,
    timing: Timing,
) -> Exit:
    try:
        ensure_spool(paths)
        prune(paths)
        request_id = submit(paths, command, args)
    except KeyboardInterrupt:
        return end(
            console,
            Exit.NOT_RUN,
            "interrupted before the request was submitted; nothing was run",
        )
    except Exception as error:
        return end(console, *failure(error))
    console.progress(f"submitted {request_id} (command {command})")
    return await_and_report(paths, request_id, console, limits, wait_timeout, timing, detach)


def require_known(paths: SpoolPaths, request_id: str) -> None:
    check_spool(paths)
    if not known(paths, request_id):
        raise ClientError(Exit.ERROR, f"no request {request_id} in {paths.spool}")


def wait(
    paths: SpoolPaths,
    request_id: str,
    console: Console,
    limits: Limits,
    wait_timeout: float,
    timing: Timing,
) -> Exit:
    try:
        require_known(paths, request_id)
    except ClientError as error:
        return end(console, error.code, error.message, request_id)
    return await_and_report(paths, request_id, console, limits, wait_timeout, timing)


def log(paths: SpoolPaths, request_id: str, console: Console, limits: Limits, setup: bool) -> Exit:
    require_known(paths, request_id)
    name = protocol.SETUP_LOG if setup else protocol.OUTPUT_LOG
    if print_log(paths, request_id, name, console, limits) is None:
        raise ClientError(Exit.ERROR, f"request {request_id} has no {name}")
    return DONE


def describe_request(paths: SpoolPaths, request_id: str) -> str:
    """One `status` line for a request (docs/interfaces.md §1)."""
    try:
        result = read_result(paths, request_id)
    except ClientError:
        error = int(Exit.ERROR)
        return f"id={request_id} state=finished outcome=error exit={error} detail='invalid result'"
    if result is not None:
        code = exit_for_outcome(result.outcome)
        outcome = result.outcome if result.outcome in OUTCOMES else name_of(code)
        return f"id={request_id} state=finished outcome={outcome} exit={int(code)}"
    status = read_status(paths, request_id)
    if status is not None:
        return f"id={request_id} state={status.state} detail={protocol.printable(status.detail)!r}"
    return f"id={request_id} state=submitted"


def status(paths: SpoolPaths, request_id: str | None, console: Console, timing: Timing) -> Exit:
    if request_id is not None:
        require_known(paths, request_id)
        console.line(describe_request(paths, request_id))
        return DONE
    check_spool(paths)
    age = heartbeat_age(paths)
    serving = age is not None and age <= timing.stale
    if serving:
        console.line(f"broker: serving this checkout (heartbeat {age:.0f}s ago)")
    else:
        console.line("broker: not serving this checkout")
        console.say(NO_BROKER)
    for name in request_ids(paths):
        console.line(describe_request(paths, name))
    return DONE if serving else NOT_SERVING


def cancel(paths: SpoolPaths, request_id: str, console: Console) -> Exit:
    check_spool(paths)
    request_cancel(paths, request_id)
    console.say(f"cancellation requested for {request_id}")
    return DONE
