"""broker_hostile_test.py — the broker against a spool arranged by a hostile agent.

Unit-level checks of each rule are in spool_test.py. These tests run the whole
broker and assert the consequences an operator cares about: nothing outside the
spool is read into a VM or written, a hostile request is handled exactly once,
and the broker keeps serving.

Coverage (contract: "Spool contents — Trust: untrusted"):
  - Load-bearing: a hard-linked result file is replaced, never written through
  - a hard-linked or FIFO request ends once as `error`/`rejected`, without a VM
  - a request directory that is a symbolic link is never served
  - malformed, oversized, and deeply nested request.json are rejected
  - Load-bearing: control characters in agent-chosen paths never reach the
    operator's terminal (with a control showing the test can fail)
  - a snapshot over the size limit is refused, not an error
  - a spool the broker may not read, and a flood of requests or of worktree
    records, neither stop the broker nor make it hold more and more
  - a defect while handling one request does not stop the broker
"""

import os
from pathlib import Path

import pytest
from conftest import World, git

from tart_taste_test import notices, protocol
from tart_taste_test.broker import Broker

ESCAPES = "\x1b]52;c;ZXZpbA==\x07"


def precious(tmp_path: Path) -> Path:
    victim = tmp_path / "operator-file"
    victim.write_text("precious\n")
    return victim


# Load-bearing: host safety — broker writes never land outside the spool.
def test_result_files_precreated_as_hard_links_are_not_written_through(
    world: World, tmp_path: Path
) -> None:
    world.write({"run_tests.sh": "echo guest-controlled bytes\n"})
    request_id = world.submit()
    result_dir = world.paths.result_dir(request_id)
    result_dir.mkdir()
    victims = {}
    for name in (
        protocol.OUTPUT_LOG,
        protocol.SETUP_LOG,
        protocol.STATUS_FILE,
        protocol.STATUS_FILE + protocol.TMP_SUFFIX,
        protocol.RESULT_FILE + protocol.TMP_SUFFIX,
    ):
        victims[name] = tmp_path / f"victim-{name}"
        victims[name].write_text("precious\n")
        os.link(victims[name], result_dir / name)

    world.serve()

    for victim in victims.values():
        assert victim.read_text() == "precious\n"
    assert world.result(request_id)["outcome"] == "passed"
    assert world.output(request_id) == "guest-controlled bytes\n"


def test_precreated_result_means_the_request_is_not_served(world: World, tmp_path: Path) -> None:
    """A request that already "has a result" is the agent fooling only itself."""
    request_id = world.submit()
    result_dir = world.paths.result_dir(request_id)
    result_dir.mkdir()
    victim = precious(tmp_path)
    os.link(victim, result_dir / protocol.RESULT_FILE)
    world.serve()
    assert victim.read_text() == "precious\n"
    assert world.audit() == [] and world.calls("clone") == []


def test_hard_linked_snapshot_is_never_sent_to_a_vm(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    source = world.paths.request_dir(request_id) / protocol.SOURCE_FILE
    source.unlink()
    os.link(precious(tmp_path), source)
    world.serve()
    result = world.result(request_id)
    assert result["outcome"] == "error"
    assert "more than one link" in result["reason"]
    assert world.calls("clone") == []


def test_fifo_request_does_not_block_the_broker(world: World) -> None:
    blocked = world.submit()
    request_file = world.paths.request_dir(blocked) / protocol.REQUEST_FILE
    request_file.unlink()
    os.mkfifo(request_file)
    honest = world.submit()
    world.serve()  # returns: an ordinary open of the FIFO would wait forever
    assert world.result(blocked)["outcome"] == "error"
    assert world.result(honest)["outcome"] == "passed"


def test_symlinked_request_directory_is_never_served(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    real = world.paths.request_dir(request_id)
    moved = tmp_path / "moved"
    real.rename(moved)
    os.symlink(moved, real)
    served = world.broker()
    assert served.serve(once=True) == 0
    assert served.serve(once=True) == 0
    assert world.audit() == [] and world.calls("clone") == []
    # Nothing was done through the link.
    assert sorted(p.name for p in moved.iterdir()) == [protocol.REQUEST_FILE, protocol.SOURCE_FILE]
    assert list(world.paths.results.iterdir()) == []


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"[" * 60000,
        b'{"schema": 1}',
        b"{" + b" " * 70000 + b"}",
        b'{"schema": 1, "id": "x", "command": "test; rm -rf /", "args": []}',
    ],
)
def test_malformed_requests_are_rejected_once_without_a_vm(world: World, content: bytes) -> None:
    request_id = world.submit()
    (world.paths.request_dir(request_id) / protocol.REQUEST_FILE).write_bytes(content)
    world.serve()
    world.serve()
    result = world.result(request_id)
    assert result["outcome"] == "rejected"
    assert result["reason"].startswith("invalid request:")
    assert len(world.audit()) == 1
    assert world.calls("clone") == []
    assert not (world.paths.request_dir(request_id) / protocol.SOURCE_FILE).exists()


