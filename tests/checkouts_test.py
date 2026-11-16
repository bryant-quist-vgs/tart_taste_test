"""checkouts_test.py — finding checkouts without trusting what an agent wrote.

Coverage (docs/protocol.md §4, "Which checkouts are served"):
  - Load-bearing: a checkout root is opened without following any link
  - Load-bearing: a worktree record is believed only if the working tree points
    back, and a record cannot aim the broker at a directory that does not
  - a linked working tree on a volume the repository's path does not cross is refused
  - records that are FIFOs, links, oversized, or relative are handled
  - the number of records read is bounded, the limit is reported, and records
    of working trees already served are read first
  - Load-bearing: every repository's claim to a working tree is kept, so a
    forged record in one repository cannot take a working tree from another
  - a directory on another volume is refused without being opened
  - running out of descriptors is a refusal, not a crash
"""

import errno
import os
from pathlib import Path

import pytest
from conftest import World, git

from tart_taste_test import checkouts, registry
from tart_taste_test.checkouts import CheckoutError


def add_worktree(world: World, path: Path) -> Path:
    git(world.repo, "worktree", "add", "--quiet", "--detach", str(path))
    return path


def record(world: World, name: str) -> Path:
    directory = world.repo / ".git" / "worktrees" / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def roots(world: World) -> list[Path]:
    return [linked.root for linked in checkouts.linked_worktrees(world.repo)]


def opens(linked: checkouts.Linked) -> bool:
    try:
        os.close(checkouts.open_linked(linked))
    except CheckoutError:
        return False
    return True


def test_real_worktree_is_found_and_opens(world: World, tmp_path: Path) -> None:
    linked = add_worktree(world, tmp_path / "linked")
    [found] = checkouts.linked_worktrees(world.repo)
    assert found.root == linked
    assert found.back_reference == f"{world.repo}/.git/worktrees/linked"
    assert opens(found)
    assert checkouts.served([world.repository]) == {
        world.repo: [(world.repository, None)],
        linked: [(world.repository, found)],
    }


