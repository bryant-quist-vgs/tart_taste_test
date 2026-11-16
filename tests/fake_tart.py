"""A stand-in for the `tart` command, used only by the test suite.

It keeps "VMs" as directories under $FAKE_TART_HOME and runs "guest" commands as
ordinary host processes whose HOME is the VM's own directory. That is enough to
exercise the real guest scripts (sh, tar, xargs) end to end without starting a
virtual machine. Every invocation is appended to calls.log so tests can assert
the exact arguments the broker passed.

Behaviour switches are files in $FAKE_TART_HOME:
  fail-run        `run` exits 1 at once (a VM that cannot start)
  exit-always-0   `exec` reports 0 whatever the guest command returned
  exec-broken     `exec` exits 1 without running anything (no guest agent)
  die-on          content TEXT: the first `exec` whose guest arguments contain
                  TEXT kills the VM instead of running the command
  die-how         content `exit-1` (default), `exit-0`, or `hang`: what that
                  `exec` does after the VM died
  undeletable     `delete` fails and leaves the VM in place
  list-empty      `list` prints nothing (a listing the broker cannot use)
  fail-list       `list` exits 1
  fail-list-after-rename
                  `rename` succeeds and turns `fail-list` on
  slow-stop       content SECONDS: `stop` of an existing VM takes that long, so
                  clean-up spans several of the broker's wait-loop turns
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ["FAKE_TART_HOME"])
VMS = HOME / "vms"


def fail(message: str, status: int = 1) -> int:
    print(message, file=sys.stderr)
    return status


def vm(name: str) -> Path:
    return VMS / name


def main(argv: list[str]) -> int:
    VMS.mkdir(parents=True, exist_ok=True)
    with (HOME / "calls.log").open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    if argv == ["--version"]:
        print("2.41.1-fake")
        return 0
    command, rest = argv[0], argv[1:]

    if command == "list":
        if (HOME / "fail-list").exists():
            return fail("cannot list VMs (simulated)")
        if (HOME / "list-empty").exists():
            return 0
        for entry in sorted(VMS.iterdir()):
            print(entry.name)
        return 0

    if command == "clone":
        source, name = rest
        if vm(name).exists():
            return fail(f"VM {name} already exists")
        if vm(source).is_dir():
            shutil.copytree(vm(source), vm(name), symlinks=True)
            (vm(name) / "running").unlink(missing_ok=True)
        elif "missing" in source:
            return fail(f"image {source} not found")
        else:
            (vm(name) / "home").mkdir(parents=True)
            (vm(name) / "image").write_text(source, encoding="utf-8")
        return 0

    if command == "set":
        if not vm(rest[0]).is_dir():
            return fail("the specified VM does not exist")
        (vm(rest[0]) / "resources").write_text(" ".join(rest[1:]), encoding="utf-8")
        return 0

    if command == "run":
        name = rest[-1]
        if not vm(name).is_dir():
            return fail("the specified VM does not exist")
        if (HOME / "fail-run").exists():
            return fail("softnet is not installed (simulated)")
        marker = vm(name) / "running"
        marker.write_text(" ".join(rest[:-1]), encoding="utf-8")
        while marker.exists():
            time.sleep(0.02)
        return 0

    if command == "exec":
        interactive = rest[0] == "-i"
        name, guest = (rest[1], rest[2:]) if interactive else (rest[0], rest[1:])
        if (HOME / "exec-broken").exists() or not (vm(name) / "running").exists():
            return fail("the guest agent is not reachable")
        trigger = HOME / "die-on"
        if trigger.exists() and trigger.read_text(encoding="utf-8") in "\0".join(guest):
            trigger.unlink()
            (vm(name) / "running").unlink(missing_ok=True)
            how = HOME / "die-how"
            mode = how.read_text(encoding="utf-8") if how.exists() else "exit-1"
            while mode == "hang":
                time.sleep(0.05)
            return 0 if mode == "exit-0" else fail("the VM went away")
        home = vm(name) / "home"
        environment = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        done = subprocess.run(
            guest,
            cwd=home,
            env=environment,
            stdin=None if interactive else subprocess.DEVNULL,
            check=False,
        )
        return 0 if (HOME / "exit-always-0").exists() else done.returncode

    if command == "stop":
        if not vm(rest[0]).is_dir():
            return fail("the specified VM does not exist")
        slow = HOME / "slow-stop"
        if slow.exists():
            time.sleep(float(slow.read_text(encoding="utf-8")))
        (vm(rest[0]) / "running").unlink(missing_ok=True)
        return 0

    if command == "rename":
        old, new = rest
        if not vm(old).is_dir():
            return fail(f"failed to rename a non-existent local VM: {old}")
        vm(old).rename(vm(new))
        if (HOME / "fail-list-after-rename").exists():
            (HOME / "fail-list").touch()
        return 0

    if command == "delete":
        if not vm(rest[0]).is_dir():
            return fail("the specified VM does not exist")
        if (HOME / "undeletable").exists():
            return fail("the VM is in use (simulated)")
        shutil.rmtree(vm(rest[0]))
        return 0

    return fail(f"fake tart: unknown command {command}", 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
