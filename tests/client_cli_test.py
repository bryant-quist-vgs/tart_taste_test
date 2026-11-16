"""client_cli_test.py — the `taste-test` command line, built against contract v1.

Coverage:
  - Load-bearing: --help prints the exit-code table (top level, run, wait)
  - every --help page fits an 80-column terminal
  - usage errors exit 2 and touch nothing
  - `--` splits the command's arguments from the client's
  - Load-bearing: the client imports nothing that can start a VM (decision D2),
    no networking module, and starts no process but Git
  - Load-bearing: a reader that goes away (`| head`, `2>&1 | head`, a closed
    standard error) never changes the exit code (decision D9)
  - Load-bearing: no exception chooses the exit code; client-side endings of
    `run` and `wait` print the reason and summary lines
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import World

from tart_taste_test import client, client_cli, exitcodes, protocol
from tart_taste_test.exitcodes import EXIT_TABLE

CLIENT_MODULES = {"client_cli", "client", "client_spool", "snapshot", "render", "protocol"}
ALLOWED_MODULES = CLIENT_MODULES | {"exitcodes"}


# Load-bearing: the help text is the agent's reference for exit codes.
@pytest.mark.parametrize("argv", [["--help"], ["run", "--help"], ["wait", "--help"]])
def test_help_lists_every_exit_code(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        client_cli.main(argv)
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    assert "exit codes (run, wait):" in text
    for row in EXIT_TABLE:
        assert f"  {int(row.code)}  {row.name}" in text
    assert "0 always and only means the tests passed" in text
    assert "--head" in text or argv == ["--help"]


def test_top_level_help_warns_against_piping(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        client_cli.main(["--help"])
    text = capsys.readouterr().out
    assert "Do not pipe the output through head or tail" in text
    assert "--head N and --tail N" in text


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["frobnicate"],
        ["run", "--head", "-1"],
        ["run", "--tail", "many"],
        ["run", "--wait-timeout", "0"],
        ["run", "make test"],
        ["run", "Test"],
        ["run", "test", "extra-positional"],
        ["wait"],
        ["wait", "not-an-id"],
        ["wait", "../../etc/passwd"],
        ["log", "20261115T183000Z-3f9a1c2b4d5e", "--", "x"],
        ["status", "--", "x"],
        ["cancel"],
        ["run", "--", "bad\x1bargument"],
        ["run", "--", *(["x"] * 65)],
    ],
)
def test_usage_errors_exit_2_without_touching_the_checkout(
    argv: list[str], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(world.repo)
    with pytest.raises(SystemExit) as stopped:
        client_cli.main(argv)
    assert stopped.value.code == 2
    assert not (world.repo / protocol.DIR_NAME).exists()


def test_outside_a_working_tree_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = client_cli.main(["status", "--checkout", str(tmp_path)])
    assert code == 2
    assert "is not inside a Git working tree" in capsys.readouterr().err


def test_double_dash_splits_arguments() -> None:
    assert client_cli.split_arguments(["run", "lint", "--tail", "5"]) == (
        ["run", "lint", "--tail", "5"],
        [],
    )
    assert client_cli.split_arguments(["run", "--", "-k", "x", "--", "y"]) == (
        ["run"],
        ["-k", "x", "--", "y"],
    )


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        client_cli.main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.startswith("taste-test ")


# Load-bearing: the executable an agent runs contains no path that starts a VM.
def test_client_imports_only_the_agent_side_modules() -> None:
    script = (
        "import sys, tart_taste_test.client_cli\n"
        "print(' '.join(sorted(m.split('.')[1] for m in sys.modules "
        "if m.startswith('tart_taste_test.'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    loaded = set(done.stdout.split())
    assert CLIENT_MODULES <= loaded
    assert loaded <= ALLOWED_MODULES
    forbidden = {"tart", "engine", "broker", "broker_cli", "mirror", "registry", "spool", "doctor"}
    assert not loaded & forbidden


# Load-bearing: the client never touches the network.
def test_client_loads_no_networking_or_process_spawning_module() -> None:
    script = "import sys, tart_taste_test.client_cli\nprint(' '.join(sorted(sys.modules)))\n"
    done = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    loaded = set(done.stdout.split())
    assert "subprocess" in loaded  # the check sees what the client loads
    for name in ("socket", "ssl", "http.client", "urllib.request", "asyncio", "multiprocessing"):
        assert name not in loaded, name


def test_client_sources_do_not_mention_tart_or_subprocess_beyond_git() -> None:
    package = Path(client_cli.__file__).parent
    for name in sorted(ALLOWED_MODULES):
        source = (package / f"{name}.py").read_text(encoding="utf-8")
        assert '"tart"' not in source and "'tart'" not in source, name
        for spawner in (
            "os.system",
            "os.exec",
            "os.spawn",
            "os.posix_spawn",
            "os.popen",
            "os.fork",
        ):
            assert spawner not in source, (name, spawner)
        for network in ("socket", "urllib", "http.client", "ssl."):
            assert network not in source, (name, network)
        if name != "snapshot":
            assert "subprocess" not in source, name
    snapshot_source = (package / "snapshot.py").read_text(encoding="utf-8")
    assert snapshot_source.count("subprocess.run(") == 1
    assert '["git", "-C", str(root), *args]' in snapshot_source


class _ClosedPipe:
    def write(self, _data: bytes) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


def test_closed_output_pipe_is_survived(monkeypatch: pytest.MonkeyPatch) -> None:
    silenced: list[object] = []
    monkeypatch.setattr(exitcodes, "silence", silenced.append)
    out = client_cli.SafeOut(_ClosedPipe())  # type: ignore[arg-type]
    assert out.write(b"line\n") == 5
    out.flush()
    assert out.write(b"more\n") == 5
    assert silenced == [sys.stdout]


def finished_request(world: World, outcome: str, command_exit: int | None) -> str:
    """A finished request with far more output than a pipe buffer holds."""
    request_id = world.submit()
    directory = world.paths.result_dir(request_id)
    directory.mkdir(parents=True)
    (directory / protocol.OUTPUT_LOG).write_bytes(b"x" * 80 + b"\n" + b"y\n" * 200_000)
    result = protocol.result_document(
        request_id,
        outcome,
        command_exit,
        "" if command_exit is not None else "stopped",
        None,
        None,
        None,
        None,
        None,
        0,
        False,
        {"total": 1.0},
    )
    (directory / protocol.RESULT_FILE).write_bytes(protocol.encode(result))
    return request_id


def start_wait(
    world: World, request_id: str, *extra: str, stdout: int, stderr: int
) -> subprocess.Popen[bytes]:
    script = (
        "import sys\n"
        "from tart_taste_test import client_cli\n"
        f"sys.exit(client_cli.main(['wait', {request_id!r}, '--checkout', {str(world.repo)!r}, "
        f"*{list(extra)!r}]))\n"
    )
    return subprocess.Popen([sys.executable, "-c", script], stdout=stdout, stderr=stderr)


def dead_pipe() -> int:
    """The write end of a pipe nobody reads: every write to it fails."""
    reader, writer = os.pipe()
    os.close(reader)
    return writer


# Load-bearing: decision D9. 120 is what Python exits with when its last flush fails.
@pytest.mark.parametrize(
    ("outcome", "command_exit", "expected"),
    [("passed", 0, 0), ("failed", 4, 1), ("cancelled", None, 7)],
)
@pytest.mark.parametrize("closed", ["stdout", "stderr", "both"])
@pytest.mark.parametrize("extra", [(), ("--tail", "2")], ids=["stream", "tail"])
def test_a_reader_that_has_gone_away_never_changes_the_exit_code(
    world: World,
    outcome: str,
    command_exit: int | None,
    expected: int,
    closed: str,
    extra: tuple[str, ...],
) -> None:
    request_id = finished_request(world, outcome, command_exit)
    dead = dead_pipe()
    try:
        out = dead if closed in ("stdout", "both") else subprocess.DEVNULL
        err = dead if closed in ("stderr", "both") else subprocess.DEVNULL
        waiter = start_wait(world, request_id, *extra, stdout=out, stderr=err)
        assert waiter.wait(timeout=60) == expected
    finally:
        os.close(dead)


def test_a_closed_pipe_does_not_change_a_usage_error_either(world: World) -> None:
    dead = dead_pipe()
    try:
        script = (
            "import sys\nfrom tart_taste_test import client_cli\n"
            "sys.exit(client_cli.main(['run', '--head', '-1']))\n"
        )
        done = subprocess.run([sys.executable, "-c", script], stdout=dead, stderr=dead, timeout=60)
        assert done.returncode == 2
        script = script.replace("['run', '--head', '-1']", "['--help']")
        done = subprocess.run([sys.executable, "-c", script], stdout=dead, stderr=dead, timeout=60)
        assert done.returncode == 0
    finally:
        os.close(dead)


def test_piping_into_a_reader_that_exits_keeps_the_exit_code(world: World) -> None:
    request_id = world.submit()
    directory = world.paths.result_dir(request_id)
    directory.mkdir(parents=True)
    (directory / protocol.OUTPUT_LOG).write_bytes(b"x" * 80 + b"\n" * 1 + b"y\n" * 200_000)
    result = protocol.result_document(
        request_id, "failed", 4, "", None, None, None, None, None, 0, False, {"total": 1.0}
    )
    (directory / protocol.RESULT_FILE).write_bytes(protocol.encode(result))
    script = (
        "import sys\n"
        "from tart_taste_test import client_cli\n"
        f"code = client_cli.main(['wait', {request_id!r}, '--checkout', {str(world.repo)!r}])\n"
        "sys.stderr.write(f'CODE={code}\\n')\n"
        "sys.exit(code)\n"
    )
    waiter = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert waiter.stdout is not None and waiter.stderr is not None
    waiter.stdout.read(10)
    waiter.stdout.close()  # the reader goes away, like `| head -c 10`
    errors = waiter.stderr.read().decode()
    assert waiter.wait(timeout=30) == 1
    assert "CODE=1" in errors
    assert "outcome=failed exit=1 command-exit=4" in errors


def test_shared_pipe_whose_reader_exits_midway_keeps_the_exit_code(world: World) -> None:
    """`taste-test wait ID 2>&1 | head -c 10` for a passed request must exit 0."""
    request_id = finished_request(world, "passed", 0)
    waiter = start_wait(world, request_id, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert waiter.stdout is not None
    waiter.stdout.read(10)
    waiter.stdout.close()
    assert waiter.wait(timeout=60) == 0


# Load-bearing: Python's own exit code for a traceback is 1, which means `failed`.
@pytest.mark.parametrize("action", ["run", "wait", "log", "status", "cancel"])
@pytest.mark.parametrize("raised", [RuntimeError("boom"), KeyboardInterrupt()])
def test_no_exception_chooses_the_exit_code(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    raised: BaseException,
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(client, action, broken)
    request_id = "20261115T183000Z-3f9a1c2b4d5e"
    argv = [action, "--checkout", str(world.repo)]
    if action in ("wait", "log", "cancel"):
        argv.insert(1, request_id)
    assert client_cli.main(argv) == 5
    assert "taste-test: error: " in capsys.readouterr().err


def test_client_side_endings_of_run_print_the_reason_and_summary_lines(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(protocol, "MAX_SOURCE_BYTES", 1024)
    world.write({"big.bin": "x" * 50_000})
    assert client_cli.main(["run", "--checkout", str(world.repo)]) == 6
    lines = capsys.readouterr().err.splitlines()
    assert lines[-2].startswith("taste-test: reason: the snapshot would be larger than 1024 bytes")
    assert lines[-1] == "taste-test: outcome=not-run exit=6"


@pytest.mark.parametrize(
    "argv", [["--help"], *([name, "--help"] for name in ("run", "wait", "log", "status", "cancel"))]
)
def test_every_help_page_fits_an_80_column_terminal(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    with pytest.raises(SystemExit):
        client_cli.main(argv)
    for line in capsys.readouterr().out.splitlines():
        assert len(line) <= 80, line
