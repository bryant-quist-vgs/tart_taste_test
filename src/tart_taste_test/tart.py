"""The Tart adapter: the only code that builds a Tart command line.

Single owner of the Tart invocations, isolation flags, network flags, and VM
names specified in docs/vm-handling.md §1. Every command is an argument list; no
shell is involved on the host. Every call but `start` has a time limit and
calls `tick` while it waits, so the broker keeps its heartbeat during long
operations, and its Tart process does not outlive the call, however the call
ends. `start` returns a running `tart run`; `Engine.booted` is what ends it.
"""

import os
import re
import selectors
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO

from tart_taste_test import process
from tart_taste_test.repo_config import IMAGE_RE, OWN_VM_PREFIX

# The complete set of flags a VM is started with, besides its network flags:
# no window, no audio device, no clipboard sharing between the host and
# untrusted guest code, and no USB accessories. Nothing else is ever passed, so
# no host directory, disk, port, or device is attached.
RUN_FLAGS: tuple[str, ...] = (
    "--no-graphics",
    "--no-audio",
    "--no-clipboard",
    "--no-usb-accessories",
)

NETWORK_FLAGS: dict[str, tuple[str, ...]] = {
    "isolated": ("--net-softnet-block=0.0.0.0/0",),
    "internet": ("--net-softnet",),
    "shared": (),
}

# Every VM this project creates has this prefix; a repository config may never
# name a VM that has it (decision D17), which is why the constant has one owner.
PREFIX = OWN_VM_PREFIX
TART_QUICK_TIMEOUT = 120.0
TART_CLONE_TIMEOUT = 3600.0
STOP_GRACE_SECONDS = 20
EXEC_EXIT_WAIT = 10.0
_CAPTURE_LIMIT = 16384
_HEX12 = r"[0-9a-f]{12}"


class TartError(Exception):
    """A Tart command failed, timed out, or could not be started."""


@dataclass(frozen=True)
class Names:
    """VM names for one operator state directory (docs/vm-handling.md §1).

    Every name carries the state key, so two brokers with different state
    directories never touch each other's VMs.
    """

    state_key: str

    def _name(self, kind: str, *parts: str) -> str:
        return "-".join((PREFIX + self.state_key, kind, *parts))

    def environment(self, repo_key: str, env_key: str) -> str:
        return self._name("env", repo_key, env_key)

    def build(self, repo_key: str, env_key: str) -> str:
        return self._name("build", repo_key, env_key)

    def run(self, token: str) -> str:
        return self._name("run", token)

    def doctor(self, token: str) -> str:
        return self._name("doctor", token)

    def owns(self, name: str) -> bool:
        """Whether `name` is a VM of this state directory, of any kind."""
        return name.startswith(f"{PREFIX}{self.state_key}-")

    def _match(self, name: str, kind: str, parts: int) -> re.Match[str] | None:
        tail = "".join(f"-({_HEX12})" for _ in range(parts))
        return re.fullmatch(f"{re.escape(PREFIX + self.state_key)}-{kind}{tail}", name)

    def is_transient(self, name: str) -> bool:
        """Run VMs, doctor VMs, and unfinished builds: safe to delete whenever found."""
        return (
            self._match(name, "run", 1) is not None
            or self._match(name, "doctor", 1) is not None
            or self._match(name, "build", 2) is not None
        )

    def environment_repo(self, name: str) -> str | None:
        """The repository key of an environment image name, or None for other names."""
        match = self._match(name, "env", 2)
        return match.group(1) if match else None


def _vm(name: str) -> str:
    """Refuse anything that is not a plain VM name or image reference.

    The pattern cannot start with `-`, so a name can never be read as an option.
    """
    if IMAGE_RE.fullmatch(name) is None:
        raise TartError(f"refusing unexpected VM or image name {name!r}")
    return name


@dataclass(frozen=True)
class ExecResult:
    """How a guest command ended. `status` is None unless it ran to completion."""

    status: int | None
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    size: int = 0


