"""tart_test.py — the Tart adapter: names, argument guards, and guest commands.

Coverage:
  - VM names carry the state key; only this state's own names are recognised
  - Load-bearing: a name that could be read as an option is refused
  - guest commands: exit status, combined output, stdin, the output limit,
    the time limit, and cancellation
  - the exact start flags for each network mode
  - a Tart command that fails or hangs is an error with its output
"""

import io
import time
from pathlib import Path

import pytest
from conftest import World

from tart_taste_test import tart

SHELL = ["/bin/sh", "-c"]


def started(world: World, name: str = "vm-a", network: str = "shared") -> tart.Tart:
    vms = world.vms()
    vms.clone("ghcr.io/example/image:latest", name)
    process = vms.start(name, network, world.tart_home / f"{name}.log")
    deadline = time.monotonic() + 10
    while not (world.tart_home / "vms" / name / "running").exists():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.01)
    return vms


def test_names_carry_the_state_key_and_are_recognised() -> None:
    names = tart.Names("1a2b3c4d")
    environment = names.environment("0123456789ab", "ba9876543210")
    assert environment == "tastetest-1a2b3c4d-env-0123456789ab-ba9876543210"
    assert names.build("0123456789ab", "ba9876543210") == (
        "tastetest-1a2b3c4d-build-0123456789ab-ba9876543210"
    )
    assert names.run("00112233aabb") == "tastetest-1a2b3c4d-run-00112233aabb"
    assert names.doctor("00112233aabb") == "tastetest-1a2b3c4d-doctor-00112233aabb"
    assert names.environment_repo(environment) == "0123456789ab"
    assert names.is_transient(names.run("00112233aabb"))
    assert names.is_transient(names.doctor("00112233aabb"))
    assert names.is_transient(names.build("0123456789ab", "ba9876543210"))
    assert not names.is_transient(environment)


@pytest.mark.parametrize(
    "name",
    [
        "tastetest-ffffffff-run-00112233aabb",  # another state directory
        "tastetest-1a2b3c4d-run-00112233aabb-x",
        "tastetest-1a2b3c4d-run-XYZ",
        "tastetest-1a2b3c4d-run-",
        "x-tastetest-1a2b3c4d-run-00112233aabb",
        "tastetest-1a2b3c4d-env-0123456789ab",
        "sequoia-base",
        "",
    ],
)
def test_foreign_names_are_never_recognised_as_ours(name: str) -> None:
    names = tart.Names("1a2b3c4d")
    assert not names.is_transient(name)
    assert names.environment_repo(name) is None


# Load-bearing: nothing that could be an option reaches Tart as a VM name.
@pytest.mark.parametrize("name", ["-rf", "--dir=/:rw", "a b", "", "a;b", "$(x)", "a\nb"])
def test_names_that_could_be_options_are_refused(world: World, name: str) -> None:
    vms = world.vms()
    for action in (
        lambda: vms.clone(name, "ok"),
        lambda: vms.clone("ok/image", name),
        lambda: vms.delete(name),
        lambda: vms.stop(name),
        lambda: vms.rename("ok", name),
        lambda: vms.exec(name, ["true"], 5),
        lambda: vms.start(name, "shared", world.tart_home / "x.log"),
    ):
        with pytest.raises(tart.TartError, match="refusing unexpected"):
            action()
    assert world.calls() == []


def test_exec_reports_status_and_combined_output(world: World) -> None:
    vms = started(world)
    sink = io.BytesIO()
    done = vms.exec("vm-a", [*SHELL, "echo out; echo err >&2; exit 5"], 10, sink=sink, limit=100)
    assert (done.status, done.timed_out, done.cancelled, done.truncated) == (5, False, False, False)
    assert sorted(sink.getvalue().split()) == [b"err", b"out"]
    assert done.size == len(sink.getvalue())


def test_exec_passes_standard_input_and_arguments(world: World, tmp_path: Path) -> None:
    vms = started(world)
    data = tmp_path / "stdin"
    data.write_bytes(b"from the host\n")
    sink = io.BytesIO()
    with data.open("rb") as handle:
        done = vms.exec(
            "vm-a", [*SHELL, 'cat; printf "%s|" "$@"', "argv0", "-t", "a b", ""], 10, handle, sink
        )
    assert done.status == 0
    assert sink.getvalue() == b"from the host\n-t|a b||"
    assert world.calls("exec")[-1][:2] == ["exec", "-i"]


def test_exec_stores_at_most_the_limit_and_keeps_draining(world: World) -> None:
    vms = started(world)
    sink = io.BytesIO()
    script = "i=0; while [ $i -lt 2000 ]; do echo 0123456789; i=$((i+1)); done; exit 3"
    done = vms.exec("vm-a", [*SHELL, script], 30, sink=sink, limit=1000)
    assert done.status == 3  # the command was not blocked by the full log
    assert (done.truncated, done.size, len(sink.getvalue())) == (True, 1000, 1000)


