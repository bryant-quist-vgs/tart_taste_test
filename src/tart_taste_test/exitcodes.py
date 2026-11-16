"""Exit codes of the `taste-test` client and the outcome-to-exit-code map.

This module is the single owner of these values. They are specified in
docs/interfaces.md §2 ("Exit codes"); a test fails if the two disagree.
"""

import contextlib
import os
import textwrap
from dataclasses import dataclass
from enum import IntEnum


class Exit(IntEnum):
    """Process exit status of `taste-test run` and `taste-test wait`."""

    PASSED = 0
    FAILED = 1
    USAGE = 2
    SETUP_FAILED = 3
    TIMED_OUT = 4
    ERROR = 5
    NOT_RUN = 6
    CANCELLED = 7
    PENDING = 8


def silence(stream: object) -> None:
    """Point a standard stream whose reader is gone at /dev/null.

    Later writes and the flush at interpreter exit then succeed, so a closed
    pipe cannot replace the exit code (Python exits 120 when that flush fails).
    Both programs use this, so the exit code each documents is the one it returns.
    """
    with contextlib.suppress(OSError, ValueError, AttributeError):
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, stream.fileno())  # type: ignore[attr-defined]
        os.close(null)


@dataclass(frozen=True)
class ExitDoc:
    """One row of the exit-code table shown by `--help` and in the docs."""

    code: Exit
    name: str
    meaning: str
    action: str


EXIT_TABLE: tuple[ExitDoc, ...] = (
    ExitDoc(Exit.PASSED, "passed", "The command ran in the VM and exited 0.", "Proceed."),
    ExitDoc(
        Exit.FAILED,
        "failed",
        "The command ran in the VM and exited non-zero.",
        "Read the output; fix the candidate.",
    ),
    ExitDoc(
        Exit.USAGE,
        "usage",
        "Bad arguments. Nothing was submitted.",
        "Fix the invocation.",
    ),
    ExitDoc(
        Exit.SETUP_FAILED,
        "setup-failed",
        "The environment could not be built from the default branch. The candidate was never run.",
        "Not caused by the candidate; report to the operator.",
    ),
    ExitDoc(
        Exit.TIMED_OUT,
        "timed-out",
        "The command exceeded its time limit and was stopped.",
        "Treat as a failure; look for a hang.",
    ),
    ExitDoc(
        Exit.ERROR,
        "error",
        "Infrastructure failure: the VM did not start, Tart failed, "
        "or the broker stopped responding. No verdict.",
        "Retry once; then report to the operator.",
    ),
    ExitDoc(
        Exit.NOT_RUN,
        "not-run",
        "Nothing was run: no broker took the request (none is serving this checkout, or one "
        "is but did not take the request in time), or the broker rejected the request (no "
        "repository config on the default branch, over a limit, unknown command).",
        "Report to the operator; the reason line says what is missing.",
    ),
    ExitDoc(
        Exit.CANCELLED,
        "cancelled",
        "The request was cancelled before it finished. No verdict.",
        "Nothing.",
    ),
    ExitDoc(
        Exit.PENDING,
        "pending",
        "The request is still queued or running: --wait-timeout expired, or --detach was given.",
        "Run `taste-test wait ID`.",
    ),
)

# Outcome names written by the broker in result.json (docs/protocol.md §3).
OUTCOME_EXIT: dict[str, Exit] = {
    "passed": Exit.PASSED,
    "failed": Exit.FAILED,
    "setup-failed": Exit.SETUP_FAILED,
    "timed-out": Exit.TIMED_OUT,
    "error": Exit.ERROR,
    "rejected": Exit.NOT_RUN,
    "cancelled": Exit.CANCELLED,
}

OUTCOMES: frozenset[str] = frozenset(OUTCOME_EXIT)

# Outcomes that carry the guest command's exit status.
OUTCOMES_WITH_COMMAND_EXIT: frozenset[str] = frozenset({"passed", "failed"})


def exit_for_outcome(outcome: str) -> Exit:
    """Map a result outcome to the client's exit code. Unknown outcomes are errors."""
    return OUTCOME_EXIT.get(outcome, Exit.ERROR)


def name_of(code: Exit) -> str:
    """The name the exit-code table gives `code` (`not-run` for 6, and so on)."""
    return next(row.name for row in EXIT_TABLE if row.code == code)


_HELP_WIDTH = 79
_HELP_INDENT = 19


def help_epilog() -> str:
    """The exit-code section appended to `taste-test --help`."""
    lines = [
        "exit codes (run, wait):",
        "  The exit code states the outcome. It is never the test command's own exit",
        "  status (that is `command-exit` on the summary line), and --head/--tail",
        "  never change it. 0 always and only means the tests passed. Codes 3 and",
        "  5-8 carry no verdict about the code under test.",
        "",
    ]
    for row in EXIT_TABLE:
        meaning = textwrap.wrap(row.meaning, width=_HELP_WIDTH - _HELP_INDENT)
        lines.append(f"  {int(row.code)}  {row.name:<13} {meaning[0]}")
        lines += [" " * _HELP_INDENT + extra for extra in meaning[1:]]
        action = textwrap.wrap("-> " + row.action, width=_HELP_WIDTH - _HELP_INDENT)
        lines.append(" " * _HELP_INDENT + action[0])
        lines += [" " * (_HELP_INDENT + 3) + extra for extra in action[1:]]
    lines += [
        "",
        "  log, status, cancel: 0 success, 2 bad arguments, 5 unknown request ID or",
        "  unreadable files. `status` without an ID: 0 when a broker is serving this",
        "  checkout, 6 when none is.",
    ]
    return "\n".join(lines)
