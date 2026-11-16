"""render_test.py — printing a stored log whole or by its first and last lines.

Coverage:
  - --head, --tail, and both: which lines are printed and where the marker goes
  - ranges that cover the whole log print every line once, with no marker
  - a final line without a newline counts as a line
  - binary output is copied unchanged
  - Follower: streams a growing log and counts its lines
  - bytes beyond the read limit are not read (also in client_hostile_test.py)
"""

import io
from pathlib import Path

import pytest

from tart_taste_test.render import Follower, Shown, omitted_marker, render_limited


def lines(count: int) -> bytes:
    return b"".join(f"line {n}\n".encode() for n in range(1, count + 1))


def render(data: bytes, head: int | None, tail: int | None) -> tuple[bytes, Shown]:
    out = io.BytesIO()
    shown = render_limited(io.BytesIO(data), out, head, tail)
    return out.getvalue(), shown


def test_head_only_prints_the_first_lines_then_the_marker() -> None:
    printed, shown = render(lines(10), 2, None)
    assert printed == b"line 1\nline 2\n[taste-test: 8 lines omitted]\n"
    assert (shown.lines, shown.omitted, shown.describe()) == (10, 8, "first-2")


def test_tail_only_prints_the_marker_then_the_last_lines() -> None:
    printed, shown = render(lines(10), None, 3)
    assert printed == b"[taste-test: 7 lines omitted]\nline 8\nline 9\nline 10\n"
    assert shown.describe() == "last-3"


def test_head_and_tail_print_both_ends_around_the_marker() -> None:
    printed, shown = render(lines(10), 2, 2)
    assert printed == (b"line 1\nline 2\n[taste-test: 6 lines omitted]\nline 9\nline 10\n")
    assert shown.describe() == "first-2+last-2"


@pytest.mark.parametrize(("head", "tail"), [(5, 5), (10, None), (None, 10), (7, 9), (100, 100)])
def test_ranges_covering_the_log_print_each_line_once(head: int | None, tail: int | None) -> None:
    printed, shown = render(lines(10), head, tail)
    assert printed == lines(10)
    assert (shown.omitted, shown.describe()) == (0, "all")


def test_no_limits_print_the_whole_log() -> None:
    printed, shown = render(lines(10), None, None)
    assert printed == lines(10)
    assert (shown.lines, shown.omitted, shown.describe()) == (10, 0, "all")


def test_one_omitted_line_is_singular() -> None:
    printed, _ = render(lines(3), 1, 1)
    assert printed == b"line 1\n[taste-test: 1 line omitted]\nline 3\n"
    assert omitted_marker(1) == b"[taste-test: 1 line omitted]\n"


def test_zero_limits_print_only_the_marker() -> None:
    printed, shown = render(lines(4), 0, 0)
    assert printed == b"[taste-test: 4 lines omitted]\n"
    assert shown.lines == 4


def test_unterminated_final_line_counts_and_is_printed() -> None:
    printed, shown = render(b"a\nb\nc", None, 1)
    assert printed == b"[taste-test: 2 lines omitted]\nc"
    assert shown.lines == 3


def test_empty_log_prints_nothing() -> None:
    printed, shown = render(b"", 5, 5)
    assert printed == b""
    assert (shown.lines, shown.describe()) == (0, "all")


def test_bytes_that_are_not_text_pass_through_unchanged() -> None:
    data = b"\xff\xfe\x00binary\n\x1b[31mred\x1b[0m\n"
    printed, shown = render(data, None, 5)
    assert printed == data
    assert shown.lines == 2


def test_follower_streams_a_growing_log(tmp_path: Path) -> None:
    log = tmp_path / "output.log"
    out = io.BytesIO()
    follower = Follower(lambda: log.open("rb"), out)
    follower.pump()  # the log does not exist yet
    assert out.getvalue() == b""
    assert follower.shown().lines == 0

    log.write_bytes(b"one\ntw")
    follower.pump()
    assert out.getvalue() == b"one\ntw"
    assert follower.shown().lines == 2

    with log.open("ab") as handle:
        handle.write(b"o\nthree\n")
    follower.pump()
    follower.pump()  # nothing new: nothing printed twice
    assert out.getvalue() == b"one\ntwo\nthree\n"
    assert follower.shown() == Shown(3, 0, None, None)
    assert follower.shown().describe() == "all"


def test_follower_leaves_other_open_failures_to_the_caller(tmp_path: Path) -> None:
    def refuse() -> io.BytesIO:
        raise IsADirectoryError("not a log")

    with pytest.raises(IsADirectoryError):
        Follower(refuse, io.BytesIO()).pump()


def test_reading_stops_at_the_limit_and_one_huge_line_is_not_kept_whole() -> None:
    out = io.BytesIO()
    shown = render_limited(io.BytesIO(b"a" * 1000), out, None, 1, limit=64)
    assert out.getvalue() == b"a" * 64
    assert shown.lines == 1
    out = io.BytesIO()
    shown = render_limited(io.BytesIO(lines(10)), out, None, None, limit=14)
    assert out.getvalue() == b"line 1\nline 2\n"
    assert shown.lines == 2
