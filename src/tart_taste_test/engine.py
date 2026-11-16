"""Build environment images and run requests inside VMs.

Implements the guest layout and steps of docs/vm-handling.md §2. Every value used
here has already been validated: the image, shell, and command strings come from
the default-branch repository config, and request arguments reach the guest only
as separate positional parameters of the guest shell, never as part of a script.
"""

import contextlib
import hashlib
import io
import os
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from tart_taste_test import process, protocol, tart
from tart_taste_test.mirror import Mirror, MirrorError
from tart_taste_test.registry import State
from tart_taste_test.repo_config import Command, RepoConfig

ENV_FORMAT = 1

INTERNAL_SHELL = ["/bin/sh", "-c"]
STEP_TIMEOUT = 60.0
TRANSFER_TIMEOUT = 600.0
READY_ATTEMPT_TIMEOUT = 15.0
READY_INTERVAL = 2.0
RUN_EXIT_WAIT = 30.0
DELETE_RETRY_PAUSE = 2.0
PROBE_STATUS = 7

# Fixed guest scripts (docs/vm-handling.md §2). They contain no value from a
# request or a repository.
SCRIPT_READY = "exit 0"
SCRIPT_PROBE = f"exit {PROBE_STATUS}"
SCRIPT_REQUIRE = (
    '[ -n "${HOME:-}" ] || exit 11; '
    "command -v tar >/dev/null 2>&1 || exit 12; "
    "command -v xargs >/dev/null 2>&1 || exit 13; "
    "exit 0"
)
REQUIRE_FAILURES = {
    11: "HOME is not set for commands run through the guest agent",
    12: "the image has no tar",
    13: "the image has no xargs",
}
SCRIPT_UNPACK_BASE = (
    'set -eu; root="$HOME/taste-test"; rm -rf "$root"; mkdir -p "$root/src"; '
    'tar -x -o -f - -C "$root/src"'
)
SCRIPT_SAVE_MANIFEST = 'set -eu; cat > "$HOME/taste-test/base-manifest"'
SCRIPT_OVERLAY = (
    'set -eu; root="$HOME/taste-test"; cd "$root/src"; '
    'xargs -0 rm -rf -- < "$root/base-manifest"; tar -x -o -f - -C "$root/src"'
)
SCRIPT_SYNC = "sync"
# Prefix of every setup and named command: one output stream, in guest order,
# starting in the source tree.
COMMAND_PREFIX = 'exec 2>&1; cd "$HOME/taste-test/src" && '
# `$0` of the command's shell. Without it the first request argument would
# become `$0` and be lost; with it the arguments are "$1", "$2", ...
ARGV0 = "taste-test"


class EngineError(Exception):
    """Infrastructure failed: outcome `error`."""


class SetupFailed(Exception):
    """A setup command failed or the setup budget ran out: outcome `setup-failed`."""


class Cancelled(Exception):
    """A cancel marker was seen: outcome `cancelled`."""


@dataclass(frozen=True)
class RunOutcome:
    outcome: str
    command_exit: int | None
    reason: str
    output_bytes: int
    truncated: bool


def environment_key(commit: str, setup_network: str) -> str:
    """First 12 hex digits identifying an environment image (docs/vm-handling.md §1).

    SHA-256 over the format number, the commit ID, and the setup network mode,
    as UTF-8 text joined by single NUL bytes.
    """
    material = "\0".join((str(ENV_FORMAT), commit, setup_network)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:12]


def command_argv(shell: tuple[str, ...], run: str, args: tuple[str, ...]) -> list[str]:
    """The guest argument vector for a command string.

    `shell... SCRIPT taste-test ARG...`: the script is the fixed prefix plus the
    config's command string, and nothing else. The arguments are the shell's
    positional parameters; a command that accepts them says where they go with
    `"$@"` (docs/interfaces.md §4).
    """
    return [*shell, COMMAND_PREFIX + run, ARGV0, *args]


def _ignore(_message: str) -> None:
    return None


