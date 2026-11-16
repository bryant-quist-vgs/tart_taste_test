"""Shared fixtures: a real Git repository, private operator state, and a fake `tart`.

No test starts a virtual machine. `fake_tart.py` stands in for the `tart`
command and runs "guest" commands in a temporary directory, so the broker's real
guest scripts execute end to end.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tart_taste_test import checkouts, client_spool, protocol, registry, repo_config, tart
from tart_taste_test.broker import Broker, Pace
from tart_taste_test.engine import Engine
from tart_taste_test.spool import Spool

FAKE_TART = Path(__file__).with_name("fake_tart.py")

DEFAULT_CONFIG = """\
schema = 1

[vm]
image = "ghcr.io/example/image:latest"

[setup]
run = ["mkdir -p deps && echo built > deps/marker"]

[commands.test]
run = 'sh run_tests.sh "$@"'
allow_args = true

[commands.lint]
run = "sh lint.sh"
"""

DEFAULT_FILES = {
    ".taste-test.toml": DEFAULT_CONFIG,
    ".gitignore": "ignored/\n*.log\n",
    "run_tests.sh": 'echo "tests ran: $*"\n',
    "lint.sh": "echo lint ok\n",
    "keep.txt": "from the default branch\n",
}

# Broker timing for tests: no pauses between readiness attempts, delete retries,
# or two requests of one checkout.
FAST = Pace(ready_interval=0.0, delete_retry_pause=0.0, turn_interval=0.0)


def open_spool(root: Path) -> Spool | None:
    """Open a checkout's spool the way the broker does for a main working tree."""
    top, _ = checkouts.open_path(root.resolve())
    try:
        return Spool.open(root, top)
    finally:
        os.close(top)


def git(cwd: Path, *args: str) -> str:
    """Run Git for a fixture, isolated from the developer's own configuration."""
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=environment
    )
    return done.stdout.strip()


class World:
    """One registered repository, its operator state, and a fake Tart."""

    def __init__(self, base: Path) -> None:
        self.repo = base / "repo"
        self.state = registry.State(base / "state")
        self.tart_home = base / "tart-home"
        self.tart_home.mkdir()
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "--initial-branch", "main")
        self.commit(DEFAULT_FILES, "initial")
        self.state.ensure()
        self.repository = registry.new_repository(self.repo, "main", None)
        registry.save(self.state, [self.repository])
        wrapper = base / "bin" / "tart"
        wrapper.parent.mkdir()
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TART}" "$@"\n')
        wrapper.chmod(0o755)
        self.tart_path = wrapper
        self.events: list[str] = []

    # -- the repository ------------------------------------------------------

    def write(self, files: Mapping[str, str | None], root: Path | None = None) -> None:
        """Create, replace, or (with None) delete working-tree files."""
        for name, content in files.items():
            path = (root or self.repo) / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

    def commit(self, files: Mapping[str, str | None], message: str = "change") -> str:
        """Write files and commit them to the default branch. Returns the commit ID."""
        self.write(files)
        git(self.repo, "add", "--all")
        git(self.repo, "commit", "--quiet", "--message", message)
        return git(self.repo, "rev-parse", "HEAD")

    @property
    def paths(self) -> client_spool.SpoolPaths:
        return client_spool.SpoolPaths(self.repo)

    # -- the client and broker ---------------------------------------------------

    def submit(
        self, command: str = "test", args: list[str] | None = None, root: Path | None = None
    ) -> str:
        paths = client_spool.SpoolPaths(root or self.repo)
        client_spool.ensure_spool(paths)
        return client_spool.submit(paths, command, args or [])

    def vms(self) -> tart.Tart:
        return tart.Tart(str(self.tart_path))

    def engine(self, warn: Callable[[str], None] | None = None) -> Engine:
        """An engine on the fake Tart with no pauses."""
        return Engine(
            self.vms(),
            self.state,
            warn=warn if warn is not None else self.events.append,
            ready_interval=0.0,
            delete_retry_pause=0.0,
        )

    def broker(self, policy: repo_config.Policy | None = None, pace: Pace = FAST) -> Broker:
        return Broker(
            self.state,
            policy or repo_config.Policy(),
            self.vms(),
            self.events.append,
            pace,
        )

    def serve(self, policy: repo_config.Policy | None = None) -> None:
        """Process every waiting request, then return."""
        assert self.broker(policy).serve(once=True) == 0

    def run(self, command: str = "test", args: list[str] | None = None) -> dict[str, Any]:
        """Submit one request, serve it, and return its result document."""
        request_id = self.submit(command, args)
        self.serve()
        return self.result(request_id)

    # -- what happened -------------------------------------------------------------

    def result(self, request_id: str, root: Path | None = None) -> dict[str, Any]:
        path = client_spool.SpoolPaths(root or self.repo).result_dir(request_id)
        document: dict[str, Any] = json.loads((path / protocol.RESULT_FILE).read_text())
        return document

    def output(self, request_id: str, name: str = protocol.OUTPUT_LOG) -> str:
        return (self.paths.result_dir(request_id) / name).read_text(encoding="utf-8")

    def calls(self, command: str | None = None) -> list[list[str]]:
        """Every fake-tart invocation so far, optionally only one subcommand."""
        log = self.tart_home / "calls.log"
        if not log.exists():
            return []
        calls: list[list[str]] = [json.loads(line) for line in log.read_text().splitlines()]
        return [c for c in calls if command is None or c[0] == command]

    def vm_names(self) -> list[str]:
        directory = self.tart_home / "vms"
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []

    def switch(self, name: str, content: str = "") -> None:
        """Turn on a fake-tart behaviour switch (see fake_tart.py)."""
        (self.tart_home / name).write_text(content, encoding="utf-8")

    def audit(self) -> list[dict[str, Any]]:
        if not self.state.audit.exists():
            return []
        return [json.loads(line) for line in self.state.audit.read_text().splitlines()]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    made = World(tmp_path)
    monkeypatch.setenv("FAKE_TART_HOME", str(made.tart_home))
    monkeypatch.setenv(registry.STATE_ENV, str(made.state.root))
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name)
    return made
