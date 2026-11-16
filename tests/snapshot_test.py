"""snapshot_test.py — the tar archive of the candidate working tree (decision D14).

Coverage:
  - tracked files as they are on disk, plus untracked files that are not ignored
  - Load-bearing: a file deleted in the working tree is absent from the archive
  - ignored files and the spool directory are never archived
  - Load-bearing: symbolic links are stored as links and never followed, at the
    end of a path or anywhere along it; nothing outside the checkout is read
  - a tracked directory replaced by a file or a link leaves its files out
  - a file that grows, shrinks, or is rewritten while being read stops the snapshot
  - special files and directories are skipped
  - owner and group are neutral; permission bits are kept
  - digest and size describe the archive as written
  - the size limit is enforced while writing
  - GIT_* variables in the caller's environment do not change what is archived
"""

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest
from conftest import World

from tart_taste_test import protocol, snapshot

LIMIT = protocol.MAX_SOURCE_BYTES


def write(
    world: World, destination: Path, paths: list[str] | None = None, limit: int = LIMIT
) -> snapshot.Snapshot:
    listed = snapshot.candidate_paths(world.repo) if paths is None else paths
    with destination.open("wb") as target:
        return snapshot.write_snapshot(world.repo, listed, target, limit)


def archive(world: World, tmp_path: Path) -> tuple[tarfile.TarFile, snapshot.Snapshot, Path]:
    destination = tmp_path / "source.tar"
    taken = write(world, destination)
    return tarfile.open(destination), taken, destination


def test_working_tree_state_is_archived(world: World, tmp_path: Path) -> None:
    world.write(
        {
            "run_tests.sh": "edited but not committed\n",
            "new/untracked.txt": "new\n",
            "ignored/build.bin": "ignored directory\n",
            "debug.log": "ignored by pattern\n",
        }
    )
    tar, taken, _ = archive(world, tmp_path)
    names = set(tar.getnames())
    assert {"run_tests.sh", "new/untracked.txt", "keep.txt", ".taste-test.toml"} <= names
    assert not any(name.startswith("ignored") or name.endswith(".log") for name in names)
    edited = tar.extractfile("run_tests.sh")
    assert edited is not None and edited.read() == b"edited but not committed\n"
    assert taken.files == len(names)


# Load-bearing: the guest must not test a file the candidate deleted.
def test_deleted_tracked_file_is_absent(world: World, tmp_path: Path) -> None:
    world.write({"keep.txt": None})
    tar, taken, _ = archive(world, tmp_path)
    assert "keep.txt" not in tar.getnames()
    assert taken.skipped == 1


def test_spool_directory_is_never_archived(world: World, tmp_path: Path) -> None:
    world.submit()  # creates .taste-test/ with a snapshot inside
    (world.repo / ".gitignore").write_text("")  # even if nothing ignores it
    (world.repo / protocol.DIR_NAME / protocol.GITIGNORE_NAME).unlink()
    tar, _, _ = archive(world, tmp_path)
    assert not any(name.startswith(protocol.DIR_NAME + "/") for name in tar.getnames())
    assert ".taste-test.toml" in tar.getnames()  # the config file is not the spool


def test_symbolic_links_are_stored_as_links_and_not_followed(world: World, tmp_path: Path) -> None:
    secret = tmp_path / "outside-secret"
    secret.write_text("host secret\n")
    os.symlink(secret, world.repo / "link-out")
    os.symlink("keep.txt", world.repo / "link-in")
    tar, _, destination = archive(world, tmp_path)
    out = tar.getmember("link-out")
    assert out.issym() and out.linkname == str(secret) and out.size == 0
    assert tar.getmember("link-in").linkname == "keep.txt"
    assert b"host secret" not in destination.read_bytes()


# Load-bearing: nothing outside the checkout is read, whatever Git lists.
def test_files_below_a_directory_replaced_by_a_link_are_left_out(
    world: World, tmp_path: Path
) -> None:
    world.commit({"d/tracked.txt": "inside\n", "d/deeper/also.txt": "inside\n"})
    outside = tmp_path / "outside"
    (outside / "deeper").mkdir(parents=True)
    (outside / "tracked.txt").write_text("OUTSIDE SECRET\n")
    (outside / "deeper" / "also.txt").write_text("OUTSIDE SECRET\n")
    for name in ("d/tracked.txt", "d/deeper/also.txt"):
        (world.repo / name).unlink()
    (world.repo / "d" / "deeper").rmdir()
    (world.repo / "d").rmdir()
    os.symlink(outside, world.repo / "d")
    assert "d/tracked.txt" in snapshot.candidate_paths(world.repo)  # Git still lists it

    tar, taken, destination = archive(world, tmp_path)
    assert b"OUTSIDE SECRET" not in destination.read_bytes()
    names = tar.getnames()
    assert "d/tracked.txt" not in names and "d/deeper/also.txt" not in names
    # Nothing is stored below a name that is stored as a link.
    assert tar.getmember("d").issym()
    assert not any(name.startswith("d/") for name in names)
    assert taken.skipped == 2


