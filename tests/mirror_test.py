"""mirror_test.py — the broker-owned mirror of the default branch (decision D5).

Uses a real Git repository as a `local` source, so no network is involved.

Coverage:
  - refresh fetches the registered branch and follows it when it moves
  - Load-bearing: the mirror reads the default branch, never the working tree
    or another branch
  - a failed fetch is an error; there is no fallback to an older commit
  - the repository config: present, absent, oversized
  - the manifest and the archive describe the commit
  - Git runs without prompting, with only the allowed transport
"""

import io
import os
import tarfile
from pathlib import Path

import pytest
from conftest import DEFAULT_CONFIG, World, git

from tart_taste_test import mirror, registry, repo_config


def made(world: World) -> mirror.Mirror:
    return mirror.Mirror(world.state, world.repository)


def test_refresh_fetches_the_branch_and_follows_it(world: World) -> None:
    first = made(world).refresh()
    assert first == git(world.repo, "rev-parse", "main")
    second_commit = world.commit({"keep.txt": "moved\n"})
    assert made(world).refresh() == second_commit != first
    assert made(world).path == world.state.mirrors / f"{world.repository.key}.git"
    assert (made(world).path / "HEAD").exists()


# Load-bearing: what the broker reads is the default branch, nothing else.
def test_working_tree_and_other_branches_are_not_what_the_mirror_reads(world: World) -> None:
    world.write({".taste-test.toml": "candidate edit, not committed\n"})
    git(world.repo, "checkout", "--quiet", "-b", "feature")
    world.commit({".taste-test.toml": "committed on a feature branch\n"}, "feature work")
    box = made(world)
    commit = box.refresh()
    assert commit == git(world.repo, "rev-parse", "main")
    assert box.read_config(commit) == DEFAULT_CONFIG.encode()


def test_failed_fetch_is_an_error_even_after_an_earlier_success(world: World) -> None:
    assert made(world).refresh()
    gone = registry.new_repository(world.repo, "no-such-branch", None)
    with pytest.raises(mirror.MirrorError, match="cannot fetch branch 'no-such-branch'"):
        mirror.Mirror(world.state, gone).refresh()


def test_missing_repository_is_an_error(world: World, tmp_path: Path) -> None:
    nowhere = registry.new_repository(tmp_path / "nowhere", "main", None)
    with pytest.raises(mirror.MirrorError):
        mirror.Mirror(world.state, nowhere).refresh()


def test_config_absent_from_the_commit_is_none(world: World) -> None:
    world.commit({".taste-test.toml": None}, "remove config")
    box = made(world)
    assert box.read_config(box.refresh()) is None


def test_oversized_config_is_refused_without_reading_it(world: World) -> None:
    world.commit({".taste-test.toml": "#" * (repo_config.MAX_CONFIG_BYTES + 1)})
    box = made(world)
    with pytest.raises(repo_config.ConfigError, match="larger than 65536 bytes"):
        box.read_config(box.refresh())


def test_manifest_lists_every_file_in_the_commit(world: World, tmp_path: Path) -> None:
    world.commit({"dir/with space.txt": "x\n", "dir/sub/deep.txt": "y\n"})
    world.write({"untracked.txt": "not in the commit\n"})
    box = made(world)
    destination = tmp_path / "manifest"
    box.write_manifest(box.refresh(), destination)
    names = [name.decode() for name in destination.read_bytes().split(b"\x00") if name]
    assert sorted(names) == sorted(
        [
            ".gitignore",
            ".taste-test.toml",
            "dir/sub/deep.txt",
            "dir/with space.txt",
            "keep.txt",
            "lint.sh",
            "run_tests.sh",
        ]
    )


def test_archive_holds_the_committed_tree(world: World, tmp_path: Path) -> None:
    world.write({"keep.txt": "uncommitted edit\n"})
    box = made(world)
    destination = tmp_path / "base.tar"
    box.write_archive(box.refresh(), destination)
    with tarfile.open(fileobj=io.BytesIO(destination.read_bytes())) as archive:
        member = archive.extractfile("keep.txt")
        assert member is not None and member.read() == b"from the default branch\n"
        assert ".taste-test.toml" in archive.getnames()


def test_only_commit_ids_are_accepted(world: World, tmp_path: Path) -> None:
    box = made(world)
    box.refresh()
    for bad in ("main", "HEAD", "--output=/tmp/x", "a" * 39, "refs/heads/main"):
        with pytest.raises(mirror.MirrorError, match="not a commit ID"):
            box.read_config(bad)
        with pytest.raises(mirror.MirrorError, match="not a commit ID"):
            box.write_archive(bad, tmp_path / "x.tar")


def test_git_environment_forbids_prompts_and_other_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    environment = mirror.git_environment(mirror.REMOTE_PROTOCOLS)
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert environment["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /operator/key")
    assert mirror.git_environment("file")["GIT_SSH_COMMAND"] == "ssh -i /operator/key"
    assert mirror.git_environment("")["GIT_ALLOW_PROTOCOL"] == ""


