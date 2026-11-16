"""broker_test.py — the broker end to end, built against contract v1.

A real client-side submission is served by a real `Broker` whose Tart is the
fake in fake_tart.py, so the guest scripts actually run (in a temporary
directory, never in a VM).

Coverage (contracts/taste_test_broker_contract.md behaviours):
  - happy path: passed, with output and an audit line
  - failed: the guest command's status decides, and is reported
  - Load-bearing: the candidate never supplies the command, image, or network
  - Load-bearing: arguments reach the guest intact and only when allowed
  - Load-bearing: environment built from the default branch, reused, rebuilt
  - Load-bearing: a deleted candidate file is gone; setup products survive
  - rejected: missing/invalid config, unknown command, ceilings, bad digest
  - setup-failed, timed-out, cancelled, error (VM cannot start)
  - Load-bearing: exit-status probe; a VM that dies mid-command is never a
    verdict, whatever status or silence Tart reports for it
  - Load-bearing: isolation flags on every VM start; after every outcome the run
    VM, the build VM, the staged snapshot, and the spool's snapshot are gone
  - recovery after a broker that stopped mid-request; stale requests
  - queue order: first seen first, checkouts taking turns, one turn per interval

Stopping, cancelling, and losing a checkout while a request runs are in
broker_lifecycle_test.py.
"""

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import DEFAULT_CONFIG, FAST, World, git

from tart_taste_test import protocol, repo_config, tart
from tart_taste_test.broker import Pace

RUN_LINE = "run = 'sh run_tests.sh \"$@\"'"
ONE_SECOND = DEFAULT_CONFIG.replace("allow_args = true", "allow_args = true\ntimeout_seconds = 1")


def names(world: World) -> tart.Names:
    return tart.Names(world.state.key)


def test_passed_request_reports_output_and_audit(world: World) -> None:
    request_id = world.submit()
    world.serve()

    result = world.result(request_id)
    assert result["outcome"] == "passed"
    assert result["command_exit"] == 0
    assert result["command"] == {"name": "test", "run": 'sh run_tests.sh "$@"', "args": []}
    assert result["network"] == {"setup": "internet", "run": "isolated"}
    assert result["default_branch"]["name"] == "main"
    assert result["environment"]["reused"] is False
    assert world.output(request_id) == "tests ran: \n"

    [line] = world.audit()
    assert line["id"] == request_id and line["outcome"] == "passed"
    assert line["checkout"] == str(world.repo)
    assert line["source_sha256"] == result["source_sha256"]


def test_failed_request_carries_the_guest_exit_status(world: World) -> None:
    world.write({"run_tests.sh": "echo boom; exit 3\n"})
    result = world.run()
    assert (result["outcome"], result["command_exit"]) == ("failed", 3)


# Load-bearing: verdict integrity — the candidate never supplies the command.
def test_candidate_config_is_ignored(world: World) -> None:
    assert RUN_LINE in DEFAULT_CONFIG
    hostile = DEFAULT_CONFIG.replace(RUN_LINE, 'run = "true"')
    world.write({".taste-test.toml": hostile, "run_tests.sh": "exit 9\n"})
    result = world.run()
    assert result["command"]["run"] == 'sh run_tests.sh "$@"'
    assert (result["outcome"], result["command_exit"]) == ("failed", 9)


# Load-bearing: arguments reach the guest shell intact, including the first one.
def test_arguments_arrive_intact_and_in_order(world: World) -> None:
    world.write({"run_tests.sh": 'for a in "$@"; do echo "[$a]"; done\n'})
    request_id = world.submit("test", ["-k", "two words", "--help", "$(id)", "; rm -rf x"])
    world.serve()
    assert world.output(request_id).splitlines() == [
        "[-k]",
        "[two words]",
        "[--help]",
        "[$(id)]",
        "[; rm -rf x]",
    ]


def test_arguments_are_rejected_when_the_command_does_not_allow_them(world: World) -> None:
    result = world.run("lint", ["x"])
    assert result["outcome"] == "rejected"
    assert "does not accept arguments" in result["reason"]
    assert world.calls("clone") == []


def test_unknown_command_is_rejected_with_the_defined_names(world: World) -> None:
    result = world.run("deploy")
    assert result["outcome"] == "rejected"
    assert "lint, test" in result["reason"]


def test_missing_config_on_the_default_branch_is_rejected(world: World) -> None:
    world.commit({".taste-test.toml": None}, "remove config")
    world.write({".taste-test.toml": DEFAULT_CONFIG})  # only in the candidate
    result = world.run()
    assert result["outcome"] == "rejected"
    assert "no .taste-test.toml on branch main" in result["reason"]


