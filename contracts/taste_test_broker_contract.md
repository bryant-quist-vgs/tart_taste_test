Contract version: v1

# taste-test-broker — the operator-side process that runs requests in Tart VMs: Contract

`taste-test-broker` is run by the [operator](../docs/glossary.md#operator),
outside any sandbox, and is the only program in this project that starts
virtual machines. It keeps a [registry](../docs/glossary.md#registry) of
repositories the operator has approved, watches the
[spool](../docs/glossary.md#spool) of each of their
[checkouts](../docs/glossary.md#checkout), and for each
[request](../docs/glossary.md#request) builds or reuses an
[environment image](../docs/glossary.md#environment-image) from the
[default branch](../docs/glossary.md#default-branch), runs the requested
[named command](../docs/glossary.md#named-command) against the
[candidate](../docs/glossary.md#candidate) in a throwaway
[run VM](../docs/glossary.md#run-vm), and writes the output and
[outcome](../docs/glossary.md#outcome) back to the spool. It runs with the
operator's authority while reading files an untrusted agent controls, so most of
this contract is about what it will not do with them.

The script is the console entry point `tart_taste_test.broker_cli:main` and the
modules it imports, directly or through them: `broker`, `served`, `checkouts`,
`spool`, `fairqueue`, `audit`, `notices`, `engine`, `tart`, `process`,
`mirror`, `registry`, `repo_config`, `doctor`, `protocol`, and `exitcodes`
(through `protocol`, for the outcome names). Its tests are
`tests/broker_cli_test.py`, `tests/broker_test.py`,
`tests/broker_hostile_test.py`, `tests/broker_limits_test.py`,
`tests/broker_lifecycle_test.py`, `tests/checkouts_test.py`,
`tests/spool_test.py`, `tests/fairqueue_test.py`, `tests/notices_test.py`,
`tests/engine_test.py`,
`tests/tart_test.py`, `tests/process_test.py`, `tests/mirror_test.py`,
`tests/registry_test.py`, `tests/repo_config_test.py`, `tests/doctor_test.py`,
`tests/protocol_test.py`, and, for the agreement between this code and the
documents it cites, `tests/docs_test.py` (see
[decision D15](../docs/decisions.md#d15-contracts-live-in-contracts-one-per-executable)).
Binding values are specified in [docs/interfaces.md](../docs/interfaces.md),
[docs/protocol.md](../docs/protocol.md), and
[docs/vm-handling.md](../docs/vm-handling.md); this contract cites them and
does not introduce values of its own.

No behaviour here has been observed against a real VM; the tests use a stand-in
for Tart. See [docs/open-questions.md](../docs/open-questions.md).

## Inputs

- **Command line** — `register`, `unregister`, `list`, `serve`, `doctor`,
  `prune`, `--version`, `--help`, with the flags in
  [interfaces.md §3](../docs/interfaces.md#3-taste-test-broker-operator-side).
  **Trust:** operator-provided (trusted). Validated for shape: the remote URL,
  branch name, and image reference against the rules in
  [protocol.md §5](../docs/protocol.md#5-operator-state) and
  [interfaces.md §4](../docs/interfaces.md#4-repository-config-taste-testtoml);
  numeric ceilings must be positive.
- **`TASTE_TEST_STATE_DIR`** — environment variable, optional. **Trust:**
  operator-provided. Overrides the [operator state](../docs/glossary.md#operator-state)
  directory.
- **`HOME`** — environment variable. Used only to compute the default state
  directory and, in `doctor`, the default location of the Tart VM store.
- **`PATH`** — environment variable. **Trust:** operator-provided. Where `tart`,
  `git`, and (for `doctor`) `softnet` are found.
- **`TMPDIR`** — environment variable, optional. Where temporary files for
  captured subprocess output are created.
- **`TART_HOME`** — environment variable, optional. **Trust:**
  operator-provided. Read by `doctor` only, to name the directory where Tart
  keeps VMs; Tart itself also reads it.
- **`GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`** — environment variables,
  optional. **Trust:** operator-provided. Passed on to Git. `GIT_SSH` and
  `GIT_SSH_COMMAND` are also tested for presence: when neither is set, Git's
  SSH is put in batch mode.
- **The rest of the environment** — passed unchanged to every Tart process, and
  to every Git process except that every other `GIT_*` variable is removed.
- **Operator state** — `registry.json` and the mirrors. **Trust:** trusted
  (written only by this program). The registry is still validated entry by
  entry on every read, and a registry that fails is an error.
- **Default-branch commit** — fetched into the
  [mirror](../docs/glossary.md#mirror): the
  [repository config](../docs/glossary.md#repository-config) blob, the file
  list, and the source archive. **Trust:** reviewed content, bounded by policy.
  The config is validated against the schema in interfaces.md §4 and the
  [policy ceilings](../docs/glossary.md#policy-ceiling); any violation rejects
  the request.
- **Spool contents** — `request.json`, `source.tar`, the `cancel` marker, and
  every name under `<checkout>/.taste-test/spool/`. **Trust: untrusted.**
  Handled only as described in
  [protocol.md §4](../docs/protocol.md#4-how-the-broker-touches-a-spool).
- **`<repository>/.git/worktrees/*/gitdir` and the `.git` file of each linked
  worktree** — read as small plain files to find linked worktrees. **Trust:
  untrusted.** A record is a claim; a worktree is used only under the rules of
  protocol.md §4 ("Which checkouts are served").
- **Output of guest commands** — bytes. **Trust: untrusted.** Stored, size
  limited, never interpreted; made printable before any part of it is shown.

## Outputs

- **Spool files** — `broker.json`, `results/<id>/status.json`,
  `results/<id>/setup.log`, `results/<id>/output.log`,
  `results/<id>/result.json` (JSON files via a `.tmp` name); the rename of
  `request.json` to `request.claimed.json`; removal of
  `requests/<id>/source.tar`.
  `Load-bearing: the client derives its exit code from result.json and prints
  output.log; the schemas in protocol.md §3 are the interface.`
- **Operator state** — `registry.json`, `registry.lock`, `broker.lock`,
  `audit.log`, `mirrors/<repo key>.git`, files under `staging/`, and
  `vm-logs/<vm>.log`, as laid out in protocol.md §5.
  `Load-bearing: audit.log is the record a reviewer trusts over the spool copy.`
- **Tart virtual machines** — created, started, stopped, renamed, and deleted
  under the names in
  [vm-handling.md §1](../docs/vm-handling.md#1-tart-invocations).
- **Standard output** — one timestamped line per event for `serve`; entries for
  `list`; `PASS`/`WARN`/`FAIL` lines for `doctor`; confirmation lines for the
  others. Agent-derived text in them is made printable. Each line of `serve` is
  flushed when it is written. If one cannot be printed (the output was closed),
  `serve` prints nothing further, points its standard output at `/dev/null`, and
  keeps serving, so its exit code is still the one below.
- **Standard error** — `Error: ...` lines.
- **Exit code** — `0` success (for `serve`: it was stopped, or `--once` found
  nothing left to do); `1` failure or refusal, or a failed `doctor` check; `2`
  usage error.

## Behaviors

1. **Register.** Finds the main working tree containing the path by looking for
   `.git` on disk (no Git command is run there) and refuses a linked worktree.
   Requires `--remote-url`, or `--trust-local` with `--branch`; the remote URL
   is never read from the repository. Without `--branch`, asks the remote which
   branch its `HEAD` points to. Validates the values and writes the entry to
   `registry.json` atomically under the registry lock. Re-registering a path
   replaces its entry. Prints what was recorded and, for `--trust-local`, a
   warning that the local branch is trusted.
   `Load-bearing: verdict integrity depends on the default branch coming from a
   source the agent cannot rewrite.`
2. **Unregister and list.** `unregister` removes the entry under the registry
   lock and deletes the repository's mirror directory; a request for it that is
   in flight ends as `error`. `list` prints the entries.
3. **Single instance, private state.** `serve`, `prune`, and `doctor --image`
   take an exclusive lock on `broker.lock` and refuse to start (exit 1) if
   another process holds it or if the state directory is accessible to other
   users.
4. **Start-up cleanup.** `serve` deletes every local VM that is a run VM,
   doctor VM, or unfinished build *of its own state key*, empties `staging/`,
   and finishes any request that was claimed but has no result as outcome
   `error`. A broker leaves at most one such request, so only a few per spool
   (protocol.md §2) get an audit-log line and an event; any others get a result
   only, and one warning says how many there were. The request the previous
   broker recorded in `in-flight.json` is the first of those few, so request
   IDs, which agents choose, do not decide whether it is logged.
   `Load-bearing: a crashed broker must not leave a VM running untrusted code.`
5. **Spool discovery.** At each scan, for every registered repository, the
   checkout roots are the registered path and each linked worktree whose claim
   holds under protocol.md §4: the root is opened from `/` without following a
   link, is on a volume the repository's own path crosses, and its `.git` file,
   read through that same open directory, names the record. When records in
   several repositories name one root, it is served for the first repository,
   in registration order, whose claim holds; a record naming another
   repository's main working tree is ignored. Several records of one
   repository naming one root are all kept as claims, so a forged record does
   not hide the true one. A root is served only if it has an existing spool
   that passes the checks there. The broker never creates a spool. The number
   of linked worktrees of one repository served at once is limited
   (protocol.md §2); one being served keeps its place and its record is read
   before any other, and one over the limit is not opened. Each scan also
   checks that every served path still leads to the directories held open; one
   that does not is reopened under all the rules. A checkout that cannot be
   served is reported once. Lines that quote agent-chosen paths, warnings and
   the lines saying a linked worktree started or stopped being served, are
   limited in number (protocol.md §2). Nothing found in a spool or a worktree
   record stops the broker.
   `Load-bearing: a forged record must not aim the broker, which runs with the
   operator's authority, at a directory the agent cannot write.`
6. **Heartbeat and acknowledgement.** At least every heartbeat interval, also
   while a request is executing, the broker rewrites `broker.json` in every
   served spool and writes a `queued` status for each newly seen request, up to
   the per-spool limit of protocol.md §2; requests beyond it are not seen until
   earlier ones end. The status says how many requests are ahead, counted
   across all served checkouts, and is written again when that changes. The
   time one scan spends listing spools is limited (protocol.md §2); spools not
   reached are listed first in the next scan. A request that was already older
   than the maximum age when first seen is rejected when its turn comes.
7. **Claim, then validate.** When a request's turn comes the broker renames
   `request.json` to `request.claimed.json` and validates the bytes of the
   claimed file against protocol.md §3. Before the rename it records the
   checkout and the request ID in `in-flight.json` in operator state, and it
   removes that file when the request ends. A request the client withdrew first
   (its `request.json`, or its whole directory, is gone) is dropped without a
   result. A request that fails validation ends as `rejected`
   and is never executed.
   `Load-bearing: this is the only place untrusted request data is admitted, and
   what is validated is exactly what was claimed.`
8. **Stage.** Copies `source.tar` to `staging/` while hashing, ends the request
   as `rejected` when the file is over the size limit or its size or digest
   differs from the request, and as `error` when it is not a single-link
   regular file. Once a request has been claimed, the spool copy is removed
   whether or not it could be staged, and also when the claimed file itself is
   refused. The archive is never unpacked, listed, or parsed on the host.
   `Load-bearing: host safety (design §6) rests on the snapshot staying opaque.`
9. **Default branch.** Fetches the registered branch into the mirror, resolves
   it to a commit, and reads `.taste-test.toml` from that commit. Every Git
   process starts in the directory `/` with inherited `GIT_*` variables removed
   (protocol.md §5), so no Git command discovers or reads the configuration of
   the directory the broker was started in. A failed fetch is outcome `error`.
   A missing or invalid config, an unknown command name, arguments for a
   command that does not allow them, or a value above a ceiling ends the
   request as `rejected`.
   `Load-bearing: verdict integrity (design §6): the candidate never supplies the
   command, the image, or the network mode.`
10. **Environment image.** Reuses the environment image for the commit when it
    exists; otherwise builds it by the steps in
    [vm-handling.md §2](../docs/vm-handling.md#2-guest-layout-and-steps), writing
    setup output to `setup.log`. A failing setup command or an exhausted setup
    budget ends the request as `setup-failed` and deletes the unfinished build.
    After a successful build, older environment images of the same repository
    are deleted; failing to list or delete them is a warning, not a failure of
    the request.
11. **After-boot checks.** After every VM boot it waits for the guest agent,
    runs the [exit-status probe](../docs/glossary.md#exit-status-probe) and
    requires status 7, and checks the image requirements. Any failure is outcome
    `error`.
    `Load-bearing: the broker must never report passed when it cannot observe
    exit statuses (decision D11).`
12. **Run.** Clones the environment image, starts it with the run network mode,
    replaces the default-branch files with the candidate, and runs the named
    command with the guest argument vector of interfaces.md §4, appending its
    output to `output.log` up to the size limit. The command string is used
    exactly as the config wrote it; nothing is appended. After the command the
    exit-status probe is repeated, whatever the command appeared to do. If the
    probe passes, exit status 0 is `passed`, a non-zero status is `failed`, and
    exceeding the time limit is `timed-out`; if it does not, the outcome is
    `error`. The same rule applies to each setup command.
    `Load-bearing: a VM that died must never be reported as a verdict
    (decision D11).`
13. **Cancellation.** A `cancel` marker ends the request as `cancelled` at the
    points listed in vm-handling.md §2.
14. **Isolation flags.** Every VM is started with exactly the argument vector
    of vm-handling.md §1: the four fixed flags and the network flags of its mode.
    `Load-bearing: this is the VM boundary the whole design depends on.`
15. **Cleanup.** The run VM is stopped and deleted, with its console log, and
    the staged archive removed when the request ends, whatever the outcome.
    `tart stop` and `tart delete` are issued whether or not the VM is listed; a
    VM that is still listed is retried once, reported, and left for the next
    cleanup. Only names of this state directory are ever stopped or deleted.
    `Load-bearing: no VM running candidate code outlives its request.`
16. **Result and audit.** Writes `result.json` atomically, sets the status to
    `finished`, and appends one line to `audit.log`. What that line takes from
    the request is bounded (protocol.md §5): long arguments are recorded as a
    count, a size, and a digest, and a long reason is cut. The log is never
    rotated or shortened. A request that has had its turn is not taken again
    while it stays in its spool, even if it could not be claimed or its result
    could not be written.
17. **Ordering and fault containment.** Executes one request at a time: first
    seen first within a checkout, checkouts in turn, at most one turn per
    checkout per scan interval (protocol.md §2). What the broker remembers about
    waiting requests is bounded and is dropped when they leave the spool. An
    unexpected error while handling one request ends that request as `error`
    and the broker continues. With `--once`, exits when no request is waiting.
18. **Stopping.** `SIGTERM` and `SIGINT` set a flag; the handlers are installed
    for the duration of `serve` and the previous ones restored. An idle broker
    then exits, and a stop noticed after a scan claims no new request. A
    request in flight is abandoned at its next wait: its
    processes are killed, its VM stopped and deleted, its result written with
    outcome `error`, and then the broker exits 0. A request whose repository is
    unregistered, or whose spool is removed or replaced, while it runs ends the
    same way, its result going to the directories it came from, and the broker
    continues. Stopping and deleting a VM is never interrupted.
    `Load-bearing: stopping the broker must not leave a VM running.`
19. **Subprocesses.** Every Git and Tart process is started as the leader of a
    new session, with an argument list and no shell, and has a time limit
    (`tart run` is ended with its phase). When the call that started one ends
    early for any reason, the whole session is sent `SIGKILL` and the broker
    waits up to 10 seconds for the process to be reaped before it continues;
    that includes a process that has exited but has not yet been reaped, so a
    helper it left behind is killed too. Nothing is signalled once a process
    has been reaped.
20. **Doctor.** Reports whether `tart`, `softnet`, and `git` are found, the Tart
    version, the directory where Tart keeps VMs and whether other users can
    write it, and whether the state directory is private and the registry
    readable. With `--image`, clones that image and checks the clone appears in
    Tart's listing, boots it under the given network mode, performs the
    after-boot checks, checks that standard input reaches the guest and that
    arguments arrive intact, probes what the guest can reach on the network,
    and deletes the VM. Under `isolated`, a network probe counts as passed only
    if it ran and nothing answered; an image without `curl`, in which the probes
    cannot run, fails the check under `isolated` and is a warning under the
    other modes. Exits 1 if any check fails.
21. **Prune.** Deletes local VMs that are run VMs, doctor VMs, or unfinished
    builds of its own state key, and files left in `staging/`; with
    `--environments`, also its environment images. Never deletes a VM with any
    other name.

Any behavior the script performs that is not listed here is **undeclared** and
constitutes drift. The script must not perform undeclared behaviors. An
adversarial reviewer treats undeclared behaviors as suspect.

## Declared capability surface

- **Filesystem reads:** the operator state directory;
  `<repository>/.git/worktrees/*/gitdir` and the `.git` file of each linked
  worktree; the spool of each served checkout; each directory on the path from
  `/` to a checkout root (opened to reach the next one; its device number is
  read, its contents are not listed; a directory on a volume the repository's
  path does not cross is not opened, only its device number is read from its
  parent); the `.git` entry of the directories above
  a path being registered (existence and kind only); for `doctor`, the owner
  and mode of the Tart VM store directory and whether `tart`, `softnet`, and
  `git` exist on `PATH`. No other reads.
- **Filesystem writes:** the operator state directory; and inside a served
  spool only the files listed under Outputs. It never writes elsewhere in a
  checkout and never creates `.taste-test/` or `spool/`. Temporary files for
  captured subprocess output are created in the system temporary directory and
  deleted when closed.
- **Network access:** none performed directly. `git fetch` and `git ls-remote`
  contact the registered remote over HTTPS or SSH; `tart clone` may pull the
  image named in the repository config from its registry. In `doctor --image`,
  the guest (not the host) attempts connections to `1.1.1.1`,
  `2606:4700:4700::1111`, and its own default gateway.
- **Subprocess invocation:** the Tart commands in vm-handling.md §1, and no
  others. Git, always started in the directory `/`, with the mirror as its
  repository or with no repository: `git init --bare`, `fetch`, `rev-parse`,
  `cat-file`, `ls-tree`, `archive`, and `git ls-remote --symref <url> HEAD`.
  All with argument lists, no shell, a session of their own, and a time limit.
  In the guest (through `tart exec`): `/bin/sh -c` with the fixed scripts of
  vm-handling.md §2 and of `doctor`, and the configured shell with the repository
  config's command strings.
- **Signals:** sends `SIGKILL` to the sessions of Git and Tart processes it
  started itself, and to nothing else. Handles `SIGTERM` and `SIGINT` while
  `serve` runs.
- **Environment variables read:** `TASTE_TEST_STATE_DIR`, `HOME`, `PATH`,
  `TMPDIR`, `TART_HOME` (`doctor` only), and `GIT_SSH` and `GIT_SSH_COMMAND`
  (presence). For
  the Git processes it starts it removes every `GIT_*` variable except
  `GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_SYSTEM`, and `GIT_CONFIG_NOSYSTEM`, and sets
  `GIT_TERMINAL_PROMPT=0`, `GIT_ALLOW_PROTOCOL`, and, when neither `GIT_SSH`
  nor `GIT_SSH_COMMAND` is set, `GIT_SSH_COMMAND=ssh -o BatchMode=yes`. Tart
  processes inherit the environment unchanged.
- **Secrets handled:** none directly. `git fetch` uses the operator's existing
  Git credentials; they are never read, logged, or passed to a VM.
- **Privilege escalation:** No privilege escalation. Never invokes `sudo`.

## Assumptions for operation

- Runtime: Python >= 3.14, standard library only; macOS on Apple silicon.
- `tart` and `git` are on `PATH`. Softnet is installed when a restricted network
  mode is used.
- The state directory is not writable by sandboxed agents.
- The directory where Tart keeps VMs (`~/.tart`, or `TART_HOME`) is not writable
  by sandboxed agents. Environment images are reused by a name an agent can
  work out, so whoever can write that directory can replace one
  ([design.md §6](../docs/design.md#6-trust-boundaries)). The broker cannot
  verify this; `doctor` names the directory and checks its owner and mode.
- The broker and the agents run as the same user.
- `tart list --source local --quiet` prints one local VM name per line (checked
  by `doctor --image`;
  [open question Q8](../docs/open-questions.md#q8-what-does-tart-list-print)).
- The image named by a repository config meets the image requirements in
  interfaces.md §4.
- `tart exec` reports the guest command's exit status (checked on every boot by
  behavior 11) and passes the items after the VM name to the guest unchanged
  (checked by `doctor --image`).

## Anti-patterns this script must not do

- Do **not** run any Git command with an agent-writable checkout as its
  repository or as its working directory, or with inherited `GIT_*` variables
  that could redirect it. The checkout may only be the source of a fetch in
  `--trust-local` mode.
- Do **not** resolve a path an agent chose through symbolic links, or open a
  checkout root by any path that contains one.
- Do **not** append to, or otherwise rewrite, a command string from the
  repository config.
- Do **not** raise out of, or let a signal interrupt, the stopping and deleting
  of a VM.
- Do **not** read the remote URL or the default branch from the checkout.
- Do **not** unpack, list, or parse a snapshot on the host.
- Do **not** interpret request data on the host. A request supplies a command
  name used as a lookup key and arguments that become separate trailing items
  of the guest argument vector.
- Do **not** open any spool path by string after a check, follow a link in a
  spool, or open an existing spool file for writing.
- Do **not** pass any flag to `tart run` beyond those in vm-handling.md §1.
- Do **not** relax a network mode or reduce a requested limit silently; reject
  instead.
- Do **not** report `passed`, `failed`, or `timed-out` unless the exit-status
  probe passed after the command.
- Do **not** let an amount an agent controls (waiting requests, worktree
  records, spool entries, argument size, planted claimed requests) grow the
  broker's memory, open descriptors, scan time, terminal output, or audit-log
  line size without a limit.
- Do **not** signal a process group after its leader has been reaped.
- Do **not** delete a VM whose name does not carry this state directory's key.
- Do **not** print agent-derived text without making it printable.

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
  hold the program's entry point and its main loop, `broker_cli.py` and
  `broker.py`, and of the test files named above that state it. The other
  modules are shared with, or imported by, those two and carry no version of
  their own; a change to any of them is a change to this script.

## Change log

- **v1** — 2026-11-15: initial version.
  Review: 2026-11-15, fresh subagents (three reviewers, each finding checked by
  a fourth and the results merged), three rounds, verdict "changes required"
  each time.
  Round 1 (the broker as first built): 46 findings, 1 refuted by verification;
  the other 45 (1 critical, 9 high, 17 medium, 18 low or informational) were
  resolved. Round 2 (those fixes): 32 findings (2 high, 12 medium, 18 low or
  informational); 31 resolved, and 1 informational accepted: the queue length
  a checkout is shown counts every checkout's requests, because that is what
  decides the wait (behavior 6). Round 3 (the whole repository): 34 findings
  (2 medium, 32 low or informational), all resolved. The fixes of round 3 were
  checked by tests, not by a further review. Nothing has run against real
  Tart; what that leaves open is in docs/open-questions.md.
