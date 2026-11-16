"""Print a stored log, whole or limited to its first and last lines.

`--head` and `--tail` are implemented here so that callers never pipe the
client through `head` or `tail` and lose its exit code (docs/decisions.md D9).
Logs are untrusted bytes: they are copied, counted by newline, and never decoded
or interpreted. At most the stored-log limit (docs/protocol.md §2) is read from
any log, which also bounds the memory `--tail` can use.
"""

from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import BinaryIO

from tart_taste_test import protocol

_CHUNK = 65536


def _lines(source: BinaryIO, limit: int) -> Iterator[bytes]:
    """The lines of `source`, stopping once `limit` bytes have been read."""
    remaining = limit
    while remaining > 0:
        line = source.readline(remaining)
        if not line:
            return
        remaining -= len(line)
        yield line


def omitted_marker(count: int) -> bytes:
    """The single line printed in place of `count` lines that were left out."""
    noun = "line" if count == 1 else "lines"
    return f"[taste-test: {count} {noun} omitted]\n".encode("ascii")


@dataclass(frozen=True)
class Shown:
    """What a rendering printed, for the `lines=` and `shown=` summary fields."""

    lines: int
    omitted: int
    head: int | None
    tail: int | None

    def describe(self) -> str:
        if self.omitted == 0:
            return "all"
        parts = []
        if self.head is not None:
            parts.append(f"first-{self.head}")
        if self.tail is not None:
            parts.append(f"last-{self.tail}")
        return "+".join(parts)


def render_limited(
    source: BinaryIO,
    out: BinaryIO,
    head: int | None,
    tail: int | None,
    limit: int = protocol.MAX_LOG_BYTES,
) -> Shown:
    """Copy the first `head` and last `tail` lines of `source` to `out`.

    A limit that is `None` was not given. With neither limit the whole log is
    printed; with one, the other end contributes no lines. When the two ranges
    cover the whole log, every line is printed exactly once and no marker
    appears. A final line without a newline counts as a line. Bytes of `source`
    beyond `limit` are not read.
    """
    if head is None and tail is None:
        total = 0
        for line in _lines(source, limit):
            total += 1
            out.write(line)
        out.flush()
        return Shown(total, 0, None, None)
    head_count = head or 0
    tail_count = tail or 0
    kept: deque[bytes] = deque(maxlen=tail_count) if tail_count else deque(maxlen=0)
    total = 0
    for line in _lines(source, limit):
        total += 1
        if total <= head_count:
            out.write(line)
        else:
            kept.append(line)
    omitted = total - min(total, head_count) - len(kept)
    if omitted > 0:
        out.write(omitted_marker(omitted))
    for line in kept:
        out.write(line)
    out.flush()
    return Shown(total, omitted, head, tail)


class Follower:
    """Copy a growing log to `out` as it grows, counting its lines.

    `opener` opens the log afresh on every call and raises `FileNotFoundError`
    while there is none. Any other `OSError` it raises (the log is not a regular
    file, for example) is the caller's to handle.
    """

    def __init__(
        self,
        opener: Callable[[], BinaryIO],
        out: BinaryIO,
        limit: int = protocol.MAX_LOG_BYTES,
    ) -> None:
        self._opener = opener
        self._out = out
        self._limit = limit
        self._offset = 0
        self._newlines = 0
        self._last_byte = b"\n"

    def pump(self) -> None:
        """Copy any bytes appended since the previous call. A missing log is empty."""
        try:
            handle = self._opener()
        except FileNotFoundError:
            return
        with handle:
            handle.seek(self._offset)
            while self._offset < self._limit:
                chunk = handle.read(min(_CHUNK, self._limit - self._offset))
                if not chunk:
                    break
                self._out.write(chunk)
                self._offset += len(chunk)
                self._newlines += chunk.count(b"\n")
                self._last_byte = chunk[-1:]
        self._out.flush()

    def shown(self) -> Shown:
        unterminated = 0 if self._last_byte == b"\n" else 1
        return Shown(self._newlines + unterminated, 0, None, None)