# Load-bearing: no component of a checkout's path may be a link.
def test_path_through_a_link_is_refused(world: World, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    os.symlink(world.repo, alias)
    with pytest.raises(CheckoutError, match="without following a link"):
        checkouts.open_path(alias)
    with pytest.raises(CheckoutError, match="without following a link"):
        checkouts.open_path(alias / "src")
    descriptor, devices = checkouts.open_path(world.repo)
    os.close(descriptor)
    assert os.stat(world.repo).st_dev in devices and os.stat("/").st_dev in devices


@pytest.mark.parametrize("path", ["relative/path", "/a/../b", "/" + "x/" * 80])
def test_paths_that_are_not_plain_and_absolute_are_refused(path: str) -> None:
    with pytest.raises(CheckoutError, match="not a plain absolute path"):
        checkouts.open_path(Path(path))


def test_missing_directory_and_file_are_refused(world: World) -> None:
    for path in (world.repo / "missing", world.repo / "keep.txt"):
        with pytest.raises(CheckoutError):
            checkouts.open_path(path)


# Load-bearing: a record alone proves nothing; the working tree must point back.
def test_forged_record_does_not_open(world: World, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    git(victim, "init", "--quiet", "--initial-branch", "main")
    (record(world, "forged") / "gitdir").write_text(f"{victim}/.git\n")
    [claim] = checkouts.linked_worktrees(world.repo)
    assert claim.root == victim
    with pytest.raises(CheckoutError, match=r"no \.git file naming a worktree record"):
        checkouts.open_linked(claim)

    other = add_worktree(world, tmp_path / "other")  # points back, but to its own record
    (record(world, "forged") / "gitdir").write_text(f"{other}/.git\n")
    # Both records are claims to the one root. The forged one is read first (its
    # name sorts first) and is refused; it does not hide the true one.
    claims = checkouts.linked_worktrees(world.repo)
    assert [claim.root for claim in claims] == [other, other]
    assert [claim.back_reference.rpartition("/")[2] for claim in claims] == ["forged", "other"]
    with pytest.raises(CheckoutError, match="does not point back"):
        checkouts.open_linked(claims[0])
    assert opens(claims[1])
    assert [linked for _, linked in checkouts.served([world.repository])[other]] == claims


def test_worktree_reached_through_a_link_is_refused(world: World, tmp_path: Path) -> None:
    real = add_worktree(world, tmp_path / "real")
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    (record(world, "real") / "gitdir").write_text(f"{alias}/.git\n")
    [claim] = checkouts.linked_worktrees(world.repo)
    assert claim.root == alias and not opens(claim)


def test_worktree_whose_git_file_is_a_link_is_refused(world: World, tmp_path: Path) -> None:
    real = add_worktree(world, tmp_path / "real")
    (real / ".git").rename(tmp_path / "dot-git")
    os.symlink(tmp_path / "dot-git", real / ".git")
    [claim] = checkouts.linked_worktrees(world.repo)
    assert not opens(claim)


def test_worktree_on_a_volume_the_repository_does_not_cross_is_refused(
    world: World, tmp_path: Path
) -> None:
    add_worktree(world, tmp_path / "linked")
    [found] = checkouts.linked_worktrees(world.repo)
    elsewhere = checkouts.Linked(found.root, found.back_reference, frozenset({-1}))
    with pytest.raises(CheckoutError, match="not on a volume the repository's own path crosses"):
        checkouts.open_linked(elsewhere)
    with pytest.raises(CheckoutError, match="not on a volume"):
        checkouts.open_path(world.repo, frozenset({os.stat("/").st_dev + 12345}))


def test_relative_records_are_resolved_as_text(world: World, tmp_path: Path) -> None:
    """Git can write both directions as relative paths (worktree.useRelativePaths)."""
    linked = add_worktree(world, tmp_path / "linked")
    back = os.path.relpath(world.repo / ".git" / "worktrees" / "linked", linked)
    (linked / ".git").write_text(f"gitdir: {back}\n")
    forward = os.path.relpath(linked / ".git", world.repo / ".git" / "worktrees" / "linked")
    (record(world, "linked") / "gitdir").write_text(f"{forward}\n")
    [found] = checkouts.linked_worktrees(world.repo)
    assert found.root == linked and opens(found)


def test_unusable_records_are_skipped(world: World, tmp_path: Path) -> None:
    os.mkfifo(record(world, "fifo") / "gitdir")  # must not block
    os.symlink(tmp_path / "x", record(world, "link") / "gitdir")
    (record(world, "huge") / "gitdir").write_text("/" + "a" * 5000 + "/.git\n")
    (record(world, "empty") / "gitdir").write_text("\n")
    (record(world, "nul") / "gitdir").write_bytes(b"/tmp/x\x00/.git\n")
    (record(world, "not-dot-git") / "gitdir").write_text(f"{tmp_path}/somewhere\n")
    (record(world, "itself") / "gitdir").write_text(f"{world.repo}/.git\n")
    record(world, "no-gitdir")
    (world.repo / ".git" / "worktrees" / "a-file").write_text("x")
    assert roots(world) == []


def test_number_of_records_read_is_bounded(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checkouts, "MAX_WORKTREES", 4)
    for index in range(12):
        (record(world, f"r{index}") / "gitdir").write_text(f"{tmp_path}/w{index}/.git\n")
    assert len(roots(world)) <= 4


# Load-bearing: records an agent adds cannot push out a working tree being served.
def test_records_of_served_working_trees_are_read_first_and_the_limit_is_reported(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checkouts, "MAX_WORKTREES", 2)
    real = add_worktree(world, tmp_path / "zz-real")
    for index in range(8):
        (record(world, f"junk{index}") / "gitdir").write_text(f"{tmp_path}/w{index}/.git\n")
    warnings: list[str] = []
    found = checkouts.linked_worktrees(world.repo, ["zz-real"], warnings.append)
    assert found[0].root == real and len(found) <= 3
    assert len(warnings) == 1 and "more than 2 worktree records" in warnings[0]
    # The same holds through `served`, which is told what is being served now.
    wanted = checkouts.served([world.repository], [found[0]])
    assert real in wanted


# Load-bearing: a record is a claim, and every repository's claim is kept, so a
# forged record in one repository cannot take a working tree from another.
def test_every_claim_to_a_working_tree_is_kept_in_registration_order(
    world: World, tmp_path: Path
) -> None:
    linked = add_worktree(world, tmp_path / "linked")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet", "--initial-branch", "main")
    forged = other / ".git" / "worktrees" / "forged"
    forged.mkdir(parents=True)
    (forged / "gitdir").write_text(f"{linked}/.git\n")
    # A record naming another repository's main working tree is not a claim at all.
    takeover = other / ".git" / "worktrees" / "takeover"
    takeover.mkdir()
    (takeover / "gitdir").write_text(f"{world.repo}/.git\n")
    intruder = registry.new_repository(other, "main", None)

    wanted = checkouts.served([intruder, world.repository])
    assert wanted[world.repo] == [(world.repository, None)]
    assert wanted[other] == [(intruder, None)]
    claims = wanted[linked]
    assert [repository for repository, _ in claims] == [intruder, world.repository]
    verdicts = [opens(claim) for _, claim in claims if claim is not None]
    assert verdicts == [False, True]  # only the repository it points back to


def test_directory_on_another_volume_is_refused_without_being_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_device = os.stat("/").st_dev
    assert os.stat("/dev").st_dev != root_device  # /dev is a file system of its own
    opened: list[str] = []
    real_open = os.open

    def recording(name: str, *args: object, **kwargs: object) -> int:
        opened.append(str(name))
        return real_open(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", recording)
    with pytest.raises(CheckoutError, match="not on a volume"):
        checkouts.open_path(Path("/dev/fd"), frozenset({root_device}))
    assert opened == ["/"]  # the directory before the boundary, and nothing on the other side


def test_running_out_of_descriptors_is_a_refusal_not_a_crash(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exhausted(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    monkeypatch.setattr(os, "open", exhausted)
    with pytest.raises(CheckoutError, match="Too many open files"):
        checkouts.open_path(world.repo)


def test_repository_without_worktrees_or_that_is_gone_has_none(
    world: World, tmp_path: Path
) -> None:
    assert roots(world) == []
    assert checkouts.linked_worktrees(tmp_path / "missing") == []
