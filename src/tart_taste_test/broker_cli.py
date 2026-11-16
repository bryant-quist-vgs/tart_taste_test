"""`taste-test-broker`: the operator's side of tart-taste-test.

Built against contract v1 (contracts/taste_test_broker_contract.md).
Commands and flags: docs/interfaces.md §3. Run it outside any sandbox; it is the
only program in this project that starts virtual machines.
"""

import argparse
import contextlib
import os
import shutil
import sys
from pathlib import Path

from tart_taste_test import __version__, doctor, mirror, registry, repo_config, tart
from tart_taste_test.broker import Broker
from tart_taste_test.engine import Engine

DESCRIPTION = """\
Operator commands for tart-taste-test. Agents run `taste-test`; this program is
run by the person who owns the machine, outside any sandbox.

One-time setup for a repository:
  taste-test-broker doctor --image IMAGE     check Tart, Softnet, and the image
  taste-test-broker register --remote-url URL
Then keep a broker running:
  taste-test-broker serve

What runs in the VM is defined by .taste-test.toml on the repository's default
branch. The broker reads that branch from its own mirror of the remote you named
when registering, never from the checkout an agent can write to. The one
exception is a repository registered with --trust-local (see `register --help`).

Stop `serve` with Ctrl-C or SIGTERM. A request in progress then ends as `error`,
and its VM is stopped and deleted before the broker exits."""

EPILOG = """\
exit codes: 0 success (for `serve`: it was stopped, or --once found nothing
left to do); 1 a command failed or refused, or a doctor check failed; 2 bad
arguments.

environment: TASTE_TEST_STATE_DIR overrides the operator state directory
(default: ~/Library/Application Support/tart-taste-test)."""


class Refusal(Exception):
    """The command will not proceed; printed as `Error: ...`, exit code 1."""


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return value


def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="taste-test-broker", description=DESCRIPTION, epilog=EPILOG, formatter_class=raw
    )
    parser.add_argument("--version", action="version", version=f"taste-test-broker {__version__}")
    commands = parser.add_subparsers(dest="action", metavar="COMMAND", required=True)
    modes = repo_config.NETWORK_MODES
    defaults = repo_config.Policy()

    register = commands.add_parser(
        "register",
        help="allow a repository's checkouts to request runs",
        description="Record a repository in the registry. PATH must be inside the main "
        "working tree; its linked worktrees are covered too. The default branch is read "
        "from the remote you name with --remote-url: check that it is the remote you "
        "trust (`git remote get-url origin` shows what the repository itself claims).",
    )
    register.add_argument("path", metavar="PATH", nargs="?", type=Path, default=Path("."))
    register.add_argument("--remote-url", metavar="URL", help="https://, ssh://, or user@host:path")
    register.add_argument(
        "--branch", metavar="NAME", help="default branch (default: the remote's HEAD branch)"
    )
    register.add_argument(
        "--trust-local",
        action="store_true",
        help="read the default branch from the repository's own local branch instead of "
        "a remote; requires --branch. Anything that can commit to that branch can then "
        "change what runs, with the setup network",
    )

    unregister = commands.add_parser(
        "unregister",
        help="remove a repository from the registry and delete its mirror",
    )
    unregister.add_argument("path", metavar="PATH", nargs="?", type=Path, default=Path("."))

    commands.add_parser("list", help="show the registered repositories")

    serve = commands.add_parser(
        "serve",
        help="run the broker: execute requests from registered checkouts, one at a time",
        description="Watch the spool of every registered checkout and execute requests in "
        "VMs. The flags are ceilings no repository config may exceed; a config that asks "
        "for more is rejected, never reduced.",
    )
    serve.add_argument("--once", action="store_true", help="exit when no request is waiting")
    serve.add_argument(
        "--max-setup-network",
        choices=modes,
        default=defaults.max_setup_network,
        help="most open network mode for building environments (default: %(default)s)",
    )
    serve.add_argument(
        "--max-run-network",
        choices=modes,
        default=defaults.max_run_network,
        help="most open network mode while untested code runs (default: %(default)s)",
    )
    serve.add_argument(
        "--max-timeout",
        metavar="SECONDS",
        type=positive_int,
        default=defaults.max_timeout,
        help="longest time limit a config may set (default: %(default)s)",
    )
    serve.add_argument(
        "--max-cpu",
        metavar="N",
        type=positive_int,
        default=defaults.max_cpu,
        help="largest vm.cpu (default: the number of host CPUs)",
    )
    serve.add_argument(
        "--max-memory-mb",
        metavar="N",
        type=positive_int,
        default=defaults.max_memory_mb,
        help="largest vm.memory_mb (default: %(default)s)",
    )
    serve.add_argument(
        "--allow-local-images",
        action="store_true",
        help="let vm.image name a local VM instead of a registry reference",
    )

    check = commands.add_parser(
        "doctor",
        help="check the installation; with --image, boot that image and test it",
        description="Without --image: check that Tart, Softnet, and Git are found and "
        "that operator state is private. With --image: also clone and boot the image "
        "and verify the guest agent, exit-status reporting, standard input, argument "
        "passing, the required tools, and what the VM can reach on the network.",
    )
    check.add_argument("--image", metavar="IMAGE", help="image reference to boot and test")
    check.add_argument(
        "--network",
        choices=modes,
        default=repo_config.DEFAULT_RUN_NETWORK,
        help="network mode to boot it with (default: %(default)s)",
    )

    prune = commands.add_parser(
        "prune",
        help="delete leftover VMs; with --environments, cached environment images too",
        description="Delete this state directory's run VMs, doctor VMs, and unfinished "
        "builds. With --environments, also every cached environment image (the next "
        "request rebuilds it). VMs with any other name are never touched.",
    )
    prune.add_argument(
        "--environments",
        action="store_true",
        help="also delete every cached environment image (rebuilt on the next request)",
    )
    return parser


