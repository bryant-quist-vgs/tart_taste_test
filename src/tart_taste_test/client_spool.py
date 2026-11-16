"""The client's side of the spool: its paths, its reads, and submitting a request.

Everything here stays inside `<checkout>/.taste-test/` (docs/protocol.md §1).
Anything in the sandbox can write that directory, so nothing in it is trusted:

- every directory below the checkout root is opened relative to its parent's
  descriptor without following a symbolic link, and files are created and read
  relative to those descriptors, so a link planted anywhere in the spool is an
  error and never a way out of `.taste-test/`;
- files are opened without blocking and must be regular files, so a FIFO cannot
  stall the client;
- every JSON document is size-limited and validated when read.
"""

import contextlib
import errno
import os
import shutil
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from tart_taste_test import __version__, protocol, snapshot
from tart_taste_test.exitcodes import Exit

REQUESTS = (protocol.SPOOL_NAME, protocol.REQUESTS_NAME)
RESULTS = (protocol.SPOOL_NAME, protocol.RESULTS_NAME)

_ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_DIR_FLAGS = _ROOT_FLAGS | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_MARKER_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_FILE_MODE = 0o600
_REASON_CHARS = 200


class ClientError(Exception):
    """A failure with the exit code the client must end with."""

    def __init__(self, code: Exit, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SpoolPaths:
    """Where the spool is. Used for messages; file access goes through `directory`."""

    root: Path

    @property
    def base(self) -> Path:
        return self.root / protocol.DIR_NAME

    @property
    def spool(self) -> Path:
        return self.base / protocol.SPOOL_NAME

    @property
    def requests(self) -> Path:
        return self.spool / protocol.REQUESTS_NAME

    @property
    def results(self) -> Path:
        return self.spool / protocol.RESULTS_NAME

    @property
    def heartbeat(self) -> Path:
        return self.spool / protocol.HEARTBEAT_FILE

    def request_dir(self, request_id: str) -> Path:
        return self.requests / request_id

    def result_dir(self, request_id: str) -> Path:
        return self.results / request_id


def _why(error: OSError) -> str:
    return error.strerror or str(error)


def resolve_checkout(directory: Path) -> SpoolPaths:
    """Find the working tree containing `directory`; a usage error when there is none."""
    try:
        root = snapshot.checkout_root(directory)
    except snapshot.SnapshotError as error:
        raise ClientError(Exit.USAGE, str(error)) from None
    if root is None:
        raise ClientError(Exit.USAGE, f"{directory} is not inside a Git working tree")
    return SpoolPaths(root)


@contextlib.contextmanager
def directory(paths: SpoolPaths, *names: str) -> Iterator[int]:
    """Open `<checkout>/.taste-test/<names...>` and yield its descriptor.

    Raises `OSError` when a component is missing, is a symbolic link, or is not
    a directory.
    """
    descriptor = os.open(paths.root, _ROOT_FLAGS)
    try:
        for name in (protocol.DIR_NAME, *names):
            child = os.open(name, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def open_regular(paths: SpoolPaths, *names: str) -> BinaryIO:
    """Open the spool file `.taste-test/<names...>` for reading.

    Raises `FileNotFoundError` when the file or a directory above it is absent,
    and another `OSError` when it is anything but a regular file reached without
    a symbolic link.
    """
    with directory(paths, *names[:-1]) as parent:
        descriptor = os.open(names[-1], _READ_FLAGS, dir_fd=parent)
    handle = os.fdopen(descriptor, "rb")
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        handle.close()
        raise OSError(errno.EINVAL, "not a regular file")
    return handle


def check_spool(paths: SpoolPaths) -> None:
    """Refuse (exit 5) a spool that has anything but a real directory on its path."""
    for path in (paths.base, paths.spool, paths.requests, paths.results):
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return
        except OSError as error:
            raise ClientError(Exit.ERROR, f"cannot examine {path}: {_why(error)}") from None
        if not stat.S_ISDIR(mode):
            raise ClientError(Exit.ERROR, f"{path} exists and is not a directory; remove it")


def _make(parent: int, name: str, path: Path, opened: contextlib.ExitStack) -> int:
    """Create the directory `name` under `parent` if it is absent, and open it."""
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, protocol.SPOOL_DIR_MODE, dir_fd=parent)
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ClientError(
                Exit.ERROR, f"{path} exists and is not a directory; remove it"
            ) from None
        raise ClientError(Exit.ERROR, f"cannot create {path}: {_why(error)}") from None
    opened.callback(os.close, descriptor)
    return descriptor


def ensure_spool(paths: SpoolPaths) -> None:
    """Create the spool directories and `.gitignore`; refuse anything in their place."""
    ignore = paths.base / protocol.GITIGNORE_NAME
    with contextlib.ExitStack() as opened:
        try:
            root = os.open(paths.root, _ROOT_FLAGS)
        except OSError as error:
            raise ClientError(Exit.ERROR, f"cannot open {paths.root}: {_why(error)}") from None
        opened.callback(os.close, root)
        base = _make(root, protocol.DIR_NAME, paths.base, opened)
        spool = _make(base, protocol.SPOOL_NAME, paths.spool, opened)
        _make(spool, protocol.REQUESTS_NAME, paths.requests, opened)
        _make(spool, protocol.RESULTS_NAME, paths.results, opened)
        try:
            descriptor = os.open(protocol.GITIGNORE_NAME, _CREATE_FLAGS, 0o644, dir_fd=base)
        except FileExistsError:
            status = os.lstat(protocol.GITIGNORE_NAME, dir_fd=base)
            if not stat.S_ISREG(status.st_mode):
                raise ClientError(
                    Exit.ERROR, f"{ignore} exists and is not a regular file; remove it"
                ) from None
        except OSError as error:
            raise ClientError(Exit.ERROR, f"cannot create {ignore}: {_why(error)}") from None
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write("*\n")


def _load(paths: SpoolPaths, *names: str) -> tuple[bytes, float]:
    """The first bytes of a spool document and the time it was last modified."""
    with open_regular(paths, *names) as handle:
        modified = os.fstat(handle.fileno()).st_mtime
        return handle.read(protocol.MAX_JSON_BYTES + 1), modified


def read_document[T](
    paths: SpoolPaths, names: tuple[str, ...], parse: Callable[[bytes], T]
) -> T | None:
    """Read and validate one spool document. Missing or invalid files are absent."""
    try:
        return parse(_load(paths, *names)[0])
    except OSError, protocol.ProtocolError:
        return None


def heartbeat_age(paths: SpoolPaths) -> float | None:
    """Seconds since the broker last wrote its heartbeat here, or None without one.

    A heartbeat dated further in the future than the stale limit is not a
    heartbeat: it would otherwise look fresh for as long as its date says.
    """
    beat = read_document(
        paths, (protocol.SPOOL_NAME, protocol.HEARTBEAT_FILE), protocol.parse_heartbeat
    )
    if beat is None:
        return None
    age = time.time() - beat.updated
    return None if age < -protocol.HEARTBEAT_STALE_SECONDS else max(0.0, age)


def _result(paths: SpoolPaths, request_id: str) -> tuple[protocol.Result, float] | None:
    try:
        data, modified = _load(paths, *RESULTS, request_id, protocol.RESULT_FILE)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ClientError(
            Exit.ERROR, f"the result of {request_id} cannot be read: {_why(error)}"
        ) from None
    try:
        result = protocol.parse_result(data)
    except protocol.ProtocolError as error:
        detail = str(error)[:_REASON_CHARS]
        raise ClientError(
            Exit.ERROR, f"the result of {request_id} is not a valid result ({detail})"
        ) from None
    if result.id != request_id:
        raise ClientError(Exit.ERROR, f"the result of {request_id} names another request")
    return result, modified


def read_result(paths: SpoolPaths, request_id: str) -> protocol.Result | None:
    """The request's result, or None while there is none.

    Raises `ClientError` (exit 5) when a `result.json` exists but is not a valid
    result for this request. The broker puts the file in place with one rename,
    so an invalid one is never a result still being written.
    """
    found = _result(paths, request_id)
    return None if found is None else found[0]


def read_status(paths: SpoolPaths, request_id: str) -> protocol.Status | None:
    status = read_document(
        paths, (*RESULTS, request_id, protocol.STATUS_FILE), protocol.parse_status
    )
    return status if status is not None and status.id == request_id else None


def known(paths: SpoolPaths, request_id: str) -> bool:
    """Whether the spool has a request or a result directory for `request_id`."""
    for parent in (REQUESTS, RESULTS):
        try:
            with directory(paths, *parent, request_id):
                return True
        except OSError:
            continue
    return False


def request_ids(paths: SpoolPaths) -> list[str]:
    """Every request ID that has a directory in the spool, sorted."""
    names: set[str] = set()
    for parent in (REQUESTS, RESULTS):
        with contextlib.suppress(OSError), directory(paths, *parent) as descriptor:
            names.update(n for n in os.listdir(descriptor) if protocol.is_request_id(n))
    return sorted(names)


def prune(paths: SpoolPaths, retention: float = protocol.RETENTION_SECONDS) -> None:
    """Remove finished requests older than the retention period. Nothing else."""
    cutoff = time.time() - retention
    try:
        with directory(paths, *RESULTS) as results, directory(paths, *REQUESTS) as requests:
            for name in sorted(os.listdir(results)):
                if not protocol.is_request_id(name):
                    continue
                try:
                    found = _result(paths, name)
                except ClientError:
                    continue
                if found is not None and found[1] < cutoff:
                    shutil.rmtree(name, ignore_errors=True, dir_fd=requests)
                    shutil.rmtree(name, ignore_errors=True, dir_fd=results)
    except OSError:
        return


def _write_request(
    paths: SpoolPaths, home: int, request_id: str, command: str, args: list[str]
) -> None:
    descriptor = os.open(protocol.SOURCE_FILE, _CREATE_FLAGS, _FILE_MODE, dir_fd=home)
    with os.fdopen(descriptor, "wb") as target:
        taken = snapshot.write_snapshot(
            paths.root, snapshot.candidate_paths(paths.root), target, protocol.MAX_SOURCE_BYTES
        )
    document = protocol.request_document(
        request_id,
        command,
        args,
        taken.sha256,
        taken.size,
        __version__,
        snapshot.head_commit(paths.root),
    )
    pending = protocol.REQUEST_FILE + protocol.TMP_SUFFIX
    descriptor = os.open(pending, _CREATE_FLAGS, _FILE_MODE, dir_fd=home)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(protocol.encode(document))
    os.rename(pending, protocol.REQUEST_FILE, src_dir_fd=home, dst_dir_fd=home)


def submit(paths: SpoolPaths, command: str, args: list[str]) -> str:
    """Write the snapshot and the request. Returns the request ID.

    A failure, or an interrupt, removes the request directory this call created.
    """
    request_id = protocol.new_request_id()
    try:
        with directory(paths, *REQUESTS) as requests:
            os.mkdir(request_id, protocol.SPOOL_DIR_MODE, dir_fd=requests)
            try:
                home = os.open(request_id, _DIR_FLAGS, dir_fd=requests)
                try:
                    _write_request(paths, home, request_id, command, args)
                finally:
                    os.close(home)
            except BaseException:
                shutil.rmtree(request_id, ignore_errors=True, dir_fd=requests)
                raise
    except snapshot.SnapshotTooLarge as error:
        raise ClientError(Exit.NOT_RUN, str(error)) from None
    except snapshot.SnapshotError as error:
        raise ClientError(Exit.ERROR, str(error)) from None
    except OSError as error:
        raise ClientError(Exit.ERROR, f"cannot write the request: {_why(error)}") from None
    return request_id


def withdraw(paths: SpoolPaths, request_id: str) -> bool:
    """Take back an unclaimed request. False when there is nothing left to take back.

    The request file is renamed away first: the broker claims by renaming the
    same file, so exactly one side wins and a withdrawn request can never run.
    False means the rename found no `request.json`: the broker has claimed the
    request. Any other failure is an error (exit 5), not a claim.
    """
    try:
        with directory(paths, *REQUESTS) as requests:
            try:
                home = os.open(request_id, _DIR_FLAGS, dir_fd=requests)
            except FileNotFoundError:
                return False
            try:
                os.rename(
                    protocol.REQUEST_FILE,
                    protocol.WITHDRAWN_FILE,
                    src_dir_fd=home,
                    dst_dir_fd=home,
                )
            except FileNotFoundError:
                return False
            finally:
                os.close(home)
            shutil.rmtree(request_id, ignore_errors=True, dir_fd=requests)
            return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ClientError(
            Exit.ERROR, f"cannot withdraw request {request_id}: {_why(error)}"
        ) from None


def request_cancel(paths: SpoolPaths, request_id: str) -> None:
    """Create the empty cancel marker in the request directory."""
    try:
        with directory(paths, *REQUESTS, request_id) as home:
            os.close(os.open(protocol.CANCEL_FILE, _MARKER_FLAGS, _FILE_MODE, dir_fd=home))
    except FileNotFoundError:
        raise ClientError(Exit.ERROR, f"no request {request_id} in {paths.spool}") from None
    except OSError as error:
        raise ClientError(Exit.ERROR, f"cannot write the cancel marker: {_why(error)}") from None
