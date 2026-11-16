"""repo_config_test.py — `.taste-test.toml` and the operator's ceilings (interfaces.md §4).

Coverage:
  - a complete file parses to the documented values; a minimal one gets defaults
  - unknown keys and tables are errors, so a misspelling cannot fall back silently
  - Load-bearing: values above a ceiling are rejected, never reduced, including
    defaults; the run network ceiling defaults to `isolated`
  - Load-bearing: vm.image cannot name a local VM or one of this project's VMs
  - type, range, and size errors name the key
"""

import pytest

from tart_taste_test import repo_config
from tart_taste_test.repo_config import ConfigError, Policy, parse

MINIMAL = b"""
schema = 1
[vm]
image = "ghcr.io/org/image:tag"
[commands.test]
run = "make test"
"""

FULL = b"""
schema = 1

[vm]
image = "ghcr.io/cirruslabs/ubuntu:latest"
cpu = 2
memory_mb = 4096
boot_timeout_seconds = 60
shell = ["/bin/bash", "-lc"]

[setup]
run = ["./bootstrap.sh", "make deps"]
network = "internet"
timeout_seconds = 600

[run]
network = "isolated"
timeout_seconds = 300

[commands.test]
run = 'make test "$@"'
allow_args = true
timeout_seconds = 120

[commands.lint]
run = "make lint"
"""

OPEN = Policy(max_setup_network="shared", max_run_network="shared", max_cpu=64)


def variant(old: str, new: str, base: bytes = FULL) -> bytes:
    text = base.decode()
    assert old in text
    return text.replace(old, new).encode()


def test_full_config() -> None:
    config = parse(FULL, OPEN)
    assert config.image == "ghcr.io/cirruslabs/ubuntu:latest"
    assert (config.cpu, config.memory_mb, config.boot_timeout_seconds) == (2, 4096, 60)
    assert config.shell == ("/bin/bash", "-lc")
    assert config.setup_run == ("./bootstrap.sh", "make deps")
    assert (config.setup_network, config.setup_timeout_seconds) == ("internet", 600)
    assert config.run_network == "isolated"
    assert config.commands["test"] == repo_config.Command("test", 'make test "$@"', True, 120)
    assert config.commands["lint"] == repo_config.Command("lint", "make lint", False, 300)


