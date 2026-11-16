"""registry_test.py — operator state and the registry (docs/protocol.md §5).

Coverage:
  - Load-bearing: a remote URL cannot be an option, a remote helper, a local
    path, or a file:// URL
  - branch names that could be options or traversals are refused
  - the registry round-trips and is written atomically and privately
  - a corrupt registry is an error, not an empty registry
  - keys: the repository key and the state key
  - the broker lock is exclusive
"""

import json
from pathlib import Path

import pytest

from tart_taste_test import registry
from tart_taste_test.registry import RegistryError, State


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:org/project.git",
        "https://github.com/org/project.git",
        "ssh://git@github.com/org/project.git",
        "ssh://git@example.com:2222/org/project",
        "deploy@git.internal:team/project",
    ],
)
def test_https_and_ssh_remotes_are_accepted(url: str) -> None:
    assert registry.check_url(url) == url


# Load-bearing: a "remote" can never be something inside the agent's reach.
@pytest.mark.parametrize(
    "url",
    [
        "",
        "-oProxyCommand=evil",
        "--upload-pack=evil",
        "ext::sh -c evil",
        "fd::3",
        "file:///Users/me/src/project",
        "/Users/me/src/project",
        "./relative/repo",
        "../sibling",
        "git://github.com/org/project.git",
        "http://github.com/org/project.git",
        "user@host:/absolute/path",
        "https://example.com/a b",
        "https://example.com/x\nextra",
        "https://example.com/\x1b[31m",
        "https://" + "a" * 2100,
    ],
)
def test_unacceptable_remotes_are_refused(url: str) -> None:
    with pytest.raises(RegistryError):
        registry.check_url(url)


@pytest.mark.parametrize("branch", ["main", "release/2.x", "feature_a-b.c", "1.0"])
def test_branch_names_are_accepted(branch: str) -> None:
    assert registry.check_branch(branch) == branch


@pytest.mark.parametrize(
    "branch", ["", "-f", "--upload-pack=x", "a..b", "a b", "a:b", "a\nb", "ref~1", "x" * 201]
)
def test_unacceptable_branch_names_are_refused(branch: str) -> None:
    with pytest.raises(RegistryError):
        registry.check_branch(branch)


def test_registry_round_trip_is_sorted_private_and_atomic(tmp_path: Path) -> None:
    state = State(tmp_path / "state")
    assert registry.load(state) == []
    second = registry.new_repository(tmp_path / "b", "main", "git@github.com:org/b.git")
    first = registry.new_repository(tmp_path / "a", "trunk", None)
    registry.save(state, [second, first])

    assert registry.load(state) == [first, second]
    assert (first.source, first.url) == ("local", None)
    assert first.fetch_source() == first.path
    assert second.fetch_source() == "git@github.com:org/b.git"
    assert state.registry.stat().st_mode & 0o777 == 0o600
    assert not state.registry.with_suffix(".json.tmp").exists()
    assert state.is_private()
    document = json.loads(state.registry.read_text())
    assert document["schema"] == 1 and len(document["repositories"]) == 2


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"schema": 2, "repositories": []}',
        '{"schema": 1}',
        '{"schema": 1, "repositories": [42]}',
        '{"schema": 1, "repositories": [{"path": "/x"}]}',
    ],
)
def test_corrupt_registry_is_an_error_not_an_empty_registry(tmp_path: Path, content: str) -> None:
    state = State(tmp_path / "state")
    state.ensure()
    state.registry.write_text(content)
    with pytest.raises(RegistryError):
        registry.load(state)


@pytest.mark.parametrize(
    "change",
    [
        {"source": "magic"},
        {"key": "../../etc"},
        {"path": "relative/path"},
        {"branch": "--evil"},
        {"url": "file:///tmp/x"},
        {"url": None},
        {"path": 7},
    ],
)
def test_tampered_registry_entries_are_refused(tmp_path: Path, change: dict[str, object]) -> None:
    state = State(tmp_path / "state")
    good = registry.new_repository(tmp_path / "a", "main", "git@github.com:org/a.git")
    registry.save(state, [good])
    document = json.loads(state.registry.read_text())
    document["repositories"][0].update(change)
    state.registry.write_text(json.dumps(document))
    with pytest.raises(RegistryError):
        registry.load(state)


def test_keys_are_stable_and_distinct(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    assert registry.repo_key(a) == registry.repo_key(a)
    assert registry.repo_key(a) != registry.repo_key(b)
    assert len(registry.repo_key(a)) == 12
    one, two = State(tmp_path / "one"), State(tmp_path / "two")
    assert len(one.key) == 8 and one.key != two.key
    assert State(tmp_path / "one").key == one.key


def test_state_directory_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(registry.STATE_ENV, str(tmp_path / "custom"))
    assert registry.state_from_environment().root == (tmp_path / "custom").resolve()
    monkeypatch.delenv(registry.STATE_ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / "Library" / "Application Support" / "tart-taste-test"
    assert registry.state_from_environment().root == expected
    monkeypatch.delenv("HOME")
    with pytest.raises(RegistryError, match="HOME is not set"):
        registry.state_from_environment()


def test_state_that_others_can_read_is_not_private(tmp_path: Path) -> None:
    state = State(tmp_path / "state")
    state.ensure()
    assert state.is_private()
    state.root.chmod(0o755)
    assert not state.is_private()


def test_broker_lock_is_exclusive(tmp_path: Path) -> None:
    state = State(tmp_path / "state")
    with registry.exclusive(state):
        with (
            pytest.raises(RegistryError, match="another taste-test-broker"),
            registry.exclusive(state),
        ):
            pass
    with registry.exclusive(state):
        pass


def test_editing_saves_what_the_block_leaves(tmp_path: Path) -> None:
    state = State(tmp_path / "state")
    entry = registry.new_repository(tmp_path / "a", "main", None)
    with registry.editing(state) as repositories:
        repositories.append(entry)
    assert registry.load(state) == [entry]
    with registry.editing(state) as repositories:
        repositories.clear()
    assert registry.load(state) == []
