"""Broker-side access to a spool, anchored to directory descriptors.

A spool lives in a directory an untrusted agent can write to, while the broker
runs with the operator's authority. Every operation here follows the rules in
docs/protocol.md §4:

- directories are opened relative to their parent's descriptor, never following
  a symbolic link, and must belong to the current user;
- files are opened without following links, without blocking on a FIFO, and
  must be regular files that belong to the current user;
- the broker never opens an existing file for writing: it removes the name and
  creates the file exclusively, or writes a temporary file and renames it;
- every name is one of the fixed names in `protocol` or an ID that matched the
  request-ID pattern.

Nothing in this module unpacks, lists, or parses a snapshot.
"""

import contextlib
import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tart_taste_test import protocol
from tart_taste_test.checkouts import DIR_FLAGS, READ_FLAGS

_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_COPY_CHUNK = 1 << 20
# More entries than this in one spool directory are ignored; an agent cannot
# make the broker's poll cycle arbitrarily slow by filling its own spool.
MAX_ENTRIES = 4096


class SpoolError(Exception):
    """A spool path is missing, of the wrong kind, foreign-owned, or over a limit."""


class SnapshotTooLarge(SpoolError):
    """`source.tar` is larger than the limit: the request is refused, not failed."""


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _owned(descriptor: int, what: str) -> os.stat_result:
    status = os.fstat(descriptor)
    if status.st_uid != os.getuid():
        raise SpoolError(f"{what} is not owned by the user running the broker")
    return status