def test_config_above_a_ceiling_is_rejected_not_reduced(world: World) -> None:
    world.commit({".taste-test.toml": DEFAULT_CONFIG + '\n[run]\nnetwork = "internet"\n'})
    result = world.run()
    assert result["outcome"] == "rejected"
    assert "--max-run-network ceiling is 'isolated'" in result["reason"]
    assert world.calls("run") == []


def test_snapshot_that_does_not_match_its_digest_is_rejected(world: World) -> None:
    request_id = world.submit()
    source = world.paths.request_dir(request_id) / protocol.SOURCE_FILE
    source.write_bytes(source.read_bytes() + b"tampered")
    world.serve()
    result = world.result(request_id)
    assert result["outcome"] == "rejected"
    assert "does not match" in result["reason"]
    assert world.calls("clone") == []


# Load-bearing: the environment comes from the default branch and is reused.
def test_environment_is_built_once_and_reused(world: World) -> None:
    first = world.run()
    second = world.run()
    assert first["environment"]["reused"] is False
    assert second["environment"] == {"vm": first["environment"]["vm"], "reused": True}
    builds = [c for c in world.calls("clone") if "-build-" in c[2]]
    assert len(builds) == 1
    assert builds[0][1] == "ghcr.io/example/image:latest"


def test_environment_is_rebuilt_when_the_default_branch_moves(world: World) -> None:
    first = world.run()
    world.commit({"keep.txt": "changed upstream\n"})
    second = world.run()
    assert second["environment"]["reused"] is False
    assert second["environment"]["vm"] != first["environment"]["vm"]
    environments = [n for n in world.vm_names() if "-env-" in n]
    assert environments == [second["environment"]["vm"]]  # the older one was deleted


# Load-bearing: a file the candidate deleted is gone; setup products survive.
def test_candidate_replaces_default_branch_files_but_keeps_setup_products(world: World) -> None:
    world.write(
        {
            "keep.txt": None,
            "new.txt": "untracked candidate file\n",
            "run_tests.sh": "ls; cat deps/marker; cat new.txt\n",
            "ignored/secret.txt": "must not travel\n",
        }
    )
    request_id = world.submit()
    world.serve()
    listing = world.output(request_id).split()
    assert "keep.txt" not in listing
    assert "ignored" not in listing
    assert {"deps", "new.txt", "run_tests.sh", "built", "untracked"} <= set(listing)


def test_setup_failure_is_not_blamed_on_the_candidate(world: World) -> None:
    broken = DEFAULT_CONFIG.replace("mkdir -p deps && echo built > deps/marker", "echo no; exit 4")
    world.commit({".taste-test.toml": broken})
    request_id = world.submit()
    world.serve()
    result = world.result(request_id)
    assert result["outcome"] == "setup-failed"
    assert "setup command 1 exited 4" in result["reason"]
    assert result["command_exit"] is None
    assert world.output(request_id, protocol.SETUP_LOG) == "no\n"
    assert world.vm_names() == []  # the unfinished build was deleted


def test_command_that_runs_too_long_is_timed_out(world: World) -> None:
    world.commit({".taste-test.toml": ONE_SECOND})
    world.write({"run_tests.sh": "echo started; sleep 30\n"})
    result = world.run()
    assert result["outcome"] == "timed-out"
    assert result["command_exit"] is None
    assert "time limit of 1s" in result["reason"]


def test_cancel_marker_before_claim_cancels_without_starting_a_vm(world: World) -> None:
    request_id = world.submit()
    (world.paths.request_dir(request_id) / protocol.CANCEL_FILE).touch()
    world.serve()
    assert world.result(request_id)["outcome"] == "cancelled"
    assert world.calls("run") == []


def test_vm_that_cannot_start_is_an_error_naming_the_network_mode(world: World) -> None:
    world.switch("fail-run")
    result = world.run()
    assert result["outcome"] == "error"
    assert "network mode 'internet'" in result["reason"]
    assert "softnet is not installed" in result["reason"]
    assert world.vm_names() == []


# Load-bearing: never `passed` when exit statuses cannot be observed (D11).
def test_exit_status_probe_failure_is_an_error_never_passed(world: World) -> None:
    world.switch("exit-always-0")
    world.write({"run_tests.sh": "exit 1\n"})
    result = world.run()
    assert result["outcome"] == "error"
    assert "exit-status probe failed" in result["reason"]


