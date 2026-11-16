"""install_script_test.py — the install script, its contract, and its tests stay together.

The script's behaviour is tested by `taste_test_install_test.sh`, which
`make test` runs. This file checks, without executing any shell, the things that
keep that trio honest and wired into the gate.

Coverage:
  - the script, its contract, and its shell tests all exist
  - the script (in its first three lines) and the shell tests name the contract
    version the contract states
  - every contract's newest change-log entry is for the version in its header
  - `make test` runs the shell tests unconditionally
  - the usage text states the exit codes the contract defines
  - the script runs exactly the uv and git commands the contract declares
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "taste_test_install.sh"
CONTRACT = ROOT / "taste_test_install_contract.md"
SHELL_TESTS = ROOT / "taste_test_install_test.sh"
CONTRACTS = [CONTRACT, *sorted((ROOT / "contracts").glob("*_contract.md"))]


def contract_version(contract: Path = CONTRACT) -> str:
    first = contract.read_text(encoding="utf-8").splitlines()[0]
    match = re.fullmatch(r"Contract version: (v\d+(?: \(unratified\))?)", first)
    assert match is not None, f"unexpected first line of the contract: {first!r}"
    return match.group(1)


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda path: path.name)
def test_newest_change_log_entry_is_for_the_version_in_the_header(contract: Path) -> None:
    assert len(CONTRACTS) == 3
    text = contract.read_text(encoding="utf-8")
    log = text.split("\n## Change log\n", 1)[1]
    entries = re.findall(r"^- \*\*(v\d+(?: \(unratified\))?)\*\*", log, flags=re.MULTILINE)
    assert entries, "the change log has no entry"
    assert entries[0] == contract_version(contract)
    assert "\n## " not in log, "the change log must be the last section"


def test_script_contract_and_tests_exist() -> None:
    for path in (SCRIPT, CONTRACT, SHELL_TESTS):
        assert path.is_file(), f"{path.name} is missing"
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")


def test_script_and_tests_cite_the_contract_version() -> None:
    version = contract_version()
    header = "\n".join(SCRIPT.read_text(encoding="utf-8").splitlines()[:3])
    assert f"Built against contract {version} (taste_test_install_contract.md)" in header
    tests_header = "\n".join(SHELL_TESTS.read_text(encoding="utf-8").splitlines()[:5])
    assert f"contract {version} (taste_test_install_contract.md)" in tests_header


def test_make_test_runs_the_shell_tests_unconditionally() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("\ntest:\n", 1)[1].split("\n\n", 1)[0]
    assert "\tbash taste_test_install_test.sh" in recipe.splitlines()
    assert "ifneq" not in recipe and "wildcard" not in recipe


def test_usage_states_the_contract_exit_codes() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")
    assert "Exit codes: 0 success; 1 refusal or failure" in script
    assert "2 usage error." in script
    for code in ("`0` success", "`1` refusal or failure", "`2` usage error"):
        assert code in contract


def declared(program: str) -> set[str]:
    """What the contract's "Subprocess invocation" bullet says `program` is run with."""
    contract = " ".join(CONTRACT.read_text(encoding="utf-8").split())
    bullet = contract.split("**Subprocess invocation:**", 1)[1].split("- **Environment", 1)[0]
    return set(re.findall(rf"`{program} ([^`]+)`", bullet))


def script_code() -> str:
    """The script without comment lines, the usage text, and the text of messages.

    Only the quoted message of `echo`, `fail` and `usage_error` is removed, so a
    command written after a message on the same line is still seen.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    code = code.split("usage() {", 1)[0] + code.split("\nEOF\n}", 1)[1]
    code = code.replace("\\\n", " ")  # a command continued on the next line is one command
    return without_prose(code)


def without_prose(code: str) -> str:
    """Remove message text and constant text that runs nothing (no `$(` or backtick)."""
    code = re.sub(r'^(readonly [A-Z_]+)="[^"$`]*"$', r"\1", code, flags=re.MULTILINE)
    return re.sub(r'\b(echo|fail|usage_error) "(?:[^"\\]|\\.)*"', r"\1", code, flags=re.DOTALL)


def test_a_command_written_after_a_message_or_in_a_constant_is_still_seen() -> None:
    sample = 'echo "done"; git config user.name x\nfail "no"\nreadonly A="$(git gc)"\n'
    assert without_prose(sample) == 'echo; git config user.name x\nfail\nreadonly A="$(git gc)"\n'
    assert without_prose('readonly WORDS="not a git repository"\n') == "readonly WORDS\n"


def test_script_runs_only_the_uv_commands_the_contract_declares() -> None:
    code = script_code()
    assert "cmd=(uv tool install --force --reinstall --compile-bytecode)" in code
    assert 'cmd=(uv tool uninstall "$TOOL_NAME")' in code
    # Every place `uv` is followed by words, whatever the subcommand; `uvx` too.
    invoked = set(re.findall(r"\buvx?\b((?: [a-z][a-z-]*| --bin)+)", code))
    assert {words.strip() for words in invoked} == {
        "tool install",
        "tool uninstall",
        "tool list",
        "tool dir --bin",
    }
    assert "uvx" not in code
    assert declared("uv") == {
        "tool install ...",
        "tool uninstall tart-taste-test",
        "tool list",
        "tool dir --bin",
    }


def test_script_runs_only_the_git_commands_the_contract_declares() -> None:
    code = script_code()
    # Every place the word `git` starts a command, with or without -C. The only
    # other use allowed is the lookup `command -v git`.
    runs = re.findall(r"(?<!command -v )\bgit\b([^\n|)>]*?)(?= >| 2>|\)|\n)", code)
    status = (
        '-C "$repo_root" -c core.fsmonitor=false status --porcelain '
        "--untracked-files=normal --ignore-submodules=none"
    )
    assert sorted(" ".join(words.split()) for words in runs) == sorted(
        ['-C "$repo_root" rev-parse --is-inside-work-tree', status]
    )
    assert len(re.findall(r"(?<!command -v )\bgit\b", code)) == 2
    assert declared("git") == {
        "-C REPO_ROOT rev-parse --is-inside-work-tree",
        status.replace('"$repo_root"', "REPO_ROOT"),
    }


def test_script_never_escalates_or_fetches() -> None:
    code = script_code()
    for word in ("sudo", "curl", "wget", "ssh", "rm -rf", "eval"):
        assert word not in code