def test_minimal_config_gets_the_documented_defaults() -> None:
    config = parse(MINIMAL, Policy())
    assert (config.cpu, config.memory_mb) == (None, None)
    assert config.boot_timeout_seconds == 180
    assert config.shell == ("/bin/sh", "-lc")
    assert config.setup_run == ()
    assert (config.setup_network, config.run_network) == ("internet", "isolated")
    assert config.setup_timeout_seconds == 1800
    assert config.commands["test"].timeout_seconds == 1800
    assert config.commands["test"].allow_args is False


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (variant("[run]", "[rnu]"), "unknown key rnu"),
        (variant('network = "isolated"', 'netwrok = "isolated"'), "unknown key run.netwrok"),
        (variant("allow_args = true", "allow_arg = true"), "unknown key commands.test.allow_arg"),
        (variant("cpu = 2", "cpus = 2"), "unknown key vm.cpus"),
        (FULL + b"\n[extra]\nx = 1\n", "unknown key extra"),
    ],
)
def test_unknown_keys_are_errors(data: bytes, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(data, OPEN)


# Load-bearing: nothing above a ceiling is accepted or silently reduced.
@pytest.mark.parametrize(
    ("data", "policy", "message"),
    [
        (
            variant('network = "isolated"', 'network = "internet"'),
            Policy(),
            r"run.network is 'internet'; the --max-run-network ceiling is 'isolated'",
        ),
        (
            variant('network = "isolated"', 'network = "shared"'),
            Policy(max_run_network="internet"),
            r"run.network is 'shared'; the --max-run-network ceiling is 'internet'",
        ),
        (
            variant('network = "internet"', 'network = "shared"'),
            Policy(),
            r"setup.network is 'shared'; the --max-setup-network ceiling is 'internet'",
        ),
        (
            MINIMAL,
            Policy(max_setup_network="isolated"),
            r"setup.network is 'internet' \(the default\); the --max-setup-network ceiling",
        ),
        (
            variant("timeout_seconds = 600", "timeout_seconds = 7201"),
            Policy(),
            r"setup.timeout_seconds is 7201; the --max-timeout ceiling is 7200",
        ),
        (
            variant("timeout_seconds = 120", "timeout_seconds = 700"),
            Policy(max_timeout=600),
            r"commands.test.timeout_seconds is 700; the --max-timeout ceiling is 600",
        ),
        (
            MINIMAL,
            Policy(max_timeout=100),
            r"setup.timeout_seconds is 1800 \(the default\); the --max-timeout ceiling is 100",
        ),
        (
            variant("cpu = 2", "cpu = 9"),
            Policy(max_cpu=8),
            r"vm.cpu is 9; the --max-cpu ceiling is 8",
        ),
        (
            variant("memory_mb = 4096", "memory_mb = 32768"),
            Policy(),
            r"vm.memory_mb is 32768; the --max-memory-mb ceiling is 16384",
        ),
    ],
)
def test_values_above_a_ceiling_are_rejected(data: bytes, policy: Policy, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(data, policy)


def test_run_network_ceiling_defaults_to_isolated() -> None:
    assert Policy().max_run_network == "isolated"
    assert Policy().max_setup_network == "internet"
    assert (
        parse(variant('network = "isolated"', 'network = "shared"'), OPEN).run_network == "shared"
    )


# Load-bearing: a config cannot run the candidate inside someone else's VM.
@pytest.mark.parametrize(
    "image",
    [
        "tastetest-1a2b3c4d-env-0123456789ab-ba9876543210",
        "tastetest-anything/else:1",
        "my-personal-vm",
        "sequoia",
    ],
)
def test_local_and_own_vm_names_are_not_images(image: str) -> None:
    data = variant("ghcr.io/cirruslabs/ubuntu:latest", image)
    with pytest.raises(ConfigError, match=r"vm\.image"):
        parse(data, OPEN)


def test_local_images_need_the_operator_flag_and_never_include_own_vms() -> None:
    data = variant("ghcr.io/cirruslabs/ubuntu:latest", "my-personal-vm")
    allowed = Policy(allow_local_images=True)
    assert parse(data, allowed).image == "my-personal-vm"
    # Whatever the letter case: the VM store's file system may not tell them apart.
    for name in ("tastetest-x-env-a-b", "TasteTest-x-env-a-b", "TASTETEST-x"):
        own = variant("ghcr.io/cirruslabs/ubuntu:latest", name)
        with pytest.raises(ConfigError, match=r"must not name a tastetest-\* VM"):
            parse(own, allowed)


# Load-bearing: the config says where request arguments go; nothing is appended.
def test_a_command_that_accepts_arguments_must_say_where_they_go() -> None:
    silent = variant("run = 'make test \"$@\"'", 'run = "make test"')
    with pytest.raises(ConfigError, match=r"allow_args is true, but commands\.test\.run does not"):
        parse(silent, OPEN)
    wrapped = variant("run = 'make test \"$@\"'", "run = 'make test \"$@\" | tee log'")
    assert parse(wrapped, OPEN).commands["test"].run == 'make test "$@" | tee log'
    # Without allow_args no placeholder is needed, and none is added.
    assert parse(MINIMAL, OPEN).commands["test"] == repo_config.Command(
        "test", "make test", False, 1800
    )


@pytest.mark.parametrize("image", ["-rf/x", "--dir=/:rw/x", "a b/c", "a/b;c", "a/$(x)", ""])
def test_image_references_that_could_be_options_or_shell_are_refused(image: str) -> None:
    with pytest.raises(ConfigError, match=r"vm\.image"):
        parse(variant("ghcr.io/cirruslabs/ubuntu:latest", image), OPEN)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "schema is required"),
        (b"schema = 2\n", "schema is 2"),
        (b"schema = true\n", "schema must be an integer"),
        (b"not toml [", "not valid TOML"),
        (b"\xff\xfe", "not valid TOML"),
        (
            variant("[vm]", "[vm]\n", MINIMAL).replace(b'image = "ghcr.io/org/image:tag"\n', b""),
            "vm.image is required",
        ),
        (
            variant("run = 'make test \"$@\"'\nallow_args", 'run = ""\nallow_args'),
            "commands.test.run must not be empty",
        ),
        (variant('run = "make lint"', "run = 5"), "commands.lint.run must be a string"),
        (variant("allow_args = true", 'allow_args = "yes"'), "allow_args must be true or false"),
        (variant("cpu = 2", "cpu = 0"), "vm.cpu is 0; the minimum is 1"),
        (variant("cpu = 2", "cpu = 2.5"), "vm.cpu must be an integer"),
        (variant("memory_mb = 4096", "memory_mb = 100"), "the minimum is 512"),
        (variant("boot_timeout_seconds = 60", "boot_timeout_seconds = 5"), "the minimum is 10"),
        (variant("boot_timeout_seconds = 60", "boot_timeout_seconds = 901"), "the maximum is 900"),
        (variant('shell = ["/bin/bash", "-lc"]', 'shell = ["bash", "-lc"]'), "absolute path"),
        (variant('shell = ["/bin/bash", "-lc"]', "shell = []"), "two to four items"),
        (variant('shell = ["/bin/bash", "-lc"]', 'shell = ["/bin/bash"]'), "two to four items"),
        (variant('shell = ["/bin/bash", "-lc"]', 'shell = ["/a","b","c","d","e"]'), "maximum is 4"),
        (variant('run = ["./bootstrap.sh", "make deps"]', 'run = "make deps"'), "array of strings"),
        (variant('network = "isolated"', 'network = "none"'), "must be one of isolated, internet"),
        (variant("[commands.lint]", "[commands.Lint]"), "not a valid command name"),
        (variant("[commands.lint]", '[commands."make all"]'), "not a valid command name"),
        (variant("timeout_seconds = 300", "timeout_seconds = 0"), "the minimum is 1"),
    ],
)
def test_invalid_configs_name_the_problem(data: bytes, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(data, OPEN)


def test_at_least_one_command_is_required() -> None:
    data = b'schema = 1\n[vm]\nimage = "ghcr.io/org/image:tag"\n'
    with pytest.raises(ConfigError, match="at least one"):
        parse(data, Policy())


def test_size_limits() -> None:
    with pytest.raises(ConfigError, match="larger than 65536 bytes"):
        parse(b"#" * 65537, Policy())
    long_command = variant('run = "make lint"', f'run = "{"x" * 4097}"')
    with pytest.raises(ConfigError, match="longer than 4096 bytes"):
        parse(long_command, OPEN)
    many = MINIMAL + b"".join(f'[commands.c{n}]\nrun = "x"\n'.encode() for n in range(32))
    with pytest.raises(ConfigError, match="more than 32 commands"):
        parse(many, Policy())


def test_network_modes_are_ordered_from_most_to_least_restricted() -> None:
    assert repo_config.NETWORK_MODES == ("isolated", "internet", "shared")
    assert repo_config.network_rank("isolated") < repo_config.network_rank("shared")