def test_files_below_a_directory_replaced_by_a_file_are_skipped(
    world: World, tmp_path: Path
) -> None:
    world.commit({"e/g.txt": "tracked\n"})
    (world.repo / "e" / "g.txt").unlink()
    (world.repo / "e").rmdir()
    (world.repo / "e").write_text("now a file\n")
    tar, taken, _ = archive(world, tmp_path)
    assert "e/g.txt" not in tar.getnames()
    assert tar.getmember("e").isreg()
    assert taken.skipped == 1


def test_special_files_and_directories_are_skipped(world: World, tmp_path: Path) -> None:
    os.mkfifo(world.repo / "pipe")
    (world.repo / "sub").mkdir()
    taken = write(world, tmp_path / "s.tar", ["pipe", "sub", "keep.txt", "gone.txt"])
    assert (taken.files, taken.skipped) == (1, 3)


@pytest.mark.parametrize("change", ["grow", "shrink", "rewrite"])
def test_a_file_that_changes_while_being_read_stops_the_snapshot(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    victim = world.repo / "keep.txt"
    victim.write_bytes(b"x" * 4096)
    real = tarfile.TarFile.addfile

    def meddling(self: tarfile.TarFile, info: tarfile.TarInfo, fileobj: object = None) -> None:
        if info.name == "keep.txt" and change == "shrink":
            victim.write_bytes(b"x" * 10)
        real(self, info, fileobj)  # type: ignore[arg-type]
        if info.name == "keep.txt" and change == "grow":
            with victim.open("ab") as handle:
                handle.write(b"more")
        if info.name == "keep.txt" and change == "rewrite":
            victim.write_bytes(b"y" * 4096)
            later = victim.stat().st_mtime + 5
            os.utime(victim, (later, later))

    monkeypatch.setattr(tarfile.TarFile, "addfile", meddling)
    with pytest.raises(snapshot.SnapshotError, match=r"keep\.txt changed while being archived"):
        write(world, tmp_path / "s.tar")


def test_ownership_is_neutral_and_modes_are_kept(world: World, tmp_path: Path) -> None:
    (world.repo / "tool.sh").write_text("#!/bin/sh\n")
    (world.repo / "tool.sh").chmod(0o755)
    tar, _, _ = archive(world, tmp_path)
    for member in tar.getmembers():
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
    assert tar.getmember("tool.sh").mode == 0o755


def test_digest_and_size_describe_the_archive(world: World, tmp_path: Path) -> None:
    _, taken, destination = archive(world, tmp_path)
    data = destination.read_bytes()
    assert taken.size == len(data)
    assert taken.sha256 == hashlib.sha256(data).hexdigest()


def test_size_limit_is_enforced(world: World, tmp_path: Path) -> None:
    world.write({"big.bin": "x" * 200_000})
    with pytest.raises(snapshot.SnapshotTooLarge, match="larger than 100000 bytes"):
        write(world, tmp_path / "s.tar", limit=100_000)


def test_unexpected_paths_are_refused(world: World, tmp_path: Path) -> None:
    for name in ("../outside", "/etc/hosts", "a/../../b"):
        with pytest.raises(snapshot.SnapshotError, match="unexpected path"):
            write(world, tmp_path / "s.tar", [name])


def test_git_environment_of_the_caller_is_ignored(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = snapshot.candidate_paths(world.repo)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / "index"))
    assert snapshot.candidate_paths(world.repo) == expected
    assert snapshot.checkout_root(world.repo) == world.repo.resolve()


def test_checkout_root_and_head(world: World, tmp_path: Path) -> None:
    nested = world.repo / "a" / "b"
    nested.mkdir(parents=True)
    assert snapshot.checkout_root(nested) == world.repo.resolve()
    assert snapshot.checkout_root(tmp_path) is None
    head = snapshot.head_commit(world.repo)
    assert head is not None and len(head) == 40


def test_archive_is_a_plain_stream_tar(world: World, tmp_path: Path) -> None:
    _, _, destination = archive(world, tmp_path)
    with tarfile.open(fileobj=io.BytesIO(destination.read_bytes()), mode="r|") as stream:
        assert "keep.txt" in [member.name for member in stream]