class Engine:
    def __init__(
        self,
        vms: tart.Tart,
        state: State,
        tick: Callable[[], None] = process.nothing,
        warn: Callable[[str], None] = _ignore,
        ready_interval: float = READY_INTERVAL,
        delete_retry_pause: float = DELETE_RETRY_PAUSE,
        shield: Callable[[], AbstractContextManager[None]] = contextlib.nullcontext,
    ) -> None:
        """`shield` wraps clean-up: while it is entered, `tick` must not raise.

        The broker uses `tick` to abandon a request (it was told to stop, or the
        request's spool went away). Stopping and deleting a VM must still run to
        the end, so those sections are entered through `shield`.
        """
        self._vms = vms
        self._state = state
        self._tick = tick
        self._warn = warn
        self._shield = shield
        self._ready_interval = ready_interval
        self._delete_retry_pause = delete_retry_pause
        self.names = tart.Names(state.key)

    # -- helpers -------------------------------------------------------------

    def _pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self._tick()
            time.sleep(min(0.2, left))

    def discard(self, name: str) -> bool:
        """Stop and delete a VM of this state directory. False (with a warning) if it survives.

        `tart stop` and `tart delete` are issued whether or not the VM is listed,
        so a listing that cannot be read never leaves a VM behind; the listing
        only confirms the result. If the VM is still listed, the two are issued
        once more after a short pause. A VM that survives both is reported and
        left for `prune` or the next start-up cleanup.
        """
        if not self.names.owns(name):
            raise EngineError(f"refusing to delete {name!r}: not a VM of this state directory")
        with self._shield():
            for attempt in (1, 2):
                for action in (self._vms.stop, self._vms.delete):
                    with contextlib.suppress(tart.TartError):
                        action(name)
                try:
                    gone = name not in self._vms.list_local()
                except tart.TartError as error:
                    self._warn(f"cannot list VMs after deleting {name}: {error}")
                    return False
                if gone:
                    with contextlib.suppress(OSError):
                        (self._state.vm_logs / f"{name}.log").unlink()
                    return True
                if attempt == 1:
                    self._pause(self._delete_retry_pause)
            self._warn(f"could not delete VM {name}; run `taste-test-broker prune`")
            return False

    def step(self, name: str, script: str, what: str, stdin: Path | None = None) -> None:
        """Run one fixed guest script; any failure is an infrastructure error."""
        capture = io.BytesIO()
        timeout = TRANSFER_TIMEOUT if stdin is not None else STEP_TIMEOUT
        with contextlib.ExitStack() as stack:
            handle = stack.enter_context(stdin.open("rb")) if stdin is not None else None
            done = self._vms.exec(name, [*INTERNAL_SHELL, script], timeout, handle, capture, 4096)
        if done.status != 0:
            detail = protocol.printable(capture.getvalue().decode("utf-8", "replace"))
            how = "timed out" if done.timed_out else f"exited {done.status}"
            raise EngineError(f"guest step failed ({what} {how}): {detail}")

    def probe(self, name: str) -> bool:
        """True when Tart reports the guest's exit status 7 as 7."""
        done = self._vms.exec(name, [*INTERNAL_SHELL, SCRIPT_PROBE], STEP_TIMEOUT)
        return done.status == PROBE_STATUS

    def _require(self, name: str) -> None:
        done = self._vms.exec(name, [*INTERNAL_SHELL, SCRIPT_REQUIRE], STEP_TIMEOUT)
        if done.status != 0:
            why = REQUIRE_FAILURES.get(done.status or 0, f"requirement check exited {done.status}")
            raise EngineError(f"the image cannot be used: {why}")

    @contextlib.contextmanager
    def booted(self, name: str, network: str, boot_timeout: int) -> Iterator[None]:
        """Start `name`, wait for its guest agent, and prove exit statuses are visible.

        The VM is stopped, and its `tart run` process ended, when the block
        exits, whatever happened.
        """
        log = self._state.vm_logs / f"{name}.log"
        running = self._vms.start(name, network, log)
        try:
            deadline = time.monotonic() + boot_timeout
            while True:
                if running.poll() is not None:
                    raise EngineError(
                        f"the VM exited while starting with network mode {network!r} "
                        f"(tart run exit {running.returncode}): {_tail(log)}"
                    )
                ready = self._vms.exec(name, [*INTERNAL_SHELL, SCRIPT_READY], READY_ATTEMPT_TIMEOUT)
                if ready.status == 0:
                    break
                if time.monotonic() >= deadline:
                    raise EngineError(
                        f"the guest agent did not answer within {boot_timeout}s; the image "
                        "must contain the Tart Guest Agent"
                    )
                self._pause(self._ready_interval)
            if not self.probe(name):
                raise EngineError(
                    f"exit-status probe failed: Tart did not report guest exit status "
                    f"{PROBE_STATUS}. No verdict can be trusted with this Tart version"
                )
            self._require(name)
            yield
        finally:
            with self._shield():
                with contextlib.suppress(tart.TartError):
                    self._vms.stop(name)
                # `wait` keeps calling `tick` and kills the process if it is
                # still there when the time is up.
                with contextlib.suppress(process.TimedOut):
                    process.wait(running, RUN_EXIT_WAIT, self._tick)

    def _still_observable(self, name: str, what: str) -> None:
        """Raise unless the VM still answers and still reports exit statuses.

        Whatever a guest command appears to have done (exited 0, exited
        non-zero, run out of time), that is the guest's doing only if the
        channel the broker observed it through is still intact. A VM that died,
        or a guest agent that went away, could produce any of the three.
        """
        try:
            intact = self.probe(name)
        except tart.TartError:
            intact = False
        if not intact:
            raise EngineError(
                f"the VM or its guest agent stopped responding during the command, so "
                f"{what} cannot be trusted; no verdict"
            )

    def _trusted_status(self, name: str, status: int | None) -> int:
        """Return a guest command's exit status once it is known to be the guest's.

        The exit-status probe is repeated after every command, whatever status
        Tart reported (decision D11).
        """
        if status is None or status < 0:
            raise EngineError(f"the guest command ended abnormally (status {status})")
        self._still_observable(name, f"its exit status ({status})")
        return status

    # -- environment images ----------------------------------------------------

    def cleanup_transient(self) -> list[str]:
        """Delete leftover run VMs, doctor VMs, and unfinished builds of this state.

        Returns the names that were deleted; `discard` warns about any that were not.
        """
        found = [name for name in self._vms.list_local() if self.names.is_transient(name)]
        return [name for name in found if self.discard(name)]

    def environments(self) -> list[str]:
        """Names of this state's environment images."""
        return [n for n in self._vms.list_local() if self.names.environment_repo(n) is not None]

    def environment_exists(self, name: str) -> bool:
        return name in self._vms.list_local()

    def build_environment(
        self,
        repo_key: str,
        commit: str,
        config: RepoConfig,
        mirror: Mirror,
        setup_log: BinaryIO,
        cancelled: Callable[[], bool] = process.never,
    ) -> str:
        """Build the environment image for `commit` and return its VM name."""
        key = environment_key(commit, config.setup_network)
        final = self.names.environment(repo_key, key)
        build = self.names.build(repo_key, key)
        archive = self._state.staging / f"{build}.base.tar"
        manifest = self._state.staging / f"{build}.manifest"
        self.discard(build)
        try:
            mirror.write_archive(commit, archive)
            mirror.write_manifest(commit, manifest)
            if cancelled():
                raise Cancelled()
            self._vms.clone(config.image, build)
            self._vms.set_resources(build, config.cpu, config.memory_mb)
            with self.booted(build, config.setup_network, config.boot_timeout_seconds):
                self.step(build, SCRIPT_UNPACK_BASE, "unpacking the default branch", archive)
                self.step(build, SCRIPT_SAVE_MANIFEST, "saving the base manifest", manifest)
                self._run_setup(build, config, setup_log, cancelled)
                self.step(build, SCRIPT_SYNC, "flushing disks")
            self._vms.rename(build, final)
            # The console log was named after the build VM, which no longer exists.
            with contextlib.suppress(OSError):
                (self._state.vm_logs / f"{build}.log").unlink()
        except (MirrorError, tart.TartError) as error:
            self.discard(build)
            raise EngineError(str(error)) from None
        except BaseException:
            self.discard(build)
            raise
        finally:
            for leftover in (archive, manifest):
                with contextlib.suppress(OSError):
                    leftover.unlink()
        # The image is built. Failing to delete older ones is not a reason to
        # fail the request that built it.
        try:
            older = [
                name
                for name in self._vms.list_local()
                if self.names.environment_repo(name) == repo_key and name != final
            ]
        except tart.TartError as error:
            self._warn(f"older environment images were not deleted: {error}")
            older = []
        for name in older:
            self.discard(name)
        return final

    def _run_setup(
        self, build: str, config: RepoConfig, log: BinaryIO, cancelled: Callable[[], bool]
    ) -> None:
        deadline = time.monotonic() + config.setup_timeout_seconds
        room = protocol.MAX_LOG_BYTES
        for index, command in enumerate(config.setup_run, start=1):
            remaining = deadline - time.monotonic()
            if cancelled():
                raise Cancelled()
            if remaining <= 0:
                raise SetupFailed(
                    f"the setup budget of {config.setup_timeout_seconds}s ran out before "
                    f"setup command {index}"
                )
            done = self._vms.exec(
                build,
                command_argv(config.shell, command, ()),
                remaining,
                None,
                log,
                room,
                cancelled,
            )
            room -= done.size
            if done.cancelled:
                raise Cancelled()
            if done.timed_out:
                self._still_observable(build, "the timeout")
                raise SetupFailed(
                    f"setup command {index} exceeded the setup budget of "
                    f"{config.setup_timeout_seconds}s"
                )
            status = self._trusted_status(build, done.status)
            if status != 0:
                raise SetupFailed(f"setup command {index} exited {status}: {command}")

    # -- running a request ------------------------------------------------------

    def run_request(
        self,
        token: str,
        environment: str,
        config: RepoConfig,
        command: Command,
        args: tuple[str, ...],
        snapshot: Path,
        output: BinaryIO,
        on_running: Callable[[], None] = process.nothing,
        cancelled: Callable[[], bool] = process.never,
    ) -> RunOutcome:
        """Run one named command against the candidate in a fresh run VM."""
        name = self.names.run(token)
        self.discard(name)
        try:
            self._vms.clone(environment, name)
            with self.booted(name, config.run_network, config.boot_timeout_seconds):
                if cancelled():
                    raise Cancelled()
                self.step(name, SCRIPT_OVERLAY, "unpacking the candidate", snapshot)
                on_running()
                done = self._vms.exec(
                    name,
                    command_argv(config.shell, command.run, args),
                    command.timeout_seconds,
                    None,
                    output,
                    protocol.MAX_LOG_BYTES,
                    cancelled,
                )
                if done.cancelled:
                    raise Cancelled()
                if done.timed_out:
                    # `timed-out` is a verdict: the command ran and did not finish.
                    self._still_observable(name, "the timeout")
                    reason = f"the command exceeded its time limit of {command.timeout_seconds}s"
                    return RunOutcome("timed-out", None, reason, done.size, done.truncated)
                status = self._trusted_status(name, done.status)
        except tart.TartError as error:
            raise EngineError(str(error)) from None
        finally:
            self.discard(name)
        outcome = "passed" if status == 0 else "failed"
        return RunOutcome(outcome, status, "", done.size, done.truncated)


def _tail(path: Path, limit: int = 600) -> str:
    """The end of a VM console log, as one printable line, for an error message."""
    try:
        with path.open("rb") as log:
            log.seek(max(0, log.seek(0, os.SEEK_END) - limit))
            data = log.read(limit)
    except OSError:
        return "(no console output)"
    return protocol.printable(data.decode("utf-8", "replace")) or "(no console output)"
