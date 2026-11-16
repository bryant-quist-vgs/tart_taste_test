"""The spool protocol shared by the client and the broker.

Single owner of the spool's file names, identifier patterns, limits, and JSON
document shapes. Specified in docs/protocol.md §1-§3; this module quotes those
values and both sides import them from here.

Everything read through this module comes from a directory an untrusted agent
can write to, so every `parse_*` function validates key by key and raises
`ProtocolError` instead of returning a partially understood document.
"""

import json
import math
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tart_taste_test.exitcodes import OUTCOMES, OUTCOMES_WITH_COMMAND_EXIT

SCHEMA = 1

# Layout (docs/protocol.md §1).
DIR_NAME = ".taste-test"
SPOOL_NAME = "spool"
REQUESTS_NAME = "requests"
RESULTS_NAME = "results"
GITIGNORE_NAME = ".gitignore"
HEARTBEAT_FILE = "broker.json"
REQUEST_FILE = "request.json"
CLAIMED_FILE = "request.claimed.json"
WITHDRAWN_FILE = "request.withdrawn.json"
SOURCE_FILE = "source.tar"
CANCEL_FILE = "cancel"
STATUS_FILE = "status.json"
SETUP_LOG = "setup.log"
OUTPUT_LOG = "output.log"
RESULT_FILE = "result.json"
TMP_SUFFIX = ".tmp"

# Identifiers and limits (docs/protocol.md §2).
REQUEST_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
COMMAND_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_ID_RE = re.compile(r"[0-9a-f]{40,64}")
OUTCOME_RE = re.compile(r"[a-z][a-z-]{0,31}")
MAX_VERSION_CHARS = 64
MAX_ARGS = 64
MAX_ARG_BYTES = 4096
MAX_JSON_BYTES = 65536
MAX_SOURCE_BYTES = 268_435_456
MAX_LOG_BYTES = 67_108_864
MAX_REQUEST_AGE_SECONDS = 3600.0
MAX_PENDING_REQUESTS = 32  # per spool; further requests are not acknowledged
HEARTBEAT_INTERVAL_SECONDS = 2.0  # also the longest time between two spool scans
HEARTBEAT_STALE_SECONDS = 15.0
ACK_WINDOW_SECONDS = 20.0
BROKER_LOST_SECONDS = 60.0
RETENTION_SECONDS = 7 * 86400.0
CLIENT_GIT_TIMEOUT_SECONDS = 60
CLIENT_POLL_SECONDS = 0.25
SPOOL_DIR_MODE = 0o700
DEFAULT_COMMAND = "test"

STATE_QUEUED = "queued"
STATE_PREPARING = "preparing"
STATE_RUNNING = "running"
STATE_FINISHED = "finished"
STATES: frozenset[str] = frozenset({STATE_QUEUED, STATE_PREPARING, STATE_RUNNING, STATE_FINISHED})


class ProtocolError(ValueError):
    """A spool document is malformed, oversized, or of an unknown schema."""


@dataclass(frozen=True)
class Request:
    id: str
    command: str
    args: tuple[str, ...]
    sha256: str
    size: int
    client_version: str
    head: str | None


@dataclass(frozen=True)
class Status:
    id: str
    state: str
    detail: str
    updated: float


@dataclass(frozen=True)
class Result:
    id: str
    outcome: str
    command_exit: int | None
    reason: str
    seconds: float  # the broker's total for the request
    truncated: bool


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    version: str
    updated: float
    pending: int


def is_request_id(value: str) -> bool:
    return REQUEST_ID_RE.fullmatch(value) is not None


def is_command_name(value: str) -> bool:
    return COMMAND_NAME_RE.fullmatch(value) is not None


def new_request_id(now: datetime | None = None) -> str:
    """A fresh request ID: UTC creation time, then 12 random hex digits."""
    moment = now if now is not None else datetime.now(UTC)
    return f"{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(6)}"


def check_args(args: list[str] | tuple[str, ...]) -> None:
    """Raise `ProtocolError` unless `args` is within the per-request limits."""
    if len(args) > MAX_ARGS:
        raise ProtocolError(f"more than {MAX_ARGS} arguments")
    for arg in args:
        # Category Cc: C0, DEL and C1 (U+0080 to U+009F).
        if any(unicodedata.category(ch) == "Cc" for ch in arg):
            raise ProtocolError("an argument contains a control character")
        try:
            size = len(arg.encode("utf-8", "surrogateescape"))
        except UnicodeEncodeError:
            # A lone surrogate that does not stand for a byte (JSON can carry one).
            raise ProtocolError("an argument is not valid text") from None
        if size > MAX_ARG_BYTES:
            raise ProtocolError(f"an argument is longer than {MAX_ARG_BYTES} bytes")


