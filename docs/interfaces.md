---
id: interfaces
title: Command-line interfaces and repository config
is_source_of_truth: true
derives_from: [design, glossary]
status: current
authority: >
  Canonical home for everything a caller types or parses: the commands of both
  executables, their exit codes, their output streams, and the repository config
  file. Code and other documents quote these values and cite this file.
---

# Command-line interfaces and repository config

Two executables are installed. They are separate on purpose: `taste-test` is the
only one an [agent](glossary.md#agent) runs, and `taste-test-broker` is the only
one that starts virtual machines. See
[decision D2](decisions.md#d2-two-executables-split-by-trust).

The file formats the two exchange are specified in [protocol.md](protocol.md).
Behaviour against a real virtual machine has not been observed yet; see
[open-questions.md](open-questions.md).

## 1. `taste-test` (agent side)

```text
taste-test run     [COMMAND] [--head N] [--tail N] [--quiet] [--detach]
                   [--wait-timeout SECONDS] [--checkout PATH] [-- ARG...]
taste-test wait    ID [--head N] [--tail N] [--quiet] [--wait-timeout SECONDS] [--checkout PATH]
taste-test log     ID [--head N] [--tail N] [--setup] [--checkout PATH]
taste-test status  [ID] [--checkout PATH]
taste-test cancel  ID [--checkout PATH]
taste-test --version | --help
```

| Command | What it does |
|---|---|
| `run` | Snapshots the [candidate](glossary.md#candidate), submits a [request](glossary.md#request), waits for the [outcome](glossary.md#outcome), prints the command's output, and exits with the code in §2. |
| `wait` | Re-attaches to a request submitted earlier (after `--detach`, or after exit code 8). Same output and exit codes as `run`. |
| `log` | Prints the stored output of a request without waiting. `--setup` prints the environment-build log instead. |
| `status` | With an ID, prints that request's state. Without one, prints whether a [broker](glossary.md#broker) is serving this [checkout](glossary.md#checkout) and lists its requests. |
| `cancel` | Asks the broker to stop a queued or running request. |

Arguments:

- `COMMAND` is the name of a [named command](glossary.md#named-command) in the
  [repository config](glossary.md#repository-config). Default: `test`. It is a
  name, never a shell string.
- `ARG...` after `--` are passed to the named command, and only when that
  command sets `allow_args = true`; otherwise the request is rejected. An
  argument may not contain a control character.
- `--head N` and `--tail N` (non-negative integers) limit the printed output to
  its first `N` and last `N` lines. Both may be given. They never change the exit
  code and never change what is stored: the stored output stays in the log file
  named on the summary line (the broker stores at most the log limit in
  [protocol.md §2](protocol.md#2-identifiers-and-limits); the summary line then
  says `truncated=yes`). When lines are left out, one marker line is printed in
  their place: `[taste-test: 1234 lines omitted]` (`1 line omitted` for one).
- `--quiet` suppresses progress lines on standard error. The reason and summary
  lines are still printed.
- `--detach` submits, waits only until a broker acknowledges the request, prints
  the request ID on standard output, and exits 8 (`pending`): the request is
  running and has no verdict yet. Collect it with `taste-test wait ID`. A
  request that has already finished by then is reported in full instead, as
  without `--detach`.
- `--wait-timeout SECONDS` (default `3600`) bounds how long `run` and `wait`
  block. On expiry the request keeps running and the exit code is 8. A request
  that no broker has acknowledged by then is withdrawn instead, and the exit
  code is 6: an unanswered request is never left behind to run later.
- `--checkout PATH` names a directory inside the checkout to act on. Default: the
  current directory.

### Output streams

- **Standard output** carries only the command's own output: its standard
  output and standard error as one stream, in the order the
  [guest](glossary.md#guest) produced them, after `--head`/`--tail`. With
  `--detach` it carries only the request ID (or the output, when the request
  has already finished). When the outcome is `setup-failed` it carries the
  setup log instead, under the same limits. Without `--head` and `--tail` the
  output is printed while the command runs; with either, it is printed when the
  request finishes. `status` prints its report here.
- **Standard error** carries progress lines, the reason line, and the summary
  line, each prefixed `taste-test: ` and each made printable (control characters
  are replaced by spaces, and runs of spaces become one). `log`, `status`, and
  `cancel` report a failure as one line `taste-test: error: <text>`, and
  `cancel` confirms with `taste-test: cancellation requested for <id>`.

Neither stream can change the exit code. If the reader of standard output or
standard error goes away (`taste-test run | head`, `2>&1 | head`), or a stream
cannot be written for any other reason, the client stops printing to it and
still exits with the code for the outcome.

`run` and `wait` always end with a summary: the last line on standard error, one
line of space-separated `key=value` pairs starting with `outcome=` and `exit=`.
This holds for every ending, including ones the client reaches on its own (no
broker, a timeout, an interrupt, a file it could not read, an internal error).
The two exceptions end with one line `taste-test: error: <text>` or the argument
parser's usage message, and exit code 2: bad arguments, and a directory that is
not inside a Git working tree.

```text
taste-test: outcome=failed exit=1 command-exit=2 id=20261115T183000Z-3f9a1c2b4d5e duration=83s lines=1422 shown=first-20+last-50 log=.taste-test/spool/results/20261115T183000Z-3f9a1c2b4d5e/output.log
```

| Field | Present | Meaning |
|---|---|---|
| `outcome` | always | The request's [outcome](glossary.md#outcome). When the client ends without a result it is the name of the exit code: `not-run`, `pending`, or `error`. A result naming an outcome the client does not know is shown as `error`. |
| `exit` | always | The exit code the client is about to return. |
| `command-exit` | outcomes `passed` and `failed` | The [command exit status](glossary.md#command-exit-status) in the guest. |
| `id` | once a request exists | The request ID. Absent only when the client ended before submitting one. |
| `duration` | with a result | Seconds the broker spent on the request (`seconds.total` in the result), rounded. |
| `lines` | with a result | Lines in the log that was printed. |
| `shown` | with a result | `all`, `first-N`, `last-N`, or `first-N+last-M`. |
| `truncated=yes` | when the stored log hit its size limit | The log on disk is incomplete. |
| `log` | with a result | Path of the stored log, relative to the current directory. It names `setup.log` when the outcome is `setup-failed`, otherwise `output.log`. Always the last field, and it runs to the end of the line, because a path can contain spaces. Like the rest of the line it is made printable, so a path with control characters or runs of spaces is not shown exactly; the log is always `.taste-test/spool/results/<id>/<name>` under the checkout root. |

Every field before `log` is a number or one of a fixed set of words; no text
from the spool is copied into the summary line.

When there is something to explain (every outcome except `passed` and `failed`,
and every client-side ending), the line before the summary is
`taste-test: reason: <text>`.

### `status` output

`taste-test status ID` prints one line; `taste-test status` prints
`broker: serving this checkout (heartbeat Ns ago)` or
`broker: not serving this checkout`, then one such line per request in the
spool:

```text
id=<id> state=<state> [outcome=<outcome> exit=<code>] [detail='<text>']
```

`state` is `submitted` while no broker has acknowledged the request, then one of
the states in [protocol.md §3](protocol.md#3-file-schemas). `outcome` and `exit`
appear when the state is `finished`, and are what `wait` would report; a result
file that is not a valid result is shown as `outcome=error exit=5`.

## 2. Exit codes

The exit code of `taste-test run` and `taste-test wait` is determined by the
outcome alone. It is never the guest command's own exit status (that is
`command-exit` on the summary line), so a test runner that happens to exit 2
cannot be mistaken for a usage error. For these two commands, **0 always and
only means the tests passed**: the client returns 0 only after reading a valid
result for the request whose outcome is `passed`, and 1 only for `failed`. No
other ending, and no failure inside the client itself, produces 0 or 1.

The verdict is only as trustworthy as the place it is read from. The result is
a file in the spool, which anything in the agent's sandbox can write, so a
process in the sandbox can forge one. The client cannot tell. A supervisor that
must not be deceived by the agent it supervises reads the broker's
[audit log](glossary.md#audit-log) instead
([protocol.md §5](protocol.md#5-operator-state)), which the sandbox cannot
write.

| Code | Name | Result outcome | Meaning | What the caller should do |
|---:|---|---|---|---|
| 0 | `passed` | `passed` | The command ran in the VM and exited 0. | Proceed. |
| 1 | `failed` | `failed` | The command ran in the VM and exited non-zero. | Read the output; fix the candidate. |
| 2 | `usage` | — | Bad arguments. Nothing was submitted. | Fix the invocation. |
| 3 | `setup-failed` | `setup-failed` | The environment could not be built from the default branch. The candidate was never run. | Not caused by the candidate; report to the operator. |
| 4 | `timed-out` | `timed-out` | The command exceeded its time limit and was stopped. | Treat as a failure; look for a hang. |
| 5 | `error` | `error` | Infrastructure failure: the VM did not start, Tart failed, or the broker stopped responding. No verdict. | Retry once; then report to the operator. |
| 6 | `not-run` | `rejected` | Nothing was run: no broker took the request (none is serving this checkout, or one is but did not take the request in time), or the broker rejected the request (no repository config on the default branch, over a limit, unknown command). | Report to the operator; the reason line says what is missing. |
| 7 | `cancelled` | `cancelled` | The request was cancelled before it finished. No verdict. | Nothing. |
| 8 | `pending` | — | The request is still queued or running: --wait-timeout expired, or --detach was given. | Run `taste-test wait ID`. |

Codes 3 and 5 to 8 say nothing about the code under test. Code 4 is a verdict:
the command ran and did not finish in time.

Client-side causes, with no result from a broker:

- **5** — the request was acknowledged but the broker's
  [heartbeat](glossary.md#heartbeat) then stayed stale for the broker-lost
  period ([protocol.md §2](protocol.md#2-identifiers-and-limits)); something on
  the spool's path is not a real directory; a file could not be read or
  written; a working-tree file changed while it was being read; the request's
  `result.json` exists but is not a valid result for it, or its log exists but
  is not a regular file; a result named an outcome the client does not know;
  the request ID given to `wait` is unknown; or the client itself failed
  unexpectedly.
- **6** — no broker acknowledged the request within the
  [acknowledgement window](glossary.md#acknowledgement-window), or before
  `--wait-timeout` ran out or the wait was interrupted (in each case the
  request is withdrawn, and the reason line says whether no broker is serving
  the checkout or one is serving but did not take the request, which happens
  when the checkout already has the most requests it may have waiting, or
  when `--wait-timeout` is shorter than the broker's scan interval). A linked
  worktree that a running broker does not serve (its repository already has
  the most linked worktrees served at once, or the broker refused the
  worktree; [protocol.md §2](protocol.md#2-identifiers-and-limits)) looks the
  same to the client as no broker at all; the broker's output says why. Also
  6: the client was interrupted before it had submitted the request; or the
  snapshot would exceed the size limit, which the broker would reject too.
- **8** — for an acknowledged request: `--wait-timeout` expired, `--detach` was
  given, or the wait was interrupted from the keyboard.

`log`, `status`, and `cancel` are different commands with their own meaning of
0: success of that command. They exit 2 on bad arguments and 5 when the request
ID is unknown in this checkout, when its files cannot be read, when they are
interrupted, and on an unexpected failure. `status` without an ID exits 0 when a
broker is serving the checkout and 6 when none is.

`taste-test-broker` exits 0 on success, 1 when a command fails or refuses (and
when `doctor` reports a failed check), and 2 on bad arguments.

This table is the canonical statement of the exit codes. `taste-test --help`
prints the same rows from the single constant set in
`src/tart_taste_test/exitcodes.py`, and a test fails if the two disagree.

## 3. `taste-test-broker` (operator side)

```text
taste-test-broker register   [PATH] --remote-url URL [--branch NAME]
taste-test-broker register   [PATH] --trust-local --branch NAME
taste-test-broker unregister [PATH]
taste-test-broker list
taste-test-broker serve      [--once] [--max-setup-network MODE] [--max-run-network MODE]
                             [--max-timeout SECONDS] [--max-cpu N] [--max-memory-mb N]
                             [--allow-local-images]
taste-test-broker doctor     [--image IMAGE] [--network MODE]
taste-test-broker prune      [--environments]
taste-test-broker --version | --help
```

| Command | What it does |
|---|---|
| `register` | Records a repository in the [registry](glossary.md#registry) so its checkouts may request runs. This is the operator's consent. |
| `unregister` | Removes a repository from the registry and deletes its [mirror](glossary.md#mirror). A request for it that is in flight ends with outcome `error`. |
| `list` | Prints the registered repositories and where each one's default branch comes from. |
| `serve` | Runs the broker: watches the [spool](glossary.md#spool) of every registered checkout and executes requests one at a time, checkouts taking turns. `--once` exits when no request is waiting. |
| `doctor` | Checks the installation, and says where Tart keeps its VMs and who can write there. With `--image`, also boots that image and verifies the guest behaviours and network restrictions the broker depends on. |
| `prune` | Deletes this state directory's leftover [run VMs](glossary.md#run-vm), doctor VMs, and unfinished builds; with `--environments`, also every cached [environment image](glossary.md#environment-image). |

`register` details:

- `PATH` (default: current directory) must be inside the main working tree of a
  Git repository. A linked worktree is refused; registering the main working
  tree covers its linked worktrees. The working tree is found by looking for
  `.git` on disk; Git is not run in the repository.
- `--remote-url URL` is required unless `--trust-local` is given. It is never
  read from the repository's own configuration, because an agent can change that
  configuration. The URL must be `https://…`, `ssh://…`, or `user@host:path`;
  local paths and `file://` URLs are refused.
- `--branch NAME` names the default branch. Without it, the branch the remote's
  `HEAD` points to is used.
- `--trust-local --branch NAME` reads the default branch from the repository's
  own local branch instead of a remote. It is weaker: anything that can commit
  to that local branch, including an agent working in the repository, controls
  what runs and runs it with the setup network. See
  [decision D5](decisions.md#d5-the-default-branch-is-read-from-a-broker-owned-mirror).

`serve` flags are the [policy ceilings](glossary.md#policy-ceiling):

| Flag | Default | Bounds |
|---|---|---|
| `--max-setup-network MODE` | `internet` | The most open [network mode](glossary.md#network-mode) while an environment image is built (default-branch code). |
| `--max-run-network MODE` | `isolated` | The most open network mode while a named command runs (candidate code). |
| `--max-timeout SECONDS` | `7200` | The longest `timeout_seconds` anywhere in a repository config. |
| `--max-cpu N` | number of host CPUs | The largest `vm.cpu`. |
| `--max-memory-mb N` | `16384` | The largest `vm.memory_mb`. |
| `--allow-local-images` | off | Lets `vm.image` name a local VM instead of a registry reference. |

Ceilings apply to the effective value of every key, whether the config states it
or leaves it to its default. A value above its ceiling rejects the request
(outcome `rejected`, exit code 6) with a reason naming the key, the value, and
the ceiling; nothing is ever reduced silently. For example, under
`--max-setup-network isolated` a config must state `setup.network = "isolated"`
itself. The one exception is the VM's size: when a config omits `vm.cpu` or
`vm.memory_mb`, the image's own value is used and is not compared with a
ceiling.

Stopping `serve`: Ctrl-C or SIGTERM. An idle broker exits within about half a
second, and a stop that arrives while it scans claims no new request. A request in
progress ends with outcome `error` (the client exits 5, and the reason says the
broker was stopped); its VM is stopped and deleted before the broker exits,
which can take up to a few minutes if Tart is slow to answer. `serve` exits 0
either way. Details: [vm-handling.md §1](vm-handling.md#1-tart-invocations).

`doctor --network` defaults to `isolated`, the default run network mode. Under
`isolated`, a network probe passes only if it ran and nothing answered it; a
probe that could not run to a verdict is a failed check, not a pass. The probes
need `curl` in the image: without it the check fails under `isolated`, and is
a warning under the other modes, where no claim about isolation is made. The IPv6
probe also passes on a host without an IPv6 route, where it says nothing about
isolation, and the line says so.

Exit codes of `taste-test-broker`: 0 success (`serve`: it was stopped, or
`--once` found nothing left to do); 1 a command failed or refused, or a `doctor`
check failed; 2 bad arguments.

## 4. Repository config: `.taste-test.toml`

The broker reads this file from the tip of the
[default branch](glossary.md#default-branch), never from the candidate. A
candidate that edits it changes nothing until the edit is merged.

```toml
schema = 1

[vm]
image = "ghcr.io/cirruslabs/ubuntu:latest"   # required
cpu = 4                                      # optional
memory_mb = 8192                             # optional
boot_timeout_seconds = 180                   # optional
shell = ["/bin/sh", "-lc"]                   # optional

[setup]
run = ["./scripts/bootstrap.sh"]             # optional, default []
network = "internet"                         # optional
timeout_seconds = 1800                       # optional

[run]
network = "isolated"                         # optional
timeout_seconds = 1800                       # optional

[commands.test]
run = "make test"                            # required
allow_args = false                           # optional: requests may not add arguments
timeout_seconds = 900                        # optional

[commands.unit]
run = 'uv run pytest "$@"'                   # "$@": where request arguments go
allow_args = true

[commands.lint]
run = "make lint"
```

| Key | Type | Default | Rules |
|---|---|---|---|
| `schema` | integer | required | Must be `1`. |
| `vm.image` | string | required | A registry image reference: it must match `[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}` and contain a `/`. A local VM name (no `/`) is accepted only when the broker runs with `--allow-local-images`. A name beginning `tastetest-`, in any letter case, is never accepted. |
| `vm.cpu` | integer | image's own | `1` to the `--max-cpu` ceiling. |
| `vm.memory_mb` | integer | image's own | `512` to the `--max-memory-mb` ceiling. |
| `vm.boot_timeout_seconds` | integer | `180` | `10` to `900`. |
| `vm.shell` | array of strings | `["/bin/sh", "-lc"]` | Two to four items: an absolute path, then the flags after which that shell takes a command string. |
| `setup.run` | array of strings | `[]` | Shell command strings, run in order in the default-branch source tree while building the environment image. At most 32. |
| `setup.network` | string | `internet` | `isolated`, `internet`, or `shared`; at most the `--max-setup-network` ceiling. |
| `setup.timeout_seconds` | integer | `1800` | Budget for all setup commands together. `1` to the `--max-timeout` ceiling. |
| `run.network` | string | `isolated` | Network mode while a named command runs; at most the `--max-run-network` ceiling. |
| `run.timeout_seconds` | integer | `1800` | Default time limit of a named command. `1` to the `--max-timeout` ceiling. |
| `commands.<name>.run` | string | required | A shell command string. `<name>` must match `[a-z][a-z0-9_-]{0,31}`. At least one command must be defined; at most 32. |
| `commands.<name>.allow_args` | boolean | `false` | When true, the request's `ARG...` are passed to the command, and `run` must contain `"$@"` to say where they go (see below). |
| `commands.<name>.timeout_seconds` | integer | `run.timeout_seconds` | Time limit for this command. `1` to the `--max-timeout` ceiling. |

Rules that apply to the whole file:

- Unknown tables and unknown keys are errors, so a misspelt key cannot silently
  fall back to a default.
- Every string must be non-empty, at most 4096 bytes, and free of NUL bytes.
- The file is at most 65536 bytes.
- A file that fails any rule produces outcome `rejected`, with a reason naming
  the key.

**Image requirements.** The image must contain the
[guest agent](glossary.md#guest-agent), a POSIX shell at `/bin/sh`, `tar`, and
`xargs`, and must set `HOME` for commands run through the guest agent. The
broker checks these after every boot and ends the request with outcome `error`
and a reason naming what is missing. `taste-test-broker doctor --image` runs the
same checks ahead of time.

**Network modes**, from most to least restricted. The first two are implemented
with [Softnet](glossary.md#softnet), and what they allow is what Tart's own
documentation of Softnet states; `doctor --image` measures what a VM can
actually reach.

| Mode | What the broker asks Tart for | What that is documented to allow | Requires |
|---|---|---|---|
| `isolated` | Softnet with every IPv4 destination blocked. | No IPv4 destination. Whether the host side of the VM's network bridge and IPv6 are also unreachable is not documented; see [open question Q2](open-questions.md#q2-what-can-a-vm-reach-in-each-network-mode). | Softnet |
| `internet` | Softnet with its default rules. | Globally routable IPv4 addresses and the host side of the VM's network bridge, so a service on the Mac that listens on all interfaces is reachable from the VM. Private and local-network ranges are blocked. | Softnet |
| `shared` | Tart's default shared (NAT) network. | Whatever the host can reach, including the local network. | Nothing extra |

The exact Tart flags for each mode are in
[vm-handling.md §1](vm-handling.md#1-tart-invocations).

**How a command string runs in the guest.** The guest argument vector is, in
order: the items of `vm.shell`; one script; the fixed word `taste-test`; then
the request's arguments, one item each. The script is

```text
exec 2>&1; cd "$HOME/taste-test/src" && <run>
```

and nothing is appended to it. So the command starts in the root of the source
tree with a login shell's environment and one output stream, `taste-test`
becomes `$0`, and the arguments are the shell's positional parameters `$1`,
`$2`, and so on, exactly as given: they are never interpolated into the script.

The `run` string itself says where the arguments go, by containing `"$@"`:

```toml
run = 'uv run pytest "$@"'                       # appended to one command
run = 'make build && ./run-tests "$@"'           # to the second of two
run = './run-tests "$@" --junit-xml report.xml'  # in the middle
```

A command with `allow_args = true` whose `run` does not contain `"$@"` is
rejected, because the arguments would silently go nowhere. Write it with the
double quotes: unquoted `$@` splits arguments that contain spaces.

The verdict is the exit status of the whole `run` string as the shell reports
it, so the string must end with the status of the tests. A pipeline reports
only its last command: `./run-tests "$@" | tee test.log` exits 0 when the tests
fail, and the request is reported as `passed`. Do not pipe the test command.
The output is already captured, so there is nothing to gain from `tee`.

A repository that wants the candidate's own dependency changes installed puts
that step in the command string (for example `uv sync --locked && pytest`) and
asks for a run network mode that permits it, which the operator must allow with
`--max-run-network`.
