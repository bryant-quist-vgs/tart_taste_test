"""The repository config (`.taste-test.toml`) and the operator's policy ceilings.

Single owner of the config schema and the network modes, specified in
docs/interfaces.md §4. The broker reads the file from the default-branch commit,
never from the candidate. Validation is strict: an unknown key, a wrong type, or
a value above a ceiling is an error, and nothing is silently reduced.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from typing import Any

from tart_taste_test import protocol

CONFIG_PATH = ".taste-test.toml"
MAX_CONFIG_BYTES = 65536
MAX_STRING_BYTES = 4096
MAX_COMMANDS = 32
MAX_SETUP_COMMANDS = 32

IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}")
# Names this project gives its own VMs; a config may never name one as its image.
OWN_VM_PREFIX = "tastetest-"
# Where a command that accepts arguments puts them. Nothing is appended to a
# command string, so there is no question of what a suffix would attach to.
ARGS_PLACEHOLDER = '"$@"'

# Network modes, from most to least restricted. The order is the comparison the
# --max-network ceiling uses.
NETWORK_MODES: tuple[str, ...] = ("isolated", "internet", "shared")

DEFAULT_SHELL: tuple[str, ...] = ("/bin/sh", "-lc")
DEFAULT_BOOT_TIMEOUT = 180
DEFAULT_SETUP_NETWORK = "internet"
DEFAULT_RUN_NETWORK = "isolated"
DEFAULT_TIMEOUT = 1800
MIN_MEMORY_MB = 512


class ConfigError(Exception):
    """The repository config is invalid or asks for more than policy allows."""


@dataclass(frozen=True)
class Policy:
    """Ceilings set by the operator on `taste-test-broker serve`.

    The run ceiling defaults to `isolated`: unless the operator raises it,
    candidate code never gets a network, whatever a repository config asks for.
    """

    max_setup_network: str = "internet"
    max_run_network: str = "isolated"
    max_timeout: int = 7200
    max_cpu: int = os.cpu_count() or 1
    max_memory_mb: int = 16384
    allow_local_images: bool = False


@dataclass(frozen=True)
class Command:
    name: str
    run: str
    allow_args: bool
    timeout_seconds: int


@dataclass(frozen=True)
class RepoConfig:
    image: str
    cpu: int | None
    memory_mb: int | None
    boot_timeout_seconds: int
    shell: tuple[str, ...]
    setup_run: tuple[str, ...]
    setup_network: str
    setup_timeout_seconds: int
    run_network: str
    commands: dict[str, Command]


def network_rank(mode: str) -> int:
    return NETWORK_MODES.index(mode)


class _Table:
    """One TOML table being validated; tracks which keys were consumed."""

    def __init__(self, data: dict[str, Any], where: str) -> None:
        self._data = data
        self._where = where
        self._seen: set[str] = set()

    def _key(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def has(self, key: str) -> bool:
        return key in self._data

    def take(self, key: str) -> Any:
        self._seen.add(key)
        if key not in self._data:
            raise ConfigError(f"{self._key(key)} is required")
        return self._data[key]

    def string(self, key: str) -> str:
        return _string(self.take(key), self._key(key))

    def integer(self, key: str, low: int, high: int, ceiling: str = "") -> int:
        value = self.take(key)
        name = self._key(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} must be an integer")
        if value < low:
            raise ConfigError(f"{name} is {value}; the minimum is {low}")
        if value > high:
            limit = f"the {ceiling} ceiling is {high}" if ceiling else f"the maximum is {high}"
            raise ConfigError(f"{name} is {value}; {limit}")
        return value

    def boolean(self, key: str) -> bool:
        value = self.take(key)
        if not isinstance(value, bool):
            raise ConfigError(f"{self._key(key)} must be true or false")
        return value

    def strings(self, key: str, most: int) -> tuple[str, ...]:
        value = self.take(key)
        name = self._key(key)
        if not isinstance(value, list):
            raise ConfigError(f"{name} must be an array of strings")
        if len(value) > most:
            raise ConfigError(f"{name} has {len(value)} items; the maximum is {most}")
        return tuple(_string(item, f"{name}[{index}]") for index, item in enumerate(value))

    def table(self, key: str) -> _Table:
        self._seen.add(key)
        value = self._data.get(key, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{self._key(key)} must be a table")
        return _Table(value, self._key(key))

    def network(self, key: str, default: str, ceiling: str, flag: str) -> str:
        """A network mode, defaulted if absent, and checked against its ceiling."""
        stated = self.has(key)
        mode = self.string(key) if stated else default
        name = self._key(key)
        if mode not in NETWORK_MODES:
            raise ConfigError(f"{name} is {mode!r}; it must be one of {', '.join(NETWORK_MODES)}")
        if network_rank(mode) > network_rank(ceiling):
            source = "" if stated else " (the default)"
            raise ConfigError(f"{name} is {mode!r}{source}; the {flag} ceiling is {ceiling!r}")
        return mode

    def timeout(self, default: int, policy: Policy) -> int:
        """`timeout_seconds`, defaulted if absent; the effective value is checked."""
        key = "timeout_seconds"
        if self.has(key):
            return self.integer(key, 1, policy.max_timeout, "--max-timeout")
        self._seen.add(key)
        if default > policy.max_timeout:
            raise ConfigError(
                f"{self._key(key)} is {default} (the default); the --max-timeout ceiling is "
                f"{policy.max_timeout}"
            )
        return default

    def finish(self) -> None:
        unknown = sorted(set(self._data) - self._seen)
        if unknown:
            raise ConfigError(f"unknown key {self._key(unknown[0])}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    if not value:
        raise ConfigError(f"{name} must not be empty")
    if "\x00" in value:
        raise ConfigError(f"{name} contains a NUL byte")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_STRING_BYTES:
        raise ConfigError(f"{name} is longer than {MAX_STRING_BYTES} bytes")
    return value


def parse(data: bytes, policy: Policy) -> RepoConfig:
    """Parse and validate the bytes of `.taste-test.toml` under `policy`."""
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{CONFIG_PATH} is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{CONFIG_PATH} is not valid TOML: {error}") from None

    top = _Table(document, "")
    top.integer("schema", 1, 1)

    vm = top.table("vm")
    image = vm.string("image")
    if IMAGE_RE.fullmatch(image) is None:
        raise ConfigError("vm.image is not a valid image reference")
    # Compared without regard to case: on a case-insensitive volume a VM named
    # `TASTETEST-...` could be one of this project's own.
    if image.lower().startswith(OWN_VM_PREFIX):
        raise ConfigError(f"vm.image must not name a {OWN_VM_PREFIX}* VM")
    if "/" not in image and not policy.allow_local_images:
        raise ConfigError(
            f"vm.image is {image!r}, a local VM name; only registry references "
            "(containing '/') are accepted unless the broker runs with --allow-local-images"
        )
    cpu = vm.integer("cpu", 1, policy.max_cpu, "--max-cpu") if vm.has("cpu") else None
    memory = (
        vm.integer("memory_mb", MIN_MEMORY_MB, policy.max_memory_mb, "--max-memory-mb")
        if vm.has("memory_mb")
        else None
    )
    boot = (
        vm.integer("boot_timeout_seconds", 10, 900)
        if vm.has("boot_timeout_seconds")
        else DEFAULT_BOOT_TIMEOUT
    )
    shell = vm.strings("shell", 4) if vm.has("shell") else DEFAULT_SHELL
    if len(shell) < 2 or not shell[0].startswith("/"):
        raise ConfigError(
            "vm.shell must have two to four items: an absolute path, then the flags after "
            'which the shell takes a command string (for example ["/bin/sh", "-c"])'
        )
    vm.finish()

    setup = top.table("setup")
    setup_run = setup.strings("run", MAX_SETUP_COMMANDS) if setup.has("run") else ()
    setup_network = setup.network(
        "network", DEFAULT_SETUP_NETWORK, policy.max_setup_network, "--max-setup-network"
    )
    setup_timeout = setup.timeout(DEFAULT_TIMEOUT, policy)
    setup.finish()

    run = top.table("run")
    run_network = run.network(
        "network", DEFAULT_RUN_NETWORK, policy.max_run_network, "--max-run-network"
    )
    run_timeout = run.timeout(DEFAULT_TIMEOUT, policy)
    run.finish()

    raw_commands = top.take("commands") if top.has("commands") else {}
    if not isinstance(raw_commands, dict) or not raw_commands:
        raise ConfigError("at least one [commands.<name>] table is required")
    if len(raw_commands) > MAX_COMMANDS:
        raise ConfigError(f"more than {MAX_COMMANDS} commands are defined")
    commands: dict[str, Command] = {}
    for name, body in raw_commands.items():
        if not protocol.is_command_name(name):
            raise ConfigError(f"commands.{name!r} is not a valid command name")
        if not isinstance(body, dict):
            raise ConfigError(f"commands.{name} must be a table")
        table = _Table(body, f"commands.{name}")
        command = Command(
            name,
            table.string("run"),
            table.boolean("allow_args") if table.has("allow_args") else False,
            table.timeout(run_timeout, policy),
        )
        if command.allow_args and ARGS_PLACEHOLDER not in command.run:
            raise ConfigError(
                f"commands.{name}.allow_args is true, but commands.{name}.run does not say "
                f"where the arguments go; write {ARGS_PLACEHOLDER} where they belong"
            )
        table.finish()
        commands[name] = command
    top.finish()

    return RepoConfig(
        image=image,
        cpu=cpu,
        memory_mb=memory,
        boot_timeout_seconds=boot,
        shell=shell,
        setup_run=setup_run,
        setup_network=setup_network,
        setup_timeout_seconds=setup_timeout,
        run_network=run_network,
        commands=commands,
    )
