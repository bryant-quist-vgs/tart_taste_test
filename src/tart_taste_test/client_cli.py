"""`taste-test`: the command an agent runs to have its code tested in a VM.

Built against contract v1 (contracts/taste_test_contract.md).
Commands, flags, output streams, and exit codes: docs/interfaces.md §1 and §2.
"""

import argparse
import sys
from pathlib import Path
from typing import BinaryIO

from tart_taste_test import __version__, client, client_spool, exitcodes, protocol
from tart_taste_test.exitcodes import Exit, help_epilog

DESCRIPTION = """\
Run this checkout's tests inside a throwaway virtual machine instead of on this
machine. The working tree is tested as it is now (tracked files plus untracked
files that are not ignored); no commit is needed.

What is run is defined by the repository's .taste-test.toml on its default
branch. You choose a command by name (default: test); you cannot pass a shell
command.

Do not pipe the output through head or tail: that replaces this command's exit
code with theirs. Use --head N and --tail N instead. When there is a result,
the stored output is kept in the log file named at the end of the last line of
standard error (stored output stops at 64 MiB; the line then says
truncated=yes)."""

EXAMPLES = """\
examples:
  taste-test run --tail 100         run `test`, print the last 100 lines
  taste-test run lint --head 20 --tail 20
  taste-test run -- -k test_parser  pass arguments, if the command allows them
  taste-test run --detach           submit, print the request ID, exit 8
  taste-test wait ID --tail 100     re-attach after --detach or exit code 8
  taste-test log ID --head 50       print stored output again, other limits
  taste-test status                 is a broker serving this checkout?
"""


class SafeOut:
    """Standard output that survives a closed pipe.

    If the reader goes away, or the stream cannot be written for any other
    reason, the client keeps going and still exits with the code for the
    outcome; it only stops printing.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._closed = False

    def write(self, data: bytes) -> int:
        if not self._closed:
            try:
                return self._stream.write(data)
            except OSError, ValueError:
                self._close()
        return len(data)

    def flush(self) -> None:
        if not self._closed:
            try:
                self._stream.flush()
            except OSError, ValueError:
                self._close()

    def _close(self) -> None:
        self._closed = True
        exitcodes.silence(sys.stdout)


def settle_streams() -> None:
    """Flush both standard streams; one that cannot be flushed is silenced.

    Without this, a reader that closed its end (`| head`, `2>&1 | head`) would
    make the interpreter's own flush at exit fail and replace the exit code.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except OSError, ValueError:
            exitcodes.silence(stream)


def non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return value


def positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def command_name(text: str) -> str:
    if not protocol.is_command_name(text):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a command name (a name from .taste-test.toml, such as `test`; "
            "to pass arguments use `--` first)"
        )
    return text