# Load-bearing: a VM that dies mid-command is `error`, never a verdict. Tart may
# report that as a failure, as success, or not at all; the probe is repeated in
# each case (decision D11).
@pytest.mark.parametrize("how", ["exit-1", "exit-0", "hang"])
def test_vm_dying_during_the_command_is_an_error_not_a_verdict(world: World, how: str) -> None:
    world.commit({".taste-test.toml": ONE_SECOND})
    world.switch("die-on", "run_tests.sh")  # the named command, whatever steps precede it
    world.switch("die-how", how)
    result = world.run()
    assert result["outcome"] == "error"
    assert result["command_exit"] is None
    assert "cannot be trusted" in result["reason"]
    assert not (world.tart_home / "die-on").exists()  # the switch really fired


# Load-bearing: the same holds while the environment image is built. A VM that
# dies during a setup command is not a setup failure (which would blame the
# default branch) and its half-built image is never kept.
@pytest.mark.parametrize("how", ["exit-1", "exit-0", "hang"])
def test_vm_dying_during_a_setup_command_is_an_error_and_no_image_is_kept(
    world: World, how: str
) -> None:
    quick = DEFAULT_CONFIG.replace("[setup]", "[setup]\ntimeout_seconds = 1")
    world.commit({".taste-test.toml": quick})
    world.switch("die-on", "deps/marker")  # the setup command
    world.switch("die-how", how)
    result = world.run()
    assert result["outcome"] == "error"
    assert "cannot be trusted" in result["reason"]
    assert not (world.tart_home / "die-on").exists()  # the switch really fired
    assert world.vm_names() == []  # neither the build VM nor an environment image


# Load-bearing: the VM boundary — exact start flags, nothing shared.
def test_every_vm_start_uses_the_isolation_flags_and_its_network_mode(world: World) -> None:
    world.run()
    starts = world.calls("run")
    assert len(starts) == 2
    fixed = ["run", "--no-graphics", "--no-audio", "--no-clipboard", "--no-usb-accessories"]
    build, run = starts
    assert build[:-1] == [*fixed, "--net-softnet"]
    assert run[:-1] == [*fixed, "--net-softnet-block=0.0.0.0/0"]
    assert "-build-" in build[-1] and "-run-" in run[-1]
    forbidden = ("--dir", "--disk", "--rosetta", "--nested", "--net-bridged", "--net-host", "--vnc")
    for call in world.calls():
        assert not any(arg.startswith(forbidden) for arg in call)


def _failing_setup(world: World) -> None:
    broken = DEFAULT_CONFIG.replace("mkdir -p deps && echo built > deps/marker", "exit 4")
    world.commit({".taste-test.toml": broken})


def _slow_command(world: World) -> None:
    world.commit({".taste-test.toml": ONE_SECOND})
    world.write({"run_tests.sh": "sleep 30\n"})


def _bad_digest(world: World) -> None:
    world.write({"keep.txt": "changed after the request was written\n"})


SCENARIOS: dict[str, tuple[Callable[[World], None], str]] = {
    "passed": (lambda world: None, "passed"),
    "failed": (lambda world: world.write({"run_tests.sh": "exit 1\n"}), "failed"),
    "timed-out": (_slow_command, "timed-out"),
    "setup-failed": (_failing_setup, "setup-failed"),
    "vm-cannot-start": (lambda world: world.switch("fail-run"), "error"),
    "vm-dies-in-the-command": (lambda world: world.switch("die-on", "run_tests.sh"), "error"),
    "vm-dies-unpacking": (lambda world: world.switch("die-on", "base-manifest"), "error"),
    "no-exit-status": (lambda world: world.switch("exit-always-0"), "error"),
    "unknown-command": (lambda world: None, "rejected"),
}


# Load-bearing: whatever the outcome, nothing of the request is left on the host.
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_nothing_is_left_behind_after_any_outcome(world: World, scenario: str) -> None:
    arrange, outcome = SCENARIOS[scenario]
    arrange(world)
    request_id = world.submit("deploy" if scenario == "unknown-command" else "test")
    world.serve()

    assert world.result(request_id)["outcome"] == outcome
    assert [n for n in world.vm_names() if "-run-" in n or "-build-" in n] == []
    assert list(world.state.staging.iterdir()) == []
    assert list(world.state.vm_logs.iterdir()) == []
    assert not (world.paths.request_dir(request_id) / protocol.SOURCE_FILE).exists()


def test_vm_resources_from_the_config_are_applied_to_the_build(world: World) -> None:
    sized = DEFAULT_CONFIG.replace("[setup]", "cpu = 1\nmemory_mb = 1024\n\n[setup]")
    world.commit({".taste-test.toml": sized})
    assert world.run()["outcome"] == "passed"
    [call] = world.calls("set")
    assert call[2:] == ["--cpu", "1", "--memory", "1024"]


