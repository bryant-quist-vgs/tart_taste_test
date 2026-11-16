"""broker_limits_test.py — what an agent can drive up, and where each amount stops.

Built against contract v1 (contracts/taste_test_broker_contract.md). An agent
writes its own spool and its repository's worktree records, and can make
requests end at once. Each test here drives one such amount past its limit on a
real `Broker` (fake Tart) and checks that the broker stays within the limit and
keeps serving.

Coverage:
  - Load-bearing: an audit-log line stays small whatever the request carries
  - a claimed request that is refused leaves no snapshot in the spool
  - an argument that is not valid text is rejected, not an internal error
  - a request whose directory is gone when its turn comes was withdrawn
  - start-up recovery logs a bounded number of planted requests, and the one the
    previous broker recorded as in flight is among them
  - Load-bearing: only so many linked working trees of a repository are served
    at once; one being served keeps its place, one over the limit is not opened,
    and another repository's working trees are not kept out
  - a working tree that comes and goes prints a bounded number of lines
  - listing spools is bounded per scan and every spool gets its turn; a listing
    does not outlive the spool it was read from
  - Load-bearing: a forged record in another repository does not take a working
    tree away from the repository it belongs to
  - a waiting request is told its place again when the place changes
  - output that cannot be printed does not stop the broker
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import FAST, World, git

from tart_taste_test import (
    audit,
    broker,
    checkouts,
    client_spool,
    notices,
    protocol,
    registry,
    repo_config,
    served,
)
from tart_taste_test.broker import Broker
from tart_taste_test.spool import Spool


def status_detail(world: World, request_id: str, root: Path | None = None) -> str | None:
    """The detail of the request's status, or None before it has one."""
    paths = client_spool.SpoolPaths(root or world.repo)
    try:
        data = (paths.result_dir(request_id) / protocol.STATUS_FILE).read_bytes()
    except OSError:
        return None
    return protocol.parse_status(data).detail


def worktree(world: World, path: Path) -> Path:
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(path))
    return path


def heartbeat(root: Path) -> Path:
    return client_spool.SpoolPaths(root).heartbeat


# Load-bearing: requests that end at once cannot fill the operator's disk through the log.
def test_audit_line_stays_small_whatever_the_request_carries(world: World) -> None:
    args = ["x" * 4000 for _ in range(10)]
    request_id = world.submit("deploy", args)  # no such command: it ends at once
    world.serve()

    result = world.result(request_id)
    assert result["outcome"] == "rejected"
    assert result["command"]["args"] == args  # the spool copy keeps everything
    [raw] = world.state.audit.read_text().splitlines()
    assert len(raw) < 2048
    packed = b"".join(arg.encode() + b"\0" for arg in args)
    assert json.loads(raw)["command"]["args"] == {
        "count": 10,
        "bytes": len(packed),
        "sha256": hashlib.sha256(packed).hexdigest(),
    }


def test_small_arguments_and_reasons_are_logged_in_full_and_long_reasons_are_cut() -> None:
    document = {"reason": "short", "command": {"name": "test", "run": None, "args": ["-k", "a b"]}}
    assert audit.bounded(document) == document
    long = dict(document, reason="r" * (audit.AUDIT_REASON_CHARS + 500))
    cut = audit.bounded(long)["reason"]
    assert len(cut) == audit.AUDIT_REASON_CHARS + len(audit.CUT) and cut.endswith(audit.CUT)
    assert audit.bounded({"command": None, "reason": ""}) == {"command": None, "reason": ""}


def test_refused_claim_leaves_no_snapshot_in_the_spool(world: World, tmp_path: Path) -> None:
    linked = world.submit()
    os.link(world.paths.request_dir(linked) / protocol.REQUEST_FILE, tmp_path / "second-name")
    fifo = world.submit()
    (world.paths.request_dir(fifo) / protocol.REQUEST_FILE).unlink()
    os.mkfifo(world.paths.request_dir(fifo) / protocol.REQUEST_FILE)
    world.serve()
    for request_id in (linked, fifo):
        assert world.result(request_id)["outcome"] == "error"
        assert not (world.paths.request_dir(request_id) / protocol.SOURCE_FILE).exists()
    assert world.calls("clone") == []


def test_argument_that_is_not_valid_text_is_rejected(world: World) -> None:
    request_id = world.submit()
    request_file = world.paths.request_dir(request_id) / protocol.REQUEST_FILE
    document = json.loads(request_file.read_text())
    document["args"] = ["\ud800"]  # written to the file as the escape \\ud800
    request_file.write_text(json.dumps(document))
    world.serve()
    result = world.result(request_id)
    assert result["outcome"] == "rejected"
    assert result["reason"] == "invalid request: an argument is not valid text"


