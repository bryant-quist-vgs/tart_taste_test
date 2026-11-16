"""exitcodes_test.py — the exit-code table has one owner and the docs quote it.

Coverage:
  - Load-bearing: docs/interfaces.md §2 and `exitcodes.EXIT_TABLE` agree row by row
  - every outcome the protocol defines maps to a code; no code is reused
  - only `passed` maps to 0 and only `failed` to 1
"""

import re
from pathlib import Path

from tart_taste_test import exitcodes
from tart_taste_test.exitcodes import EXIT_TABLE, OUTCOME_EXIT, Exit

DOCS = Path(__file__).resolve().parent.parent / "docs"
ROW = re.compile(r"^\| (\d) \| `([a-z-]+)` \| ([^|]*) \| ([^|]*) \| ([^|]*) \|$", re.MULTILINE)


def documented_rows() -> list[tuple[int, str, str, str, str]]:
    text = (DOCS / "interfaces.md").read_text(encoding="utf-8")
    section = text.split("## 2. Exit codes", 1)[1].split("\n## ", 1)[0]
    return [
        (int(code), name, outcome.strip(), meaning.strip(), action.strip())
        for code, name, outcome, meaning, action in ROW.findall(section)
    ]


# Load-bearing: the help text and the documentation must be the same table.
def test_documentation_matches_the_constants() -> None:
    rows = documented_rows()
    assert [(int(r.code), r.name, r.meaning, r.action) for r in EXIT_TABLE] == [
        (code, name, meaning, action) for code, name, _, meaning, action in rows
    ]
    documented_outcomes = {
        outcome.strip("`"): code for code, _, outcome, _, _ in rows if outcome.startswith("`")
    }
    assert documented_outcomes == {name: int(code) for name, code in OUTCOME_EXIT.items()}


def test_codes_are_contiguous_and_unique() -> None:
    assert [int(row.code) for row in EXIT_TABLE] == list(range(9))
    assert sorted(int(code) for code in Exit) == list(range(9))
    assert len({row.name for row in EXIT_TABLE}) == 9


def test_only_passed_is_zero_and_only_failed_is_one() -> None:
    assert [name for name, code in OUTCOME_EXIT.items() if code == 0] == ["passed"]
    assert [name for name, code in OUTCOME_EXIT.items() if code == 1] == ["failed"]
    assert exitcodes.exit_for_outcome("passed") is Exit.PASSED
    for unknown in ("", "PASSED", "ok", "success", "passed\n"):
        assert exitcodes.exit_for_outcome(unknown) is Exit.ERROR


def test_protocol_document_lists_the_same_outcomes() -> None:
    text = (DOCS / "protocol.md").read_text(encoding="utf-8")
    section = text.split("`outcome` is one of", 1)[1].split("\n\n`command_exit`", 1)[0]
    documented = set(re.findall(r"^\| `([a-z-]+)` \|", section, re.MULTILINE))
    assert documented == set(exitcodes.OUTCOMES)


def test_help_epilog_fits_a_terminal() -> None:
    assert max(len(line) for line in exitcodes.help_epilog().splitlines()) <= 80


def test_every_code_has_a_name() -> None:
    assert [exitcodes.name_of(code) for code in Exit] == [row.name for row in EXIT_TABLE]
    assert exitcodes.name_of(Exit.NOT_RUN) == "not-run"
