"""The broker's own output: events and warnings, one line each.

Three rules hold for every line. Text that may quote what an agent wrote is made
safe to print first (`protocol.printable`). A line an agent could cause again and
again is printed a bounded number of times. And printing never fails the code
that asked for the line: when the output is gone (a closed pipe), the broker
goes quiet and keeps serving, and its standard output is pointed at /dev/null
so the exit code stays the broker's own.
"""

import sys
import time
from collections import deque
from collections.abc import Callable

from tart_taste_test import exitcodes, protocol

# Lines about linked working trees quote paths an agent chose. Only this many
# different warnings about them are ever printed...
MAX_CHECKOUT_WARNINGS = 64
# ...and only this many "serving" / "no longer serving" lines in any one period.
MAX_CHECKOUT_EVENTS = 64
CHECKOUT_EVENT_PERIOD_SECONDS = 3600.0


def _print(line: str) -> None:
    # Flushed, so a reader of a pipe or a file sees each line when it is written
    # and a closed pipe is noticed here, not at interpreter exit.
    print(line, flush=True)


class Notices:
    def __init__(self, say: Callable[[str], None] | None = None) -> None:
        self._say = _print if say is None else say
        self._standard = say is None
        self._quiet = False
        self._once: set[str] = set()
        self._checkouts: set[str] = set()
        # When each recent line about a linked working tree being served was printed.
        self._linked_events: deque[float] = deque(maxlen=MAX_CHECKOUT_EVENTS)

    def event(self, message: str) -> None:
        """Print one line with the time. Never raises."""
        if self._quiet:
            return
        try:
            self._say(f"{time.strftime('%H:%M:%S')} {protocol.printable(message)}")
        except OSError, ValueError:
            self._quiet = True
            if self._standard:
                exitcodes.silence(sys.stdout)

    def event_checkout(self, linked: bool, message: str) -> None:
        """An event about a checkout starting or stopping to be served.

        An agent can make a linked working tree come and go at every scan, so
        such lines are bounded in number per period; one warning says so.
        """
        now = time.monotonic()
        recent = self._linked_events
        if not linked:
            self.event(message)
        elif len(recent) < MAX_CHECKOUT_EVENTS or now - recent[0] >= CHECKOUT_EVENT_PERIOD_SECONDS:
            recent.append(now)
            self.event(message)
        else:
            self.warn_once(
                f"linked working trees started or stopped being served more than "
                f"{MAX_CHECKOUT_EVENTS} times in {CHECKOUT_EVENT_PERIOD_SECONDS / 60:g} "
                "minutes; further such lines are left out",
                key="linked working tree events",
            )

    def warn(self, message: str) -> None:
        self.event(f"warning: {message}")

    def warn_once(self, message: str, key: str | None = None) -> None:
        """Print a warning unless one with the same key (default: the same text) was printed."""
        key = message if key is None else key
        if key not in self._once:
            self._once.add(key)
            self.warn(message)

    def warn_checkout(self, linked: bool, message: str) -> None:
        """Warn about a checkout. Warnings about linked working trees are bounded in number."""
        if not linked:
            self.warn_once(message)
        elif message not in self._checkouts:
            if len(self._checkouts) < MAX_CHECKOUT_WARNINGS:
                self._checkouts.add(message)
                self.warn(message)
            else:
                self.warn_once("further warnings about linked working trees are not shown")
