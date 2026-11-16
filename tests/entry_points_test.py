"""entry_points_test.py — the installed commands exist and load.

Coverage:
  - both console scripts declared in pyproject.toml resolve to callables
  - the installed scripts run and report the package version
"""

import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

import tart_taste_test

EXPECTED = {
    "taste-test": "tart_taste_test.client_cli:main",
    "taste-test-broker": "tart_taste_test.broker_cli:main",
}


def test_console_scripts_are_declared_and_load() -> None:
    scripts = {
        entry.name: entry
        for entry in metadata.entry_points(group="console_scripts")
        if entry.name in EXPECTED
    }
    assert {name: entry.value for name, entry in scripts.items()} == EXPECTED
    for entry in scripts.values():
        assert callable(entry.load())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_installed_script_runs(name: str) -> None:
    script = shutil.which(name, path=str(Path(sys.executable).parent))
    assert script is not None, f"{name} is not installed beside the test interpreter"
    done = subprocess.run([script, "--version"], check=True, capture_output=True, text=True)
    assert done.stdout.strip() == f"{name} {tart_taste_test.__version__}"
    helped = subprocess.run([script, "--help"], check=True, capture_output=True, text=True)
    assert "exit codes" in helped.stdout
