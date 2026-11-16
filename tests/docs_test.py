"""docs_test.py — the documentation agrees with itself and with the code.

`make docs-lint` checks the glossary and links to it. This file checks the rest:
ordinary links and anchors, document metadata, and the binding values the
documents quote from code.

Coverage:
  - every relative Markdown link resolves to a file, and every anchor to a heading
  - every document under docs/ declares its identity, authority, and status
  - the values docs/protocol.md and docs/interfaces.md quote match the constants
  - the guest scripts in docs/vm-handling.md §2 are the ones the engine runs
  - the Tart start line in the docs is the one the adapter builds
  - the client's and the broker's --help agree with docs/interfaces.md
  - nothing in the repository refers to documents that are not part of it
"""

import re
import string
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tart_taste_test import (
    audit,
    broker,
    broker_cli,
    checkouts,
    client_cli,
    engine,
    exitcodes,
    mirror,
    notices,
    process,
    protocol,
    repo_config,
    served,
    spool,
    tart,
)

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DOCUMENTS = sorted([*DOCS.glob("*.md"), *(ROOT / "contracts").glob("*.md"), *ROOT.glob("*.md")])
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(```|~~~)")
REQUIRED_FRONT_MATTER = ("id", "is_source_of_truth", "derives_from", "status")
STATUSES = {"draft", "current", "policy", "superseded", "archived"}
# Typographic punctuation GitHub also drops from anchors: quotes, dashes, ellipsis, etc.
TYPOGRAPHIC = "\u2018\u2019\u201c\u201d\u2013\u2014\u2026\u00a7\u2192"


def git_listing() -> list[str] | None:
    """The repository's files as Git lists them, or None outside a Git working tree."""
    command = ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"]
    try:
        done = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError:
        return None  # no git at all
    return done.stdout.splitlines() if done.returncode == 0 else None


def tracked_documents(listing: Callable[[], list[str] | None] = git_listing) -> list[Path]:
    """Documents that belong to the repository.

    In a Git working tree, ignored and privately excluded files are skipped.
    Outside one (a source archive, or the VM this suite runs in under
    `taste-test`, which has no `.git`) only repository files are present, so
    every document counts. The suite must be collectable there too.
    """
    listed = listing()
    if listed is None:
        return list(DOCUMENTS)
    known = {ROOT / name for name in listed}
    return [path for path in DOCUMENTS if path in known]


def test_documents_are_found_with_and_without_a_git_working_tree() -> None:
    assert tracked_documents(lambda: None) == DOCUMENTS
    assert tracked_documents(lambda: ["README.md", "docs/protocol.md", "private.md"]) == [
        ROOT / "README.md",
        ROOT / "docs" / "protocol.md",
    ]
    assert tracked_documents(lambda: []) == []


def prose_lines(path: Path) -> list[str]:
    """The lines of a document that are outside fenced code blocks."""
    lines, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    text = re.sub(r"<[^>]+>", "", heading).strip().lower()
    dropped = set(string.punctuation) - {"-", "_"}
    kept = "".join(ch for ch in text if ch not in dropped and ch not in TYPOGRAPHIC)
    return kept.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {
        slug(match.group(1))
        for line in prose_lines(path)
        if (match := re.match(r"#{1,6}\s+(.*)$", line))
    }


def section(document: str, heading: str) -> str:
    text = (DOCS / document).read_text(encoding="utf-8")
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("path", tracked_documents(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(path: Path) -> None:
    for number, line in enumerate(prose_lines(path), start=1):
        for target in LINK.findall(line):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue  # an absolute URL
            file_part, _, fragment = target.partition("#")
            linked = (path.parent / file_part).resolve() if file_part else path
            assert linked.exists(), f"{path.name}:{number}: {target} does not exist"
            if fragment and linked.suffix == ".md":
                assert fragment in anchors(linked), (
                    f"{path.name}:{number}: no heading for #{fragment} in {linked.name}"
                )


@pytest.mark.parametrize("path", sorted(DOCS.glob("*.md")), ids=lambda p: p.name)
def test_documents_declare_identity_and_status(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} has no front matter"
    front = text.split("---\n", 2)[1]
    keys = dict(line.split(":", 1) for line in front.splitlines() if re.match(r"[a-z_]+:", line))
    for key in REQUIRED_FRONT_MATTER:
        assert key in keys, f"{path.name} lacks {key}"
    assert keys["status"].strip() in STATUSES
    assert keys["id"].strip() == path.stem


def test_document_ids_referenced_by_derives_from_exist() -> None:
    ids = {path.stem for path in DOCS.glob("*.md")}
    for path in DOCS.glob("*.md"):
        front = path.read_text(encoding="utf-8").split("---\n", 2)[1]
        declared = re.search(r"^derives_from:\s*\[(.*)\]$", front, re.MULTILINE)
        assert declared is not None, path.name
        for name in filter(None, (item.strip() for item in declared.group(1).split(","))):
            assert name in ids, f"{path.name} derives from unknown document {name}"


def test_design_document_map_names_every_document() -> None:
    design = (DOCS / "design.md").read_text(encoding="utf-8")
    for path in DOCS.glob("*.md"):
        assert f"docs/{path.name}" in design, f"design.md does not map {path.name}"


def test_protocol_limits_match_the_constants() -> None:
    table = section("protocol.md", "## 2. Identifiers and limits")
    for value in (
        protocol.REQUEST_ID_RE.pattern,
        protocol.COMMAND_NAME_RE.pattern,
        str(protocol.MAX_ARGS),
        str(protocol.MAX_ARG_BYTES),
        str(protocol.MAX_JSON_BYTES),
        str(protocol.MAX_SOURCE_BYTES),
        str(protocol.MAX_LOG_BYTES),
        f"{protocol.MAX_REQUEST_AGE_SECONDS:g} seconds",
        f"{protocol.HEARTBEAT_INTERVAL_SECONDS:g} seconds",
        f"{protocol.HEARTBEAT_STALE_SECONDS:g} seconds",
        f"{protocol.ACK_WINDOW_SECONDS:g} seconds",
        f"{protocol.BROKER_LOST_SECONDS:g} seconds",
        f"{protocol.CLIENT_GIT_TIMEOUT_SECONDS} seconds",
        f"{protocol.CLIENT_POLL_SECONDS:g} seconds",
        protocol.COMMIT_ID_RE.pattern,
        protocol.OUTCOME_RE.pattern,
        f"at most {protocol.MAX_VERSION_CHARS} printable characters",
        f"{protocol.RETENTION_SECONDS / 86400:g} days",
        f"{protocol.SPOOL_DIR_MODE:04o}",
        f"| {protocol.MAX_PENDING_REQUESTS}. Further requests",
        f"| {spool.MAX_ENTRIES} |",
        f"| {checkouts.MAX_WORKTREES}, each at most {checkouts.SMALL_FILE_BYTES} bytes",
        f"at most {checkouts.MAX_PATH_PARTS} |",
        f"| {process.POLL_SECONDS:g} seconds |",
        f"served at once | at most {served.MAX_LINKED_SPOOLS}, first come first served",
        f"the broker prints | {notices.MAX_CHECKOUT_WARNINGS} in the life of the broker",
        f"stopped being served | {notices.MAX_CHECKOUT_EVENTS} in any "
        f"{notices.CHECKOUT_EVENT_PERIOD_SECONDS:g} seconds",
        f"the up to {process.REAP_SECONDS:g} seconds the broker waits",
        f"| {served.SCAN_BUDGET_SECONDS:g} seconds, plus one spool",
        f"arguments up to {audit.AUDIT_ARGS_BYTES} bytes in total",
        f"a reason up to {audit.AUDIT_REASON_CHARS} characters",
        f"audit-log line at start-up | {broker.MAX_RECOVERIES_LOGGED} |",
    ):
        assert value in table, f"protocol.md §2 does not state {value!r}"
    # The shortest time between two turns of one checkout is the scan interval.
    assert broker.Pace().turn_interval == protocol.HEARTBEAT_INTERVAL_SECONDS
    assert "| Shortest time between two requests of one spool starting | 2 seconds |" in table


def test_spool_file_names_match_the_constants() -> None:
    layout = section("protocol.md", "## 1. Spool layout")
    for name in (
        protocol.DIR_NAME,
        protocol.SPOOL_NAME,
        protocol.REQUESTS_NAME,
        protocol.RESULTS_NAME,
        protocol.HEARTBEAT_FILE,
        protocol.REQUEST_FILE,
        protocol.CLAIMED_FILE,
        protocol.WITHDRAWN_FILE,
        protocol.SOURCE_FILE,
        protocol.CANCEL_FILE,
        protocol.STATUS_FILE,
        protocol.SETUP_LOG,
        protocol.OUTPUT_LOG,
        protocol.RESULT_FILE,
    ):
        assert name in layout, f"protocol.md §1 does not name {name}"


def test_guest_scripts_in_the_documentation_are_the_ones_that_run() -> None:
    steps = section("vm-handling.md", "## 2. Guest layout and steps")
    for script in (
        engine.SCRIPT_READY,
        engine.SCRIPT_PROBE,
        engine.SCRIPT_REQUIRE,
        engine.SCRIPT_UNPACK_BASE,
        engine.SCRIPT_SAVE_MANIFEST,
        engine.SCRIPT_OVERLAY,
        engine.SCRIPT_SYNC,
        engine.COMMAND_PREFIX,
    ):
        assert script in steps, f"vm-handling.md §2 does not quote: {script}"
    assert f"`{engine.ARGV0}`" in steps


def test_tart_command_lines_in_the_documentation_are_the_ones_built() -> None:
    invocations = section("vm-handling.md", "## 1. Tart invocations")
    assert f"tart run {' '.join(tart.RUN_FLAGS)} <network flags> <name>" in invocations
    for mode, flags in tart.NETWORK_FLAGS.items():
        shown = " ".join(flags) if flags else "none"
        assert re.search(rf"\| `{mode}` \| `?{re.escape(shown)}`? \|", invocations), mode
    names = tart.Names("<state key>")
    for example in (
        names.environment("<repo key>", "<environment key>"),
        names.build("<repo key>", "<environment key>"),
        names.run("<token>"),
        names.doctor("<token>"),
    ):
        assert example in invocations, example


def test_config_defaults_in_the_documentation_match_the_code() -> None:
    schema = section("interfaces.md", "## 4. Repository config")
    for needle in (
        repo_config.IMAGE_RE.pattern,
        f"`{repo_config.DEFAULT_BOOT_TIMEOUT}`",
        f"`{repo_config.DEFAULT_TIMEOUT}`",
        '`["/bin/sh", "-lc"]`',
        f"`{repo_config.MIN_MEMORY_MB}`",
        str(repo_config.MAX_CONFIG_BYTES),
        str(repo_config.MAX_STRING_BYTES),
    ):
        assert needle in schema, f"interfaces.md §4 does not state {needle}"
    policy = repo_config.Policy()
    flags = section("interfaces.md", "## 3. `taste-test-broker`")
    assert f"| `--max-setup-network MODE` | `{policy.max_setup_network}` |" in flags
    assert f"| `--max-run-network MODE` | `{policy.max_run_network}` |" in flags
    assert f"| `--max-timeout SECONDS` | `{policy.max_timeout}` |" in flags
    assert f"| `--max-memory-mb N` | `{policy.max_memory_mb}` |" in flags


def options_of(parser_help: str) -> set[str]:
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", parser_help))


def test_every_command_line_flag_is_documented() -> None:
    interfaces = (DOCS / "interfaces.md").read_text(encoding="utf-8")
    for parser in (client_cli.build_parser(), broker_cli.build_parser()):
        subparsers = next(
            action for action in parser._actions if hasattr(action, "choices") and action.choices
        )
        for name, sub in subparsers.choices.items():  # type: ignore[union-attr]
            assert re.search(rf"{parser.prog} {name}\b", interfaces), f"{parser.prog} {name}"
            for flag in options_of(sub.format_help()) - {"--help"}:
                assert flag in interfaces, f"{parser.prog} {name} {flag} is not documented"


def test_nothing_refers_to_documents_outside_the_repository() -> None:
    needle = "green" + "field"
    listed = git_listing()
    if listed is None:  # not a Git working tree: every file outside hidden directories
        inside = (path.relative_to(ROOT) for path in ROOT.rglob("*") if path.is_file())
        listed = [
            str(path)
            for path in inside
            if not path.parts[0].startswith(".") or path.name == path.parts[0]
        ]
    for name in listed:
        path = ROOT / name
        if not path.is_file() or path.suffix in {".lock", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        assert needle not in text, f"{name} mentions a document that is not in the repository"
        assert needle not in name.lower()


def test_readme_quotes_the_exit_codes_and_names() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quoted = re.findall(r"^\| (\d) \| `([a-z-]+)` \| (.*) \|$", readme, re.MULTILINE)
    assert quoted == [(str(int(row.code)), row.name, row.meaning) for row in exitcodes.EXIT_TABLE]


TOML_BLOCK = re.compile(r"^```toml\n(.*?)^```", re.DOTALL | re.MULTILINE)


# The configs shown to readers must be ones the broker accepts.
@pytest.mark.parametrize(
    ("document", "heading"),
    [("README.md", "## Repository config"), ("docs/interfaces.md", "## 4. Repository config")],
)
def test_example_configs_are_accepted(document: str, heading: str) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    examples = [m for m in TOML_BLOCK.findall(text[text.index(heading) :]) if "schema" in m]
    assert examples, f"{document} shows no complete example config"
    policy = repo_config.Policy(max_setup_network="shared", max_run_network="shared", max_cpu=64)
    for example in examples:
        config = repo_config.parse(example.encode(), policy)
        for command in config.commands.values():
            assert not command.allow_args or repo_config.ARGS_PLACEHOLDER in command.run


def test_this_repositorys_own_config_is_accepted_under_the_default_policy() -> None:
    raw = (ROOT / repo_config.CONFIG_PATH).read_bytes()
    config = repo_config.parse(raw, repo_config.Policy())
    assert set(config.commands) == {"test", "installer", "lint"}
    assert config.run_network == "isolated"
    for command in config.commands.values():
        assert not command.allow_args or repo_config.ARGS_PLACEHOLDER in command.run
    # uv is installed from a versioned URL, not from whatever is newest.
    assert "astral.sh/uv/0." in config.setup_run[0] and "/install.sh" in config.setup_run[0]


def test_git_and_stop_rules_in_the_documentation_match_the_code() -> None:
    state = section("protocol.md", "## 5. Operator state")
    for name in mirror.GIT_VARIABLES_KEPT:
        assert f"`{name}`" in state, f"protocol.md §5 does not name {name}"
    assert f"directory `{mirror.GIT_WORKING_DIRECTORY}`" in state
    invocations = " ".join(section("vm-handling.md", "## 1. Tart invocations").split())
    assert f'"{broker.STOPPED}"' in invocations
    assert f"a wait looks for a stop every {process.POLL_SECONDS:g} seconds" in invocations
    assert (
        f"waits up to {process.REAP_SECONDS:g} seconds for the process to be reaped" in invocations
    )
    assert f"`tart stop <name> --timeout {tart.STOP_GRACE_SECONDS}`" in invocations
    for limit in (tart.TART_QUICK_TIMEOUT, tart.TART_CLONE_TIMEOUT):
        assert f"{limit:g} s" in invocations
    for limit in (mirror.GIT_FETCH_TIMEOUT, mirror.GIT_ARCHIVE_TIMEOUT, mirror.GIT_QUICK_TIMEOUT):
        assert f"{limit:g}" in invocations
    assert f"waited for {engine.RUN_EXIT_WAIT:g} seconds" in invocations
    assert f"after {engine.DELETE_RETRY_PAUSE:g} seconds" in invocations
    steps = " ".join(section("vm-handling.md", "## 2. Guest layout and steps").split())
    assert f"every {process.POLL_SECONDS:g} seconds while a setup or named command runs" in steps
