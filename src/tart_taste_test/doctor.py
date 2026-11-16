"""`taste-test-broker doctor`: check the installation and, optionally, a real VM.

The live checks are the evidence for the open questions in
docs/open-questions.md: they exercise, against a real image, every guest
behaviour the broker depends on and the network restrictions it promises.
"""

import contextlib
import os
import secrets
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from tart_taste_test import registry, tart
from tart_taste_test.engine import INTERNAL_SHELL, STEP_TIMEOUT, Engine, EngineError

DOCTOR_BOOT_TIMEOUT = 300
NETWORK_TIMEOUT = 30.0
STDIN_TOKEN = b"taste-test-doctor\n"

SCRIPT_STDIN = '[ "$(cat)" = "taste-test-doctor" ]'
# Run as: /bin/sh -c SCRIPT taste-test -t "a b" --help ""
SCRIPT_ARGV = (
    '[ "$#" -eq 4 ] && [ "$1" = "-t" ] && [ "$2" = "a b" ] && [ "$3" = "--help" ] && [ -z "$4" ]'
)
ARGV_SAMPLE = ["taste-test", "-t", "a b", "--help", ""]

NO_ANSWER = 1
NO_CURL = 90
NO_GATEWAY = 91
# Network probes. Each exits 1 when nothing answered (curl could not connect, 7,
# or ran out of time, 28), 0 when anything else happened (so an answer of any
# kind counts as reached), and 90 when the image has no curl and nothing was
# learnt. The same rule is used for both address families.
_NEED_CURL = "command -v curl >/dev/null 2>&1 || exit 90; "
_CURL = "curl -s -o /dev/null --connect-timeout 5 --max-time 8"
_VERDICT = 'if [ "$rc" -eq 7 ] || [ "$rc" -eq 28 ]; then exit 1; fi; exit 0'
SCRIPT_PUBLIC_V4 = f"{_NEED_CURL}{_CURL} http://1.1.1.1/; rc=$?; {_VERDICT}"
SCRIPT_PUBLIC_V6 = f"{_NEED_CURL}{_CURL} -6 'http://[2606:4700:4700::1111]/'; rc=$?; {_VERDICT}"
# Exit 0: the default gateway (the host side of the VM bridge) answered, even if
# only to refuse the connection. Exit 1: the attempt timed out. Exit 91: no
# default gateway could be determined.
SCRIPT_GATEWAY = (
    f"{_NEED_CURL}"
    "gw=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}'); "
    '[ -n "$gw" ] || gw=$(route -n get default 2>/dev/null | '
    "awk '/gateway:/ {print $2; exit}'); "
    f'[ -n "$gw" ] || exit {NO_GATEWAY}; '
    f'{_CURL} "http://$gw:9/"; rc=$?; '
    'if [ "$rc" -eq 28 ]; then exit 1; fi; exit 0'
)
TART_HOME_ENV = "TART_HOME"


class Checks:
    """Collects PASS / WARN / FAIL lines."""

    def __init__(self, say: Callable[[str], None]) -> None:
        self._say = say
        self.failed = False

    def ok(self, message: str) -> None:
        self._say(f"PASS {message}")

    def warn(self, message: str) -> None:
        self._say(f"WARN {message}")

    def fail(self, message: str) -> None:
        self.failed = True
        self._say(f"FAIL {message}")

    def check(self, passed: bool, message: str, failure: str = "") -> None:
        if passed:
            self.ok(message)
        else:
            self.fail(f"{message}: {failure}" if failure else message)


def check_installation(state: registry.State, vms: tart.Tart, report: Checks) -> None:
    """Checks that start no VM."""
    if shutil.which("tart") is None:
        report.fail("tart is on PATH: not found; install Tart first")
    else:
        try:
            report.ok(f"tart is on PATH (version {vms.version()})")
        except tart.TartError as error:
            report.fail(f"tart runs: {error}")
    if shutil.which("softnet") is None:
        report.warn(
            "softnet is not on PATH: the `isolated` and `internet` network modes need "
            "Softnet, and VMs asked for them will fail to start"
        )
    else:
        report.ok("softnet is on PATH")
    report.check(shutil.which("git") is not None, "git is on PATH", "not found")
    _vm_store(report)
    try:
        state.ensure()
        report.check(
            state.is_private(),
            f"operator state {state.root} is private to this user",
            "its mode allows other users; run chmod 700 on it",
        )
        count = len(registry.load(state))
        report.ok(f"registry is readable ({count} registered)")
    except (OSError, registry.RegistryError) as error:
        report.fail(f"operator state is usable: {error}")


def _vm_store(report: Checks) -> None:
    """Say where Tart keeps VMs and check what can be checked about who may write there.

    Environment images are reused by name, so anything that can write the VM
    store can replace one. The broker cannot see sandbox rules; it reports the
    directory so the operator can keep it out of every agent's reach.
    """
    store = Path(os.environ.get(TART_HOME_ENV) or Path.home() / ".tart")
    try:
        status = store.stat()
    except OSError:
        report.ok(f"the Tart VM store {store} does not exist yet; keep it out of agents' reach")
        return
    private = status.st_uid == os.getuid() and not status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    report.check(
        private,
        f"the Tart VM store {store} is writable only by this user (agent sandboxes must "
        "not be able to write it either; that cannot be checked from here)",
        "other users can write it, so cached environment images cannot be trusted",
    )