def test_exec_stops_a_command_at_its_time_limit(world: World) -> None:
    vms = started(world)
    begun = time.monotonic()
    done = vms.exec("vm-a", [*SHELL, "echo start; sleep 30"], 0.5, sink=io.BytesIO())
    assert done.timed_out and done.status is None
    assert time.monotonic() - begun < 15


def test_exec_stops_when_cancelled(world: World) -> None:
    vms = started(world)
    begun = time.monotonic()
    done = vms.exec(
        "vm-a", [*SHELL, "sleep 30"], 60, cancelled=lambda: time.monotonic() - begun > 0.3
    )
    assert done.cancelled and done.status is None and not done.timed_out


def test_exec_in_a_vm_that_is_not_running_fails(world: World) -> None:
    vms = world.vms()
    vms.clone("ghcr.io/example/image:latest", "vm-a")
    assert vms.exec("vm-a", [*SHELL, "exit 0"], 5).status == 1


@pytest.mark.parametrize(
    ("network", "flags"),
    [
        ("isolated", ["--net-softnet-block=0.0.0.0/0"]),
        ("internet", ["--net-softnet"]),
        ("shared", []),
    ],
)
def test_start_uses_exactly_the_documented_flags(
    world: World, network: str, flags: list[str]
) -> None:
    vms = started(world, "vm-a", network)
    [call] = world.calls("run")
    assert call == [
        "run",
        "--no-graphics",
        "--no-audio",
        "--no-clipboard",
        "--no-usb-accessories",
        *flags,
        "vm-a",
    ]
    vms.stop("vm-a")
    assert world.calls("stop")[-1] == ["stop", "vm-a", "--timeout", "20"]


def test_lifecycle_commands(world: World) -> None:
    vms = world.vms()
    assert vms.version() == "2.41.1-fake"
    vms.clone("ghcr.io/example/image:latest", "one")
    vms.set_resources("one", None, None)  # nothing to set: no command is run
    assert world.calls("set") == []
    vms.set_resources("one", 2, 2048)
    assert world.calls("set") == [["set", "one", "--cpu", "2", "--memory", "2048"]]
    vms.rename("one", "two")
    assert vms.list_local() == ["two"]
    assert world.calls("list") == [["list", "--source", "local", "--quiet"]]
    vms.delete("two")
    assert vms.list_local() == []


def test_failed_tart_command_is_an_error_with_its_output(world: World) -> None:
    vms = world.vms()
    with pytest.raises(tart.TartError, match=r"tart delete failed \(exit 1\): .*does not exist"):
        vms.delete("no-such-vm")
    with pytest.raises(tart.TartError, match="image missing/image not found"):
        vms.clone("missing/image", "x")


def test_missing_tart_executable_is_an_error(tmp_path: Path) -> None:
    vms = tart.Tart(str(tmp_path / "no-such-tart"))
    with pytest.raises(tart.TartError, match="cannot run"):
        vms.version()
    with pytest.raises(tart.TartError, match="cannot run"):
        vms.exec("vm", ["true"], 5)


def test_hanging_tart_command_is_stopped_and_ticks_meanwhile(tmp_path: Path) -> None:
    slow = tmp_path / "tart"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    ticks: list[float] = []
    vms = tart.Tart(str(slow), lambda: ticks.append(time.monotonic()))
    with pytest.raises(tart.TartError, match="did not finish"):
        vms._run(["list"], 0.7)
    assert len(ticks) >= 2


def test_tart_exec_that_closes_its_output_but_never_exits_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its status is unknown, so it must not be read as any status at all."""
    stuck = tmp_path / "tart"
    stuck.write_text("#!/bin/sh\nexec >&- 2>&-\nsleep 30\n")
    stuck.chmod(0o755)
    monkeypatch.setattr(tart, "EXEC_EXIT_WAIT", 0.3)
    began = time.monotonic()
    with pytest.raises(tart.TartError, match="did not exit; its status is unknown"):
        tart.Tart(str(stuck)).exec("vm", ["true"], 20)
    assert time.monotonic() - began < 10  # and the process was not waited for


def test_only_names_of_this_state_directory_are_owned() -> None:
    names = tart.Names("0a1b2c3d")
    for own in (
        names.run("00112233aabb"),
        names.doctor("00112233aabb"),
        names.environment("a", "b"),
    ):
        assert names.owns(own)
    for foreign in ("tastetest-ffffffff-run-00112233aabb", "tastetest-0a1b2c3d", "my-vm", ""):
        assert not names.owns(foreign)