def test_operators_ssh_program_is_not_overridden_by_the_batch_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Git prefers GIT_SSH_COMMAND to GIT_SSH, so adding the default beside an
    # operator's GIT_SSH would silently replace their program.
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("GIT_SSH", "/operator/deploy-key-ssh")
    environment = mirror.git_environment(mirror.REMOTE_PROTOCOLS)
    assert environment["GIT_SSH"] == "/operator/deploy-key-ssh"
    assert "GIT_SSH_COMMAND" not in environment


@pytest.mark.parametrize("empty", ["GIT_SSH", "GIT_SSH_COMMAND"])
def test_an_empty_ssh_setting_is_not_a_choice(monkeypatch: pytest.MonkeyPatch, empty: str) -> None:
    for name in mirror.SSH_CHOICES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(empty, "")
    environment = mirror.git_environment(mirror.REMOTE_PROTOCOLS)
    assert environment["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert "GIT_SSH" not in environment


def test_git_environment_keeps_only_the_operators_transport_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = {
        "GIT_DIR": "/somewhere/.git",
        "GIT_WORK_TREE": "/somewhere",
        "GIT_INDEX_FILE": "/somewhere/index",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "/bin/evil",
        "GIT_CONFIG_PARAMETERS": "'core.pager=evil'",
        "GIT_EXEC_PATH": "/somewhere/bin",
        "GIT_PROXY_COMMAND": "/bin/evil",
        "GIT_ALLOW_PROTOCOL": "file:ext",
    }
    kept = {"GIT_ASKPASS": "/operator/askpass", "GIT_CONFIG_GLOBAL": "/operator/gitconfig"}
    for name, value in {**hostile, **kept}.items():
        monkeypatch.setenv(name, value)
    environment = mirror.git_environment(mirror.REMOTE_PROTOCOLS)
    for name in hostile:
        assert name not in environment or environment[name] != hostile[name]
    assert environment["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert {name: environment[name] for name in kept} == kept
    assert environment["PATH"] == os.environ["PATH"]  # everything else passes through


# Load-bearing: Git never acts on the repository the broker happens to stand in.
def test_git_runs_outside_any_checkout(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(world.repo)
    status, _, _ = mirror.run_git(["rev-parse", "--git-dir"], timeout=30)
    assert status != 0  # no repository is found from the neutral directory
    monkeypatch.setenv("GIT_DIR", str(world.repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(world.repo))
    status, _, _ = mirror.run_git(["rev-parse", "--git-dir"], timeout=30)
    assert status != 0  # nor through the environment


# Load-bearing: `register` run from inside a checkout is not steered by that
# checkout's own Git configuration (which an agent can write).
def test_default_branch_lookup_ignores_the_checkout_it_is_run_from(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "ssh-arguments"
    recorder = tmp_path / "ssh-recorder"
    recorder.write_text(f'#!/bin/sh\nfor a in "$@"; do echo "$a"; done > "{seen}"\nexit 1\n')
    recorder.chmod(0o755)
    git(world.repo, "config", "url.ssh://attacker.invalid/.insteadOf", "ssh://trusted.invalid/")
    git(world.repo, "config", "core.sshCommand", "/bin/false")
    monkeypatch.chdir(world.repo)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(recorder))  # the operator's own choice is kept
    with pytest.raises(mirror.MirrorError, match="git ls-remote failed"):
        mirror.remote_default_branch("ssh://trusted.invalid/repo.git")
    arguments = seen.read_text().splitlines()
    assert "trusted.invalid" in arguments and "attacker.invalid" not in arguments


def test_config_path_that_is_not_a_file_is_refused(world: World) -> None:
    world.commit({".taste-test.toml": None, ".taste-test.toml/inside": "x\n"}, "a directory")
    box = mirror.Mirror(world.state, world.repository)
    with pytest.raises(repo_config.ConfigError, match="not a file"):
        box.read_config(box.refresh())


def test_remote_mode_cannot_fetch_from_a_local_path(world: World) -> None:
    """A registry entry claiming a remote but naming a path is refused by Git itself."""
    forged = registry.Repository(
        path=str(world.repo),
        key=world.repository.key,
        branch="main",
        source="remote",
        url=str(world.repo),  # bypasses check_url, as a tampered registry would
        registered="2026-11-15T00:00:00Z",
    )
    with pytest.raises(mirror.MirrorError, match="cannot fetch"):
        mirror.Mirror(world.state, forged).refresh()


def test_git_that_runs_too_long_is_stopped(tmp_path: Path) -> None:
    with pytest.raises(mirror.MirrorError, match="did not finish"):
        mirror.run_git(["-c", "alias.wait=!sleep 30", "wait"], timeout=0.3)


def test_default_branch_of_a_remote_requires_an_acceptable_url() -> None:
    with pytest.raises(registry.RegistryError):
        mirror.remote_default_branch("/local/path")
    with pytest.raises(registry.RegistryError):
        mirror.remote_default_branch("ext::sh -c evil")