def _ended(status: int | None) -> str:
    """How a probe that gave no verdict ended, for a message."""
    if status is None:
        return "the probe did not finish in time"
    return f"the probe ended with status {status}"


def _isolated(report: Checks, family: str, status: int | None, note: str = "") -> None:
    """One isolation verdict. Only a probe that ran and got no answer is a pass."""
    claim = f"isolated: a public {family} address is not reachable"
    if status == NO_ANSWER:
        report.ok(claim + note)
    elif status == 0:
        report.fail(f"{claim}: the VM reached one; the isolated mode does not cover {family}")
    else:
        report.fail(f"{claim}: could not be checked ({_ended(status)})")


def _network(vms: tart.Tart, name: str, network: str, report: Checks) -> None:
    """Report what the guest can actually reach under `network`."""

    def run(script: str) -> int | None:
        return vms.exec(name, [*INTERNAL_SHELL, script], NETWORK_TIMEOUT).status

    public = run(SCRIPT_PUBLIC_V4)
    if public == NO_CURL:
        if network == "isolated":
            # A pass here is what tells the operator that isolation works, so a
            # check that cannot run is a failure, not a note.
            report.fail(
                "isolated: network reach could not be checked because the image has no "
                "curl; check with an image that has curl before relying on isolation"
            )
        else:
            report.warn("network reach was not checked: the image has no curl")
        return
    if network == "isolated":
        _isolated(report, "IPv4", public)
        _isolated(
            report,
            "IPv6",
            run(SCRIPT_PUBLIC_V6),
            " (this is also what a host without an IPv6 route shows, so on such a host it "
            "says nothing about isolation)",
        )
    elif public == 0:
        report.ok(f"{network}: a public IPv4 address is reachable")
    elif public == NO_ANSWER:
        report.warn(f"{network}: a public IPv4 address is not reachable (is the host online?)")
    else:
        report.warn(f"{network}: public reach could not be checked ({_ended(public)})")
    gateway = run(SCRIPT_GATEWAY)
    if gateway == 0:
        report.warn(
            f"{network}: the host side of the VM bridge answers the VM. Services on this "
            "Mac that listen on all interfaces are reachable by code in the VM"
        )
    elif gateway == NO_ANSWER:
        report.ok(f"{network}: the host side of the VM bridge does not answer the VM")
    elif gateway == NO_GATEWAY:
        report.warn(f"{network}: the VM's default gateway could not be determined")
    else:
        report.warn(
            f"{network}: whether the host side of the VM bridge answers could not be "
            f"checked ({_ended(gateway)})"
        )


def check_image(
    state: registry.State,
    vms: tart.Tart,
    report: Checks,
    image: str,
    network: str,
    engine: Engine | None = None,
) -> None:
    """Boot `image` under `network` and verify what the broker depends on."""
    engine = engine if engine is not None else Engine(vms, state, warn=report.warn)
    name = engine.names.doctor(secrets.token_hex(6))
    token_file = state.staging / f"{name}.stdin"
    try:
        state.ensure()
        token_file.write_bytes(STDIN_TOKEN)
        vms.clone(image, name)
        report.ok(f"image {image} can be cloned")
        report.check(
            name in vms.list_local(),
            "a cloned VM appears in `tart list --source local --quiet`",
            "it does not, so the broker could not find or clean up its own VMs",
        )
        with engine.booted(name, network, DOCTOR_BOOT_TIMEOUT):
            report.ok(f"guest agent answers with network mode {network}")
            report.ok("exit status is reported (guest `exit 7` seen as 7)")
            report.ok("HOME is set and tar and xargs are present")
            with token_file.open("rb") as handle:
                piped = vms.exec(name, [*INTERNAL_SHELL, SCRIPT_STDIN], STEP_TIMEOUT, handle)
            report.check(piped.status == 0, "standard input reaches the guest", "it did not")
            argv = vms.exec(name, [*INTERNAL_SHELL, SCRIPT_ARGV, *ARGV_SAMPLE], STEP_TIMEOUT)
            report.check(
                argv.status == 0,
                "guest arguments arrive intact (option-like, spaced, and empty values)",
                "Tart or the guest agent altered them; do not use allow_args",
            )
            _network(vms, name, network, report)
    except (EngineError, tart.TartError) as error:
        report.fail(f"image {image} with network mode {network}: {error}")
    finally:
        with contextlib.suppress(OSError):
            token_file.unlink()
        engine.discard(name)


def run(
    state: registry.State,
    vms: tart.Tart,
    say: Callable[[str], None],
    image: str | None,
    network: str,
    engine: Engine | None = None,
) -> bool:
    """Run the checks. True when none failed."""
    report = Checks(say)
    check_installation(state, vms, report)
    if image is not None and not report.failed:
        check_image(state, vms, report, image, network, engine)
    return not report.failed


def staging_leftovers(state: registry.State) -> list[Path]:
    """Files left in staging (for `prune`)."""
    try:
        return sorted(state.staging.iterdir())
    except OSError:
        return []
