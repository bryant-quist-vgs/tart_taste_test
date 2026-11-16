"""The broker-owned mirror of a repository's default branch.

Everything the broker learns about the default branch comes from a bare
repository in operator state (docs/decisions.md D5). Git is never run with an
agent-writable checkout as its repository: every Git process starts in
`GIT_WORKING_DIRECTORY`, never in the directory the operator happened to be in,
and without the `GIT_*` variables that could point it at another repository. In
`local` mode the checkout is only the source of a fetch.
"""

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from tart_taste_test import process, protocol
from tart_taste_test.registry import Repository, State, check_branch, check_url
from tart_taste_test.repo_config import CONFIG_PATH, MAX_CONFIG_BYTES, ConfigError

MIRROR_REF = "refs/taste-test/default"
REMOTE_PROTOCOLS = "https:ssh"
LOCAL_PROTOCOLS = "file"
GIT_FETCH_TIMEOUT = 300.0
GIT_QUICK_TIMEOUT = 60.0
GIT_ARCHIVE_TIMEOUT = 600.0
# Where every Git process starts. It is not inside any repository, so a command
# without `-C` (`ls-remote`, `init`) reads no repository's configuration. An
# agent can write its checkout's `.git/config`: a URL rewrite, proxy, or
# credential helper there must never apply to the broker's Git.
GIT_WORKING_DIRECTORY = "/"
# `GIT_*` variables of the operator's that are passed on to Git: how to reach
# and authenticate to the remote, and which user-level configuration to read.
# Every other `GIT_*` variable (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_COUNT`,
# ...) is removed.
GIT_VARIABLES_KEPT = frozenset(
    {
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    }
)
SSH_CHOICES = frozenset({"GIT_SSH", "GIT_SSH_COMMAND"})
BATCH_SSH = "ssh -o BatchMode=yes"
_STDERR_LIMIT = 4096


class MirrorError(Exception):
    """Git failed, timed out, or returned something unexpected."""


def git_environment(protocols: str) -> dict[str, str]:
    """The environment of every Git process the broker starts.

    Git may not prompt, may use only the transports in `protocols`, cannot be
    redirected to another repository by an inherited `GIT_*` variable, and SSH
    runs in batch mode (no passphrase or host-key prompt) unless the operator
    has already chosen an SSH program with `GIT_SSH` or `GIT_SSH_COMMAND`.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_") or name in GIT_VARIABLES_KEPT
    }
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL=protocols)
    # Git prefers GIT_SSH_COMMAND to GIT_SSH, so the default must not be added
    # beside an operator's GIT_SSH: it would silently replace their program.
    # An empty value is no choice: it is dropped, so Git cannot fall back to an
    # ssh that prompts.
    if not any(environment.get(name) for name in SSH_CHOICES):
        for name in SSH_CHOICES:
            environment.pop(name, None)
        environment["GIT_SSH_COMMAND"] = BATCH_SSH
    return environment


def run_git(
    args: list[str],
    timeout: float,
    tick: Callable[[], None] = process.nothing,
    stdout: Path | None = None,
    limit: int = 1 << 20,
    protocols: str = "",
) -> tuple[int, bytes, str]:
    """Run Git with an argument list and a time limit.

    Returns (status, standard output up to `limit` bytes, standard error text).
    With `stdout`, standard output is written to that file instead. `protocols`
    is the colon-separated list of transports Git may use; empty means none.
    Git starts in `GIT_WORKING_DIRECTORY` whatever the broker's own directory.
    """
    environment = git_environment(protocols)
    with tempfile.TemporaryFile() as captured, tempfile.TemporaryFile() as errors:
        target = stdout.open("wb") if stdout is not None else captured
        try:
            try:
                child = process.start(
                    ["git", *args],
                    stdout=target,
                    stderr=errors,
                    env=environment,
                    cwd=GIT_WORKING_DIRECTORY,
                )
            except OSError as error:
                raise MirrorError(f"cannot run git: {error.strerror}") from None
            try:
                status = process.wait(child, timeout, tick)
            except process.TimedOut:
                raise MirrorError(f"git {args[0]} did not finish in {timeout:g}s") from None
        finally:
            if stdout is not None:
                target.close()
        captured.seek(0)
        errors.seek(0)
        output = captured.read(limit + 1)
        message = errors.read(_STDERR_LIMIT).decode("utf-8", "replace").strip()
    return status, output, message


def remote_default_branch(url: str) -> str:
    """The branch a remote's HEAD points to."""
    status, output, message = run_git(
        ["ls-remote", "--symref", check_url(url), "HEAD"],
        GIT_QUICK_TIMEOUT,
        protocols=REMOTE_PROTOCOLS,
    )
    if status != 0:
        raise MirrorError(f"git ls-remote failed: {message}")
    for line in output.decode("utf-8", "replace").splitlines():
        match = re.fullmatch(r"ref: refs/heads/(\S+)\tHEAD", line)
        if match:
            return check_branch(match.group(1))
    raise MirrorError("the remote does not say which branch HEAD points to; pass --branch")


