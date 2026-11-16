Contract version: v1

# taste-test — the agent-side client that requests a test run in a VM: Contract

`taste-test` is the only program an [agent](../docs/glossary.md#agent) runs. It
packages the working tree of the current [checkout](../docs/glossary.md#checkout)
into a [snapshot](../docs/glossary.md#snapshot), writes a
[request](../docs/glossary.md#request) into the checkout's
[spool](../docs/glossary.md#spool), waits for a
[broker](../docs/glossary.md#broker) to execute it in a virtual machine, prints
the command's output (optionally only its first and last lines), and exits with
a code that states the [outcome](../docs/glossary.md#outcome). It never starts a
VM, never touches the network, and writes nothing outside
`<checkout>/.taste-test/`.

The script is the console entry point `tart_taste_test.client_cli:main` and the
modules it imports: `client`, `client_spool`, `snapshot`, `render`, `protocol`,
`exitcodes`. Its tests are `tests/client_cli_test.py`, `tests/client_test.py`,
`tests/client_waiting_test.py`, `tests/client_hostile_test.py`,
`tests/snapshot_test.py`, `tests/render_test.py`, `tests/protocol_test.py`,
`tests/exitcodes_test.py`, and `tests/entry_points_test.py` (the installed
`taste-test` command), with the fixtures in `tests/conftest.py` and
`tests/client_helpers.py`. The broker's side of the withdraw-or-claim
rename is tested in `tests/spool_test.py` (see
[decision D15](../docs/decisions.md#d15-contracts-live-in-contracts-one-per-executable)).
Binding values (commands, flags, exit codes, file names, limits) are specified
in [docs/interfaces.md](../docs/interfaces.md) and
[docs/protocol.md](../docs/protocol.md); this contract cites them and does not
introduce values of its own.

## Inputs

- **Command line** — `run`, `wait`, `log`, `status`, `cancel`, `--version`,
  `--help`, with the flags in
  [interfaces.md §1](../docs/interfaces.md#1-taste-test-agent-side). **Trust:**
  caller-provided. Validated by the argument parser: `--head` and `--tail` are
  non-negative integers; `--wait-timeout` is a positive number; `COMMAND`, `ID`,
  and `ARG...` must satisfy the patterns and limits in
  [protocol.md §2](../docs/protocol.md#2-identifiers-and-limits).
- **Current directory, or `--checkout PATH`** — a directory inside a Git working
  tree. **Trust:** caller-provided. Resolved to the working tree's root with
  Git; a path outside any working tree is a usage error.
- **Working-tree files** — every tracked file as it is on disk and every
  untracked file that is not ignored, as listed by Git. **Trust:** untrusted
  content, copied byte for byte into the snapshot and never interpreted.
- **Spool files written by the broker** — `broker.json`, `status.json`,
  `result.json`, `output.log`, `setup.log` under
  `<checkout>/.taste-test/spool/`. **Trust:** untrusted (the directory is
  writable by anything in the sandbox). Every file is reached through
  directories opened one by one without following a symbolic link, is opened
  without blocking, and must be a regular file. JSON files are read under the
  size limit and validated in full against the schemas in
  [protocol.md §3](../docs/protocol.md#3-file-schemas). A `status.json` or
  `broker.json` that fails any of this is treated as absent. A `result.json`
  that exists but fails any of this, or whose `id` is not the request's, ends
  the client with exit 5. A log that exists but is not a regular file does the
  same. Log files are copied to standard output as bytes, up to the stored-log
  limit, and never interpreted. Text from the spool that reaches standard error
  is made printable first, and none of it is copied into the summary line.

The client's own code reads no environment variable. The Git processes it
starts receive its environment with every `GIT_*` variable removed, so `PATH`
finds `git`, and the user's own Git configuration (through `HOME` and
`XDG_CONFIG_HOME`) applies: a global excludes file, for example, decides which
untracked files count as ignored. Standard input is not read.

## Outputs

- **Exit code** — as defined in
  [interfaces.md §2](../docs/interfaces.md#2-exit-codes).
  `Load-bearing: agents branch on this code alone. For run and wait, 0 is
  returned only after reading a valid result for the request whose outcome is
  passed, and 1 only for failed; no failure to obtain a verdict, no interrupt,
  no closed output stream, and no error inside the client exits 0 or 1.`
  The code is as trustworthy as the spool: a process in the sandbox can forge a
  result there, and the client cannot tell
  ([interfaces.md §2](../docs/interfaces.md#2-exit-codes)).
- **Standard output** — the command's output after `--head`/`--tail`; the
  request ID for `--detach`; the setup log when the outcome is `setup-failed`;
  status text for `status`; usage text for `--help`.
  `Load-bearing: agents read this as the test output; nothing else is mixed
  into it except the single omitted-lines marker.`
- **Standard error** — each line prefixed `taste-test: ` and made printable:
  progress lines; the reason line and the summary line (`run`, `wait`); one
  `error: <text>` line for a failure of `log`, `status`, or `cancel` and for a
  directory outside any working tree; the confirmation
  `cancellation requested for <id>`; and, from `status`, what the operator must
  run when no broker is serving. Also the argument parser's usage errors.
  `Load-bearing: the summary line format in interfaces.md §1 is parsed by callers
  that want the request ID, the log path, or the command exit status.`
- **Files under `<checkout>/.taste-test/`** — the names and modes in
  [protocol.md §1](../docs/protocol.md#1-spool-layout): `.gitignore`; the
  directories `spool/`, `spool/requests/`, `spool/results/`; and per request
  `source.tar`, `request.json` (via `request.json.tmp`),
  `request.withdrawn.json` (momentarily, while withdrawing), and `cancel`.
  `Load-bearing: the broker consumes request.json and source.tar exactly as
  specified in protocol.md §1 and §3.`

## Behaviors

1. **Argument parsing and help.** Parses the command line; any invalid
   combination prints an error and exits 2 without touching the filesystem.
   Arguments after the first `--` belong to the named command and are accepted
   only by `run`. `--help` (top level, `run`, `wait`) prints usage and the
   exit-code table generated from `exitcodes`.
   `Load-bearing: the exit-code table in --help is the agent's reference and must
   equal the table in interfaces.md §2.`
2. **Checkout resolution.** Runs `git rev-parse --show-toplevel` in the given
   directory. Failure (not a working tree, Git missing) exits 2 with a message.
3. **Spool access and creation.** Every command refuses (exit 5) a spool that
   has anything but a real directory on its path (for example a symbolic link
   at `.taste-test`, `spool`, `requests`, or `results`). All access below the
   checkout root goes through directories opened one at a time, relative to the
   parent's descriptor, without following a link. `run` creates
   `.taste-test/`, the spool directories, and `.gitignore` if absent; the file
   is created exclusively, and something else under that name is refused, never
   written through.
   `Load-bearing: nothing is read, created, or removed outside
   <checkout>/.taste-test/ through a link planted in the spool.`
4. **Pruning.** On `run`, removes `requests/<id>` and `results/<id>`
   directories whose name matches the request-ID pattern, that hold a valid
   result for that ID, and whose result is older than the retention period.
   Nothing else is removed.
5. **Snapshot.** Lists files with
   `git ls-files -z --cached --others --exclude-standard`, skips anything under
   `.taste-test/`, and writes the rest to `source.tar`: regular files with their
   content and permission bits, symbolic links as links. No link is followed, at
   the end of a path or along it: each directory on a path is opened relative
   to its parent without following a link. Skipped: paths that no longer exist,
   paths below something that is no longer a real directory (a file or a link
   where Git listed a directory), directories (submodules), and special files.
   Owner and group are written as `0` with empty names. Stops with exit 6 if the
   archive would exceed the snapshot size limit, and with exit 5 if a file
   cannot be read or if its size or modification time changes while it is being
   read. Computes the SHA-256 of the archive as written.
   `Load-bearing: the broker verifies the digest and size; a file deleted in the
   working tree must be absent from the archive so the guest does not test it;
   no file outside the checkout is read into the archive.`
6. **Submit.** Generates a request ID (UTC timestamp plus 12 random hex digits
   from the operating system's random source), writes `source.tar`, then writes
   `request.json` atomically. The request contains the command name, the
   arguments, the snapshot digest and size, the client version, and the current
   `HEAD` commit ID when one exists. `source.tar` and `request.json.tmp` are
   created exclusively inside the new request directory. A failure, or an
   interrupt, removes the request directory this run created; an interrupt
   before the request exists exits 6.
7. **Wait.** Polls the result directory. Prints a progress line to standard
   error when the state changes (unless `--quiet`). Ends when a valid
   `result.json` for this request appears; with exit 5 when a `result.json`
   exists that is not a valid result for this request; with exit 8 when the
   wait timeout expires or the wait is interrupted from the keyboard and a
   broker has acknowledged the request (which is left in place); or under
   behavior 8.
8. **No broker.** A request no broker has acknowledged is withdrawn before the
   client stops waiting for it: when the acknowledgement window ends, when the
   wait timeout expires first, or when the wait is interrupted. Withdrawing
   renames `request.json` to `request.withdrawn.json`. If the rename succeeds,
   the client removes the request directory and exits 6 with a reason. The
   reason depends on what ended the wait and on the heartbeat. After the
   acknowledgement window: when the heartbeat is fresh, a broker is serving the
   checkout but did not take the request (the checkout already has the most
   requests it may have waiting), and the reason says to try again later; when
   it is not, the reason gives the commands the operator must run, and says
   that a running broker may also be declining this working tree (a linked
   worktree it refused or has no room for), which the broker's output
   explains. When `--wait-timeout` ran out before the window ended, the reason
   names the timeout, whether or not a broker is serving. If there is
   no `request.json` to rename, the broker has claimed the request, and the
   client keeps waiting (or exits 8 if it was stopping). Any other failure to
   withdraw exits 5. If the request was acknowledged but the heartbeat then
   stays stale for the broker-lost period, exits 5 and leaves the request in
   place.
   `Load-bearing: a request must not sit unserved and execute hours later when a
   broker starts; the rename makes withdrawal and claim mutually exclusive.`
9. **Output.** With neither `--head` nor `--tail`, copies `output.log` to
   standard output as it grows. Otherwise, after the request finishes, prints
   the first `--head` lines, the omitted-lines marker when lines were left out,
   and the last `--tail` lines. The stored log is never modified, and at most
   the stored-log limit is read from it. A standard output or standard error
   that can no longer be written (a closed pipe, for example `| head` or
   `2>&1 | head`) stops the printing to that stream but not the client, which
   still exits with the code for the outcome.
   `Load-bearing: --head, --tail, and a reader that goes away must not change
   the exit code (decision D9).`
10. **Reason, summary, and exit.** Prints the reason line for every outcome but
    `passed` and `failed` and for every ending the client reaches on its own,
    then the summary line, and exits with the code `exitcodes` maps the outcome
    to. An outcome name it does not know exits 5 and is shown as `error`. An
    unexpected exception anywhere in the client ends the same way with exit 5.
    `Load-bearing: an uncaught exception would exit 1, which means failed.`
11. **Detach.** With `--detach`, stops waiting once the request is acknowledged,
    prints the request ID on standard output, and exits 8 (`pending`); if no
    broker acknowledges, behaves as in behavior 8. A request that has already
    finished is reported in full instead.
12. **`wait`.** Behaviors 7 to 10 for an existing request ID. An unknown ID
    exits 5.
13. **`log`.** Prints `output.log` (or `setup.log` with `--setup`) for a request
    ID under `--head`/`--tail`, without waiting. Exits 0, or 5 if the request or
    the log does not exist or the log is not a regular file.
14. **`status`.** With an ID, prints that request's state and, when finished,
    its outcome; exits 0, or 5 for an unknown ID. Without an ID, prints whether
    the heartbeat is fresh and lists the requests in the spool; exits 0 when it
    is fresh and 6 when it is not.
15. **`cancel`.** Creates the empty `cancel` marker in the request directory,
    without following a link at that name. Exits 0, or 5 if the request
    directory does not exist or the marker cannot be created.

Any behavior the script performs that is not listed here is **undeclared** and
constitutes drift. The script must not perform undeclared behaviors. An
adversarial reviewer treats undeclared behaviors as suspect.

## Declared capability surface

- **Filesystem reads:** the working-tree files Git lists for the checkout (to
  archive them), reached without following a link; files under
  `<checkout>/.taste-test/`, likewise. No other reads by the client's own code.
  (Git itself reads the repository and the user's Git configuration.)
- **Filesystem writes:** only under `<checkout>/.taste-test/`, as listed under
  Outputs, plus removal of its own request directory (behaviors 6 and 8) and of
  expired request and result directories (behavior 4). When standard output or
  standard error can no longer be written, it redirects that stream of its own
  process to `/dev/null`. No other writes.
- **Network access:** No network access.
- **Subprocess invocation:** `git -C <dir> rev-parse --show-toplevel`;
  `git -C <root> ls-files -z --cached --others --exclude-standard`;
  `git -C <root> rev-parse --verify --quiet HEAD`. Each with an argument list,
  no shell, a time limit, and no `GIT_*` variable. No other subprocess. It never
  runs `tart`.
- **Environment variables read:** none by the client's own code. Git receives
  the client's environment without any `GIT_*` variable, so `PATH`, `HOME`, and
  `XDG_CONFIG_HOME` influence Git as they would for the user.
- **Secrets handled:** No secrets handled. Whatever is in the working tree and
  not ignored is copied into the snapshot unread.
- **Privilege escalation:** No privilege escalation.

## Assumptions for operation

- Runtime: Python >= 3.14, standard library only.
- `git` is on `PATH`.
- The checkout is on a local POSIX filesystem where rename within a directory is
  atomic.
- A broker run by the same user serves the checkout; without one the client
  exits 6.
- File contents are bytes; paths are handled as the operating system returns
  them (UTF-8 with surrogate escapes).

## Anti-patterns this script must not do

- Do **not** import or invoke anything that can start a VM or read operator
  state (`tart`, `engine`, `broker`, `broker_cli`, `mirror`, `registry`,
  `spool`, `repo_config`, `doctor`). A test enforces the import list.
- Do **not** exit 0 from `run` or `wait` unless a valid result with outcome
  `passed` was read, or 1 unless one with outcome `failed` was read. Do **not**
  let an exception, an interrupt, or a closed stream choose the exit code.
- Do **not** follow symbolic links when archiving, at any level of a path, and
  do **not** archive anything Git does not list.
- Do **not** write, create, or delete anything outside `<checkout>/.taste-test/`,
  and do **not** open anything in the spool by a path that could run through a
  link.
- Do **not** leave an unacknowledged request behind when the client stops
  waiting.
- Do **not** send a command string, image name, or setting in a request.
- Do **not** print spool text on standard error without making it printable,
  and do **not** copy spool text into the summary line.

## Revision rules

- Removing a behavior, an input, an output, or an assumption requires a high
  threshold and a deliberate version bump. Removals are recheck signals for
  everything that depends on the script.
- Added assumptions never relax previous assumptions.
- Added capabilities to the declared capability surface require operator
  review; a maintenance session that adds a capability silently is producing
  drift.
- A version is ratified only after review; one change is one bump.
- The contract version is referenced in the header of the two modules that
  hold the program's entry point and its logic, `client_cli.py` and
  `client.py`, and of the test files named above that state it. The other
  modules are imported by those two and carry no version of their own; a
  change to any of them is a change to this script.

## Change log

- **v1** — 2026-11-15: initial version.
  Review: 2026-11-15, fresh subagents (three reviewers, each finding checked by
  a fourth and the results merged), three rounds, verdict "changes required"
  each time.
  Round 1 (the client as first built): 44 findings (10 high, 7 medium, 27 low
  or informational), all resolved. Rounds 2 and 3 reviewed the fixes together
  with the broker's; their findings on the client (the reasons given for a
  withdrawn request, the wording of exit code 6, the argument checks) were all
  resolved. The fixes of round 3 were checked by tests, not by a further
  review. Nothing has been checked against a broker driving real Tart.