def test_request_naming_a_command_string_never_reaches_a_shell(world: World) -> None:
    request_id = world.submit()
    request_file = world.paths.request_dir(request_id) / protocol.REQUEST_FILE
    document = request_file.read_text().replace('"command": "test"', '"command": "true; id"')
    request_file.write_text(document)
    world.serve()
    assert world.result(request_id)["outcome"] == "rejected"
    assert world.calls("exec") == []


def hostile_checkouts(world: World, tmp_path: Path) -> tuple[Path, str]:
    """A linked working tree and a forged record whose paths carry terminal escapes."""
    linked = tmp_path / f"evil{ESCAPES}tree"
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(linked))
    stranger = tmp_path / f"stranger{ESCAPES}dir"
    stranger.mkdir()
    forged = world.repo / ".git" / "worktrees" / "forged"
    forged.mkdir()
    (forged / "gitdir").write_bytes(os.fsencode(f"{stranger}/.git\n"))
    return linked, world.submit(root=linked)


# Load-bearing: agent-chosen bytes are printed only after `protocol.printable`.
def test_control_characters_never_reach_the_operators_terminal(
    world: World, tmp_path: Path
) -> None:
    linked, request_id = hostile_checkouts(world, tmp_path)
    world.serve()
    assert world.result(request_id, linked)["outcome"] == "passed"
    printed = "\n".join(world.events)
    # The hostile paths really were printed: in a "serving" event, in the
    # "claimed" event, and in the warning about the forged record.
    assert printed.count("evil") >= 2 and "stranger" in printed
    assert all(ch.isprintable() or ch == "\n" for ch in printed)