class Mirror:
    """One repository's mirror in operator state."""

    def __init__(
        self, state: State, repository: Repository, tick: Callable[[], None] = process.nothing
    ) -> None:
        self._repository = repository
        self._tick = tick
        self.path = state.mirrors / f"{repository.key}.git"

    def _git(
        self,
        args: list[str],
        timeout: float = GIT_QUICK_TIMEOUT,
        stdout: Path | None = None,
        protocols: str = "",
    ) -> tuple[int, bytes, str]:
        return run_git(
            ["-C", str(self.path), *args], timeout, self._tick, stdout, protocols=protocols
        )

    def refresh(self) -> str:
        """Fetch the default branch and return its commit ID.

        A failed fetch is an error even when an earlier fetch succeeded: the
        broker does not fall back to a commit that may since have been replaced.
        """
        if not (self.path / "HEAD").exists():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            status, _, message = run_git(
                ["init", "--quiet", "--bare", str(self.path)], GIT_QUICK_TIMEOUT, self._tick
            )
            if status != 0:
                raise MirrorError(f"cannot create the mirror: {message}")
        repository = self._repository
        refspec = f"+refs/heads/{check_branch(repository.branch)}:{MIRROR_REF}"
        remote = repository.source == "remote"
        status, _, message = self._git(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-recurse-submodules",
                "--force",
                repository.fetch_source(),
                refspec,
            ],
            GIT_FETCH_TIMEOUT,
            protocols=REMOTE_PROTOCOLS if remote else LOCAL_PROTOCOLS,
        )
        if status != 0:
            raise MirrorError(
                f"cannot fetch branch {repository.branch!r} from "
                f"{repository.fetch_source()}: {message}"
            )
        status, output, message = self._git(
            ["rev-parse", "--verify", "--quiet", MIRROR_REF + "^{commit}"]
        )
        commit = output.decode("ascii", "replace").strip()
        if status != 0 or protocol.COMMIT_ID_RE.fullmatch(commit) is None:
            raise MirrorError(f"the fetched branch does not resolve to a commit: {message}")
        return commit

    def read_config(self, commit: str) -> bytes | None:
        """The repository config at `commit`, or None when the file is absent.

        Raises `ConfigError` when the path is not a file (a directory, say) or
        the file is larger than the config size limit: what the default branch
        holds is the repository's doing, not an infrastructure failure.
        """
        blob = f"{self._commit(commit)}:{CONFIG_PATH}"
        status, output, _ = self._git(["cat-file", "-t", blob])
        if status != 0:
            return None
        kind = output.decode("ascii", "replace").strip()
        if kind != "blob":
            raise ConfigError(f"{CONFIG_PATH} is not a file")
        status, output, message = self._git(["cat-file", "-s", blob])
        if status != 0:
            raise MirrorError(f"cannot read the size of {CONFIG_PATH}: {message}")
        try:
            size = int(output.decode("ascii", "replace").strip())
        except ValueError:
            raise MirrorError("git cat-file -s returned an unexpected size") from None
        if size > MAX_CONFIG_BYTES:
            raise ConfigError(f"{CONFIG_PATH} is larger than {MAX_CONFIG_BYTES} bytes")
        status, output, message = self._git(["cat-file", "blob", blob])
        if status != 0:
            raise MirrorError(f"cannot read {CONFIG_PATH}: {message}")
        return output

    def write_manifest(self, commit: str, destination: Path) -> None:
        """Write the NUL-separated path of every file in `commit` to `destination`."""
        status, _, message = self._git(
            ["ls-tree", "-r", "-z", "--name-only", self._commit(commit)],
            GIT_QUICK_TIMEOUT,
            stdout=destination,
        )
        if status != 0:
            raise MirrorError(f"git ls-tree failed: {message}")

    def write_archive(self, commit: str, destination: Path) -> None:
        """Write a tar archive of the source tree at `commit` to `destination`."""
        status, _, message = self._git(
            ["archive", "--format=tar", self._commit(commit)],
            GIT_ARCHIVE_TIMEOUT,
            stdout=destination,
        )
        if status != 0:
            raise MirrorError(f"git archive failed: {message}")

    @staticmethod
    def _commit(commit: str) -> str:
        if protocol.COMMIT_ID_RE.fullmatch(commit) is None:
            raise MirrorError("not a commit ID")
        return commit