def printable(text: str) -> str:
    """Collapse untrusted text to one line that is safe to print on a terminal.

    Control characters (including ESC) and other non-printable characters become
    spaces, and runs of whitespace collapse to one space.
    """
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def encode(document: dict[str, Any]) -> bytes:
    """Serialize a spool document."""
    return (json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def decode(data: bytes) -> dict[str, Any]:
    """Parse a spool document: size limit, JSON object, known schema.

    `NaN` and `Infinity`, which Python's parser would otherwise accept, are
    refused: no document in the protocol contains one.
    """
    if len(data) > MAX_JSON_BYTES:
        raise ProtocolError(f"document is larger than {MAX_JSON_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ProtocolError(f"not valid JSON: {error}") from None
    if not isinstance(document, dict):
        raise ProtocolError("document is not a JSON object")
    if _integer(document, "schema") != SCHEMA:
        raise ProtocolError(f"unknown schema {document.get('schema')!r}")
    return document


def _get(document: dict[str, Any], key: str) -> Any:
    if key not in document:
        raise ProtocolError(f"missing key {key!r}")
    return document[key]


def _string(document: dict[str, Any], key: str) -> str:
    value = _get(document, key)
    if not isinstance(value, str):
        raise ProtocolError(f"{key!r} is not a string")
    return value


def _integer(document: dict[str, Any], key: str) -> int:
    value = _get(document, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key!r} is not an integer")
    return value


def _number(document: dict[str, Any], key: str) -> float:
    """A finite number. `1e400` parses to infinity and a huge integer has no float."""
    value = _get(document, key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProtocolError(f"{key!r} is not a number")
    try:
        number = float(value)
    except OverflowError:
        raise ProtocolError(f"{key!r} is out of range") from None
    if not math.isfinite(number):
        raise ProtocolError(f"{key!r} is not a finite number")
    return number


def _object_or_null(document: dict[str, Any], key: str) -> None:
    value = _get(document, key)
    if value is not None and not isinstance(value, dict):
        raise ProtocolError(f"{key!r} is not an object or null")


def _object(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = _get(document, key)
    if not isinstance(value, dict):
        raise ProtocolError(f"{key!r} is not an object")
    return value


def _only_keys(document: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ProtocolError(f"unknown key {unknown[0]!r} in {where}")


def request_document(
    request_id: str,
    command: str,
    args: list[str],
    sha256: str,
    size: int,
    client_version: str,
    head: str | None,
) -> dict[str, Any]:
    """Build `request.json` (client side)."""
    return {
        "schema": SCHEMA,
        "id": request_id,
        "command": command,
        "args": list(args),
        "source": {"sha256": sha256, "bytes": size},
        "client": {"version": client_version, "head": head},
    }


def parse_request(data: bytes, expected_id: str) -> Request:
    """Validate `request.json` (broker side; untrusted input)."""
    document = decode(data)
    _only_keys(document, {"schema", "id", "command", "args", "source", "client"}, "request")
    request_id = _string(document, "id")
    if not is_request_id(request_id) or request_id != expected_id:
        raise ProtocolError("request id does not match its directory")
    command = _string(document, "command")
    if not is_command_name(command):
        raise ProtocolError("command is not a valid command name")
    raw_args = _get(document, "args")
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        raise ProtocolError("'args' is not an array of strings")
    check_args(raw_args)
    source = _object(document, "source")
    _only_keys(source, {"sha256", "bytes"}, "source")
    sha256 = _string(source, "sha256")
    if SHA256_RE.fullmatch(sha256) is None:
        raise ProtocolError("'sha256' is not 64 lowercase hex digits")
    size = _integer(source, "bytes")
    if size < 0 or size > MAX_SOURCE_BYTES:
        raise ProtocolError(f"snapshot size {size} is outside 0..{MAX_SOURCE_BYTES}")
    client = _object(document, "client")
    _only_keys(client, {"version", "head"}, "client")
    version = _string(client, "version")
    head = _get(client, "head")
    if head is not None and not (isinstance(head, str) and COMMIT_ID_RE.fullmatch(head)):
        raise ProtocolError("'head' is not a commit id or null")
    if len(version) > MAX_VERSION_CHARS or not version.isprintable():
        raise ProtocolError("'version' is not a short printable string")
    return Request(request_id, command, tuple(raw_args), sha256, size, version, head)


def status_document(request_id: str, state: str, detail: str, updated: float) -> dict[str, Any]:
    """Build `status.json` (broker side)."""
    return {
        "schema": SCHEMA,
        "id": request_id,
        "state": state,
        "detail": detail,
        "updated": updated,
    }


def parse_status(data: bytes) -> Status:
    document = decode(data)
    state = _string(document, "state")
    if state not in STATES:
        raise ProtocolError(f"unknown state {state!r}")
    return Status(
        _string(document, "id"), state, _string(document, "detail"), _number(document, "updated")
    )


def parse_result(data: bytes) -> Result:
    """Validate `result.json` against docs/protocol.md §3 (client side; untrusted).

    Every required key must be present and of its type. An outcome must look
    like an outcome name; one the client does not know is still returned, and
    maps to exit code 5. For a known outcome, `command_exit` is an integer
    exactly when the outcome is `passed` (then 0) or `failed` (then not 0).
    """
    document = decode(data)
    request_id = _string(document, "id")
    outcome = _string(document, "outcome")
    if OUTCOME_RE.fullmatch(outcome) is None:
        raise ProtocolError("'outcome' is not an outcome name")
    command_exit = _get(document, "command_exit")
    if command_exit is not None and (
        isinstance(command_exit, bool) or not isinstance(command_exit, int)
    ):
        raise ProtocolError("'command_exit' is not an integer or null")
    if outcome in OUTCOMES:
        if (outcome in OUTCOMES_WITH_COMMAND_EXIT) != (command_exit is not None):
            raise ProtocolError(f"'command_exit' does not fit outcome {outcome!r}")
        if command_exit is not None and (outcome == "passed") != (command_exit == 0):
            raise ProtocolError(f"'command_exit' contradicts outcome {outcome!r}")
    reason = _string(document, "reason")
    for key in ("command", "default_branch", "environment", "network"):
        _object_or_null(document, key)
    digest = _get(document, "source_sha256")
    if digest is not None and not (isinstance(digest, str) and SHA256_RE.fullmatch(digest)):
        raise ProtocolError("'source_sha256' is not 64 lowercase hex digits or null")
    output = _object(document, "output")
    if _integer(output, "bytes") < 0:
        raise ProtocolError("'bytes' is negative")
    truncated = _get(output, "truncated")
    if not isinstance(truncated, bool):
        raise ProtocolError("'truncated' is not a boolean")
    seconds = _object(document, "seconds")
    for key in seconds:
        if _number(seconds, key) < 0:
            raise ProtocolError(f"{key!r} is negative")
    return Result(request_id, outcome, command_exit, reason, _number(seconds, "total"), truncated)


def result_document(
    request_id: str,
    outcome: str,
    command_exit: int | None,
    reason: str,
    command: dict[str, Any] | None,
    default_branch: dict[str, Any] | None,
    environment: dict[str, Any] | None,
    network: dict[str, str] | None,
    source_sha256: str | None,
    output_bytes: int,
    truncated: bool,
    seconds: dict[str, float],
) -> dict[str, Any]:
    """Build `result.json` (broker side)."""
    return {
        "schema": SCHEMA,
        "id": request_id,
        "outcome": outcome,
        "command_exit": command_exit,
        "reason": reason,
        "command": command,
        "default_branch": default_branch,
        "environment": environment,
        "network": network,
        "source_sha256": source_sha256,
        "output": {"bytes": output_bytes, "truncated": truncated},
        "seconds": {key: round(value, 3) for key, value in seconds.items()},
    }


def heartbeat_document(pid: int, version: str, updated: float, pending: int) -> dict[str, Any]:
    """Build `broker.json` (broker side)."""
    return {
        "schema": SCHEMA,
        "pid": pid,
        "version": version,
        "updated": updated,
        "pending": pending,
    }


def parse_heartbeat(data: bytes) -> Heartbeat:
    document = decode(data)
    return Heartbeat(
        _integer(document, "pid"),
        _string(document, "version"),
        _number(document, "updated"),
        _integer(document, "pending"),
    )