def test_start_up_recovery_logs_a_bounded_number_of_planted_requests(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "MAX_RECOVERIES_LOGGED", 2)
    planted = [world.submit() for _ in range(5)]
    for request_id in planted:
        home = world.paths.request_dir(request_id)
        os.rename(home / protocol.REQUEST_FILE, home / protocol.CLAIMED_FILE)
    world.serve()
    # Every one is ended, so no client waits for ever and none is found again...
    assert [world.result(request_id)["outcome"] for request_id in planted] == ["error"] * 5
    # ...but the operator's log and terminal take only the first few.
    assert len(world.audit()) == 2
    assert len([event for event in world.events if broker.LEFT_BEHIND in event]) == 2
    warnings = [event for event in world.events if "held 5 claimed requests" in event]
    assert len(warnings) == 1
    world.serve()
    assert len(world.audit()) == 2


# Load-bearing: agents choose request IDs, so the order of IDs must not decide
# whether the request a broker really left behind reaches the audit log.
def test_request_recorded_as_in_flight_is_logged_before_planted_ones(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "MAX_RECOVERIES_LOGGED", 2)
    claimed = sorted(world.submit() for _ in range(5))
    for request_id in claimed:
        home = world.paths.request_dir(request_id)
        os.rename(home / protocol.REQUEST_FILE, home / protocol.CLAIMED_FILE)
    genuine = claimed[-1]  # every planted ID sorts before it
    audit.note_in_flight(world.state, Path(world.repository.path), genuine)
    world.serve()
    assert [line["id"] for line in world.audit()] == [genuine, claimed[0]]
    assert not world.state.in_flight.exists()


