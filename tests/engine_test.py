"""engine_test.py — guest steps that are easier to pin down below the broker.

The request flow as a whole is covered in broker_test.py. This file pins the
values other artifacts depend on and the failure paths of the VM lifecycle.

Coverage:
  - the environment key is the documented hash (docs/vm-handling.md §1)
  - Load-bearing: the guest argument vector keeps every request argument
  - the guest scripts are fixed text with no interpolation
  - an image without HOME, tar, or xargs is refused with the reason
  - a VM whose guest agent never answers is an error after the boot timeout
  - a VM that cannot be deleted is reported, retried, and does not stop the broker
  - a VM is deleted even when the VM listing cannot be read; only own VMs ever are
  - Load-bearing: stopping and deleting a VM run to their end inside the shield,
    with controls showing that without the shield the same `tick` interrupts them
  - a listing that fails after a successful build does not fail the build
"""

import contextlib
import hashlib
import io
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import DEFAULT_CONFIG, World, git

from tart_taste_test import engine, process, repo_config, tart
from tart_taste_test.engine import Engine, EngineError
from tart_taste_test.mirror import Mirror

COMMIT = "0123456789abcdef0123456789abcdef01234567"
IMAGE = "ghcr.io/example/image:latest"


def made(world: World, warnings: list[str] | None = None) -> Engine:
    sink: list[str] = warnings if warnings is not None else []
    return world.engine(sink.append)


def test_environment_key_is_the_documented_hash() -> None:
    expected = hashlib.sha256(f"1\0{COMMIT}\0internet".encode()).hexdigest()[:12]
    assert engine.environment_key(COMMIT, "internet") == expected
    assert engine.environment_key(COMMIT, "isolated") != expected
    assert engine.environment_key(COMMIT[::-1], "internet") != expected
    assert engine.ENV_FORMAT == 1


# Load-bearing: `$0` is a fixed word so the first request argument is not lost,
# and the command string is used exactly as the config wrote it.
def test_command_argv_places_arguments_after_a_fixed_argv0() -> None:
    argv = engine.command_argv(("/bin/sh", "-lc"), 'make test "$@"', ("-k", "a b"))
    assert argv == [
        "/bin/sh",
        "-lc",
        'exec 2>&1; cd "$HOME/taste-test/src" && make test "$@"',
        "taste-test",
        "-k",
        "a b",
    ]
    without = engine.command_argv(("/bin/zsh", "-l", "-c"), "make lint", ())
    assert without == [
        "/bin/zsh",
        "-l",
        "-c",
        'exec 2>&1; cd "$HOME/taste-test/src" && make lint',
        "taste-test",
    ]


def test_guest_scripts_are_fixed_text() -> None:
    scripts = (
        engine.SCRIPT_READY,
        engine.SCRIPT_PROBE,
        engine.SCRIPT_REQUIRE,
        engine.SCRIPT_UNPACK_BASE,
        engine.SCRIPT_SAVE_MANIFEST,
        engine.SCRIPT_OVERLAY,
        engine.SCRIPT_SYNC,
    )
    for script in scripts:
        assert "{" not in script.replace("${HOME:-}", "")  # no format placeholders
    assert engine.SCRIPT_PROBE == "exit 7"
    assert engine.SCRIPT_OVERLAY == (
        'set -eu; root="$HOME/taste-test"; cd "$root/src"; '
        'xargs -0 rm -rf -- < "$root/base-manifest"; tar -x -o -f - -C "$root/src"'
    )
    assert engine.SCRIPT_UNPACK_BASE == (
        'set -eu; root="$HOME/taste-test"; rm -rf "$root"; mkdir -p "$root/src"; '
        'tar -x -o -f - -C "$root/src"'
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [(11, "HOME is not set"), (12, "no tar"), (13, "no xargs"), (1, "requirement check exited 1")],
)
def test_unusable_image_is_refused_with_the_reason(
    world: World, monkeypatch: pytest.MonkeyPatch, status: int, reason: str
) -> None:
    monkeypatch.setattr(engine, "SCRIPT_REQUIRE", f"exit {status}")
    box = made(world)
    world.vms().clone("ghcr.io/example/image:latest", "vm-a")
    with pytest.raises(EngineError, match=reason), box.booted("vm-a", "shared", 10):
        pytest.fail("the block must not run for an unusable image")
    assert not (world.tart_home / "vms" / "vm-a" / "running").exists()  # stopped again


