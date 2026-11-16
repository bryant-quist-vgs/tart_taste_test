---
id: decisions
title: Decision log
is_source_of_truth: true
derives_from: [design, glossary]
status: current
authority: >
  Canonical home for why the design is the way it is. Each entry is a settled
  decision. Unsettled matters live in open-questions.md, not here.
---

# Decision log

Every entry records: status, date, context, decision, rationale, rejected
alternatives, and consequences. A decision that replaces another names it under
"Supersedes". Entries are never deleted; a reversed decision is marked
`superseded` and points to its replacement.

### D1. Untested code runs only inside a Tart virtual machine

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Agents need test results for code nobody has reviewed. Running it
  on the [host](glossary.md#host) exposes the operator's files, credentials, and
  network to whatever the code does.
- **Decision:** The [candidate](glossary.md#candidate) is unpacked and executed
  only inside a [guest](glossary.md#guest) started by [Tart](glossary.md#tart).
  Nothing of the host is attached to it: no directory, disk, port, USB device,
  or clipboard.
- **Rationale:** A VM is a boundary the candidate cannot argue with. Tart is
  already the operator's virtualization tool and clones images copy-on-write, so
  a fresh VM per request is cheap.
- **Rejected:** Containers (share the host kernel; weaker boundary on macOS,
  where they run in a VM anyway). Running tests in the agent's own sandbox (the
  sandbox restricts files and network but still executes the code on the host).
- **Consequences:** macOS on Apple silicon only. A VM boot is added to every
  request. A hypervisor escape is outside what this project defends against.

### D2. Two executables, split by trust

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** A sandboxed [agent](glossary.md#agent) cannot run Tart: Tart
  needs to write its own storage and start VMs, which enterprise sandboxes deny.
  The agent still has to be able to ask for a run without a human in the loop.
- **Decision:** Ship `taste-test` (the [client](glossary.md#client), run by
  agents) and `taste-test-broker` (the [broker](glossary.md#broker), run by the
  [operator](glossary.md#operator) outside any sandbox). The client's code path
  imports nothing that can start a VM.
- **Rationale:** A sandbox policy can allow one executable and never the other.
  The agent-facing `--help` stays short. The capability surface of each program
  can be stated, reviewed, and tested separately.
- **Rejected:** One executable with subcommands for both roles (an allow rule
  for the binary would allow starting the broker). Letting the client fall back
  to running Tart itself when no broker is present (two code paths to secure,
  and the fallback is exactly the one that runs with the agent's authority).
- **Consequences:** A broker must be running for a request to be served. Without
  one the client exits with code 6 and says what the operator must start. The
  split is defence in depth: both programs are installed together, and what
  really keeps an agent from acting as a broker is its sandbox denying Tart's
  storage and the operator state directory.

### D3. Requests travel through a file spool in the checkout

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** The client needs a channel to the broker that works from inside a
  sandbox. Sandboxes commonly block binding ports and reaching sockets outside
  the working directory, and remap the temporary directory per session. The one
  thing a coding agent can always do is write files in its own
  [checkout](glossary.md#checkout).
- **Decision:** The channel is the directory `.taste-test/spool` in the checkout
  ([protocol.md §1](protocol.md#1-spool-layout)). The client writes requests;
  the broker scans registered checkouts and writes results.
- **Rationale:** Needs no permission the agent does not already have. Survives a
  client that is killed and re-attached. Leaves an inspectable trail.
- **Rejected:** A Unix socket or local TCP port (blocked by many sandboxes, and
  needs an authentication scheme). A spool in the temporary directory (its path
  differs between the sandbox and the broker).
- **Consequences:** The broker reads from a directory the agent controls, which
  drives D10. Requests are noticed by scanning, so acknowledgement takes up to
  one scan interval ([protocol.md §2](protocol.md#2-identifiers-and-limits)).

### D4. The default branch decides what runs; a request only names a command

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** If the candidate could define its own test command, image, or
  network access, an agent could make any run pass, or point the VM at the local
  network.
- **Decision:** The [repository config](glossary.md#repository-config) is read
  from the tip of the [default branch](glossary.md#default-branch). A
  [request](glossary.md#request) carries a
  [named command](glossary.md#named-command) and, where that command allows it,
  arguments. Arguments reach the guest shell as positional parameters after a
  fixed `$0` and are never spliced into a command string.
- **Rationale:** The default branch is the reviewed state of the repository. The
  same rule that makes the environment trustworthy makes the verdict
  trustworthy.
- **Rejected:** Reading the config from the candidate (defeats the purpose).
  Letting the request carry a command string (turns the request into a script).
- **Consequences:** Adopting the tool in a repository requires merging a
  `.taste-test.toml` to the default branch first. A candidate that changes the
  config or the dependency set is tested against the old ones until merged. The
  command string is fixed, but the files it reads (a Makefile, the tests) come
  from the candidate.

### D5. The default branch is read from a broker-owned mirror

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** The agent can write to the checkout's `.git` directory. It can
  move local branches and remote-tracking refs, change the configured remote
  URL, and set Git configuration that makes ordinary Git commands execute
  programs.
- **Decision:** The broker keeps a bare [mirror](glossary.md#mirror) per
  [registered repository](glossary.md#registered-repository) in
  [operator state](glossary.md#operator-state) and fetches the default branch
  into it. The remote URL is typed by the operator at `register`
  (`--remote-url`); it is never read from the repository, must be HTTPS or SSH,
  and Git is restricted to those transports when fetching it. Everything the
  broker learns about the default branch comes from the mirror. A failed fetch
  ends the request with outcome `error`. `--trust-local` is an explicit opt-in
  that fetches from the checkout's own branch instead. `register` finds the
  working tree by looking for `.git` on disk and runs no Git command in it.
- **Rationale:** The agent can rewrite neither the operator's registry nor the
  mirror, so it cannot substitute a branch of its own. The broker never executes
  Git inside an agent-writable repository; in `--trust-local` mode the only Git
  process that reads the checkout is the upload side of a fetch, which Git
  documents as safe to run against an untrusted repository.
- **Rejected:** Reading `origin/<branch>` or `remote.origin.url` from the
  checkout (both agent-writable). Running `git worktree list` or `git archive`
  in the checkout (executes with the checkout's configuration). Falling back to
  the previously fetched commit when a fetch fails (a stale config could be one
  that was later tightened; an agent could make fetches fail to keep it).
- **Consequences:** The broker needs network access and the operator's Git
  credentials for the remote, and cannot serve a `remote` repository while
  offline. With `--trust-local`, anything that can commit to the local branch
  controls the config and runs setup commands with the setup network.

### D6. One cached environment image per default-branch commit

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Installing dependencies on every request would make each run take
  minutes and would need network access while untrusted code is present.
- **Decision:** Setup runs once per default-branch commit, on the default-branch
  source, and the stopped VM is kept as an
  [environment image](glossary.md#environment-image). Each request clones it. In
  the clone, the paths in the [base manifest](glossary.md#base-manifest) are
  deleted and the candidate is unpacked in their place.
- **Rationale:** Setup (reviewed code) and the test run (untrusted code) happen
  in different boots, so each gets its own network mode. Clones are
  copy-on-write. Deleting by manifest keeps what setup installed while making
  sure a file the candidate deleted is really gone.
- **Rejected:** Unpacking the candidate over the default-branch tree without
  deleting (deleted files would survive and could change the result). Replacing
  the whole tree (would discard dependencies installed inside it).
- **Consequences:** The first request after the default branch moves pays for a
  rebuild. Older environment images of the same repository are deleted after a
  successful build.

### D7. Network access is a declared mode, restricted by default, never relaxed silently

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** A VM on Tart's default network can reach the internet and the
  operator's local network. Untested code with that reach can leak the source it
  is testing or probe internal services. An agent whose own sandbox has no
  network would gain one by asking for a test run.
- **Decision:** Three [network modes](glossary.md#network-mode), and a separate
  operator [ceiling](glossary.md#policy-ceiling) for each phase. Setup defaults
  to `internet` under a ceiling of `internet`. The run phase defaults to
  `isolated` under a ceiling of `isolated`, so candidate code gets a network
  only if the operator passes `--max-run-network`. The restricted modes are
  implemented with [Softnet](glossary.md#softnet); if Softnet is not installed
  the VM fails to start and the outcome is `error`. The documentation claims for
  each mode only what Tart documents, and `doctor --image` measures the rest.
- **Rationale:** Safe by default means the default must be the restricted one,
  a missing dependency must not quietly produce the unrestricted one, and the
  broker must not become a way around the agent's sandbox.
- **Rejected:** Falling back to the shared network when Softnet is absent.
  Clamping a config that asks for more than a ceiling (the run would proceed
  under conditions nobody asked for). One ceiling for both phases (it would
  have to be `internet` for setup to work, handing the same reach to candidate
  code).
- **Consequences:** Operators must install Softnet. What `isolated` blocks
  beyond IPv4 destinations is
  [open question Q2](open-questions.md#q2-what-can-a-vm-reach-in-each-network-mode).

### D8. The client's exit code is derived from the outcome, not passed through

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Agents branch on the exit code. If the client passed through the
  guest command's status, a test runner exiting 2 would look like a usage error
  and an infrastructure failure could collide with a real test result.
- **Decision:** A fixed table
  ([interfaces.md §2](interfaces.md#2-exit-codes)): 0 passed, 1 failed, and
  distinct codes for every way a verdict can be absent. For `run` and `wait`, 0
  is returned only for a result whose outcome is `passed`; a detached submission
  exits 8 (`pending`), not 0. The guest command's own status is reported as
  `command-exit` on the [summary line](glossary.md#summary-line).
- **Rationale:** One number answers "what do I do next" without parsing output,
  and "0" can never be read as a pass that did not happen.
- **Rejected:** Passing the status through. Folding all non-verdicts into one
  code (the agent's next action differs between them). Exit 0 for a successful
  `--detach` (it would make 0 ambiguous for the one command agents rely on).
- **Consequences:** Callers that want the raw status read the summary line or
  `result.json`. Scripts that use `--detach` must expect a non-zero exit.

### D9. Head and tail are built into the client

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Agents shorten long output with `| head` or `| tail`, and the
  pipeline's exit status is then that of `head` or `tail`, hiding a failure.
- **Decision:** `--head N` and `--tail N` on `run`, `wait`, and `log`. They
  affect only what is printed. The full output stays on disk and its path is on
  the summary line. If a caller pipes the client anyway and the reader goes
  away, the client keeps going and still exits with the code for the outcome.
- **Rationale:** Removes the reason to pipe, so the exit code always survives.
- **Rejected:** Documenting `set -o pipefail` (depends on every caller's shell).
- **Consequences:** Output is rendered from the stored log, so limited output is
  printed when the request finishes; unlimited output is streamed while it runs.

### D10. The broker treats the spool as hostile

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** D3 makes the broker, which runs with the operator's full
  authority, read and write in a directory an untrusted agent controls. That is
  the classic shape of a confused deputy. The agent runs as the same user, so
  file ownership tells the broker nothing.
- **Decision:** The rules in
  [protocol.md §4](protocol.md#4-how-the-broker-touches-a-spool): descriptor
  anchored opens that follow no link and never block; files created exclusively
  and never opened for writing; a single hard link required on what is read;
  fixed file names and validated identifiers; a back-reference required from
  linked worktrees; and no unpacking or parsing of the
  [snapshot](glossary.md#snapshot) on the host.
- **Rationale:** With these rules a symbolic link, a hard link, or a FIFO
  planted in the spool cannot make the broker read another file into a VM,
  overwrite a file outside the spool, or hang; and no agent-controlled byte is
  interpreted on the host.
- **Rejected:** Path-string checks such as resolving and comparing prefixes
  (raceable). Relying on `O_NOFOLLOW` alone (does nothing against hard links).
  Unpacking the snapshot on the host to inspect it (archive traversal on the
  host).
- **Consequences:** Spool access is concentrated in one module, `spool`, with
  adversarial tests for symbolic links, hard links, FIFOs, directories in place
  of files, and malformed names (`tests/spool_test.py`,
  `tests/broker_hostile_test.py`).

### D11. The broker proves it can see exit statuses before trusting one

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** The whole tool rests on `tart exec` reporting the guest command's
  exit status. If a Tart version did not, every run would report `passed`. And a
  non-zero status from `tart exec` could be Tart's own failure (the VM died) and
  not the command's.
- **Decision:** The [exit-status probe](glossary.md#exit-status-probe): after
  each boot and before any candidate code runs, the broker executes `exit 7` in
  the guest and requires status 7. After every setup and named command it runs
  the probe again, whatever Tart reported for that command: status 0, a
  non-zero status, or nothing before the time limit. Only if 7 is still
  reported is what the broker saw taken as the command's own doing. In every
  other case the outcome is `error`.
- **Rationale:** A cheap check, made before untrusted code can interfere, that
  turns a silent always-pass into a loud failure; and a second one that keeps a
  dead VM from being reported as a verdict. What Tart reports when a VM dies
  under a running command has not been observed
  ([open question Q9](open-questions.md#q9-what-does-tart-exec-report-when-the-vm-dies)),
  so no status is exempt: a VM that died must not become `passed` because Tart
  happened to exit 0, nor `timed-out` because Tart hung.
- **Rejected:** Trusting the documented behaviour. Having the command print its
  status into the output stream (the candidate could forge it). Repeating the
  probe only after a non-zero status (the first version of this decision): it
  left `passed` and `timed-out` resting on an unobserved property of Tart.
- **Consequences:** One extra guest command per boot, and one more after each
  command.

### D12. Requests execute one at a time, in the order first seen

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Apple's virtualization framework limits concurrent macOS guests,
  and VMs compete for the host's memory. Request IDs are chosen by clients, so
  an agent could pick one that sorts first.
- **Decision:** One broker per state directory; one request at a time. Within
  a checkout, requests run in the order the broker first saw them. Across
  checkouts, the one served longest ago goes next. Each checkout holds at most
  32 places in the queue and gets at most one turn per scan interval; requests
  beyond 32 are not acknowledged until earlier ones end. The broker is a single
  process without threads; every wait loop in it runs the spool scan, so
  heartbeats and acknowledgements continue while a request executes. Because
  the scan runs inside those waits, what it costs is bounded too: the number of
  linked worktrees served at once, and the time one scan spends listing spools
  (the values are in [protocol.md §2](protocol.md#2-identifiers-and-limits)).
- **Rationale:** Predictable resource use, no shared-state races, and no way to
  jump the queue by choosing an ID. An agent writes its own spool freely, so
  without the limits one checkout could hold up every other, fill the audit log
  as fast as the disk allows with requests that are refused at once, or make
  the broker remember an unbounded number of requests.
- **Rejected:** A worker pool (recorded in [deferred-work.md](deferred-work.md)).
  Ordering by request ID (agent-chosen). One global first-seen order (the first
  version of this decision): fair to requests, not to checkouts.
- **Consequences:** A long request delays the ones behind it; the status shows
  how many are ahead, counted across every checkout the broker serves. A
  checkout that floods its queue delays only itself. A spool filled with
  thousands of entries makes new requests, from any checkout, be noticed later,
  but cannot stall the request that is running.

### D13. Standard-library Python, installed with uv

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** The broker runs with the operator's authority, so every
  dependency is code the operator must trust.
- **Decision:** No runtime dependencies. Python 3.14 or newer, the version the
  gate runs. Installed as an isolated tool with `uv`, so running the client
  needs no network and writes nothing outside the checkout.
- **Rationale:** The smallest supply chain that can do the job; consistent with
  the sibling tools this repository lints itself with.
- **Rejected:** A compiled language (slower to audit and change here). A shim
  that resolves the environment on each run (writes caches at run time, which a
  sandbox may deny).
- **Consequences:** `uv` is required to install. An installed copy is a
  snapshot; re-running the installer upgrades it.

### D14. The snapshot is the working tree, not a commit

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Agents want to test before committing, and new files are
  untracked until then.
- **Decision:** The [snapshot](glossary.md#snapshot) contains every tracked file
  as it is on disk plus every untracked file that is not ignored. Symbolic links
  are stored as links. Submodule contents are not included. Git's own `GIT_*`
  environment variables are ignored when listing files.
- **Rationale:** Tests what the agent actually has. Honouring the ignore rules
  keeps build output, virtual environments, and ignored secret files out.
- **Rejected:** Requiring a commit (forces commits of untested code). Including
  ignored files (large, and where local secrets usually live).
- **Consequences:** An untracked, un-ignored secret file would be copied into
  the VM. It stays on the machine, and the run network is isolated by default,
  but the README tells users to ignore such files.

### D15. Contracts live in contracts/, one per executable

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Each script in this repository is paired with a contract (what it
  may do) and tests (what holds it to that). The two programs here are console
  entry points of one Python package made of several modules, not single files.
- **Decision:** One contract per executable, named after the command:
  `contracts/taste_test_contract.md` for `taste-test` and
  `contracts/taste_test_broker_contract.md` for `taste-test-broker`. Each names
  the modules that make up its program. Tests live in `tests/`, in files named
  `<subject>_test.py`: most subjects are a module, some are a concern that
  spans modules (a hostile spool, the broker's limits, the documentation). Each
  contract lists the test files that enforce it. The install script is a single
  file and keeps its contract and test beside it at the repository root.
- **Rationale:** The unit a reviewer approves is the program an operator or an
  agent runs, with one capability surface. Keeping contracts out of `src/` keeps
  them out of the installed package.
- **Rejected:** A contract per module (some twenty documents for two capability
  surfaces). Contracts beside the modules in `src/` (shipped in the wheel, and
  no single file is "the script").
- **Consequences:** A change to any module is checked against the contract of
  each executable that imports it.

### D16. Every VM name carries the state key

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Tart has one namespace of VM names per user, while the broker's
  lock is per state directory. Two brokers with different state directories (a
  test and a real one) would otherwise delete each other's VMs at start-up.
- **Decision:** Every VM the broker creates is named
  `tastetest-<state key>-<kind>-…`
  ([vm-handling.md §1](vm-handling.md#1-tart-invocations)), and start-up cleanup and
  `prune` touch only names carrying their own [state key](glossary.md#state-key).
  Run and doctor VMs use a random token chosen by the broker, not the request
  ID.
- **Rationale:** Cleanup can be unconditional for the broker's own names and
  never touches anything else, including the operator's own VMs.
- **Rejected:** A single per-user lock (a test run would then be blocked by, or
  block, the operator's real broker).
- **Consequences:** Moving the state directory orphans its VMs; they are removed
  by hand with `tart delete`.

### D17. A repository config names a registry image, not a local VM

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** `vm.image` is cloned by name, and a name can also be a local VM:
  another repository's environment image, or a personal VM of the operator's
  that holds credentials.
- **Decision:** `vm.image` must be a registry reference (it contains a `/`) and
  may never begin with `tastetest-`. Local VM names are accepted only when the
  operator starts the broker with `--allow-local-images`.
- **Rationale:** A config, which under `--trust-local` an agent can influence,
  must not be able to run candidate code inside a VM that was not built for it.
- **Rejected:** Accepting any name Tart accepts.
- **Consequences:** Operators who build their own base images either push them
  to a registry or opt in with the flag.

### D18. Stopping the broker is cooperative

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** A signal can arrive at any instruction, including in the middle
  of stopping and deleting a VM. An exception raised from a signal handler
  there leaves a VM running, with candidate code in it, after the broker has
  gone. The same question arises when the spool or the registration a request
  came from disappears while it runs.
- **Decision:** SIGTERM, SIGINT, and the loss of a request's spool or
  registration only set a flag. The step every wait in the broker already calls
  turns the flag into an exception, and only while a request is in flight and
  no clean-up is running. The request then ends as `error` through the ordinary
  path: its processes are killed, its VM is stopped and deleted, its result and
  audit line are written. Clean-up is never interrupted. The spool a request
  came from is kept open until that request has its result.
- **Rationale:** One path for every way a request can be abandoned, and that
  path is the tested clean-up path. A request never loses its result because
  the directory it came from changed underneath the broker.
- **Rejected:** Raising from the signal handler (it interrupts clean-up).
  Finishing the request before exiting (a run may last hours). Letting a second
  signal skip clean-up (SIGKILL already does that, and the next start-up cleans
  up after it).
- **Consequences:** Ctrl-C takes as long as stopping and deleting one VM. A
  wait that does not call the scan step would delay stopping, so every wait
  loop in the broker (for a process to exit, for guest output, between
  attempts) calls it at least every 0.2 seconds.

### D19. A command says where its arguments go

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Request arguments are the shell's positional parameters. The
  first design appended ` "$@"` to the command string when `allow_args` was
  true. That gives the arguments to whatever happens to be last in the string:
  after a pipe, a redirection, or a comment, they go to the wrong command or
  nowhere, and the run still looks normal.
- **Decision:** The command string is used exactly as written. A command with
  `allow_args = true` must contain `"$@"`; a config without it is rejected.
- **Rationale:** The author of the config, whose change is reviewed, decides
  where untrusted arguments land. Nothing is added to a script behind their
  back, and a config that would drop the arguments fails loudly.
- **Rejected:** Appending (ambiguous, as above). Parsing the command string to
  find the right place (a shell parser on the host, for text the guest
  interprets).
- **Consequences:** A config that set `allow_args = true` under the first
  design needs `"$@"` added. The check is textual, so it accepts a `"$@"` that
  sits in a comment; review of the default branch is the control for that.

### D20. A checkout is reached without following links, and worktree claims are compared as text

- **Status:** accepted. **Date:** 2026-11-15. **Supersedes:** none.
- **Context:** Worktree records live in the repository's `.git` directory,
  which an agent can write, and they contain paths. Resolving such a path
  through symbolic links lets the agent choose what is resolved and when: the
  link can change between the check and the use, and it can point into a
  network or automounted file system that makes the broker wait.
- **Decision:** A checkout root is opened one component at a time from `/`
  without following any link. A record's paths are made absolute and normalised
  as text only, and compared as text with the registered repository path, which
  `register` resolved once. The working tree's side of the claim is read
  through the descriptor of the directory that was opened. A linked worktree
  must be on a volume the repository's own path crosses.
- **Rationale:** The directory that was checked is the directory that is
  served, and no agent-chosen path is ever handed to the kernel to resolve
  through links.
- **Rejected:** `realpath` on both sides and comparing the results (the first
  design). Refusing linked worktrees altogether (agents commonly work in them).
- **Consequences:** A worktree reached through a symbolic link, or on another
  volume, is not served, and the broker says so once. A repository registered
  under a path whose letter case differs from what Git recorded has its linked
  worktrees refused.
