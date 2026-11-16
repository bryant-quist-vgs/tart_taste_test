"""What the broker records about a request: result.json, the audit line, and
which request is in flight."""

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tart_taste_test import protocol
from tart_taste_test.registry import Repository, State

# An agent chooses its request's arguments and can make requests end at once, so
# what an audit line takes from a request is bounded: arguments longer than this
# in total are recorded as a count and a digest, and a longer reason is cut.
AUDIT_ARGS_BYTES = 1024
AUDIT_REASON_CHARS = 2048
CUT = " [cut]"


@dataclass
class Report:
    """What is known about a request when it ends."""

    request_id: str
    outcome: str = "error"
    reason: str = ""
    command_exit: int | None = None
    command: dict[str, Any] | None = None
    default_branch: dict[str, Any] | None = None
    environment: dict[str, Any] | None = None
    network: dict[str, str] | None = None
    source_sha256: str | None = None
    output_bytes: int = 0
    truncated: bool = False
    seconds: dict[str, float] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return protocol.result_document(
            self.request_id,
            self.outcome,
            self.command_exit,
            self.reason,
            self.command,
            self.default_branch,
            self.environment,
            self.network,
            self.source_sha256,
            self.output_bytes,
            self.truncated,
            self.seconds,
        )


def bounded(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with what an agent chose kept within the audit line's limits.

    Arguments over `AUDIT_ARGS_BYTES` become `{"count", "bytes", "sha256"}`
    (the digest is over the arguments as UTF-8, each followed by a NUL byte),
    and a reason over `AUDIT_REASON_CHARS` is cut. `result.json` keeps both in
    full; only the operator's log is bounded.
    """
    line = dict(document)
    reason = line.get("reason")
    if isinstance(reason, str) and len(reason) > AUDIT_REASON_CHARS:
        line["reason"] = reason[:AUDIT_REASON_CHARS] + CUT
    command = line.get("command")
    if isinstance(command, dict):
        packed = b"".join(
            str(arg).encode("utf-8", "surrogateescape") + b"\0" for arg in command.get("args", [])
        )
        if len(packed) > AUDIT_ARGS_BYTES:
            summary = {
                "count": len(command["args"]),
                "bytes": len(packed),
                "sha256": hashlib.sha256(packed).hexdigest(),
            }
            line["command"] = dict(command, args=summary)
    return line


def append(
    state: State,
    document: dict[str, Any],
    repository: Repository,
    checkout: Path,
    run_vm: str | None,
) -> None:
    """Append one line to the operator's audit log. Raises `OSError`.

    The line is the result document plus what only the operator's side knows.
    The log lives in operator state, which agents cannot write, so it is the
    record to consult when a result in a spool is in doubt.
    """
    line = dict(
        bounded(document),
        time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        repository={"key": repository.key, "path": repository.path},
        checkout=str(checkout),
        run_vm=run_vm,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(state.audit, flags, 0o600), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")


def note_in_flight(state: State, checkout: Path, request_id: str) -> None:
    """Record in operator state the request the broker is about to claim. Raises `OSError`.

    Agents choose request IDs and can plant claimed requests in their own spool.
    After an unclean stop this record, which they cannot write, says which
    request the broker really left behind, so that one is sure of its audit line.
    """
    temporary = state.in_flight.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(temporary, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump({"checkout": str(checkout), "id": request_id}, handle)
    os.replace(temporary, state.in_flight)


def in_flight(state: State) -> tuple[str, str] | None:
    """The (checkout, request ID) a previous broker recorded and never cleared, if any."""
    try:
        data = json.loads(state.in_flight.read_text(encoding="utf-8"))
        return str(data["checkout"]), str(data["id"])
    except OSError, ValueError, KeyError, TypeError:
        return None


def clear_in_flight(state: State) -> None:
    with contextlib.suppress(OSError):
        state.in_flight.unlink()