class Tart:
    def __init__(
        self, executable: str = "tart", tick: Callable[[], None] = process.nothing
    ) -> None:
        self._tart = executable
        self.tick = tick

    # -- plumbing ----------------------------------------------------------

    def _run(self, args: list[str], timeout: float) -> tuple[int, str]:
        """Run one Tart command to completion. Returns (status, captured output)."""
        with tempfile.TemporaryFile() as capture:
            try:
                child = process.start([self._tart, *args], stdout=capture)
            except OSError as error:
                raise TartError(f"cannot run {self._tart}: {error.strerror}") from None
            try:
                status = process.wait(child, timeout, self.tick)
            except process.TimedOut:
                raise TartError(f"tart {args[0]} did not finish in {timeout:g}s") from None
            capture.seek(0)
            output = capture.read(_CAPTURE_LIMIT).decode("utf-8", "replace").strip()
        return status, output

    def _checked(self, args: list[str], timeout: float = TART_QUICK_TIMEOUT) -> str:
        status, output = self._run(args, timeout)
        if status != 0:
            raise TartError(f"tart {args[0]} failed (exit {status}): {output}")
        return output

    # -- VM lifecycle --------------------------------------------------------

    def version(self) -> str:
        return self._checked(["--version"])

    def list_local(self) -> list[str]:
        output = self._checked(["list", "--source", "local", "--quiet"])
        return [line.strip() for line in output.splitlines() if line.strip()]

    def clone(self, source: str, name: str) -> None:
        self._checked(["clone", _vm(source), _vm(name)], TART_CLONE_TIMEOUT)

    def set_resources(self, name: str, cpu: int | None, memory_mb: int | None) -> None:
        args = ["set", _vm(name)]
        if cpu is not None:
            args += ["--cpu", str(int(cpu))]
        if memory_mb is not None:
            args += ["--memory", str(int(memory_mb))]
        if len(args) > 2:
            self._checked(args)

    def start(self, name: str, network: str, log: Path) -> subprocess.Popen[bytes]:
        """Start a VM in the background. Its console output goes to `log`."""
        args = [self._tart, "run", *RUN_FLAGS, *NETWORK_FLAGS[network], _vm(name)]
        try:
            with log.open("wb") as sink:
                return process.start(args, stdout=sink)
        except OSError as error:
            raise TartError(f"cannot start {name}: {error.strerror}") from None

    def stop(self, name: str) -> None:
        self._checked(["stop", _vm(name), "--timeout", str(STOP_GRACE_SECONDS)])

    def rename(self, old: str, new: str) -> None:
        self._checked(["rename", _vm(old), _vm(new)])

    def delete(self, name: str) -> None:
        self._checked(["delete", _vm(name)])

    # -- guest commands ------------------------------------------------------

    def exec(
        self,
        name: str,
        argv: list[str],
        timeout: float,
        stdin: IO[bytes] | None = None,
        sink: BinaryIO | None = None,
        limit: int = _CAPTURE_LIMIT,
        cancelled: Callable[[], bool] = process.never,
    ) -> ExecResult:
        """Run `argv` in the guest, copying its combined output to `sink`.

        At most `limit` bytes are stored; the rest is read and discarded so the
        guest command is never blocked by a full pipe. `stdin`, when given, is
        handed to Tart as the command's standard input. `cancelled` and the time
        limit are checked about every `process.POLL_SECONDS`.
        """
        args = [self._tart, "exec"]
        if stdin is not None:
            args.append("-i")
        args += [_vm(name), *argv]
        try:
            child = process.start(
                args,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            )
        except OSError as error:
            raise TartError(f"cannot run {self._tart}: {error.strerror}") from None
        assert child.stdout is not None  # noqa: S101 - PIPE was requested above
        pipe = child.stdout
        deadline = time.monotonic() + timeout
        stored = 0
        truncated = timed_out = was_cancelled = False
        try:
            with selectors.DefaultSelector() as selector, pipe:
                selector.register(pipe, selectors.EVENT_READ)
                open_pipe = True
                while open_pipe:
                    if selector.select(process.POLL_SECONDS):
                        chunk = os.read(pipe.fileno(), 65536)
                        if not chunk:
                            open_pipe = False
                        elif sink is not None:
                            room = limit - stored
                            if len(chunk) > room:
                                truncated = True
                            if room > 0:
                                sink.write(chunk[:room])
                                stored += min(len(chunk), room)
                    self.tick()
                    if time.monotonic() >= deadline:
                        timed_out = True
                    elif cancelled():
                        was_cancelled = True
                    if timed_out or was_cancelled:
                        break
            if sink is not None:
                sink.flush()
            if timed_out or was_cancelled:
                process.stop(child)
                return ExecResult(None, timed_out, was_cancelled, truncated, stored)
            try:
                status = process.wait(child, EXEC_EXIT_WAIT, self.tick)
            except process.TimedOut:
                raise TartError(
                    "tart exec closed its output but did not exit; its status is unknown"
                ) from None
        except BaseException:
            # Whatever interrupted the wait (a failed write to the log, a request
            # to stop), the Tart process does not outlive this call.
            process.stop(child)
            raise
        return ExecResult(status, False, False, truncated, stored)
