"""Build the snapshot: a tar archive of the candidate working tree.

The snapshot holds every tracked file as it is on disk plus every untracked file
that is not ignored (docs/decisions.md D14). File contents are copied byte for
byte and never interpreted. Symbolic links are stored as links and never
followed, at any level of a path: a file below a directory that has been
replaced by a link is left out, so nothing outside the checkout is read.
"""

import errno
import hashlib
import os
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from tart_taste_test import protocol

GIT_TIMEOUT_SECONDS = protocol.CLIENT_GIT_TIMEOUT_SECONDS


class SnapshotError(Exception):
    """The snapshot could not be built; the message is shown to the caller."""


class SnapshotTooLarge(SnapshotError):
    """The snapshot would exceed the size limit; the broker would reject it too."""


@dataclass(frozen=True)
class Snapshot:
    sha256: str
    size: int
    files: int
    skipped: int


def git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one read-only Git command in `root` with a time limit and no shell.

    Every `GIT_*` variable is removed from the command's environment, so a
    `GIT_DIR`, `GIT_WORK_TREE`, or `GIT_INDEX_FILE` the caller happened to have
    cannot point Git at another repository. The rest of the environment is
    passed through, so the user's own Git configuration still applies; a global
    excludes file, for example, decides which untracked files count as ignored.
    """
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            env=environment,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise SnapshotError("git was not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise SnapshotError(f"git {args[0]} did not finish in {GIT_TIMEOUT_SECONDS}s") from None


def checkout_root(directory: Path) -> Path | None:
    """The root of the working tree containing `directory`, or None."""
    done = git(directory, "rev-parse", "--show-toplevel")
    if done.returncode != 0:
        return None
    return Path(os.fsdecode(done.stdout.rstrip(b"\n")))


def head_commit(root: Path) -> str | None:
    """The commit ID of HEAD, or None when the repository has no commit yet."""
    done = git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    text = done.stdout.decode("ascii", "replace").strip()
    return text if done.returncode == 0 and text else None


def candidate_paths(root: Path) -> list[str]:
    """Repository-relative paths of the candidate's files, sorted."""
    done = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise SnapshotError(f"git ls-files failed: {detail}")
    names = {os.fsdecode(item) for item in done.stdout.split(b"\x00") if item}
    prefix = protocol.DIR_NAME + "/"
    return sorted(n for n in names if n != protocol.DIR_NAME and not n.startswith(prefix))


class _HashingSink:
    """A write-only stream that hashes and size-limits what passes through it."""

    def __init__(self, target: BinaryIO, limit: int) -> None:
        self._target = target
        self._limit = limit
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self.size += len(data)
        if self.size > self._limit:
            raise SnapshotTooLarge(
                f"the snapshot would be larger than {self._limit} bytes; "
                "add build output and large data files to .gitignore"
            )
        self._digest.update(data)
        return self._target.write(data)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# What opening a directory reports when the path is gone, runs through a file,
# or runs through a symbolic link.
_NOT_A_DIRECTORY = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)


class _Tree:
    """Directories below the checkout root, opened without following any link.

    Each component is opened relative to its parent's descriptor, so a directory
    that was replaced by a symbolic link is never entered. The most recent
    directory stays open because Git lists a directory's files together.
    """

    def __init__(self, root: Path) -> None:
        self._root = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self._parts: tuple[str, ...] | None = None
        self._descriptor = -1

    def directory(self, parts: tuple[str, ...]) -> int | None:
        """A descriptor for `parts` below the root, or None when it is not a real directory."""
        if parts == self._parts:
            return self._descriptor
        self._forget()
        descriptor = os.dup(self._root)
        try:
            for part in parts:
                child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except OSError as error:
            os.close(descriptor)
            if error.errno in _NOT_A_DIRECTORY:
                return None
            raise SnapshotError(f"cannot read {'/'.join(parts)}: {error.strerror}") from None
        self._parts, self._descriptor = parts, descriptor
        return descriptor

    def _forget(self) -> None:
        if self._parts is not None:
            os.close(self._descriptor)
            self._parts = None

    def close(self) -> None:
        self._forget()
        os.close(self._root)


def _entry(name: str, status: os.stat_result) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = stat.S_IMODE(status.st_mode)
    info.mtime = int(status.st_mtime)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_snapshot(root: Path, paths: list[str], target: BinaryIO, limit: int) -> Snapshot:
    """Archive `paths` (relative to `root`) into the open file `target`.

    Skipped and counted: paths that no longer exist, paths below something that
    is not a real directory (a file or a symbolic link where Git listed a
    directory), directories (submodules), and special files. Raises
    `SnapshotTooLarge` when the archive would exceed `limit` bytes and
    `SnapshotError` when a file cannot be read or changes while it is read.
    """
    files = skipped = 0
    sink = _HashingSink(target, limit)
    tree = _Tree(root)
    try:
        with tarfile.open(fileobj=sink, mode="w|") as archive:  # type: ignore[call-overload]
            for name in paths:
                parts = Path(name).parts
                if not parts or os.path.isabs(name) or ".." in parts:
                    raise SnapshotError(f"refusing to archive unexpected path {name!r}")
                directory = tree.directory(parts[:-1])
                if directory is None or not _add(archive, directory, parts[-1], name):
                    skipped += 1
                else:
                    files += 1
    finally:
        tree.close()
    return Snapshot(sink.hexdigest(), sink.size, files, skipped)


def _add(archive: tarfile.TarFile, directory: int, leaf: str, name: str) -> bool:
    """Add one path to the archive. False when it is gone or is not a file or a link."""
    try:
        status = os.lstat(leaf, dir_fd=directory)
        if stat.S_ISLNK(status.st_mode):
            info = _entry(name, status)
            info.type = tarfile.SYMTYPE
            info.linkname = os.readlink(leaf, dir_fd=directory)
            archive.addfile(info)
            return True
        if not stat.S_ISREG(status.st_mode):
            return False
        descriptor = os.open(leaf, _FILE_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SnapshotError(f"cannot read {name}: {error.strerror}") from None
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SnapshotError(f"{name} changed while being archived")
        info = _entry(name, before)
        info.size = before.st_size
        try:
            archive.addfile(info, handle)
            grew = bool(handle.read(1))
        except OSError as error:
            if _changed(descriptor, before):
                raise SnapshotError(f"{name} changed while being archived") from None
            raise SnapshotError(f"cannot archive {name}: {error}") from None
        if grew or _changed(descriptor, before):
            raise SnapshotError(f"{name} changed while being archived")
    return True


def _changed(descriptor: int, before: os.stat_result) -> bool:
    after = os.fstat(descriptor)
    return (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)