def test_request_claimed_by_a_broker_that_stopped_is_finished_as_error(world: World) -> None:
    request_id = world.submit()
    request_dir = world.paths.request_dir(request_id)
    os.rename(request_dir / protocol.REQUEST_FILE, request_dir / protocol.CLAIMED_FILE)
    leftover = names(world).run("0123456789ab")
    (world.tart_home / "vms" / leftover / "home").mkdir(parents=True)
    unrelated = "my-own-vm"
    (world.tart_home / "vms" / unrelated).mkdir()

    world.serve()

    result = world.result(request_id)
    assert result["outcome"] == "error"
    assert "a previous broker stopped" in result["reason"]
    assert world.vm_names() == [unrelated]  # only this project's leftover was deleted


def test_request_that_waited_too_long_for_a_broker_is_rejected(world: World) -> None:
    request_id = world.submit()
    request_file = world.paths.request_dir(request_id) / protocol.REQUEST_FILE
    old = time.time() - protocol.MAX_REQUEST_AGE_SECONDS - 60
    os.utime(request_file, (old, old))
    world.serve()
    result = world.result(request_id)
    assert result["outcome"] == "rejected"
    assert "waited too long" in result["reason"]
    assert world.calls("clone") == []


def test_requests_run_in_the_order_the_broker_first_saw_them(world: World) -> None:
    first = world.submit()
    # A later request with an ID that sorts before every honest ID.
    backdated = "00000000T000000Z-000000000000"
    source = world.paths.request_dir(first)
    target = world.paths.request_dir(backdated)
    broker = world.broker()
    broker.tick(force=True)  # the broker sees `first` before the backdated one exists
    target.mkdir()
    (target / protocol.SOURCE_FILE).write_bytes((source / protocol.SOURCE_FILE).read_bytes())
    document = (source / protocol.REQUEST_FILE).read_text().replace(first, backdated)
    (target / protocol.REQUEST_FILE).write_text(document)
    assert broker.serve(once=True) == 0
    order = [line["id"] for line in world.audit()]
    assert order == [first, backdated]


# Checkouts take turns, so one checkout's backlog does not hold up another's.
def test_checkouts_take_turns(world: World, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(linked))
    broker = world.broker()
    first = world.submit()
    broker.tick(force=True)
    second = world.submit()
    broker.tick(force=True)  # both of the main checkout's requests are seen first
    other = world.submit(root=linked)
    assert broker.serve(once=True) == 0
    assert [line["id"] for line in world.audit()] == [first, other, second]


# One checkout gets at most one turn per interval, however fast its requests end.
def test_one_checkout_gets_one_turn_per_interval(world: World) -> None:
    for _ in range(3):
        world.submit("deploy")  # refused at once: no such command
    interval = 0.4
    broker = world.broker(pace=Pace(FAST.ready_interval, FAST.delete_retry_pause, interval))
    began = time.monotonic()
    assert broker.serve(once=True) == 0
    assert time.monotonic() - began >= 2 * interval
    assert [line["outcome"] for line in world.audit()] == ["rejected"] * 3


def test_linked_worktree_is_served_and_a_forged_record_is_not(world: World, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(linked))
    world.write({"run_tests.sh": "echo from the linked worktree\n"}, linked)
    # A forged record pointing at a directory that does not point back.
    victim = tmp_path / "victim"
    victim.mkdir()
    git(victim, "init", "--quiet", "--initial-branch", "main")
    forged = world.repo / ".git" / "worktrees" / "forged"
    forged.mkdir()
    (forged / "gitdir").write_text(f"{victim}/.git\n")
    victim_request = world.submit(root=victim)

    request_id = world.submit(root=linked)
    world.serve()

    assert world.result(request_id, linked)["outcome"] == "passed"
    victim_results = victim / protocol.DIR_NAME / protocol.SPOOL_NAME / protocol.RESULTS_NAME
    assert list(victim_results.iterdir()) == []
    assert not (victim / protocol.DIR_NAME / protocol.SPOOL_NAME / "broker.json").exists()
    assert victim_request  # submitted, never served


def test_policy_is_enforced_per_phase(world: World) -> None:
    world.submit()
    world.serve(repo_config.Policy(max_setup_network="isolated"))
    [line] = world.audit()
    assert line["outcome"] == "rejected"
    assert "setup.network is 'internet' (the default)" in line["reason"]