def main_tree(path: Path) -> Path:
    """The main working tree containing `path`, found without running Git."""
    start = path.resolve()
    if not start.is_dir():
        raise Refusal(f"{path} is not a directory")
    for directory in (start, *start.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return directory
        if dot_git.is_file():
            raise Refusal(
                f"{directory} is a linked worktree. Register the repository's main working "
                "tree instead; that covers every linked worktree"
            )
    raise Refusal(f"{path} is not inside a Git working tree")


def register(state: registry.State, options: argparse.Namespace) -> int:
    root = main_tree(options.path)
    if options.trust_local:
        if options.remote_url:
            raise Refusal("--trust-local and --remote-url cannot be combined")
        if not options.branch:
            raise Refusal("--trust-local requires --branch NAME")
        entry = registry.new_repository(root, options.branch, None)
    else:
        if not options.remote_url:
            raise Refusal(
                "--remote-url URL is required. It is not read from the repository, because "
                "an agent can change the repository's own configuration. Run "
                "`git remote get-url origin` to see its remote, and check it is the one you trust"
            )
        url = registry.check_url(options.remote_url)
        branch = options.branch or mirror.remote_default_branch(url)
        entry = registry.new_repository(root, branch, url)
    with registry.editing(state) as repositories:
        repositories[:] = [r for r in repositories if r.path != entry.path] + [entry]
    print(f"registered {entry.path}")
    if entry.source == "remote":
        print(f"  default branch: {entry.branch} fetched from {entry.url}")
    else:
        print(f"  default branch: local branch {entry.branch} of the repository itself")
        print(
            "  WARNING: anything that can commit to that branch, including an agent working "
            "in the repository, controls what runs and runs it with the setup network."
        )
    print("  merge a .taste-test.toml to that branch, then run: taste-test-broker serve")
    return 0


def unregister(state: registry.State, options: argparse.Namespace) -> int:
    target = str(options.path.resolve())
    with contextlib.suppress(Refusal):
        target = str(main_tree(options.path))
    with registry.editing(state) as repositories:
        removed = [r for r in repositories if r.path == target]
        repositories[:] = [r for r in repositories if r.path != target]
    if not removed:
        raise Refusal(f"{target} is not registered")
    for entry in removed:
        shutil.rmtree(state.mirrors / f"{entry.key}.git", ignore_errors=True)
        print(f"unregistered {entry.path}")
    return 0


def list_repositories(state: registry.State) -> int:
    repositories = registry.load(state)
    if not repositories:
        print("no repositories are registered")
    for entry in repositories:
        source = entry.url if entry.source == "remote" else "local branch (trusted)"
        print(f"{entry.path}\n  branch {entry.branch} from {source}; key {entry.key}")
    return 0


def serve(state: registry.State, options: argparse.Namespace) -> int:
    policy = repo_config.Policy(
        max_setup_network=options.max_setup_network,
        max_run_network=options.max_run_network,
        max_timeout=options.max_timeout,
        max_cpu=options.max_cpu,
        max_memory_mb=options.max_memory_mb,
        allow_local_images=options.allow_local_images,
    )
    with registry.exclusive(state):
        return Broker(state, policy, tart.Tart()).serve(once=options.once)


def run_doctor(state: registry.State, options: argparse.Namespace) -> int:
    vms = tart.Tart()
    if options.image is None:
        return 0 if doctor.run(state, vms, print, None, options.network) else 1
    if repo_config.IMAGE_RE.fullmatch(options.image) is None:
        raise Refusal(f"{options.image!r} is not a valid image reference")
    with registry.exclusive(state):
        return 0 if doctor.run(state, vms, print, options.image, options.network) else 1


def prune(state: registry.State, options: argparse.Namespace) -> int:
    with registry.exclusive(state):
        engine = Engine(tart.Tart(), state, warn=lambda message: print(f"warning: {message}"))
        doomed = engine.cleanup_transient()
        if options.environments:
            doomed += [name for name in engine.environments() if engine.discard(name)]
        for leftover in doctor.staging_leftovers(state):
            with contextlib.suppress(OSError):
                os.unlink(leftover)
    for name in doomed:
        print(f"deleted {name}")
    if not doomed:
        print("nothing to delete")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0, 1, or 2 (docs/interfaces.md §2)."""
    options = build_parser().parse_args(argv)
    try:
        state = registry.state_from_environment()
        if options.action == "register":
            return register(state, options)
        if options.action == "unregister":
            return unregister(state, options)
        if options.action == "list":
            return list_repositories(state)
        if options.action == "serve":
            return serve(state, options)
        if options.action == "doctor":
            return run_doctor(state, options)
        return prune(state, options)
    except (Refusal, registry.RegistryError, mirror.MirrorError, tart.TartError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        # For example an operator state directory that cannot be created or read.
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
