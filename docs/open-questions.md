---
id: open-questions
title: Open questions
is_source_of_truth: true
derives_from: [design, protocol]
status: current
authority: >
  Canonical home for questions that are not yet settled. The design does not
  guess around an entry here. A resolved question keeps its entry, marked
  resolved, with a link to where the answer now lives.
---

# Open questions

Every question below needs evidence from a real [Tart](glossary.md#tart) VM or
from infrastructure outside this repository. The software was written and tested
in a sandbox that cannot start a VM: the test suite replaces Tart with a
stand-in, and the Tart command lines were checked only for acceptance by Tart
2.41.1. Each dependency on real behaviour is therefore either guarded, so that a
wrong assumption produces outcome `error` instead of a false pass, or stated
here as unverified.

Each entry gives: the question, whether it blocks V1, the evidence that closes
it, the owner, what it depends on, the step that resolves it, and where the
answer will be recorded. No task tracker exists for this repository yet, so the
resolving step is described in place. `taste-test-broker doctor --image` is the
instrument for Q1 to Q5 and Q8: its `PASS`, `WARN`, and `FAIL` lines are the
evidence.

| ID | Status | Blocks V1 |
|---|---|---|
| [Q1](#q1-does-tart-exec-report-the-guest-commands-exit-status) | open | No (guarded) |
| [Q2](#q2-what-can-a-vm-reach-in-each-network-mode) | open | Yes, for relying on `isolated` to contain untrusted code |
| [Q3](#q3-does-tart-exec-work-while-the-vm-network-is-isolated) | open | Yes, for the default run network mode |
| [Q4](#q4-which-images-meet-the-image-requirements) | open | No (per-repository choice, guarded) |
| [Q5](#q5-do-guest-arguments-arrive-unchanged) | open | Only for commands with `allow_args` |
| [Q6](#q6-can-the-client-run-inside-the-target-enterprise-sandbox) | open | Yes, for unattended agent use |
| [Q7](#q7-how-does-ci-get-read-access-to-the-linter-repositories) | open | No (CI only) |
| [Q8](#q8-what-does-tart-list-print) | open | No (guarded) |
| [Q9](#q9-what-does-tart-exec-report-when-the-vm-dies) | open | No (guarded) |
| [Q10](#q10-do-the-install-scripts-tests-pass) | open | Yes, for relying on the install script |

### Q1. Does tart exec report the guest command's exit status?

- **Question:** With the installed Tart version, does `tart exec <vm> <command>`
  exit with the status of `<command>` in the [guest](glossary.md#guest)?
- **Blocks V1:** No. The
  [exit-status probe](glossary.md#exit-status-probe)
  ([decision D11](decisions.md#d11-the-broker-proves-it-can-see-exit-statuses-before-trusting-one))
  turns a "no" into outcome `error` on every request, never a false `passed`.
- **Evidence needed:** `doctor --image <image>` prints
  `PASS exit status is reported`.
- **Owner:** operator.
- **Depends on:** a host where Tart can start VMs, and one bootable image.
- **Resolving step:** first operator run of `doctor --image` after installation.
- **Answer goes to:** [vm-handling.md §2](vm-handling.md#2-guest-layout-and-steps),
  with the Tart version tested.

### Q2. What can a VM reach in each network mode?

- **Question:** Under `isolated` (`--net-softnet-block=0.0.0.0/0`), can the VM
  reach the host side of its network bridge, a DNS resolver, or anything over
  IPv6? Under `internet`, Tart's help states that the bridge's host side is
  reachable; which host services does that expose in practice?
- **Blocks V1:** Yes, for treating `isolated` as containment of untrusted code.
  Until answered, the only claim made is the one Tart documents: no IPv4
  destination is allowed. [interfaces.md §4](interfaces.md#4-repository-config-taste-testtoml)
  says exactly that and no more.
- **Evidence needed:** `doctor --image <image> --network isolated` prints
  `PASS isolated: a public IPv4 address is not reachable`,
  `PASS isolated: a public IPv6 address is not reachable`, and either
  `PASS isolated: the host side of the VM bridge does not answer the VM` or the
  corresponding `WARN`.
- **Owner:** operator.
- **Depends on:** [Softnet](glossary.md#softnet) installed; an image with
  `curl` (without it the checks print `WARN ... was not checked`).
- **Resolving step:** the `doctor` run of Q1, with `--network isolated`, and
  once more with `--network internet`.
- **Answer goes to:** the network-mode table in interfaces.md §4. If the host
  side of the bridge answers under `isolated`, the table says so and
  [decision D7](decisions.md#d7-network-access-is-a-declared-mode-restricted-by-default-never-relaxed-silently)
  is revisited.

### Q3. Does tart exec work while the VM network is isolated?

- **Question:** With every IPv4 destination blocked, can the host still reach
  the [guest agent](glossary.md#guest-agent)? The design assumes the guest
  agent is reached over a channel that does not depend on the guest's IP
  network.
- **Blocks V1:** Yes for `run.network = "isolated"`, the default. If the answer
  is no, isolated runs end in outcome `error` (the guest agent never answers);
  nothing unsafe happens, but the default would be unusable.
- **Evidence needed:** `doctor --image <image> --network isolated` prints
  `PASS guest agent answers with network mode isolated`.
- **Owner:** operator.
- **Depends on:** Softnet installed; Q4.
- **Resolving step:** the same `doctor` run as Q2.
- **Answer goes to:** [vm-handling.md §1](vm-handling.md#1-tart-invocations). If the
  answer is no, a new decision replaces the flags for `isolated`.

### Q4. Which images meet the image requirements?

- **Question:** Which published images contain the Tart Guest Agent, `/bin/sh`,
  `tar`, and `xargs`, and set `HOME` for commands run through the guest agent
  (the requirements in interfaces.md §4)?
- **Blocks V1:** No. The image is chosen per repository; an unsuitable one ends
  each request with outcome `error` and a reason naming what is missing.
- **Evidence needed:** `doctor --image <image>` with no `FAIL` line, for each
  image a repository intends to use. For this repository's own
  `.taste-test.toml`, also a setup that completes on the image: on arm64 Linux
  `uv sync --locked` has to build `shellcheck-py`, for which the lockfile has
  no prebuilt package, and that build has not been tried.
- **Owner:** operator, per repository.
- **Depends on:** nothing.
- **Resolving step:** `doctor --image` before a repository's first
  `.taste-test.toml` is merged.
- **Answer goes to:** the README's "Images known to work" section, which is
  empty until the first such run.

### Q5. Do guest arguments arrive unchanged?

- **Question:** Does `tart exec` deliver each argument after the VM name to the
  guest as its own item, unchanged, including values that begin with `-`,
  contain spaces, or are empty? Tart 2.41.1 was seen to accept option-like items
  there without interpreting them; delivery into a guest has not been observed.
- **Blocks V1:** Only for [named commands](glossary.md#named-command) with
  `allow_args = true`. Commands without arguments use fixed argument vectors.
- **Evidence needed:** `doctor --image <image>` prints
  `PASS guest arguments arrive intact`.
- **Owner:** operator.
- **Depends on:** Q4.
- **Resolving step:** the same `doctor` run as Q1.
- **Answer goes to:** [protocol.md §4](protocol.md#4-how-the-broker-touches-a-spool).
  If the answer is no, `allow_args` is withdrawn until arguments are delivered
  another way.

### Q6. Can the client run inside the target enterprise sandbox?

- **Question:** Does the sandbox policy applied to agents allow executing
  `taste-test`, running `git rev-parse` and `git ls-files` in the
  [checkout](glossary.md#checkout), reading the working tree, and creating,
  renaming, and deleting files under `.taste-test/` there? Those are the only
  capabilities the [client](glossary.md#client) needs; its
  [contract](../contracts/taste_test_contract.md) lists the exact commands.
- **Blocks V1:** Yes for unattended use by sandboxed agents; no for use from an
  ordinary shell.
- **Evidence needed:** an agent in the target sandbox runs `taste-test status`
  and `taste-test run` against a running [broker](glossary.md#broker) and
  receives exit code 0 or 1.
- **Owner:** operator.
- **Depends on:** the organisation's sandbox policy; the tool being installed
  somewhere the sandbox may execute from.
- **Resolving step:** first end-to-end run from an agent session.
- **Answer goes to:** the README's sandbox section; and, if a capability is
  denied, a new entry in [deferred-work.md](deferred-work.md).

### Q7. How does CI get read access to the linter repositories?

- **Question:** The CI job installs glossarian and llm-size-guard from two
  private repositories using a secret named `LINTER_REPOS_TOKEN`. Does that
  secret exist, and does the HTTPS rewrite in the workflow authenticate
  correctly?
- **Blocks V1:** No. It affects CI only; the same gate runs locally.
- **Evidence needed:** one green run of the `ci` workflow.
- **Owner:** operator.
- **Depends on:** a pushed branch and a provisioned repository secret.
- **Resolving step:** the first push to the remote.
- **Answer goes to:** the comment above the install step in
  `.github/workflows/ci.yml`.

### Q8. What does tart list print?

- **Question:** Does `tart list --source local --quiet` print exactly one local
  VM name per line and nothing else, with the installed Tart version? The
  [broker](glossary.md#broker) uses that listing to find leftover VMs of its
  own, to decide whether an [environment image](glossary.md#environment-image)
  exists, and to confirm that a VM it deleted is gone.
- **Blocks V1:** No. `tart stop` and `tart delete` are issued for a VM whether
  or not the listing shows it, so a listing the broker cannot read does not
  leave a [run VM](glossary.md#run-vm) behind at the end of a request. What a
  wrong format would break is start-up cleanup after a killed broker, `prune`,
  and reuse of environment images (each request would rebuild).
- **Evidence needed:** `doctor --image <image>` prints
  ``PASS a cloned VM appears in `tart list --source local --quiet` ``.
- **Owner:** operator.
- **Depends on:** Q4.
- **Resolving step:** the same `doctor` run as Q1.
- **Answer goes to:** [vm-handling.md §1](vm-handling.md#1-tart-invocations), with the
  Tart version tested.

### Q9. What does tart exec report when the VM dies?

- **Question:** When a VM stops, or its [guest agent](glossary.md#guest-agent)
  goes away, while `tart exec` is waiting for a command, does `tart exec` exit
  non-zero, exit 0, or keep waiting?
- **Blocks V1:** No. The broker does not depend on the answer: after every
  command it repeats the [exit-status probe](glossary.md#exit-status-probe),
  and reports outcome `error` unless the probe still succeeds
  ([decision D11](decisions.md#d11-the-broker-proves-it-can-see-exit-statuses-before-trusting-one)).
  The test suite covers all three answers with a stand-in for Tart.
- **Evidence needed:** an observation on a real host: start a long command with
  `tart exec`, run `tart stop` on the VM from another shell, and record what
  the first command does.
- **Owner:** operator.
- **Depends on:** a host where Tart can start VMs.
- **Resolving step:** once, when the first image is set up; not required before
  use.
- **Answer goes to:** [vm-handling.md §2](vm-handling.md#2-guest-layout-and-steps), as
  a note beside the probe step.

### Q10. Do the install script's tests pass?

- **Question:** Does `bash taste_test_install_test.sh` pass, and with it
  `make test` and `make commit`? The session that wrote the install script was
  not permitted to execute shell scripts, so the script and its tests were
  checked by reading and by shellcheck only. The Python test suite and every
  other gate were run.
- **Blocks V1:** Yes, for relying on the install script. The two commands can
  be installed without it (`uv tool install .` from a reviewed checkout).
- **Evidence needed:** `make commit` exits 0 on macOS.
- **Owner:** operator.
- **Depends on:** nothing.
- **Resolving step:** run `make commit` once, before the first commit.
- **Answer goes to:** the change log of `taste_test_install_contract.md`, which
  stays at v0 until then, and
  [design.md §9](design.md#9-current-and-planned).