def test_guest_agent_that_never_answers_is_an_error(world: World) -> None:
    world.switch("exec-broken")
    box = made(world)
    world.vms().clone("ghcr.io/example/image:latest", "vm-a")
    with (
        pytest.raises(EngineError, match="guest agent did not answer within 1s"),
        box.booted("vm-a", "shared", 1),
    ):
        pytest.fail("the block must not run without a guest agent")
    assert not (world.tart_home / "vms" / "vm-a" / "running").exists()


def test_failing_guest_step_reports_what_and_why(world: World) -> None:
    box = made(world)
    world.vms().clone("ghcr.io/example/image:latest", "vm-a")
    with box.booted("vm-a", "shared", 10):
        box.step("vm-a", "exit 0", "a step that works")
        with pytest.raises(EngineError, match=r"guest step failed \(unpacking exited 3\): oops"):
            box.step("vm-a", "echo oops\x1b[31m; exit 3", "unpacking")


def test_discard_deletes_a_vm_and_tolerates_one_that_is_gone(world: World) -> None:
    warnings: list[str] = []
    box = made(world, warnings)
    name = box.names.run("00112233aabb")
    world.vms().clone(IMAGE, name)
    (world.state.vm_logs / f"{name}.log").write_text("console output")
    assert box.discard(name) is True
    assert world.vm_names() == []
    assert list(world.state.vm_logs.iterdir()) == []  # its console log went with it
    assert box.discard(name) is True
    assert warnings == []


def test_vm_that_cannot_be_deleted_is_retried_and_reported(world: World) -> None:
    warnings: list[str] = []
    box = made(world, warnings)
    name = box.names.run("00112233aabb")
    world.vms().clone(IMAGE, name)
    world.switch("undeletable")
    assert box.discard(name) is False
    assert len(world.calls("delete")) == 2  # one retry
    assert warnings == [f"could not delete VM {name}; run `taste-test-broker prune`"]
    assert box.cleanup_transient() == []  # only VMs that were really deleted are reported


def test_vm_is_deleted_even_when_the_listing_cannot_be_read(world: World) -> None:
    warnings: list[str] = []
    box = made(world, warnings)
    name = box.names.run("00112233aabb")
    world.vms().clone(IMAGE, name)
    world.switch("fail-list")
    assert box.discard(name) is False  # it could not be confirmed
    assert world.vm_names() == []  # but stop and delete were issued all the same
    assert len(warnings) == 1 and warnings[0].startswith(f"cannot list VMs after deleting {name}")


def test_only_vms_of_this_state_directory_are_ever_deleted(world: World) -> None:
    box = made(world)
    for name in ("operators-own-vm", tart.Names("ffffffff").run("00112233aabb")):
        world.vms().clone(IMAGE, name)
        with pytest.raises(EngineError, match="not a VM of this state directory"):
            box.discard(name)
    assert len(world.vm_names()) == 2
    assert world.calls("delete") == [] and world.calls("stop") == []


class Abandon(BaseException):
    """What a broker's `tick` raises to abandon a request."""


class Abandoning:
    """A `tick` that abandons whenever it is called outside the shield, once armed."""

    def __init__(self) -> None:
        self.armed = False
        self.depth = 0

    def tick(self) -> None:
        if self.armed and not self.depth:
            raise Abandon()

    @contextlib.contextmanager
    def shield(self) -> Iterator[None]:
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


