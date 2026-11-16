---
id: design
title: tart-taste-test design
is_source_of_truth: true
derives_from: [glossary]
status: current
authority: >
  The central, declarative description of what tart-taste-test is and how its
  parts fit together. Detailed values live in the documents named in the
  document map; rationale lives in the decision log.
---

# tart-taste-test design

This document describes the software as it is built and tested. One caveat
applies throughout and is stated once: the test suite replaces Tart with a
stand-in, and nothing has yet been run against a real virtual machine. §9 says
exactly what is verified and what is not.

## 1. Purpose, users, and boundary

An [agent](glossary.md#agent) that has just written code needs to run that
code's tests before anyone reviews it. Running unreviewed code on the
[host](glossary.md#host) is the thing review exists to prevent, and reviewing
before any test has run wastes the reviewer on defects a test would have found.

tart-taste-test breaks the circle. The agent asks for a test run; the
[candidate](glossary.md#candidate) is executed inside a throwaway virtual
machine whose environment was built from the reviewed
[default branch](glossary.md#default-branch); the output and one unambiguous
[exit code](glossary.md#exit-code) come back to the agent. The untested code
never runs on the host.

Users:

- **Agents** run `taste-test run` from inside their sandbox and act on the exit
  code. They need no permission beyond writing inside their own
  [checkout](glossary.md#checkout).
- **Operators** install the tool, register repositories, and keep a
  [broker](glossary.md#broker) running. After that one-time setup no operator
  action is needed per test run.
- **Reviewers** (human or agent) can confirm what was actually tested from the
  [audit log](glossary.md#audit-log).

Boundary: the project wraps [Tart](glossary.md#tart) on an Apple silicon Mac. It
decides what runs in a VM and reports what happened. It does not review code,
does not replace CI, and does not defend against a break-out from the
hypervisor. What is deliberately excluded is listed in
[out-of-scope.md](out-of-scope.md).

## 2. System context

```text
  sandbox (agent)                         │  operator session (no sandbox)
                                          │
  agent ──runs──▶ taste-test (client)     │      taste-test-broker (broker)
                     │   ▲                │         │        │        │
        writes       │   │ reads          │  reads/ │        │ git    │ tart
        request +    ▼   │ status,        │  writes │        ▼        ▼
        snapshot   <checkout>/.taste-test/spool ◀───┘     mirror    VMs (guests)
                                          │                 ▲          │
                                          │        fetch    │          │ runs the
                                          │   default branch│          ▼ candidate
                                          │            Git remote   restricted network
```

The only channel between the two sides is the [spool](glossary.md#spool), a
directory of plain files inside the checkout. Nothing listens on a port, and the
agent never needs to start a process outside its sandbox.

## 3. Components and ownership

| Component | Module | Side | Responsibility |
|---|---|---|---|
| Client CLI | `client_cli` | agent | Argument parsing, help text, exit. |
| Client | `client` | agent | Wait for a request and report its outcome; `log`, `status`, `cancel`. |
| Client spool | `client_spool` | agent | The client's paths in the spool; submit, withdraw, prune; validated reads. |
| Snapshot | `snapshot` | agent | Build the [snapshot](glossary.md#snapshot) of the candidate. |
| Renderer | `render` | agent | Print stored output with `--head`/`--tail`. |
| Exit codes | `exitcodes` | both | The exit-code table and the outcome-to-code map. |
| Protocol | `protocol` | both | Spool file names, identifiers, limits, and document shapes. |
| Broker CLI | `broker_cli` | operator | Argument parsing for the operator commands; `register`, `unregister`, `list`, `prune`. |
| Broker | `broker` | operator | Run the scan step, claim and validate requests, write results; stop cleanly. |
| Served spools | `served` | operator | Keep the open spools equal to what the registry and the checkouts say; bound their number and the time spent listing them. |
| Checkouts | `checkouts` | operator | Find the claims to checkouts; open a checkout root without following a link; verify worktree records. |
| Spool access | `spool` | operator | Descriptor-anchored reads and writes in an agent-controlled directory. |
| Queue | `fairqueue` | operator | Which waiting request runs next: first seen first, checkouts in turn, within limits. |
| Audit | `audit` | operator | The record of a request: the result document, the bounded audit-log line, and which request is in flight. |
| Notices | `notices` | operator | The broker's terminal output: safe to print, bounded, and never able to fail its caller. |
| Subprocesses | `process` | operator | Start, wait for, and end the broker's Git and Tart processes; none is left behind. |
| Engine | `engine` | operator | Build environment images and run requests in VMs. |
| Tart adapter | `tart` | operator | The only code that builds a Tart command line; VM names. |
| Mirror | `mirror` | operator | Fetch and read the default branch. |
| Registry | `registry` | operator | Registered repositories and operator state paths. |
| Repository config | `repo_config` | operator | Parse and validate `.taste-test.toml`; apply policy ceilings. |
| Doctor | `doctor` | operator | Installation and live-VM checks. |

Shared interfaces each have exactly one owner. Other modules import from the
owner; they do not restate its values.

| Shared thing | Owner | Specified in |
|---|---|---|
| Exit codes and the outcome-to-exit-code map | `exitcodes` | [interfaces.md §2](interfaces.md#2-exit-codes) |
| Spool layout, file names, JSON schemas, identifier patterns, limits | `protocol` | [protocol.md §1–§3](protocol.md#1-spool-layout) |
| Descriptor-anchored spool access | `spool` | [protocol.md §4](protocol.md#4-how-the-broker-touches-a-spool) |
| Which claims to a checkout exist, and how a checkout root is opened | `checkouts` | [protocol.md §4](protocol.md#4-how-the-broker-touches-a-spool) |
| Which spools are open, and the limits on their number and listing time | `served` | [protocol.md §2](protocol.md#2-identifiers-and-limits) |
| What an audit-log line holds, and the record of the request in flight | `audit` | [protocol.md §5](protocol.md#5-operator-state) |
| How many interrupted requests are logged at start-up | `broker` | [protocol.md §2](protocol.md#2-identifiers-and-limits) |
| How many lines about linked worktrees are printed | `notices` | [protocol.md §2](protocol.md#2-identifiers-and-limits) |
| Keeping a closed output from changing an exit code | `exitcodes` | [interfaces.md §2](interfaces.md#2-exit-codes) |
| Queue order and its limits | `fairqueue` | [protocol.md §2](protocol.md#2-identifiers-and-limits) |
| How host subprocesses are started and ended | `process` | [vm-handling.md §1](vm-handling.md#1-tart-invocations) |
| Repository config schema, network modes, policy ceilings | `repo_config` | [interfaces.md §4](interfaces.md#4-repository-config-taste-testtoml) |
| Tart command lines and VM names | `tart` | [vm-handling.md §1](vm-handling.md#1-tart-invocations) |
| Guest scripts and steps | `engine` | [vm-handling.md §2](vm-handling.md#2-guest-layout-and-steps) |
| Vocabulary | [docs/glossary.md](glossary.md) | the glossary itself |

Callers and consumers: the client's caller is an agent or a person at a shell,
and its exit code and summary line are consumed by that caller. The broker's
only consumers are the client (through the spool) and the operator (through its
terminal output and the audit log). Nothing else calls into the package.

## 4. Module boundaries

Each module does one of the jobs in §3 and stays small enough for an LLM to
hold whole: the size guard warns at 500 lines and fails at 800
(`.llm-file-size-guard.toml`). A module that approaches the warning level is
split along a responsibility in the table above, never by moving arbitrary
functions into a `utils` module.

The agent side may import exactly `client_cli`, `client`, `client_spool`,
`snapshot`, `render`, `protocol`, `exitcodes`, and the standard library. A test
loads the client and fails if any other module of the package was imported, so
the code path an agent runs contains nothing that can start a VM or read
operator state. This is defence in depth, not the security boundary: both
executables are installed together, and what actually stops a sandboxed agent
from acting as a broker is its sandbox denying Tart's storage and the operator
state directory.

Each executable has a contract in `contracts/` that states what it may do; see
[decision D15](decisions.md#d15-contracts-live-in-contracts-one-per-executable).

## 5. How a request flows

1. **Submit.** The client lists the candidate's files with Git, writes them to
   a [snapshot](glossary.md#snapshot) in the spool, then writes `request.json`.
   The [request](glossary.md#request) names a
   [named command](glossary.md#named-command) and may carry arguments; it
   carries no command text, image, or setting.
2. **Acknowledge.** Within its scan interval the broker sees the request and
   writes a `queued` status; this happens before the request is validated. The
   client, seeing no status within the
   [acknowledgement window](glossary.md#acknowledgement-window), withdraws the
   request and exits with code 6.
3. **Claim and validate.** When the request's turn comes, the broker claims it
   by renaming the request file, reads what it claimed, and validates it. It
   copies the snapshot to private staging and checks its size and digest.
4. **Default branch.** The broker refreshes the [mirror](glossary.md#mirror) and
   reads the [repository config](glossary.md#repository-config) from the
   default-branch commit. It rejects the request if the config is missing,
   invalid, or exceeds a [policy ceiling](glossary.md#policy-ceiling).
5. **Environment.** If no [environment image](glossary.md#environment-image)
   exists for that commit, the broker builds one: default-branch source, setup
   commands, setup network mode.
6. **Run.** The broker clones the environment image into a
   [run VM](glossary.md#run-vm), starts it with the run network mode, replaces
   the default-branch files with the candidate's, and runs the named command.
7. **Report.** The broker writes the output log and `result.json`, appends a
   line to the audit log, and deletes the run VM. The client prints the output
   (limited by `--head`/`--tail` if asked), prints the
   [summary line](glossary.md#summary-line), and exits with the code for the
   [outcome](glossary.md#outcome).

The exact files and states are in [protocol.md](protocol.md); the exact Tart
commands and guest steps are in [vm-handling.md](vm-handling.md).

## 6. Trust boundaries

| Input | Trust | Where it is used | Control |
|---|---|---|---|
| Operator's command line, including the remote URL typed at `register`, and the policy ceilings | Trusted | Broker | Validated for shape. The remote URL is never read from the repository. |
| [Operator state](glossary.md#operator-state): registry, mirrors | Trusted (written only by the broker) | Broker | Private directory; a registry that fails validation is an error. |
| Default-branch commit and its repository config | Reviewed content, bounded by policy | Broker, guest | Strict schema; values above a ceiling are rejected. With `--trust-local` this content is only as trustworthy as the local branch. |
| `request.json` | **Untrusted** | Broker | Size-limited JSON, validated key by key. Only a command *name* and guest-side arguments leave it. |
| Snapshot | **Untrusted** | Guest only | Opaque bytes on the host: one hard link, size and digest checked, never unpacked. |
| Every name inside the spool | **Untrusted** | Broker | Descriptor-anchored opens that follow no link and never block; files created exclusively, never opened for writing. |
| Linked-worktree records under `.git/worktrees` | **Untrusted** | Broker (to locate spools) | Read as small plain files; a worktree counts only if it points back at the repository, is reached without following a link, and is on a volume the repository's own path crosses. |
| The Tart VM store (`~/.tart`, or `TART_HOME`) | Trusted (assumed out of agents' reach) | Tart, on the broker's behalf | Not something the broker can enforce; `doctor` names the directory and checks its ownership and mode. See the assumption below. |
| Candidate code and its output | **Untrusted** | Guest; output stored on the host as bytes | Runs only in the run VM; nothing of the host is attached; run network mode `isolated` unless the operator raises the ceiling. |
| Guest output and spool text shown to people | **Untrusted** | Operator's terminal, client's standard error | Control characters replaced before printing. |
| The checkout the two programs were installed from | Trusted (assumed out of agents' reach) | The install script; the installed broker | Not something the tool can enforce. The install script refuses source with uncommitted changes, which catches an accident only. |

Two properties follow and are the core of the design.

**Host safety.** Nothing an agent controls is executed, unpacked, parsed as
anything but size-limited JSON, or interpreted as a command on the host. Request
arguments do appear on a host command line, but only as separate trailing items
of `tart exec`, after the VM name, the guest shell, the script, and a fixed
word, where Tart was seen to treat them as guest arguments and not as its own
options ([open question Q5](open-questions.md#q5-do-guest-arguments-arrive-unchanged)).
Git is never run in, or from, a directory an agent can write
([protocol.md §5](protocol.md#5-operator-state)). The worst a hostile request
can do is occupy one VM within the policy ceilings, and the worst a hostile
spool can do is go unserved, or make the broker slower to notice new requests
(within the limits of [protocol.md §2](protocol.md#2-identifiers-and-limits)).

The broker is only as trustworthy as the source it was installed from. It must
be installed from a checkout no agent can write, at a reviewed commit; an agent
that could change that checkout would be changing code that runs with the
operator's authority (the README's "Install" section, and the install script's
contract).

**Verdict integrity.** The command string, the image, and the network modes come
from the default branch and the operator, not from the candidate. A candidate
cannot change `run = "make test"` to `run = "true"`. It can still change any
file that command reads, including the Makefile and the tests themselves, so a
`passed` result is evidence for a reviewer, not a substitute for review.

**Assumption: agents cannot write the Tart VM store.** An
[environment image](glossary.md#environment-image) is reused by name, and the
name can be worked out from the state directory's path, the repository's path,
and the default-branch commit. Anything that can write the directory where Tart
keeps VMs could therefore replace an environment image, and later requests would
run in an environment the default branch did not define. The broker cannot see
sandbox rules, so this is an assumption about the deployment, on a par with
operator state being out of agents' reach: the sandbox applied to agents must
deny writes to `~/.tart` (or `TART_HOME`). `taste-test-broker doctor` prints the
directory and fails if other users can write it. Recording what each image was
built from, so a replaced image is detected, is
[deferred work DW10](deferred-work.md#dw10-detect-a-replaced-environment-image).

The agent's sandbox is the outer boundary and is not something this project
provides. The broker adds a second way for an agent's bytes to leave the
sandbox (into a VM), which is why the run network ceiling defaults to
`isolated`: an agent whose sandbox blocks the network must not gain it by asking
for a test run.

## 7. Errors

Every request ends in exactly one [outcome](glossary.md#outcome), and every
outcome maps to exactly one exit code. The classes are kept apart so an agent
never mistakes "the infrastructure failed" for "the tests failed" or, worse,
for "the tests passed":

- a verdict on the candidate: `passed`, `failed`, `timed-out`;
- no verdict, and not the candidate's doing: `setup-failed`, `error`;
- no verdict because the request did not run to completion: `rejected`,
  `cancelled` (which may have started), and the client-side endings *no broker*
  and *still pending*.

The broker fails closed. If it cannot observe exit statuses (the
[exit-status probe](glossary.md#exit-status-probe) fails after boot, or fails
when it is repeated after a command, whatever that command appeared to do),
cannot start the VM with the requested network mode, cannot refresh the default
branch, or cannot validate an input, the outcome is `error` or `rejected`, never
a verdict. A defect in the broker while handling one request ends that request
as `error` and the broker carries on.

A broker that is asked to stop (Ctrl-C, SIGTERM) while a request runs ends that
request as `error`, stops and deletes its VM, writes its result, and only then
exits. The same happens to a request whose repository is unregistered, or whose
spool is removed or replaced, while it runs. A broker that is killed outright
leaves a claimed request without a result; the next broker to start finishes it
as `error` and deletes any leftover VM of its own.

## 8. Invariants

Security:

- Candidate code executes only inside a run VM.
- A VM is started with a fixed set of flags
  ([vm-handling.md §1](vm-handling.md#1-tart-invocations)): no host directory, disk,
  port, or device is attached and clipboard sharing is off.
- Unless the operator raises `--max-run-network`, candidate code runs with
  network mode `isolated`. A network mode is never relaxed silently: if
  [Softnet](glossary.md#softnet) is missing the VM fails to start and the
  outcome is `error`.
- The broker never runs Git with an agent-writable checkout as its repository
  or as its working directory: Git starts in `/`, with inherited `GIT_*`
  variables removed. The single exception is `--trust-local`, where the
  checkout is the *source* of a fetch into the mirror.
- The broker reaches a checkout only by a path with no symbolic link in it, and
  serves a linked worktree only if that worktree points back at the registered
  repository.
- The broker never opens an existing spool file for writing and never follows a
  link inside a spool.
- The broker deletes only VMs whose names carry its own state key.

Reliability:

- Every host-side subprocess has a time limit except `tart run`, which the
  broker ends when its phase ends. Every log has a size limit. No subprocess
  outlives the call that started it: it runs in a session of its own, which is
  killed whole if the call ends early.
- A run VM is deleted when its request ends, including on error, cancellation,
  a request to stop the broker, and broker restart. A VM that cannot be deleted
  is reported and retried at the next start-up; it does not stop the broker.
- Nothing an agent writes can stop the broker or make it use more and more
  memory, file descriptors, or terminal output: what it remembers about
  requests and what it warns about are both bounded.
- Writes a reader depends on are atomic (write to a temporary name, rename).
- One broker per state directory; requests execute one at a time, each in the
  order the broker first saw it within its checkout, checkouts taking turns.

Operational:

- The exit code alone tells an agent what to do next; no output parsing is
  needed. For `run` and `wait`, 0 means the tests passed and nothing else.
- `--head` and `--tail` change what is printed, never the exit code and never
  what is stored.
- Every request the broker claims and finishes leaves one audit-log line the
  agent cannot alter. (Requests found already claimed when a broker starts are
  ended too, but only the first few per spool are logged:
  [protocol.md §5](protocol.md#5-operator-state).)

Compatibility:

- Every exchanged file carries `"schema": 1`. A reader rejects a schema it does
  not know instead of guessing.
- Exit codes 0 to 8 are a public interface; a meaning is never reassigned.

## 9. Current and planned

| Area | Status |
|---|---|
| Repository layout, quality gates, CI definition | Current. The CI workflow has not run yet ([open question Q7](open-questions.md#q7-how-does-ci-get-read-access-to-the-linter-repositories)). |
| The install script | Written and reviewed, **never executed**: its shell tests, and so `make test` and `make commit` as a whole, have not been run ([open question Q10](open-questions.md#q10-do-the-install-scripts-tests-pass)). Its contract stays at v0 until they have. |
| Client and broker, as specified in interfaces.md and protocol.md | Current: implemented, and exercised end to end by the test suite against a stand-in for Tart. |
| Tart command lines | Checked for acceptance by Tart 2.41.1 without starting a VM. |
| Behaviour against a real VM: exit-status reporting, the guest agent under `isolated`, what each network mode really allows, argument delivery, the VM listing format | **Unverified.** Each is an entry in [docs/open-questions.md](open-questions.md), and `taste-test-broker doctor --image` exists to produce the evidence. |
| Everything in [docs/deferred-work.md](deferred-work.md) | Deferred; not part of V1. |

## 10. Document map

| Role | Canonical file |
|---|---|
| Central design | `docs/design.md` (this file) |
| Glossary | [docs/glossary.md](glossary.md) |
| Command-line interfaces, exit codes, repository config | [docs/interfaces.md](interfaces.md) |
| Spool protocol, limits, operator state, how the broker touches a spool | [docs/protocol.md](protocol.md) |
| Tart commands, VM names, time limits, stopping, guest steps | [docs/vm-handling.md](vm-handling.md) |
| Decisions and their rationale | [docs/decisions.md](decisions.md) |
| Open questions | [docs/open-questions.md](open-questions.md) |
| Out of scope | [docs/out-of-scope.md](out-of-scope.md) |
| Deferred work | [docs/deferred-work.md](deferred-work.md) |
| Contract of the client | [contracts/taste_test_contract.md](../contracts/taste_test_contract.md) |
| Contract of the broker | [contracts/taste_test_broker_contract.md](../contracts/taste_test_broker_contract.md) |
| Contract of the install script | `taste_test_install_contract.md`, beside the script |

Ripple review: a change to a file on the left requires re-reading the files on
the right. `tests/docs_test.py` and `tests/exitcodes_test.py` check the quoted
values mechanically; the rest is by reading.

| Changed | Re-read |
|---|---|
| `glossary.md` | every document (links are checked by `make docs-lint`) |
| `interfaces.md` | `protocol.md`, `README.md`, both contracts, `exitcodes`, `repo_config`, both CLI modules |
| `protocol.md` | `interfaces.md`, `vm-handling.md`, both contracts, `protocol`, `spool`, `checkouts`, `served`, `fairqueue`, `audit`, `notices`, `broker`, `mirror` |
| `vm-handling.md` | `protocol.md`, the broker's contract, `tart`, `engine`, `process`, `broker` |
| `decisions.md` | `design.md`, and the document the decision governs |
| `design.md` | `README.md`, `open-questions.md`, `out-of-scope.md`, `deferred-work.md` |
| a contract | the modules it names, their tests, and `interfaces.md` or `protocol.md` where it cites them |

Local, untracked directories and what they are for: `.gate-scratch/` holds
throwaway files of the Makefile gates, `.assessments/` holds
review records of the work (findings and how each was resolved), and
`.checklists/_authoring/` and `.checklists/rendered/` are reserved for local
planning scratch and generated views. None of them is a source of truth; a
lasting conclusion is moved into a document in this map.