def test_the_control_character_test_fails_without_printable(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: with the filter off, the same run prints the escapes."""
    monkeypatch.setattr(protocol, "printable", lambda text: text)
    hostile_checkouts(world, tmp_path)
    world.serve()
    assert "\x1b" in "\n".join(world.events) and "\x07" in "\n".join(world.events)


def test_snapshot_over_the_limit_is_refused(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = world.submit()
    source = world.paths.request_dir(request_id) / protocol.SOURCE_FILE
    limit = source.stat().st_size + 100  # the request states a size within the limit
    monkeypatch.setattr(protocol, "MAX_SOURCE_BYTES", limit)
    with source.open("ab") as handle:
        handle.write(b"x" * 200)  # the file itself is over it
    world.serve()
    result = world.result(request_id)
    assert result["outcome"] == "rejected"  # the client exits 6, not 5
    assert f"larger than {limit} bytes" in result["reason"]
    assert world.calls("clone") == []
    assert list(world.state.staging.iterdir()) == []


@pytest.mark.parametrize("level", ["base", "spool", "requests"])
def test_spool_the_broker_may_not_read_is_reported_not_fatal(world: World, level: str) -> None:
    world.submit()
    target: Path = getattr(world.paths, level)
    try:
        for mode in (0o000, 0o400):
            target.chmod(mode)
            world.serve()  # returns; it neither crashes nor hangs
    finally:
        target.chmod(0o700)
    assert world.audit() == []
    world.serve()  # and once readable again, the request is served
    assert [line["outcome"] for line in world.audit()] == ["passed"]


def test_results_directory_the_broker_may_not_write_ends_the_request_once(world: World) -> None:
    request_id = world.submit()
    try:
        world.paths.results.chmod(0o500)
        served = world.broker()
        assert served.serve(once=True) == 0
        # No result could be written, so the audit log is the only record; there is one.
        [line] = world.audit()
        assert (line["id"], line["outcome"]) == (request_id, "error")
        assert any("result could not be written" in event for event in world.events)
        # A broker started later finds the request claimed and unanswered, and
        # records that; it does not run it.
        world.serve()
    finally:
        world.paths.results.chmod(0o700)
    assert [(line["id"], line["outcome"]) for line in world.audit()] == [(request_id, "error")] * 2
    assert world.calls("clone") == []


def test_scanning_hostile_checkouts_leaks_no_descriptors(world: World, tmp_path: Path) -> None:
    hostile_checkouts(world, tmp_path)
    broken = tmp_path / "broken"
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(broken))
    world.submit(root=broken)
    spool_dir = broken / protocol.DIR_NAME / protocol.SPOOL_NAME
    (spool_dir / protocol.RESULTS_NAME).rmdir()
    os.symlink(tmp_path, spool_dir / protocol.RESULTS_NAME)  # refused at the last step
    served = world.broker()
    for _ in range(3):
        served.tick(force=True)
    before = len(os.listdir("/dev/fd"))
    for _ in range(40):
        served.tick(force=True)
    assert len(os.listdir("/dev/fd")) == before


# A flood of requests takes a bounded number of places; the rest wait unseen.
def test_requests_beyond_the_pending_limit_are_not_acknowledged_until_there_is_room(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "MAX_PENDING_REQUESTS", 2)
    submitted = [world.submit("deploy") for _ in range(5)]
    served = world.broker()
    served.tick(force=True)
    acknowledged = [i for i in submitted if (world.paths.result_dir(i)).exists()]
    assert len(acknowledged) == 2
    assert protocol.parse_heartbeat(world.paths.heartbeat.read_bytes()).pending == 2
    assert served.serve(once=True) == 0  # places free up as requests end
    assert sorted(line["id"] for line in world.audit()) == sorted(submitted)


def test_warnings_about_linked_worktrees_are_bounded(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notices, "MAX_CHECKOUT_WARNINGS", 3)
    served = world.broker()
    for index in range(12):
        target = tmp_path / f"nowhere-{index}"
        target.mkdir()
        record = world.repo / ".git" / "worktrees" / f"forged-{index}"
        record.mkdir(parents=True)
        (record / "gitdir").write_text(f"{target}/.git\n")
        served.tick(force=True)
    warnings = [event for event in world.events if "warning:" in event]
    assert len(warnings) == 4  # three of them, then one saying the rest are not shown
    assert "further warnings about linked working trees are not shown" in warnings[-1]


def test_defect_while_handling_one_request_does_not_stop_the_broker(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = world.submit()
    second = world.submit()
    original = Broker._execute
    calls: list[str] = []

    def flaky(self: Broker, request_id: str, *rest: object) -> None:
        calls.append(request_id)
        if len(calls) == 1:
            raise RuntimeError("simulated defect")
        original(self, request_id, *rest)  # type: ignore[arg-type]

    monkeypatch.setattr(Broker, "_execute", flaky)
    world.serve()
    outcomes = {line["id"]: line for line in world.audit()}
    assert set(calls) == {first, second}
    broken, healthy = calls  # whichever the broker happened to see first
    assert outcomes[broken]["outcome"] == "error"
    assert "internal error (RuntimeError): simulated defect" in outcomes[broken]["reason"]
    assert outcomes[healthy]["outcome"] == "passed"


def test_heartbeat_and_acknowledgement_are_written_for_waiting_requests(world: World) -> None:
    request_id = world.submit()
    broker = world.broker()
    broker.tick(force=True)
    beat = protocol.parse_heartbeat(world.paths.heartbeat.read_bytes())
    assert beat.pending == 1 and beat.pid == os.getpid()
    status_file = world.paths.result_dir(request_id) / protocol.STATUS_FILE
    status = protocol.parse_status(status_file.read_bytes())
    assert (status.state, status.detail) == ("queued", "next")


def test_unregistered_checkout_is_never_served(world: World, tmp_path: Path) -> None:
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    git(stranger, "init", "--quiet", "--initial-branch", "main")
    request_id = world.submit(root=stranger)
    world.serve()
    spool_dir = stranger / protocol.DIR_NAME / protocol.SPOOL_NAME
    assert not (spool_dir / protocol.HEARTBEAT_FILE).exists()
    assert list((spool_dir / protocol.RESULTS_NAME).iterdir()) == []
    assert (spool_dir / protocol.REQUESTS_NAME / request_id / protocol.REQUEST_FILE).exists()