def _open_directory(name: str | Path, parent: int | None, what: str) -> int:
    try:
        descriptor = os.open(name, DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        raise SpoolError(f"cannot open {what}: {error.strerror}") from None
    try:
        _owned(descriptor, what)
    except SpoolError:
        os.close(descriptor)
        raise
    return descriptor


def _open_regular(name: str, parent: int, what: str) -> int:
    try:
        descriptor = os.open(name, READ_FLAGS, dir_fd=parent)
    except OSError as error:
        raise SpoolError(f"cannot open {what}: {error.strerror}") from None
    try:
        status = _owned(descriptor, what)
        if not stat.S_ISREG(status.st_mode):
            raise SpoolError(f"{what} is not a regular file")
    except SpoolError:
        os.close(descriptor)
        raise
    return descriptor


def _create(name: str, parent: int, mode: int = 0o644) -> int:
    """Create `name` exclusively, replacing whatever directory entry was there."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=parent)
    return os.open(name, _CREATE_FLAGS, mode, dir_fd=parent)


def _write_atomic(name: str, parent: int, data: bytes) -> None:
    temporary = name + protocol.TMP_SUFFIX
    descriptor = _create(temporary, parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=parent)
        raise


class Spool:
    """One checkout's spool, held open by descriptor for as long as it is served."""

    def __init__(self, root: Path, spool: int, requests: int, results: int) -> None:
        self.root = root
        self._spool = spool
        self._requests = requests
        self._results = results

    @classmethod
    def open(cls, root: Path, top: int) -> Spool | None:
        """Open the spool of the checkout whose root directory is open as `top`.

        None when the checkout has no spool yet. `top` is the caller's (see
        `checkouts`) and is not closed here; every descriptor opened here is
        closed again unless a `Spool` is returned. Any failure is a `SpoolError`.
        """
        with contextlib.ExitStack() as stack:

            def enter(name: str, parent: int, what: str) -> int | None:
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                descriptor = _open_directory(name, parent, what)
                stack.callback(os.close, descriptor)
                return descriptor

            try:
                _owned(top, f"{root}")
                outer = enter(protocol.DIR_NAME, top, f"{protocol.DIR_NAME} under {root}")
                if outer is None:
                    return None
                spool = enter(protocol.SPOOL_NAME, outer, f"the spool under {root}")
                if spool is None:
                    return None
                requests = _open_directory(protocol.REQUESTS_NAME, spool, "the requests directory")
                stack.callback(os.close, requests)
                results = _open_directory(protocol.RESULTS_NAME, spool, "the results directory")
                # From here the three descriptors belong to the Spool.
                stack.pop_all()
            except OSError as error:
                raise SpoolError(f"cannot open the spool under {root}: {error.strerror}") from None
        os.close(outer)
        return cls(root, spool, requests, results)

    def close(self) -> None:
        for descriptor in (self._spool, self._requests, self._results):
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def current(self, top: int) -> bool:
        """Whether the checkout root open as `top` still leads to the directories held here.

        False once the spool, or its requests or results directory, has been
        removed, moved, or replaced, or the checkout itself has. Every step is
        relative to a descriptor and follows no link. A mismatch only makes the
        broker stop using this object and open the spool again under all the
        usual checks.
        """
        try:
            outer = os.open(protocol.DIR_NAME, DIR_FLAGS, dir_fd=top)
            try:
                now = os.stat(protocol.SPOOL_NAME, dir_fd=outer, follow_symlinks=False)
            finally:
                os.close(outer)
            if _identity(now) != _identity(os.fstat(self._spool)):
                return False
            for name, held in (
                (protocol.REQUESTS_NAME, self._requests),
                (protocol.RESULTS_NAME, self._results),
            ):
                inside = os.stat(name, dir_fd=self._spool, follow_symlinks=False)
                if _identity(inside) != _identity(os.fstat(held)):
                    return False
        except OSError:
            return False
        return True

    # -- listing ---------------------------------------------------------

    def _ids(self, directory: int) -> list[str]:
        names: list[str] = []
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_ENTRIES:
                    break
                if protocol.is_request_id(entry.name):
                    names.append(entry.name)
        return sorted(names)

    def _has(self, directory: int, request_id: str, name: str) -> bool:
        """Whether `<request_id>/<name>` exists, with `<request_id>` a real directory.

        The ID's directory is opened without following a link before anything in
        it is looked at, so a link planted in its place is never looked through.
        """
        try:
            inner = _open_directory(request_id, directory, request_id)
        except SpoolError:
            return False
        try:
            os.stat(name, dir_fd=inner, follow_symlinks=False)
        except OSError:
            return False
        finally:
            os.close(inner)
        return True

    def unclaimed(self) -> list[str]:
        """IDs with a `request.json` and no result yet, sorted by ID.

        The order says nothing about age: agents choose their IDs. The broker
        keeps its own first-seen order (`fairqueue`).
        """
        return [
            i
            for i in self._ids(self._requests)
            if self._has(self._requests, i, protocol.REQUEST_FILE)
            and not self._has(self._results, i, protocol.RESULT_FILE)
        ]

    def interrupted(self) -> list[str]:
        """IDs that were claimed but have no result: a broker stopped mid-request."""
        return [
            i
            for i in self._ids(self._requests)
            if self._has(self._requests, i, protocol.CLAIMED_FILE)
            and not self._has(self._results, i, protocol.RESULT_FILE)
        ]

    def acknowledged(self, request_id: str) -> bool:
        return self._has(self._results, request_id, protocol.STATUS_FILE)

    def cancel_requested(self, request_id: str) -> bool:
        return self._has(self._requests, request_id, protocol.CANCEL_FILE)

    # -- reading a request -------------------------------------------------

    @contextlib.contextmanager
    def _request_dir(self, request_id: str) -> Iterator[int]:
        if not protocol.is_request_id(request_id):
            raise SpoolError("not a request ID")
        descriptor = _open_directory(request_id, self._requests, f"request {request_id}")
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def request_modified(self, request_id: str) -> float | None:
        """When the unclaimed `request.json` was last modified, or None without one."""
        try:
            with self._request_dir(request_id) as directory:
                status = os.stat(protocol.REQUEST_FILE, dir_fd=directory, follow_symlinks=False)
        except SpoolError, OSError:
            return None
        return status.st_mtime

    def claim(self, request_id: str) -> bytes | None:
        """Claim a request and return the bytes of the claimed file. None if it is gone.

        `request.json` is renamed to `request.claimed.json` first and the claimed
        file is then read once, so what is validated is exactly what was claimed:
        nothing the agent writes to `request.json` afterwards is ever read. The
        client withdraws by renaming the same file and then removing the
        request's directory, so only one side can win, and a directory that is
        already gone is a withdrawal too. A claimed file that fails its checks
        is refused with `SpoolError`, and the request's `source.tar` is removed
        first: once a request is claimed the spool copy of its snapshot never
        stays behind.
        """
        if protocol.is_request_id(request_id):
            try:
                os.stat(request_id, dir_fd=self._requests, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError:
                pass  # reported by the open below
        with self._request_dir(request_id) as directory:
            try:
                os.rename(
                    protocol.REQUEST_FILE,
                    protocol.CLAIMED_FILE,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                )
            except FileNotFoundError:
                return None
            except OSError as error:
                raise SpoolError(f"cannot claim request: {error.strerror}") from None
            try:
                descriptor = _open_regular(protocol.CLAIMED_FILE, directory, "the claimed request")
                with os.fdopen(descriptor, "rb") as handle:
                    if os.fstat(handle.fileno()).st_nlink != 1:
                        raise SpoolError("the claimed request has more than one link")
                    return handle.read(protocol.MAX_JSON_BYTES + 1)
            except SpoolError, OSError:
                with contextlib.suppress(OSError):
                    os.unlink(protocol.SOURCE_FILE, dir_fd=directory)
                raise

    def stage_source(self, request_id: str, destination: Path, limit: int) -> tuple[str, int]:
        """Copy `source.tar` to private staging, hashing it. Returns (sha256, size).

        The file must be a regular file with exactly one link, so it cannot be a
        hard link to some other file of the operator's. It is copied as bytes
        and never unpacked.
        """
        digest = hashlib.sha256()
        size = 0
        with self._request_dir(request_id) as directory:
            source = _open_regular(protocol.SOURCE_FILE, directory, "source.tar")
            with os.fdopen(source, "rb") as reader:
                status = os.fstat(reader.fileno())
                if status.st_nlink != 1:
                    raise SpoolError("source.tar has more than one link")
                if status.st_size > limit:
                    raise SnapshotTooLarge(f"the snapshot is larger than {limit} bytes")
                target = os.open(destination, _CREATE_FLAGS, 0o600)
                with os.fdopen(target, "wb") as writer:
                    while chunk := reader.read(_COPY_CHUNK):
                        size += len(chunk)
                        if size > limit:
                            raise SnapshotTooLarge(f"the snapshot is larger than {limit} bytes")
                        digest.update(chunk)
                        writer.write(chunk)
        return digest.hexdigest(), size

    def remove_source(self, request_id: str) -> None:
        with contextlib.suppress(SpoolError, OSError), self._request_dir(request_id) as directory:
            os.unlink(protocol.SOURCE_FILE, dir_fd=directory)

    # -- writing results ---------------------------------------------------

    @contextlib.contextmanager
    def _result_dir(self, request_id: str) -> Iterator[int]:
        if not protocol.is_request_id(request_id):
            raise SpoolError("not a request ID")
        try:
            os.mkdir(request_id, 0o755, dir_fd=self._results)
        except FileExistsError:
            pass
        except OSError as error:
            raise SpoolError(f"cannot create the result directory: {error.strerror}") from None
        descriptor = _open_directory(request_id, self._results, f"result {request_id}")
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def write_document(self, request_id: str, name: str, document: dict[str, Any]) -> None:
        """Atomically write one JSON document into the request's result directory."""
        try:
            with self._result_dir(request_id) as directory:
                _write_atomic(name, directory, protocol.encode(document))
        except OSError as error:
            raise SpoolError(f"cannot write {name}: {error.strerror}") from None

    def create_log(self, request_id: str, name: str) -> int:
        """Create an empty log exclusively and return a descriptor open for writing."""
        try:
            with self._result_dir(request_id) as directory:
                return _create(name, directory)
        except OSError as error:
            if error.errno in (errno.EISDIR, errno.EPERM, errno.EEXIST):
                raise SpoolError(f"{name} is in the way and cannot be replaced") from None
            raise SpoolError(f"cannot create {name}: {error.strerror}") from None

    def write_heartbeat(self, document: dict[str, Any]) -> None:
        try:
            _write_atomic(protocol.HEARTBEAT_FILE, self._spool, protocol.encode(document))
        except OSError as error:
            raise SpoolError(f"cannot write the heartbeat: {error.strerror}") from None
