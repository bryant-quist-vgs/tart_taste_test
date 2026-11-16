"""Helpers shared by the client test files: a console to capture, and a stand-in broker.

The broker is played by the test: these functions write the heartbeat, status,
log, and result files a broker would write.
"""

import io
import time

from conftest import World

from tart_taste_test import client, client_spool, protocol
from tart_taste_test.exitcodes import Exit

FAST = client.Timing(poll=0.01, ack_window=0.15, stale=15.0, broker_lost=0.15)
NO_LIMITS = client.Limits()


def console(quiet: bool = False) -> tuple[client.Console, io.BytesIO, io.StringIO]:
    out, err = io.BytesIO(), io.StringIO()
    return client.Console(out, err, quiet), out, err


def beat(world: World, age: float = 0.0) -> None:
    client_spool.ensure_spool(world.paths)
    document = protocol.heartbeat_document(1, "0.1.0", time.time() - age, 0)
    world.paths.heartbeat.write_bytes(protocol.encode(document))


def status(world: World, request_id: str, state: str, detail: str = "") -> None:
    directory = world.paths.result_dir(request_id)
    directory.mkdir(parents=True, exist_ok=True)
    document = protocol.status_document(request_id, state, detail, time.time())
    (directory / protocol.STATUS_FILE).write_bytes(protocol.encode(document))


def finish(
    world: World,
    request_id: str,
    outcome: str,
    command_exit: int | None = None,
    reason: str = "",
    output: bytes = b"",
    setup: bytes | None = None,
    truncated: bool = False,
) -> None:
    directory = world.paths.result_dir(request_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / protocol.OUTPUT_LOG).write_bytes(output)
    if setup is not None:
        (directory / protocol.SETUP_LOG).write_bytes(setup)
    document = protocol.result_document(
        request_id,
        outcome,
        command_exit,
        reason,
        None,
        None,
        None,
        None,
        "a" * 64,
        len(output),
        truncated,
        {"running": 12.4, "total": 12.4},
    )
    (directory / protocol.RESULT_FILE).write_bytes(protocol.encode(document))


def wait(
    world: World,
    request_id: str,
    limits: client.Limits = NO_LIMITS,
    wait_timeout: float = 5.0,
) -> tuple[Exit, str, str]:
    con, out, err = console()
    code = client.wait(world.paths, request_id, con, limits, wait_timeout, FAST)
    return code, out.getvalue().decode(), err.getvalue()