def request_id(text: str) -> str:
    if not protocol.is_request_id(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a request ID")
    return text


def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="taste-test",
        description=DESCRIPTION,
        epilog=EXAMPLES + "\n" + help_epilog(),
        formatter_class=raw,
    )
    parser.add_argument("--version", action="version", version=f"taste-test {__version__}")
    commands = parser.add_subparsers(dest="action", metavar="COMMAND", required=True)

    checkout = argparse.ArgumentParser(add_help=False)
    checkout.add_argument(
        "--checkout",
        metavar="PATH",
        type=Path,
        default=Path("."),
        help="a directory inside the checkout to act on (default: current directory)",
    )
    limits = argparse.ArgumentParser(add_help=False)
    limits.add_argument(
        "--head", metavar="N", type=non_negative_int, help="print only the first N lines"
    )
    limits.add_argument(
        "--tail", metavar="N", type=non_negative_int, help="print only the last N lines"
    )
    waiting = argparse.ArgumentParser(add_help=False)
    waiting.add_argument("--quiet", action="store_true", help="no progress lines on standard error")
    waiting.add_argument(
        "--wait-timeout",
        metavar="SECONDS",
        type=positive_seconds,
        default=3600.0,
        help="stop waiting after this long and exit 8; the request keeps running. A request "
        "no broker has acknowledged by then is withdrawn instead (exit 6). Default: 3600",
    )

    run = commands.add_parser(
        "run",
        parents=[limits, waiting, checkout],
        help="test the working tree in a VM and wait for the result",
        description="Snapshot the working tree, have a named command run against it in a\n"
        "VM, print the command's output, and exit with the code for the outcome.",
        epilog=help_epilog(),
        formatter_class=raw,
        usage="taste-test run [COMMAND] [--head N] [--tail N] [--quiet] [--detach]\n"
        "                      [--wait-timeout SECONDS] [--checkout PATH] [-- ARG...]",
    )
    run.add_argument(
        "command",
        metavar="COMMAND",
        nargs="?",
        type=command_name,
        default=protocol.DEFAULT_COMMAND,
        help="name of a command defined in .taste-test.toml (default: test)",
    )
    run.add_argument(
        "--detach",
        action="store_true",
        help="print the request ID once a broker accepts the request and exit 8 "
        "(pending) without waiting for the result",
    )

    wait = commands.add_parser(
        "wait",
        parents=[limits, waiting, checkout],
        help="re-attach to a request submitted earlier",
        description="Wait for a request and report it exactly as `run` would.",
        epilog=help_epilog(),
        formatter_class=raw,
    )
    wait.add_argument("id", metavar="ID", type=request_id)

    log = commands.add_parser(
        "log",
        parents=[limits, checkout],
        help="print the stored output of a request",
        description="Print a request's stored output without waiting. Exit 0, or 5 if "
        "the request or its log does not exist.",
    )
    log.add_argument("id", metavar="ID", type=request_id)
    log.add_argument("--setup", action="store_true", help="print the environment-build log instead")

    status = commands.add_parser(
        "status",
        parents=[checkout],
        help="show a request's state, or whether a broker is serving this checkout",
        description="With an ID: print that request's state (exit 0, or 5 if unknown). "
        "Without: say whether a broker is serving this checkout and list its requests "
        "(exit 0 when one is, 6 when none is).",
    )
    status.add_argument("id", metavar="ID", type=request_id, nargs="?")

    cancel = commands.add_parser(
        "cancel",
        parents=[checkout],
        help="ask the broker to stop a request",
        description="Ask the broker to stop a queued or running request. Exit 0, or 5 if "
        "the request does not exist.",
    )
    cancel.add_argument("id", metavar="ID", type=request_id)
    return parser


def split_arguments(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split the command line at the first `--`; what follows is passed to the command."""
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1 :]
    return argv, []


def dispatch(options: argparse.Namespace, extra: list[str], pace: client.Timing) -> int:
    """Run the chosen command and return its exit code."""
    console = client.Console(
        SafeOut(sys.stdout.buffer),  # type: ignore[arg-type]
        sys.stderr,
        quiet=getattr(options, "quiet", False),
    )
    try:
        paths = client_spool.resolve_checkout(options.checkout)
        if options.action == "run":
            return client.run(
                paths,
                options.command,
                extra,
                console,
                client.Limits(options.head, options.tail),
                options.wait_timeout,
                options.detach,
                pace,
            )
        if options.action == "wait":
            limits = client.Limits(options.head, options.tail)
            return client.wait(paths, options.id, console, limits, options.wait_timeout, pace)
        if options.action == "log":
            limits = client.Limits(options.head, options.tail)
            return client.log(paths, options.id, console, limits, options.setup)
        if options.action == "status":
            return client.status(paths, options.id, console, pace)
        return client.cancel(paths, options.id, console)
    except client_spool.ClientError as error:
        console.say(f"error: {error.message}")
        return int(error.code)
    except KeyboardInterrupt:
        console.say("error: interrupted")
        return int(Exit.ERROR)
    except Exception as error:
        # Never let a traceback choose the exit code: Python's would be 1 (`failed`).
        console.say(f"error: internal error ({type(error).__name__}: {error})")
        return int(Exit.ERROR)


def main(argv: list[str] | None = None, timing: client.Timing | None = None) -> int:
    """Entry point. Returns the process exit code (docs/interfaces.md §2)."""
    try:
        own, extra = split_arguments(list(sys.argv[1:] if argv is None else argv))
        parser = build_parser()
        options = parser.parse_args(own)
        if extra and options.action != "run":
            parser.error("arguments after `--` are only accepted by `run`")
        try:
            protocol.check_args(extra)
        except protocol.ProtocolError as error:
            parser.error(str(error))
        return int(dispatch(options, extra, timing if timing is not None else client.Timing()))
    finally:
        settle_streams()


if __name__ == "__main__":
    sys.exit(main())
