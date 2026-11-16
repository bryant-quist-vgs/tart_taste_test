"""protocol_test.py — spool documents, identifiers, and limits (docs/protocol.md §2-§3).

Coverage:
  - request IDs and command names: accepted and refused shapes
  - request.json round trip (client builds, broker parses)
  - Load-bearing trust boundary: every malformed or hostile request.json is
    refused with ProtocolError, never partially accepted
  - argument limits, including control characters
  - status and heartbeat parsing
  - Load-bearing: result.json is validated in full (required keys, outcome
    shape, command_exit fitting the outcome, finite numbers); the duration is
    the broker's total; the example in docs/protocol.md is a valid result
  - printable(): untrusted text made safe for a terminal
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tart_taste_test import protocol

ID = "20261115T183000Z-3f9a1c2b4d5e"
DIGEST = "a" * 64
DOCS = Path(__file__).resolve().parent.parent / "docs"


def request(**changes: Any) -> bytes:
    document = protocol.request_document(ID, "test", ["-k", "x"], DIGEST, 10, "0.1.0", None)
    document.update(changes)
    return protocol.encode(document)


def test_new_request_id_has_the_documented_shape() -> None:
    made = protocol.new_request_id(datetime(2026, 11, 15, 18, 30, 0, tzinfo=UTC))
    assert made.startswith("20261115T183000Z-")
    assert protocol.is_request_id(made)
    assert protocol.new_request_id() != protocol.new_request_id()


@pytest.mark.parametrize(
    "value",
    ["", "..", "../x", "20261115T183000Z-3F9A1C2B4D5E", ID + "x", "x" + ID, ID + "\n", "a/b"],
)
def test_malformed_request_ids_are_refused(value: str) -> None:
    assert not protocol.is_request_id(value)


@pytest.mark.parametrize("value", ["test", "lint", "a", "unit-tests_2", "a" * 32])
def test_command_names_are_accepted(value: str) -> None:
    assert protocol.is_command_name(value)


@pytest.mark.parametrize(
    "value", ["", "Test", "1test", "-x", "a b", "a;b", "make test", "a" * 33, "tést", "a\n"]
)
def test_command_strings_are_not_command_names(value: str) -> None:
    assert not protocol.is_command_name(value)


def test_request_round_trip() -> None:
    parsed = protocol.parse_request(request(), ID)
    assert parsed == protocol.Request(ID, "test", ("-k", "x"), DIGEST, 10, "0.1.0", None)


# Load-bearing: the only place untrusted request data is admitted.
@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"", "empty"),
        (b"not json", "not json"),
        (b"[]", "array"),
        (b'"x"', "string"),
        (b"[" * 60000, "deep nesting"),
        (b"{" + b" " * protocol.MAX_JSON_BYTES + b"}", "oversized"),
        (request(schema=2), "unknown schema"),
        (request(schema=True), "boolean schema"),
        (request(id="20261115T183000Z-000000000000"), "id is not the directory name"),
        (request(id="../../etc"), "traversal id"),
        (request(command="make test"), "command string"),
        (request(command=7), "command type"),
        (request(args="x"), "args type"),
        (request(args=[1]), "arg type"),
        (request(args=["x"] * 65), "too many args"),
        (request(args=["x" * 4097]), "arg too long"),
        (request(args=["a\x00b"]), "NUL in arg"),
        (request(args=["a\x1b]52;c;x\x07"]), "escape sequence in arg"),
        (request(source={"sha256": "A" * 64, "bytes": 1}), "uppercase digest"),
        (request(source={"sha256": DIGEST, "bytes": -1}), "negative size"),
        (request(source={"sha256": DIGEST, "bytes": protocol.MAX_SOURCE_BYTES + 1}), "too big"),
        (request(source={"sha256": DIGEST, "bytes": True}), "boolean size"),
        (request(source={"sha256": DIGEST, "bytes": 1, "file": "/etc/passwd"}), "extra key"),
        (request(client={"version": "1", "head": "not a commit"}), "bad head"),
        (request(client={"version": "x" * 65, "head": None}), "long version"),
        (request(run="rm -rf /"), "unknown top-level key"),
        (request(image="evil"), "image key"),
    ],
)
def test_hostile_requests_are_refused(data: bytes, why: str) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_request(data, ID)


def test_missing_keys_are_refused() -> None:
    for key in ("id", "command", "args", "source", "client"):
        document = json.loads(request())
        del document[key]
        with pytest.raises(protocol.ProtocolError, match="missing key"):
            protocol.parse_request(protocol.encode(document), ID)


def test_status_and_heartbeat_round_trip() -> None:
    status = protocol.parse_status(
        protocol.encode(protocol.status_document(ID, "running", "test", 12.5))
    )
    assert status == protocol.Status(ID, "running", "test", 12.5)
    beat = protocol.parse_heartbeat(
        protocol.encode(protocol.heartbeat_document(42, "0.1.0", 99.0, 3))
    )
    assert beat == protocol.Heartbeat(42, "0.1.0", 99.0, 3)
    with pytest.raises(protocol.ProtocolError, match="unknown state"):
        protocol.parse_status(protocol.encode(protocol.status_document(ID, "paused", "", 1.0)))


SECONDS = {"preparing": 3.4, "running": 78.9, "total": 82.3}  # the broker's real key set


def result(outcome: str = "passed", command_exit: int | None = 0) -> dict[str, Any]:
    return protocol.result_document(
        ID, outcome, command_exit, "", None, None, None, None, DIGEST, 20, True, SECONDS
    )


def test_result_round_trip_takes_the_total_as_the_duration() -> None:
    parsed = protocol.parse_result(protocol.encode(result("failed", 2)))
    assert parsed == protocol.Result(ID, "failed", 2, "", 82.3, True)


def test_result_shown_in_the_protocol_document_is_valid() -> None:
    text = (DOCS / "protocol.md").read_text(encoding="utf-8")
    example = text.split("### `result.json`", 1)[1].split("```json\n", 1)[1].split("```", 1)[0]
    document = json.loads(example.replace("\u2026", ID))
    document["source_sha256"] = DIGEST
    parsed = protocol.parse_result(protocol.encode(document))
    assert (parsed.outcome, parsed.command_exit, parsed.seconds) == ("failed", 2, 82.3)


@pytest.mark.parametrize(
    "change",
    [
        {"command_exit": "0"},
        {"command_exit": True},
        {"output": {"truncated": "no", "bytes": 0}},
        {"output": {"truncated": False}},
        {"output": {"truncated": False, "bytes": -1}},
        {"seconds": 5},
        {"seconds": {"running": 1.0}},
        {"seconds": {"total": -1.0}},
        {"seconds": {"total": 1.0, "running": "2"}},
        {"outcome": "PASSED"},
        {"outcome": "passed exit=0"},
        {"outcome": ""},
        {"outcome": 0},
        {"reason": None},
        {"source_sha256": "abc"},
        {"command": "make test"},
        {"network": []},
        {"id": 7},
    ],
)
def test_malformed_results_are_refused(change: dict[str, Any]) -> None:
    document = result()
    document.update(change)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_result(protocol.encode(document))


@pytest.mark.parametrize(
    "key",
    [
        "id",
        "outcome",
        "command_exit",
        "reason",
        "command",
        "default_branch",
        "environment",
        "network",
        "source_sha256",
        "output",
        "seconds",
    ],
)
def test_results_lacking_a_required_key_are_refused(key: str) -> None:
    document = result()
    del document[key]
    with pytest.raises(protocol.ProtocolError, match="missing key"):
        protocol.parse_result(protocol.encode(document))


# Load-bearing: a result can say `passed` only with a command that exited 0.
@pytest.mark.parametrize(
    ("outcome", "command_exit"),
    [
        ("passed", None),
        ("passed", 3),
        ("failed", None),
        ("failed", 0),
        ("error", 0),
        ("timed-out", 1),
        ("rejected", 0),
        ("cancelled", 2),
        ("setup-failed", 1),
    ],
)
def test_command_exit_must_fit_the_outcome(outcome: str, command_exit: int | None) -> None:
    with pytest.raises(protocol.ProtocolError, match="command_exit"):
        protocol.parse_result(protocol.encode(result(outcome, command_exit)))


def test_an_outcome_from_a_newer_broker_is_parsed_and_left_to_the_exit_table() -> None:
    parsed = protocol.parse_result(protocol.encode(result("quarantined", None)))
    assert parsed.outcome == "quarantined"


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400", "1" + "0" * 400])
def test_numbers_that_are_not_finite_are_refused_everywhere(number: str) -> None:
    beat = f'{{"schema": 1, "pid": 1, "version": "x", "updated": {number}, "pending": 0}}'
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_heartbeat(beat.encode())
    state = f'{{"schema": 1, "id": "{ID}", "state": "queued", "detail": "", "updated": {number}}}'
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_status(state.encode())
    text = protocol.encode(result()).decode().replace('"total": 82.3', f'"total": {number}')
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_result(text.encode())


def test_printable_removes_control_characters_and_newlines() -> None:
    hostile = "ok\x1b]52;c;ZXZpbA==\x07\nnext\tline\x00 \x7f end"
    cleaned = protocol.printable(hostile)
    assert cleaned == "ok ]52;c;ZXZpbA== next line end"
    assert all(ch.isprintable() for ch in cleaned)


def test_check_args_accepts_ordinary_arguments() -> None:
    protocol.check_args(["-k", "two words", "--help", "$(id)", "é", ""])
    protocol.check_args(["bytes-\udcff"])  # a non-UTF-8 byte from the command line


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\x7f", "\x85", "\x9b"])
def test_check_args_refuses_every_control_character(control: str) -> None:
    with pytest.raises(protocol.ProtocolError, match="control character"):
        protocol.check_args([f"a{control}b"])


def test_check_args_refuses_text_that_cannot_be_bytes() -> None:
    # JSON can carry a lone surrogate that stands for no byte at all.
    with pytest.raises(protocol.ProtocolError, match="not valid text"):
        protocol.check_args(["\ud800"])