def abandoning(world: World, shielded: bool) -> tuple[Engine, tart.Tart, Abandoning]:
    """An engine whose every `tart stop` outlasts several wait-loop turns.

    With `shielded` false the engine gets no shield at all. Those runs are the
    control: they show the same `tick` does interrupt clean-up, so the shielded
    runs pass because of the shield and not because clean-up was too quick to
    be interrupted.
    """
    world.switch("slow-stop", str(process.POLL_SECONDS * 3))
    control = Abandoning()
    vms = world.vms()
    vms.tick = control.tick
    shield = control.shield if shielded else contextlib.nullcontext
    box = Engine(vms, world.state, control.tick, world.events.append, 0.0, 0.0, shield)
    return box, vms, control


# Load-bearing: a request abandoned from `tick` still has its VM stopped and
# its `tart run` process ended.
@pytest.mark.parametrize("shielded", [True, False], ids=["shielded", "control-no-shield"])
def test_stopping_a_vm_runs_to_its_end_only_inside_the_shield(world: World, shielded: bool) -> None:
    box, vms, control = abandoning(world, shielded)
    name = box.names.run("00112233aabb")
    vms.clone(IMAGE, name)
    started: list[subprocess.Popen[bytes]] = []
    original = vms.start

    def start(*args: Any) -> subprocess.Popen[bytes]:
        started.append(original(*args))
        return started[-1]

    vms.start = start  # type: ignore[method-assign, assignment]
    with pytest.raises(Abandon), box.booted(name, "shared", 10):
        control.armed = True
        vms.exec(name, ["/bin/sh", "-c", "sleep 30"], 30.0)
    control.armed = False
    still_running = (world.tart_home / "vms" / name / "running").exists()
    assert still_running is not shielded
    assert (started[0].poll() is not None) is shielded  # `tart run` ended inside the shield
    process.stop(started[0])


# Load-bearing: a request abandoned from `tick` still has its VM deleted.
@pytest.mark.parametrize("shielded", [True, False], ids=["shielded", "control-no-shield"])
def test_deleting_a_vm_runs_to_its_end_only_inside_the_shield(world: World, shielded: bool) -> None:
    box, vms, control = abandoning(world, shielded)
    name = box.names.run("00112233aabb")
    vms.clone(IMAGE, name)
    control.armed = True
    if shielded:
        assert box.discard(name) is True
        assert world.vm_names() == []
    else:
        with pytest.raises(Abandon):
            box.discard(name)
        assert world.vm_names() == [name]


def test_failing_to_list_older_images_does_not_fail_the_build(world: World) -> None:
    warnings: list[str] = []
    box = made(world, warnings)
    config = repo_config.parse(DEFAULT_CONFIG.encode(), repo_config.Policy())
    commit = git(world.repo, "rev-parse", "HEAD")
    mirror = Mirror(world.state, world.repository)
    assert mirror.refresh() == commit
    world.switch("fail-list-after-rename")
    built = box.build_environment(world.repository.key, commit, config, mirror, io.BytesIO())
    assert built in world.vm_names()
    assert any("older environment images were not deleted" in text for text in warnings)


def test_cleanup_removes_only_this_states_transient_vms(world: World) -> None:
    box = made(world)
    vms = world.vms()
    names = box.names
    other = tart.Names("ffffffff")
    transient = [
        names.run("00112233aabb"),
        names.doctor("00112233aabb"),
        names.build("0123456789ab", "ba9876543210"),
    ]
    kept = [
        names.environment("0123456789ab", "ba9876543210"),
        other.run("00112233aabb"),
        "operators-own-vm",
    ]
    for name in transient + kept:
        vms.clone("ghcr.io/example/image:latest", name)
    assert sorted(box.cleanup_transient()) == sorted(transient)
    assert world.vm_names() == sorted(kept)
    assert box.environments() == [names.environment("0123456789ab", "ba9876543210")]


def test_console_log_tail_reads_only_the_end(tmp_path: Path) -> None:
    log = tmp_path / "vm.log"
    log.write_bytes(b"x" * 100_000 + b"the end")
    assert engine._tail(log, 7) == "the end"
    log.write_bytes(b"short")
    assert engine._tail(log) == "short"
    assert engine._tail(tmp_path / "missing.log") == "(no console output)"
