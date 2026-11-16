"""doctor_test.py — `taste-test-broker doctor`.

The live checks run against the fake Tart. The network probes would make real
connections from the test host, so the image tests replace them with a script
that exits with the status a guest would report. The real probe scripts are run
separately, with a stand-in `curl` first on PATH, to pin how they read curl's
exit status.

Coverage:
  - installation checks: Tart, Softnet (a warning when absent), Git, private
    state, and who can write the Tart VM store
  - image checks: the clone is listed, guest agent, exit status, stdin, arguments
  - Load-bearing: an image under `isolated` that reaches the internet FAILS, and
    so does a probe that did not run to a verdict
  - the probe scripts: one reading of curl's status for both address families
  - the host bridge answering the VM is reported as a warning
  - an image that cannot boot, or a Tart that hides exit statuses, fails
  - the doctor VM is always deleted
"""

import subprocess
from pathlib import Path

import pytest
from conftest import World

from tart_taste_test import doctor

IMAGE = "ghcr.io/example/image:latest"
REACHED, BLOCKED, NO_CURL, NO_GATEWAY = "exit 0", "exit 1", "exit 90", "exit 91"


def probes(
    monkeypatch: pytest.MonkeyPatch, v4: str = BLOCKED, v6: str = BLOCKED, gateway: str = BLOCKED
) -> None:
    monkeypatch.setattr(doctor, "SCRIPT_PUBLIC_V4", v4)
    monkeypatch.setattr(doctor, "SCRIPT_PUBLIC_V6", v6)
    monkeypatch.setattr(doctor, "SCRIPT_GATEWAY", gateway)


def run(
    world: World, image: str | None = IMAGE, network: str = "isolated"
) -> tuple[bool, list[str]]:
    lines: list[str] = []
    engine = world.engine(lines.append)
    passed = doctor.run(world.state, world.vms(), lines.append, image, network, engine)
    return passed, lines


