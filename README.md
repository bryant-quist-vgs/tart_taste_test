# tart-taste-test

Run a repository's tests inside a throwaway [Tart](https://tart.run) virtual
machine, on request from a sandboxed coding agent, before the code is trusted on
your Mac.

An agent that has just written code needs test results before review, but
running unreviewed code on the host is what review is supposed to prevent.
`taste-test` breaks that circle: the agent asks for a run, the code is tested in
a VM whose environment was built from the repository's reviewed default branch,
and the output and one unambiguous exit code come back.

```console
$ taste-test run --tail 5
taste-test: submitted 20261115T183000Z-3f9a1c2b4d5e (command test)
taste-test: queued: next
taste-test: preparing: staging the snapshot
taste-test: preparing: refreshing the default branch
taste-test: preparing: starting the run VM
taste-test: running: test
[taste-test: 1417 lines omitted]
FAILED tests/test_parser.py::test_nested - AssertionError
1 failed, 212 passed in 41.07s
taste-test: outcome=failed exit=1 command-exit=1 id=20261115T183000Z-3f9a1c2b4d5e duration=83s lines=1422 shown=last-5 log=.taste-test/spool/results/20261115T183000Z-3f9a1c2b4d5e/output.log
$ echo $?
1
```

> **Status.** The two programs are written and their Python test suite and
> lint gates pass, but the suite replaces Tart with a stand-in: nothing has yet
> been run against a real virtual machine. The install script's own shell tests
> have not been executed either, so `make test` and `make commit` have never
> run as a whole, and CI has not run. Run `make commit`, and then
> `taste-test-broker doctor --image IMAGE`, before relying on it; what remains
> unverified is listed in [docs/open-questions.md](docs/open-questions.md).

## How it works

There are two programs, split by who may run them.

| Program | Run by | Does |
|---|---|---|
| `taste-test` | the agent, inside its sandbox | Snapshots the working tree, files a request, waits, prints output, exits with a verdict. Never starts a VM and never leaves the checkout. |
| `taste-test-broker` | you, outside any sandbox | Watches registered checkouts, runs each request in a VM, writes the result back. The only program that runs Tart. |

They exchange plain files in `.taste-test/` inside the checkout, the one place a
sandboxed agent can always write, so the agent needs no socket, no port, and no
permission it does not already have. After a one-time setup, no operator action
is needed per test run.

What makes it safe by default:

- **Untested code runs only in a VM.** Nothing of the host is attached to it: no
  directory, disk, port, USB device, or clipboard.
- **The agent chooses a command by name, never a command line.** What `test`
  means, which image is used, and what network the VM gets come from
  `.taste-test.toml` on the repository's default branch, read through the
  broker's own mirror of the remote. Code under test cannot redefine them.
