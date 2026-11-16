---
id: deferred-work
title: Deferred work
is_source_of_truth: true
derives_from: [design]
status: current
authority: >
  Canonical list of work that belongs to tart-taste-test but is not in V1.
  Things the product deliberately does not do are in out-of-scope.md instead.
---

# Deferred work

Each entry gives a target horizon, the reason for deferral, the dependency or
trigger that would start it, an owner, and how it is tracked. No issue tracker
is set up for this repository yet; until one is, the entry here is the tracking
record and its ID is the handle. Nothing below is needed for V1's acceptance:
V1 is a single operator running one [broker](glossary.md#broker) for agents on
the same machine.

### DW1. Start the broker automatically at login

- **What:** A launchd agent (installed and removed by the install script) that
  keeps `taste-test-broker serve` running.
- **Horizon:** first release after V1.
- **Why deferred:** V1 keeps the broker a foreground process the operator can
  see and stop, until the behaviour against real VMs has been observed.
- **Trigger:** [open questions](open-questions.md) Q1 and Q2 closed.
- **Owner:** operator. **Tracking:** this entry.

### DW2. Run more than one request at a time

- **What:** A worker pool with a concurrency limit.
- **Horizon:** when queueing becomes a measured problem.
- **Why deferred:** see
  [decision D12](decisions.md#d12-requests-execute-one-at-a-time-in-the-order-first-seen).
- **Trigger:** agents regularly waiting behind each other.
- **Owner:** operator. **Tracking:** this entry.

### DW3. Rebuild the environment only when its inputs change

- **What:** Key the [environment image](glossary.md#environment-image) on the
  content of files the [repository config](glossary.md#repository-config) names
  (lockfiles, the setup script) instead of on the whole default-branch commit.
- **Horizon:** first release after V1.
- **Why deferred:** keying on the commit is always correct; the narrower key
  needs a config field and a rule for stale default-branch source.
- **Trigger:** rebuild time after each merge becomes a complaint.
- **Owner:** operator. **Tracking:** this entry.

### DW4. Operator allowlist for VM images

- **What:** A broker option restricting `vm.image` to registries or patterns the
  operator approves.
- **Horizon:** before the tool is used with `--trust-local` repositories by more
  than one person.
- **Why deferred:** V1 already refuses local VM names and this project's own VM
  names ([decision D17](decisions.md#d17-a-repository-config-names-a-registry-image-not-a-local-vm));
  what remains is choosing between registries, and an image only ever runs
  inside the VM boundary.
- **Trigger:** a shared machine, or a policy on which registries may be pulled.
- **Owner:** operator. **Tracking:** this entry.

### DW5. Submodule contents in the snapshot

- **What:** Include the working trees of Git submodules in the
  [snapshot](glossary.md#snapshot).
- **Horizon:** when a repository that uses submodules adopts the tool.
- **Why deferred:** needs recursion into nested repositories and a rule for
  their ignore files; no current user needs it.
- **Trigger:** such a repository.
- **Owner:** operator. **Tracking:** this entry.

### DW6. Retrieve files from the run VM

- **What:** Let a [named command](glossary.md#named-command) declare result
  files (coverage data, JUnit XML) that are copied back with the output.
- **Horizon:** first release after V1.
- **Why deferred:** copying files out of an untrusted VM onto the
  [host](glossary.md#host) needs the same care as the spool (size limits, no
  path chosen by the guest) and is not needed for a pass/fail verdict.
- **Trigger:** a reviewer workflow that consumes coverage.
- **Owner:** operator. **Tracking:** this entry.

### DW7. Policy ceilings in a file

- **What:** Read the [policy ceilings](glossary.md#policy-ceiling) from a file in
  operator state in addition to `serve` flags.
- **Horizon:** together with DW1, which has no command line to carry flags.
- **Why deferred:** flags are sufficient while the broker is started by hand.
- **Trigger:** DW1.
- **Owner:** operator. **Tracking:** this entry.

### DW8. A command that writes a starter repository config

- **What:** `taste-test-broker init` that writes a commented `.taste-test.toml`.
- **Horizon:** first release after V1.
- **Why deferred:** the README carries a complete example.
- **Trigger:** more than a handful of repositories adopting the tool.
- **Owner:** operator. **Tracking:** this entry.

### DW9. Serve a remote repository while offline

- **What:** An operator option to use the last fetched default-branch commit
  when a fetch fails.
- **Horizon:** when operators report being blocked while offline.
- **Why deferred:** V1 fails closed on a failed fetch (see
  [decision D5](decisions.md#d5-the-default-branch-is-read-from-a-broker-owned-mirror));
  a fallback must be an explicit choice, and must say so in every result.
- **Trigger:** such a report.
- **Owner:** operator. **Tracking:** this entry.

### DW10. Detect a replaced environment image

- **What:** Record, in operator state, an identity for each
  [environment image](glossary.md#environment-image) when it is built, and
  check it before the image is reused, so an image that something else wrote
  under the same name is rebuilt instead of trusted.
- **Horizon:** first release after V1.
- **Why deferred:** V1 assumes agents cannot write the Tart VM store
  ([design.md §6](design.md#6-trust-boundaries)), as it assumes for operator
  state. A check needs an identity Tart exposes for a local VM, which has to be
  established against a real installation first.
- **Trigger:** a deployment where the agent sandbox cannot be made to deny the
  VM store, or [open question Q8](open-questions.md#q8-what-does-tart-list-print)
  closed.
- **Owner:** operator. **Tracking:** this entry.

### DW11. Keep the audit log from growing without limit

- **What:** A way to archive or cap the [audit log](glossary.md#audit-log)
  that does not let an [agent](glossary.md#agent) erase its history, for
  example rotation into dated files that are only ever removed by the operator.
- **Horizon:** first release after V1.
- **Why deferred:** V1 bounds how fast the log can grow: one line per checkout
  every 2 seconds, each line bounded in what it takes from the request
  ([protocol.md §5](protocol.md#5-operator-state)). An agent that keeps
  submitting requests can still add on the order of tens of megabytes a day per
  checkout. Rotation that deletes old lines would turn that into a way to push
  earlier lines out of the record, which is worse than a large file.
- **Trigger:** an operator reports the size of the log as a problem.
- **Owner:** operator. **Tracking:** this entry.