@pytest.fixture(autouse=True)
def tart_on_path(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", f"{world.tart_path.parent}:/usr/bin:/bin")
    store = tmp_path / "vm-store"
    store.mkdir(mode=0o700)
    monkeypatch.setenv(doctor.TART_HOME_ENV, str(store))


def test_installation_checks_pass_and_warn_about_missing_softnet(world: World) -> None:
    passed, lines = run(world, image=None)
    assert passed
    assert "PASS tart is on PATH (version 2.41.1-fake)" in lines
    assert any(line.startswith("WARN softnet is not on PATH") for line in lines)
    assert "PASS git is on PATH" in lines
    assert f"PASS operator state {world.state.root} is private to this user" in lines
    assert "PASS registry is readable (1 registered)" in lines
    assert world.calls("clone") == []  # no VM without --image


def test_softnet_found_is_reported(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    softnet = world.tart_path.parent / "softnet"
    softnet.write_text("#!/bin/sh\n")
    softnet.chmod(0o755)
    _, lines = run(world, image=None)
    assert "PASS softnet is on PATH" in lines


def test_missing_tart_fails_and_skips_the_image_checks(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    passed, lines = run(world)
    assert not passed
    assert any(line.startswith("FAIL tart is on PATH") for line in lines)
    assert world.calls("clone") == []


def test_state_readable_by_others_fails(world: World) -> None:
    world.state.root.chmod(0o755)
    passed, lines = run(world, image=None)
    assert not passed
    assert any("FAIL operator state" in line and "chmod 700" in line for line in lines)


def test_vm_store_is_named_and_checked(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "vm-store"
    passed, lines = run(world, image=None)
    assert passed
    assert any(
        line.startswith(f"PASS the Tart VM store {store} is writable only by this user")
        and "agent sandboxes" in line
        for line in lines
    )
    store.chmod(0o777)
    passed, lines = run(world, image=None)
    assert not passed
    assert any(line.startswith("FAIL the Tart VM store") for line in lines)
    monkeypatch.setenv(doctor.TART_HOME_ENV, str(tmp_path / "not-there"))
    passed, lines = run(world, image=None)
    assert passed
    assert any("does not exist yet" in line for line in lines)


def test_isolated_image_that_reaches_nothing_passes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes(monkeypatch)
    passed, lines = run(world)
    assert passed, lines
    for expected in (
        f"PASS image {IMAGE} can be cloned",
        "PASS guest agent answers with network mode isolated",
        "PASS exit status is reported (guest `exit 7` seen as 7)",
        "PASS HOME is set and tar and xargs are present",
        "PASS standard input reaches the guest",
        "PASS guest arguments arrive intact (option-like, spaced, and empty values)",
        "PASS a cloned VM appears in `tart list --source local --quiet`",
        "PASS isolated: a public IPv4 address is not reachable",
        "PASS isolated: the host side of the VM bridge does not answer the VM",
    ):
        assert expected in lines
    # The IPv6 line says what it cannot show.
    [v6] = [line for line in lines if "IPv6" in line]
    assert v6.startswith("PASS isolated: a public IPv6 address is not reachable (")
    assert "without an IPv6 route" in v6
    [start] = world.calls("run")
    assert "--net-softnet-block=0.0.0.0/0" in start
    assert world.vm_names() == []  # the doctor VM was deleted


# Load-bearing: `isolated` that is not isolating must fail loudly.
@pytest.mark.parametrize(
    ("v4", "v6", "needle"),
    [
        (REACHED, BLOCKED, "the isolated mode does not cover IPv4"),
        (BLOCKED, REACHED, "the isolated mode does not cover IPv6"),
        # A probe that did not end in a verdict is never read as "isolated".
        ("exit 5", BLOCKED, "IPv4 address is not reachable: could not be checked"),
        (BLOCKED, "exit 5", "IPv6 address is not reachable: could not be checked"),
        (BLOCKED, NO_CURL, "IPv6 address is not reachable: could not be checked"),
        (BLOCKED, "sleep 60", "IPv6 address is not reachable: could not be checked"),
    ],
)
def test_isolated_image_that_reaches_the_internet_fails(
    world: World, monkeypatch: pytest.MonkeyPatch, v4: str, v6: str, needle: str
) -> None:
    probes(monkeypatch, v4=v4, v6=v6)
    monkeypatch.setattr(doctor, "NETWORK_TIMEOUT", 1.0)
    passed, lines = run(world)
    assert not passed
    assert any(line.startswith("FAIL isolated:") and needle in line for line in lines)
    assert world.vm_names() == []


def stand_ins(directory: Path, **scripts: str) -> str:
    """Executables for a probe script to find first on PATH. Returns that PATH."""
    directory.mkdir(exist_ok=True)
    for name, body in scripts.items():
        tool = directory / name
        tool.write_text(f"#!/bin/sh\n{body}\n")
        tool.chmod(0o755)
    return f"{directory}:/usr/bin:/bin"


def probe(script: str, path: str) -> int:
    done = subprocess.run(["/bin/sh", "-c", script], env={"PATH": path}, check=False)
    return done.returncode


# The evidence for open question Q2 rests on how these scripts read curl's status:
# only "could not connect" (7) and "timed out" (28) mean nothing answered.
@pytest.mark.parametrize("script", ["SCRIPT_PUBLIC_V4", "SCRIPT_PUBLIC_V6"])
@pytest.mark.parametrize(
    ("curl_status", "expected"),
    [(0, 0), (7, 1), (28, 1), (52, 0), (56, 0), (22, 0), (35, 0)],
)
def test_public_probes_read_curl_the_same_way_for_both_families(
    tmp_path: Path, script: str, curl_status: int, expected: int
) -> None:
    path = stand_ins(tmp_path / "bin", curl=f"exit {curl_status}")
    assert probe(getattr(doctor, script), path) == expected


@pytest.mark.parametrize("script", ["SCRIPT_PUBLIC_V4", "SCRIPT_PUBLIC_V6", "SCRIPT_GATEWAY"])
def test_probes_say_so_when_the_image_has_no_curl(tmp_path: Path, script: str) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert probe(getattr(doctor, script), str(empty)) == doctor.NO_CURL


@pytest.mark.parametrize(
    ("curl_status", "expected"),
    [(0, 0), (7, 0), (52, 0), (28, 1)],  # a refusal is an answer; only silence is not
)
def test_gateway_probe_asks_the_default_gateway(
    tmp_path: Path, curl_status: int, expected: int
) -> None:
    asked = tmp_path / "asked"
    path = stand_ins(
        tmp_path / "bin",
        ip="echo 'default via 192.0.2.1 dev eth0'",
        curl=f'for a in "$@"; do echo "$a"; done > "{asked}"; exit {curl_status}',
    )
    assert probe(doctor.SCRIPT_GATEWAY, path) == expected
    assert asked.read_text().splitlines()[-1] == "http://192.0.2.1:9/"


def test_gateway_probe_reports_an_unknown_gateway(tmp_path: Path) -> None:
    path = stand_ins(tmp_path / "bin", ip="exit 0", route="exit 0", curl="exit 0")
    assert probe(doctor.SCRIPT_GATEWAY, path) == 91


def test_clone_that_is_not_listed_fails(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    probes(monkeypatch)
    world.switch("list-empty")
    passed, lines = run(world)
    assert not passed
    assert any(line.startswith("FAIL a cloned VM appears in `tart list") for line in lines)


def test_host_bridge_answering_the_vm_is_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes(monkeypatch, gateway=REACHED)
    passed, lines = run(world)
    assert passed
    assert any(
        line.startswith("WARN isolated: the host side of the VM bridge answers the VM")
        for line in lines
    )


def test_internet_mode_reports_reachability_without_failing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes(monkeypatch, v4=REACHED, gateway=NO_GATEWAY)
    passed, lines = run(world, network="internet")
    assert passed
    assert "PASS internet: a public IPv4 address is reachable" in lines
    assert "WARN internet: the VM's default gateway could not be determined" in lines
    probes(monkeypatch, v4=BLOCKED)
    passed, lines = run(world, network="internet")
    assert passed
    assert any(
        line.startswith("WARN internet: a public IPv4 address is not reachable") for line in lines
    )
    probes(monkeypatch, v4="exit 5")
    passed, lines = run(world, network="internet")
    assert passed
    assert (
        "WARN internet: public reach could not be checked (the probe ended with status 5)" in lines
    )


# Load-bearing: isolation is never reported as working when it was not checked.
def test_image_without_curl_fails_isolated_and_only_warns_otherwise(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes(monkeypatch, v4=NO_CURL)
    passed, lines = run(world)
    assert not passed
    assert any(
        line.startswith("FAIL isolated: network reach could not be checked") for line in lines
    )
    assert not any(line.startswith("PASS isolated") for line in lines)
    passed, lines = run(world, network="internet")
    assert passed
    assert "WARN network reach was not checked: the image has no curl" in lines
    assert not any("reachable" in line for line in lines)


def test_gateway_probe_that_ends_unexpectedly_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes(monkeypatch, gateway="exit 5")
    passed, lines = run(world)
    assert passed
    assert any("could not be checked (the probe ended with status 5)" in line for line in lines)
    assert not any("the host side of the VM bridge does not answer" in line for line in lines)
    probes(monkeypatch, gateway=NO_GATEWAY)
    _, lines = run(world)
    assert "WARN isolated: the VM's default gateway could not be determined" in lines


def test_tart_that_hides_exit_statuses_fails(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    probes(monkeypatch)
    world.switch("exit-always-0")
    passed, lines = run(world)
    assert not passed
    assert any("exit-status probe failed" in line for line in lines)
    assert world.vm_names() == []


def test_image_that_cannot_be_cloned_or_started_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes(monkeypatch)
    passed, lines = run(world, image="missing/image:1")
    assert not passed
    assert any(line.startswith("FAIL image missing/image:1") for line in lines)

    world.switch("fail-run")
    passed, lines = run(world)
    assert not passed
    assert any("softnet is not installed" in line for line in lines)
    assert world.vm_names() == []


def test_argument_and_stdin_scripts_detect_a_broken_channel(world: World) -> None:
    """The checks really fail when what they test is broken (negative control)."""
    intact = subprocess.run(["/bin/sh", "-c", doctor.SCRIPT_ARGV, *doctor.ARGV_SAMPLE], check=False)
    assert intact.returncode == 0
    joined = subprocess.run(
        ["/bin/sh", "-c", doctor.SCRIPT_ARGV, "taste-test", "-t", "a", "b", "--help", ""],
        check=False,
    )
    assert joined.returncode != 0
    piped = subprocess.run(
        ["/bin/sh", "-c", doctor.SCRIPT_STDIN], input=doctor.STDIN_TOKEN, check=False
    )
    assert piped.returncode == 0
    empty = subprocess.run(["/bin/sh", "-c", doctor.SCRIPT_STDIN], input=b"", check=False)
    assert empty.returncode != 0


def test_probe_that_does_not_finish_is_reported_as_such(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "NETWORK_TIMEOUT", 0.3)
    probes(monkeypatch, v4="sleep 30")
    passed, lines = run(world, network="isolated")
    assert not passed
    assert (
        "FAIL isolated: a public IPv4 address is not reachable: could not be checked "
        "(the probe did not finish in time)" in lines
    )
    assert not any("None" in line for line in lines)