- **Untested code gets the most restricted network mode** unless you allow
  more (`--max-run-network`). That mode allows no IPv4 destination. Whether it
  also keeps the VM from the host's side of the VM bridge and from IPv6 has not
  been verified ([open question Q2](docs/open-questions.md#q2-what-can-a-vm-reach-in-each-network-mode));
  `doctor --image` reports what a VM can actually reach on your Mac.
- **The exit code is a verdict, not a pass-through.** `0` means the tests
  passed and nothing else; an infrastructure problem can never look like a pass
  or a test failure.
- **It fails closed.** If the broker cannot observe exit statuses, cannot give
  the VM the requested network mode, or cannot validate something, the result is
  an error, never a pass.

The full design is in [docs/design.md](docs/design.md).

## Requirements

- macOS on Apple silicon.
- [Tart](https://tart.run) 2.41 or newer, and Git.
- [Softnet](https://github.com/cirruslabs/softnet) for the restricted network
  modes (the default).
- [uv](https://docs.astral.sh/uv/) to install; it provides Python 3.14.
- A VM image containing the Tart Guest Agent, `/bin/sh`, `tar`, and `xargs`,
  and that sets `HOME` for commands run through the guest agent
  ([details](docs/interfaces.md#4-repository-config-taste-testtoml)).

### Images known to work

None yet: no image has been checked on a real host. An image belongs here once
`taste-test-broker doctor --image IMAGE` prints no `FAIL` line for it.

## Install

`taste-test-broker` runs with your authority, outside any sandbox. Install it
from a checkout that **no agent can write**, at a commit you have reviewed: a
separate clone kept for installing is the simple way. The installer, and the
source it installs, are whatever that checkout holds.

```bash
bash taste_test_install.sh
```

```bash
bash taste_test_install.sh --check
```

The installer delegates to `uv tool install`, which puts `taste-test` and
`taste-test-broker` in uv's tool bin directory; if that directory is not on
your `PATH`, the installer says how to add it. It refuses to install when Git
reports uncommitted or untracked changes in the checkout (`--allow-dirty`
overrides that). This catches forgotten edits; it is not a defence against an
agent that can write the checkout, which is why the checkout must be out of
agents' reach. It never uses `sudo` and does not install Tart, Softnet, or
images. Re-run it to upgrade; `--uninstall` removes the tool; `--dry-run` shows
the command without running it. `--editable` is for developing this project
only: it makes the installed commands run the checkout's source directly. Exit
codes: `0` success, `1` refusal or failure, `2` usage error. Its full behaviour
is in [taste_test_install_contract.md](taste_test_install_contract.md).

## Operator setup

1. Check the installation and an image. This boots the image once and tests
   everything the broker depends on, including what the VM can reach on the
   network:

   ```bash
   taste-test-broker doctor --image ghcr.io/cirruslabs/ubuntu:latest
   ```

2. Add a `.taste-test.toml` to the repository and merge it to the default
   branch ([example below](#repository-config)).

3. Register the repository, from its main working tree. Name the remote
   yourself; it is deliberately not read from the repository, because an agent
   can change the repository's own settings:

   ```bash
   taste-test-broker register --remote-url git@github.com:your-org/your-repo.git
   ```

4. Keep a broker running in a terminal outside any sandbox:

   ```bash
   taste-test-broker serve
   ```

`serve` takes the limits no repository may exceed. The defaults are the safe
ones:

| Flag | Default | Meaning |
|---|---|---|
| `--max-run-network` | `isolated` | Most open network while untested code runs. |
| `--max-setup-network` | `internet` | Most open network while an environment is built from the default branch. |
| `--max-timeout` | `7200` | Longest time limit, in seconds. |
| `--max-cpu`, `--max-memory-mb` | host CPUs, `16384` | Largest VM. |
| `--allow-local-images` | off | Allow `vm.image` to name a local VM. |

A config that asks for more is rejected with a reason; nothing is silently
reduced. All commands and flags:
[docs/interfaces.md §3](docs/interfaces.md#3-taste-test-broker-operator-side).

Stop the broker with Ctrl-C. A request that is running ends with an error (no
verdict), and its VM is stopped and deleted before the broker exits.

Two directories must be out of your agents' reach, which is for the sandbox you
give them to enforce: the broker's own state
(`~/Library/Application Support/tart-taste-test`) and the directory where Tart
keeps VMs (`~/.tart`, or `TART_HOME`). Test environments are cached there as
VMs, so anything that can write it can change what later tests run in.
`taste-test-broker doctor` prints both locations and checks who can write them.

For a repository with no remote, `register --trust-local --branch main` reads
the default branch from the repository itself. That is weaker: anything that can
commit to that branch, including an agent working in the repository, then
controls what runs.

## Using it from an agent

```bash
taste-test run --tail 100
```

- `taste-test run [COMMAND]` tests the working tree as it is now: tracked files
  plus untracked files that are not ignored. No commit is needed. `COMMAND`
  defaults to `test`.
- `--head N` and `--tail N` limit what is printed. **Do not pipe the output
  through `head` or `tail`**: a pipeline's exit status is that of its last
  command, which hides the verdict. The output is kept in the log file named
  on the last line of standard error, up to 64 MiB; beyond that it is cut and
  the summary says so.
- `taste-test run -- -k test_parser` passes arguments to the command, if the
  repository's config allows arguments for it.
- `taste-test run --detach` returns as soon as a broker has acknowledged the
  request, with the request ID (exit code 8), or with exit code 6 if none does;
  `taste-test wait ID` collects the result later. Use this when a run may
  outlast your tool's own time limit.
- `taste-test log ID --head 50`, `taste-test status`, and `taste-test cancel ID`
  do what they say.

Exit codes of `run` and `wait`, word for word from
[docs/interfaces.md §2](docs/interfaces.md#2-exit-codes) (also in
`taste-test --help`):

| Code | Name | Meaning |
|---:|---|---|
| 0 | `passed` | The command ran in the VM and exited 0. |
| 1 | `failed` | The command ran in the VM and exited non-zero. |
| 2 | `usage` | Bad arguments. Nothing was submitted. |
| 3 | `setup-failed` | The environment could not be built from the default branch. The candidate was never run. |
| 4 | `timed-out` | The command exceeded its time limit and was stopped. |
| 5 | `error` | Infrastructure failure: the VM did not start, Tart failed, or the broker stopped responding. No verdict. |
| 6 | `not-run` | Nothing was run: no broker took the request (none is serving this checkout, or one is but did not take the request in time), or the broker rejected the request (no repository config on the default branch, over a limit, unknown command). |
| 7 | `cancelled` | The request was cancelled before it finished. No verdict. |
| 8 | `pending` | The request is still queued or running: --wait-timeout expired, or --detach was given. |

A paragraph you can put in a repository's agent instructions:

```text
Before asking for review, run the tests with `taste-test run --tail 100`. It
tests your working tree in an isolated VM; do not run the test suite directly.
Exit 0 means the tests passed and 1 means they failed (read the output). Any
other code means there is no verdict: report the `reason:` line instead of
treating it as a pass or a failure. Never pipe taste-test through head or tail;
use --head/--tail.
```

### What the agent's sandbox must allow

Only these: executing `taste-test`; running Git read-only in the checkout
(`git rev-parse` and `git ls-files`); reading the working tree; and creating,
renaming, and deleting files under `.taste-test/` in the checkout. No network,
and nothing outside the checkout. The exact commands are in the client's
[contract](contracts/taste_test_contract.md). Whether your organisation's
sandbox policy permits this has not been tested here; `taste-test status` from
an agent session is the quickest check.

## Repository config

`.taste-test.toml`, at the root of the default branch:

```toml
schema = 1

[vm]
image = "ghcr.io/cirruslabs/ubuntu:latest"

[setup]
# Runs once per default-branch commit, on the default branch's source, with
# the setup network. The result is cached and cloned for every request.
run = ["./scripts/bootstrap.sh"]

[commands.test]
# "$@" is where arguments given after `taste-test run test --` go.
run = 'uv run pytest "$@"'
allow_args = true

[commands.lint]
run = "make lint"
```

Every key, default, and limit:
[docs/interfaces.md §4](docs/interfaces.md#4-repository-config-taste-testtoml).

Things worth knowing:

- The config is read from the default branch. Editing it in your working tree
  changes nothing until the edit is merged.
- The environment is built from the default branch too, so a branch that adds a
  dependency is tested without it. To install the branch's own dependencies,
  put that step in the command (`uv sync --locked && pytest`) and ask for a run
  network that permits it, which the operator must allow.
- A command that sets `allow_args = true` must contain `"$@"`, in double quotes,
  where the arguments belong. Nothing is appended for you, so a command like
  `make build && ./run-tests "$@"` gets its arguments in the right place, and a
  config that would drop them is rejected.
- The verdict is the exit status of the whole command string. Do not pipe the
  test command into another program (`./run-tests | tee log` exits 0 when the
  tests fail); the output is captured for you.
- Untracked files that are not ignored travel into the VM. Keep local secret
  files in `.gitignore`.

## What it does not do

- It does not review code. A passing run means the configured command exited 0
  on that working tree; the working tree may have changed its own tests.
- It does not defend against an escape from the hypervisor.
- It does not give the VM any credentials.

More in [docs/out-of-scope.md](docs/out-of-scope.md); planned work is in
[docs/deferred-work.md](docs/deferred-work.md).

## Development

```bash
make install-linters
```

```bash
make commit
```

`make commit` is the pre-commit gate and the only thing CI runs: `format-check`,
`lint` (ruff, mypy, shellcheck), `docs-lint` (glossarian), `test` (pytest, then
the install script's shell tests), and `llm-size-guard`. `make install-linters`
installs the two documentation linters, glossarian and llm-size-guard, at the
commits pinned in the Makefile. The tests never start a VM; they put a stand-in
for `tart` on PATH. This repository's own `.taste-test.toml` runs the Python
suite and, as separate commands, the linters and the installer's shell tests;
its comments say which gates a VM run does not cover.

| Path | Contents |
|---|---|
| `src/tart_taste_test/` | The package: the client, the broker, and their modules. |
| `tests/` | Tests named `<subject>_test.py`; shared fixtures in `conftest.py` and `client_helpers.py`; `fake_tart.py`, the stand-in for Tart. |
| `contracts/` | What each program is allowed to do. |
| `docs/` | Design, interfaces, protocol, VM handling, glossary, decisions, open questions, scope. |
| `taste_test_install.sh` | The installer, with its contract and tests beside it. |

Start with [docs/design.md](docs/design.md); its document map says where
everything else lives.
