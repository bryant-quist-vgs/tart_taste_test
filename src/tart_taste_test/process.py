"""Host subprocesses with a time limit that are never left behind.

The one place the broker starts `git` and `tart`, and the helper its wait loops
share. Every child runs in a session of its own, so that when it has to be
stopped its helpers go with it (`git fetch` runs `ssh` or `git-remote-https`;
`tart run` runs Softnet). Every wait calls `tick` so the broker keeps scanning
spools, and if a call ends early (the time limit, an exception from `tick`, a
request to stop) the child's whole session is killed and the child reaped before
the exception continues. A child that exits by itself is taken to have ended its
own helpers: once it has been reaped its process-group ID may belong to someone
else, so nothing is signalled after that.
"""

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import IO

# How often a wait loop calls `tick`, and so how soon a cancel marker or a
# request to stop is noticed (docs/vm-handling.md §1).
POLL_SECONDS = 0.2
REAP_SECONDS = 10.0


class TimedOut(Exception):
    """The process did not finish within its time limit and has been killed."""


def nothing() -> None:
    """A `tick` that does nothing."""
    return None


def never() -> bool:
    """A `cancelled` that never is."""
    return False


def start(
    argv: list[str],
    stdin: int | IO[bytes] = subprocess.DEVNULL,
    stdout: int | IO[bytes] = subprocess.DEVNULL,
    stderr: int | IO[bytes] = subprocess.STDOUT,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.Popen[bytes]:
    """Start `argv` (an argument list; no shell) as the leader of a new session."""
    return subprocess.Popen(
        argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )


def stop(process: subprocess.Popen[bytes]) -> None:
    """Kill the process and everything in its session, then reap it.

    The session is signalled whenever the child has not been reaped yet, even
    if the child itself has already exited: a helper it left behind (one that
    still holds the output pipe, say) goes too. Until the child is reaped its
    process ID, and so the group ID, cannot have been given to anything else.
    """
    if process.returncode is None:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=REAP_SECONDS)


def wait(
    process: subprocess.Popen[bytes], timeout: float, tick: Callable[[], None] = nothing
) -> int:
    """Wait for the process and return its exit status.

    Raises `TimedOut` after `timeout` seconds. On that, or on any exception
    raised by `tick`, the process is stopped first.
    """
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                return process.wait(timeout=POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline:
                raise TimedOut()
            tick()
    except BaseException:
        stop(process)
        raise