def test_broker_records_the_request_in_flight_and_clears_the_record(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = world.submit()
    cleared: list[bool] = []
    real = audit.clear_in_flight

    def clear(state: registry.State) -> None:
        cleared.append(audit.in_flight(state) == (world.repository.path, request_id))
        real(state)

    monkeypatch.setattr(audit, "clear_in_flight", clear)
    world.serve()
    assert True in cleared  # the record named the request while it ran
    assert audit.in_flight(world.state) is None
    assert world.state.in_flight.parent == world.state.root  # operator state, not the spool


def test_request_whose_directory_is_gone_was_withdrawn(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = world.submit()
    real = Spool.claim

    def withdrawn_first(self: Spool, wanted: str) -> bytes | None:
        # The client's withdrawal, completed between the scan and the claim.
        shutil.rmtree(world.paths.request_dir(wanted))
        return real(self, wanted)

    monkeypatch.setattr(Spool, "claim", withdrawn_first)
    world.serve()
    assert not (world.paths.result_dir(request_id) / protocol.RESULT_FILE).exists()
    assert world.audit() == []
    assert any("was withdrawn before it was claimed" in event for event in world.events)


# Load-bearing: the number of descriptors held open does not grow with what agents create.
def test_only_so_many_linked_working_trees_are_served_at_once(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(served, "MAX_LINKED_SPOOLS", 1)
    trees = [worktree(world, tmp_path / name) for name in ("one", "two")]
    for tree in trees:
        world.submit(root=tree)
    world.submit()
    serving = world.broker()
    for _ in range(3):
        serving.tick(force=True)
    assert heartbeat(world.repo).exists()  # a main working tree does not count
    first = [tree for tree in trees if heartbeat(tree).exists()]
    assert len(first) == 1
    limit = [event for event in world.events if "which is the most at once" in event]
    assert len(limit) == 1

    # The one over the limit is not opened at all while the limit holds.
    opened: list[Path] = []
    real = checkouts.open_linked

    def watch(linked: checkouts.Linked) -> int:
        opened.append(linked.root)
        return real(linked)

    monkeypatch.setattr(checkouts, "open_linked", watch)
    serving.tick(force=True)
    assert opened == first
    monkeypatch.setattr(checkouts, "open_linked", real)

    # The one being served keeps its place until it goes away; then the other is served.
    shutil.rmtree(first[0] / protocol.DIR_NAME)
    serving.tick(force=True)
    serving.tick(force=True)
    [other] = [tree for tree in trees if tree != first[0]]
    assert heartbeat(other).exists()


# Load-bearing: the limit is per repository, so one repository's agents cannot
# keep another repository's working trees from being served.
def test_one_repositorys_working_trees_do_not_take_anothers_places(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(served, "MAX_LINKED_SPOOLS", 1)
    for name in ("one", "two"):
        world.submit(root=worktree(world, tmp_path / name))
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet", "--initial-branch", "main")
    git(other, "commit", "--quiet", "--allow-empty", "-m", "first")
    theirs = tmp_path / "theirs"
    git(other, "worktree", "add", "--quiet", "--detach", str(theirs))
    client_spool.ensure_spool(client_spool.SpoolPaths(theirs))
    registry.save(world.state, [world.repository, registry.new_repository(other, "main", None)])

    serving = world.broker()
    for _ in range(3):
        serving.tick(force=True)
    assert heartbeat(theirs).exists()
    assert [heartbeat(tmp_path / name).exists() for name in ("one", "two")].count(True) == 1


def test_working_tree_that_comes_and_goes_prints_a_bounded_number_of_lines(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notices, "MAX_CHECKOUT_EVENTS", 3)
    tree = worktree(world, tmp_path / "linked")
    paths = client_spool.SpoolPaths(tree)
    serving = world.broker()
    for _ in range(6):
        client_spool.ensure_spool(paths)
        serving.tick(force=True)
        shutil.rmtree(paths.base)
        serving.tick(force=True)
    about = [event for event in world.events if f"serving {tree}" in event]
    assert len(about) == 3
    assert [event.split(" ", 1)[1] for event in about[:2]] == [
        f"serving {tree}",
        f"no longer serving {tree}",
    ]
    assert len([event for event in world.events if "further such lines are left out" in event]) == 1


def test_listing_does_not_outlive_the_spool_it_was_read_from(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(served, "SCAN_BUDGET_SECONDS", 0.0)  # one spool is listed per scan
    worktree(world, tmp_path / "linked")
    client_spool.ensure_spool(client_spool.SpoolPaths(tmp_path / "linked"))
    old = world.submit()
    spools = served.ServedSpools(world.state, notices.Notices(world.events.append))
    try:
        spools.refresh()
        root = Path(world.repository.path)
        for _ in range(2):  # one spool per scan: main, then linked
            spools.unclaimed()
        # Main again, so the main spool is the one listed most recently: without
        # a fresh listing it would be the last to be read after the replacement.
        assert spools.unclaimed()[root] == [old]
        shutil.rmtree(world.paths.base)
        client_spool.ensure_spool(world.paths)  # a new spool at the same path
        spools.refresh()
        assert spools.unclaimed()[root] == []
    finally:
        spools.close()


def test_listing_spools_is_bounded_per_scan_and_every_spool_gets_its_turn(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(served, "SCAN_BUDGET_SECONDS", 0.0)  # every listing is over budget
    linked = worktree(world, tmp_path / "linked")
    here, there = world.submit(), world.submit(root=linked)
    serving = world.broker()

    def seen() -> list[bool]:
        return [
            status_detail(world, here) is not None,
            status_detail(world, there, linked) is not None,
        ]

    serving.tick(force=True)
    assert sorted(seen()) == [False, True]  # one spool was listed, the other must wait
    serving.tick(force=True)
    assert seen() == [True, True]
    assert len([event for event in world.events if "listing the spools takes" in event]) == 1
    assert serving.serve(once=True) == 0
    assert world.result(here)["outcome"] == world.result(there, linked)["outcome"] == "passed"


# Load-bearing: one repository's records cannot take over another's working tree.
def test_forged_record_in_another_repository_does_not_take_a_working_tree(
    world: World, tmp_path: Path
) -> None:
    linked = worktree(world, tmp_path / "linked")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet", "--initial-branch", "main")
    forged = other / ".git" / "worktrees" / "forged"
    forged.mkdir(parents=True)
    (forged / "gitdir").write_text(f"{linked}/.git\n")
    intruder = registry.new_repository(other, "main", None)
    registry.save(world.state, [intruder, world.repository])  # the forger is asked first

    request_id = world.submit(root=linked)
    world.serve()

    assert world.result(request_id, linked)["outcome"] == "passed"
    [line] = world.audit()
    assert line["repository"]["key"] == world.repository.key


def test_waiting_request_is_told_its_place_again_when_it_changes(world: World) -> None:
    serving = world.broker()
    waiting = []
    for _ in range(3):
        waiting.append(world.submit())
        serving.tick(force=True)  # one at a time, so the order seen is the order submitted
    assert [status_detail(world, i) for i in waiting] == ["next", "1 ahead", "2 ahead"]
    assert client_spool.withdraw(world.paths, waiting[0])
    serving.tick(force=True)
    assert [status_detail(world, i) for i in waiting[1:]] == ["next", "1 ahead"]


def test_output_that_cannot_be_printed_does_not_stop_the_broker(world: World) -> None:
    def closed(_line: str) -> None:
        raise BrokenPipeError("the operator's terminal is gone")

    request_id = world.submit()
    deaf = Broker(world.state, repo_config.Policy(), world.vms(), closed, FAST)
    assert deaf.serve(once=True) == 0
    assert world.result(request_id)["outcome"] == "passed"
    assert [line["outcome"] for line in world.audit()] == ["passed"]
