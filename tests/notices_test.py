"""notices_test.py — the broker's own output.

Coverage:
  - Load-bearing: text that quotes an agent is made safe to print
  - a repeated warning is printed once; warnings about linked working trees are
    bounded in number
  - lines about linked working trees coming and going are bounded per period
  - a failure to print silences the output instead of reaching the caller
  - Load-bearing: a closed standard output does not change the exit code
"""

import subprocess
import sys
import time

import pytest

from tart_taste_test import notices
from tart_taste_test.notices import Notices


# Load-bearing: an agent-chosen path cannot drive the operator's terminal.
def test_control_characters_never_reach_the_output() -> None:
    lines: list[str] = []
    Notices(lines.append).warn("not serving /tmp/x\x1b]52;c;ZXZpbA==\x07\nfake line")
    [line] = lines
    assert "\x1b" not in line and "\x07" not in line and "\n" not in line
    assert "warning: not serving /tmp/x" in line


def test_a_warning_is_printed_once_per_key() -> None:
    lines: list[str] = []
    voice = Notices(lines.append)
    voice.warn_once("the registry cannot be read")
    voice.warn_once("the registry cannot be read")
    voice.warn_once("a scan failed: one text", key="scan")
    voice.warn_once("a scan failed: another text", key="scan")
    assert len(lines) == 2


def test_warnings_about_linked_working_trees_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notices, "MAX_CHECKOUT_WARNINGS", 2)
    lines: list[str] = []
    voice = Notices(lines.append)
    for index in range(10):
        voice.warn_checkout(True, f"not serving /agent/chosen/{index}")
        voice.warn_checkout(True, f"not serving /agent/chosen/{index}")
    assert len(lines) == 3 and "are not shown" in lines[-1]
    voice.warn_checkout(False, "not serving /registered/by/the/operator")
    assert len(lines) == 4  # a main working tree is the operator's and is always reported


@pytest.mark.parametrize("error", [BrokenPipeError("closed"), ValueError("I/O on closed file")])
def test_a_failure_to_print_silences_the_output(error: Exception) -> None:
    attempts: list[str] = []

    def broken(line: str) -> None:
        attempts.append(line)
        raise error

    voice = Notices(broken)
    voice.event("first")
    voice.warn("second")
    voice.warn_once("third")
    assert len(attempts) == 1  # it tried once, then went quiet


def test_lines_about_linked_working_trees_coming_and_going_are_bounded_per_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices, "MAX_CHECKOUT_EVENTS", 3)
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    lines: list[str] = []
    voice = Notices(lines.append)
    for index in range(10):
        voice.event_checkout(True, f"serving /agent/chosen/{index}")
    assert len(lines) == 4 and "further such lines are left out" in lines[-1]
    voice.event_checkout(False, "serving /registered/by/the/operator")
    assert len(lines) == 5  # a main working tree is always reported
    now[0] += notices.CHECKOUT_EVENT_PERIOD_SECONDS
    for index in range(10):
        voice.event_checkout(True, f"no longer serving /agent/chosen/{index}")
    assert len(lines) == 8  # three more in the new period; the warning is not repeated


# Load-bearing: 120 is what Python exits with when its last flush fails. The
# second case shows the check can fail: a `say` that is plain `print` leaves the
# stream as it is.
@pytest.mark.parametrize(("voice", "expected"), [("Notices()", 0), ("Notices(print)", 120)])
def test_closed_standard_output_does_not_change_the_exit_code(voice: str, expected: int) -> None:
    script = (
        "import sys\n"
        "from tart_taste_test.notices import Notices\n"
        f"voice = {voice}\n"
        "for index in range(50000):\n"  # far more than a pipe holds
        "    voice.event(f'line {index}')\n"
        "sys.exit(0)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert process.stdout is not None
    process.stdout.read(10)
    process.stdout.close()
    assert process.wait(timeout=60) == expected
