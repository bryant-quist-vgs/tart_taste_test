"""broker_cli_test.py — the `taste-test-broker` command line, built against contract v1.

Coverage:
  - Load-bearing: register never takes the remote from the repository; it
    requires --remote-url, and refuses local paths as remotes
  - register: --trust-local needs --branch; a linked worktree is refused
  - unregister removes the entry and its mirror; list shows the entries
  - serve --once processes waiting requests using the `tart` found on PATH
  - Load-bearing: serve keeps serving and exits 0 when its output is closed
  - prune deletes only this state's VMs; --environments adds cached images
  - doctor exit codes; usage errors exit 2; refusals exit 1
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import World, git

from tart_taste_test import broker_cli, registry, tart

REMOTE = "git@github.com:org/project.git"


@pytest.fixture(autouse=True)
def tart_on_path(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f"{world.tart_path.parent}:/usr/bin:/bin")


def fresh_repo(tmp_path: Path, name: str = "other") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch", "main")
    return repo


# Load-bearing: the trusted remote is never read from agent-writable config.
def test_register_requires_an_explicit_remote_url(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = fresh_repo(tmp_path)
    git(repo, "remote", "add", "origin", "https://attacker.example/evil.git")
    assert broker_cli.main(["register", str(repo)]) == 1
    error = capsys.readouterr().err
    assert "--remote-url URL is required" in error
    assert "attacker.example" not in error
    assert [r.path for r in registry.load(world.state)] == [str(world.repo)]


def test_register_records_the_remote_and_branch(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = fresh_repo(tmp_path)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert (
        broker_cli.main(["register", str(nested), "--remote-url", REMOTE, "--branch", "trunk"]) == 0
    )
    entry = next(r for r in registry.load(world.state) if r.path == str(repo.resolve()))
    assert (entry.source, entry.url, entry.branch) == ("remote", REMOTE, "trunk")
    output = capsys.readouterr().out
    assert f"default branch: trunk fetched from {REMOTE}" in output


@pytest.mark.parametrize(
    "url", ["/tmp/local/repo", "file:///tmp/local/repo", "../sibling", "ext::sh -c x", "-oX=y"]
)
def test_register_refuses_local_and_option_like_remotes(
    world: World, tmp_path: Path, url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = fresh_repo(tmp_path)
    assert broker_cli.main(["register", str(repo), f"--remote-url={url}", "--branch", "main"]) == 1
    assert capsys.readouterr().err.startswith("Error: ")
    assert len(registry.load(world.state)) == 1


def test_trust_local_needs_a_branch_and_warns(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = fresh_repo(tmp_path)
    assert broker_cli.main(["register", str(repo), "--trust-local"]) == 1
    assert "--trust-local requires --branch" in capsys.readouterr().err
    assert broker_cli.main(["register", str(repo), "--trust-local", "--remote-url", REMOTE]) == 1
    assert "cannot be combined" in capsys.readouterr().err

    assert broker_cli.main(["register", str(repo), "--trust-local", "--branch", "main"]) == 0
    output = capsys.readouterr().out
    assert "WARNING: anything that can commit to that branch" in output
    entry = next(r for r in registry.load(world.state) if r.path == str(repo.resolve()))
    assert (entry.source, entry.url) == ("local", None)


def test_register_replaces_an_existing_entry(world: World) -> None:
    assert (
        broker_cli.main(["register", str(world.repo), "--remote-url", REMOTE, "--branch", "x"]) == 0
    )
    entries = registry.load(world.state)
    assert len(entries) == 1 and entries[0].branch == "x"


def test_linked_worktree_and_non_repository_are_refused(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = tmp_path / "linked"
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(linked))
    assert broker_cli.main(["register", str(linked), "--remote-url", REMOTE, "--branch", "m"]) == 1
    assert "is a linked worktree" in capsys.readouterr().err
    plain = tmp_path / "plain"
    plain.mkdir()
    assert broker_cli.main(["register", str(plain), "--remote-url", REMOTE, "--branch", "m"]) == 1
    assert "is not inside a Git working tree" in capsys.readouterr().err
    assert (
        broker_cli.main(["register", str(tmp_path / "nope"), "--trust-local", "--branch", "m"]) == 1
    )
    assert "is not a directory" in capsys.readouterr().err


def test_unregister_removes_the_entry_and_its_mirror(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.run()  # creates the mirror
    mirror_dir = world.state.mirrors / f"{world.repository.key}.git"
    assert mirror_dir.is_dir()
    assert broker_cli.main(["unregister", str(world.repo)]) == 0
    assert registry.load(world.state) == []
    assert not mirror_dir.exists()
    assert broker_cli.main(["unregister", str(world.repo)]) == 1
    assert "is not registered" in capsys.readouterr().err


def test_list_shows_each_repository(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert broker_cli.main(["list"]) == 0
    output = capsys.readouterr().out
    assert str(world.repo) in output and "branch main from local branch (trusted)" in output
    registry.save(world.state, [])
    assert broker_cli.main(["list"]) == 0
    assert "no repositories are registered" in capsys.readouterr().out


def test_serve_once_runs_waiting_requests_with_tart_from_path(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    request_id = world.submit()
    assert broker_cli.main(["serve", "--once"]) == 0
    assert world.result(request_id)["outcome"] == "passed"
    output = capsys.readouterr().out
    assert "max run network isolated" in output and f"{request_id} passed" in output


# Load-bearing: the exit code is the broker's own, not Python's 120 for a failed flush.
def test_serve_keeps_serving_and_exits_0_when_its_output_is_closed(world: World) -> None:
    request_id = world.submit()
    reader, dead = os.pipe()
    os.close(reader)
    script = (
        "import sys\nfrom tart_taste_test import broker_cli\n"
        "sys.exit(broker_cli.main(['serve', '--once']))\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", script], stdout=dead, stderr=subprocess.DEVNULL, timeout=120
        )
    finally:
        os.close(dead)
    assert done.returncode == 0
    assert world.result(request_id)["outcome"] == "passed"


def test_serve_flags_set_the_ceilings(world: World) -> None:
    request_id = world.submit()
    assert broker_cli.main(["serve", "--once", "--max-timeout", "60"]) == 0
    result = world.result(request_id)
    assert result["outcome"] == "rejected"
    assert "--max-timeout ceiling is 60" in result["reason"]


def test_second_broker_on_the_same_state_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    with registry.exclusive(world.state):
        assert broker_cli.main(["serve", "--once"]) == 1
        assert "another taste-test-broker" in capsys.readouterr().err
        assert broker_cli.main(["prune"]) == 1


def test_prune_deletes_only_this_states_vms(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    result = world.run()
    names = tart.Names(world.state.key)
    vms = world.vms()
    leftover = names.run("00112233aabb")
    foreign = tart.Names("ffffffff").run("00112233aabb")
    for name in (leftover, foreign, "operators-own-vm"):
        vms.clone("ghcr.io/example/image:latest", name)

    assert broker_cli.main(["prune"]) == 0
    assert capsys.readouterr().out.strip() == f"deleted {leftover}"
    assert set(world.vm_names()) == {result["environment"]["vm"], foreign, "operators-own-vm"}

    assert broker_cli.main(["prune", "--environments"]) == 0
    assert set(world.vm_names()) == {foreign, "operators-own-vm"}
    assert broker_cli.main(["prune"]) == 0
    assert "nothing to delete" in capsys.readouterr().out


def test_doctor_exit_codes(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert broker_cli.main(["doctor"]) == 0
    assert "PASS tart is on PATH" in capsys.readouterr().out
    world.state.root.chmod(0o755)
    assert broker_cli.main(["doctor"]) == 1
    assert "FAIL operator state" in capsys.readouterr().out
    assert broker_cli.main(["serve", "--once"]) == 1  # the broker refuses to start, too
    assert "accessible to other users" in capsys.readouterr().err
    world.state.root.chmod(0o700)
    assert broker_cli.main(["doctor"]) == 0


def test_doctor_refuses_an_image_that_could_be_an_option(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert broker_cli.main(["doctor", "--image=-rf"]) == 1
    assert "is not a valid image reference" in capsys.readouterr().err
    assert world.calls("clone") == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["frobnicate"],
        ["serve", "--max-run-network", "everything"],
        ["serve", "--max-timeout", "0"],
        ["serve", "--max-cpu", "many"],
        ["doctor", "--network", "none"],
        ["register", "a", "b"],
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        broker_cli.main(argv)
    assert stopped.value.code == 2


def test_help_states_exit_codes_and_where_to_run(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        broker_cli.main(["--help"])
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    assert "exit codes: 0 success" in text
    assert "1 a command failed or refused, or a doctor check failed" in text
    assert "outside any sandbox" in text
    assert "TASTE_TEST_STATE_DIR" in text
    # The default-branch rule is stated with its one exception, and stopping is explained.
    assert "exception is a repository registered with --trust-local" in text
    assert "Ctrl-C or SIGTERM" in text and "ends as `error`" in text
    assert all(len(line) <= 80 for line in text.splitlines())


def test_state_directory_that_cannot_be_used_is_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv(registry.STATE_ENV, str(blocker / "state"))
    for argv in (["serve", "--once"], ["prune"], ["register", "--trust-local", "--branch", "main"]):
        assert broker_cli.main(argv) == 1
        assert capsys.readouterr().err.startswith("Error: ")
