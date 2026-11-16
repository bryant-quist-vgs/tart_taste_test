---
id: glossary
title: Glossary
is_source_of_truth: true
derives_from: []
status: current
---

# Glossary

Canonical vocabulary for tart-taste-test. Other documents link here; they do not redefine
terms. Each `###` entry under Definitions is one term; link to it by this file's relative
path plus `#` and the term's slug.

## Governance

```glossary
governs: ../
```

This glossary governs every document in the repository. A term is added, renamed, or
retired in the same change that introduces, renames, or removes the thing it names. A
renamed term keeps a row in the renames table, and the rename is applied across the whole
repository in the same change. `make docs-lint` checks this file's structure and resolves
every link to it from any Markdown file in the repository.

## Definitions

### agent
```glossary
short: The automated worker that writes code and asks for it to be tested.
confusable: client, operator
```
An automated coding assistant working inside a sandbox on a [checkout](glossary.md#checkout).
It is not trusted: the code it wrote has not been reviewed, and the agent itself may have
been steered by hostile input. It uses only the [client](glossary.md#client).

### operator
```glossary
short: The human who owns the machine and decides what may run on it.
confusable: agent, broker
```
The person responsible for the [host](glossary.md#host). The operator installs the tool,
registers repositories, starts the [broker](glossary.md#broker), and sets the
[policy ceilings](glossary.md#policy-ceiling). Operator input is trusted.

### host
```glossary
short: The Mac that runs Tart, the broker, and the agent's sandbox.
confusable: guest
```
The physical machine. Protecting the host from the [candidate](glossary.md#candidate) is
the purpose of the project.

### guest
```glossary
short: The operating system running inside a VM.
confusable: host
```
The operating system inside a virtual machine started by the [broker](glossary.md#broker).
Untrusted code runs only in a guest.

### Tart
The virtualization command-line tool this project wraps. It creates, clones, runs, and
deletes virtual machines on Apple silicon Macs. Only the [broker](glossary.md#broker) runs
it.

### Softnet
The software network filter Tart can place in front of a VM. The `isolated` and `internet`
[network modes](glossary.md#network-mode) depend on it. It is installed separately from
Tart.

### guest agent
```glossary
short: The service inside a guest that runs commands for tart exec.
confusable: agent
```
The Tart Guest Agent: a service inside the [guest](glossary.md#guest) that lets the host
run a command in the guest and read its output and exit status. An image without it cannot
be used. It is unrelated to the coding [agent](glossary.md#agent).

### client
```glossary
aliases: taste-test
short: The taste-test command an agent runs.
confusable: agent, broker
```
The `taste-test` executable. It snapshots the [candidate](glossary.md#candidate), writes a
[request](glossary.md#request) into the [spool](glossary.md#spool), waits, prints the
output, and exits with the code for the [outcome](glossary.md#outcome). It never runs Tart
and needs no access outside the [checkout](glossary.md#checkout).

### broker
```glossary
aliases: taste-test-broker
short: The operator-run process that executes requests in VMs.
confusable: client, operator
```
The `taste-test-broker` executable, started by the [operator](glossary.md#operator)
outside any sandbox. It is the only component that runs Tart. It watches the spools of
registered checkouts and executes one [request](glossary.md#request) at a time.

### checkout
```glossary
short: One working tree of a repository; the main one or a linked worktree.
confusable: registered repository
```
A Git working tree: either a repository's main working tree or one of its linked
worktrees. Each checkout has its own [spool](glossary.md#spool). An agent's sandbox is
usually limited to one checkout.

### registered repository
```glossary
short: A repository the operator has allowed to request runs.
confusable: checkout
```
A repository whose main working tree the [operator](glossary.md#operator) recorded in the
[registry](glossary.md#registry). Registration covers the main working tree and every
linked worktree of that repository.

### registry
The [broker](glossary.md#broker)'s list of
[registered repositories](glossary.md#registered-repository), kept in the operator's state
directory where an [agent](glossary.md#agent) cannot write.

### default branch
```glossary
short: The reviewed branch the environment and the repository config come from.
confusable: candidate
```
The branch a [registered repository](glossary.md#registered-repository) treats as reviewed
and trusted, recorded at registration. The
[repository config](glossary.md#repository-config) and the
[environment image](glossary.md#environment-image) are always taken from its tip.

### mirror
A bare Git repository in the operator's state directory into which the
[broker](glossary.md#broker) fetches the [default branch](glossary.md#default-branch). The
broker reads the default branch only from the mirror, never from the
[checkout](glossary.md#checkout).

### repository config
```glossary
aliases: .taste-test.toml
short: The file on the default branch that says how to build and test.
```
The file `.taste-test.toml` at the root of the [default branch](glossary.md#default-branch).
It names the VM image, the setup commands, the [named commands](glossary.md#named-command),
and the [network modes](glossary.md#network-mode). A copy in the
[candidate](glossary.md#candidate) is ignored.

### candidate
```glossary
short: The unreviewed working-tree state being tested.
confusable: default branch
```
The state of a [checkout](glossary.md#checkout) at the moment of a
[request](glossary.md#request): every tracked file as it is on disk, plus every untracked
file that is not ignored. The candidate is untrusted and is only ever unpacked and executed
inside a [guest](glossary.md#guest).

### snapshot
The tar archive of the [candidate](glossary.md#candidate) that the
[client](glossary.md#client) writes into the [spool](glossary.md#spool). The
[broker](glossary.md#broker) treats it as opaque bytes and never unpacks it on the
[host](glossary.md#host).

### spool
The directory `.taste-test/spool` inside a [checkout](glossary.md#checkout) through which
the [client](glossary.md#client) and the [broker](glossary.md#broker) exchange files.
Everything in it is writable by the [agent](glossary.md#agent), so the broker treats all of
it as untrusted input.

### request
One submission by the [client](glossary.md#client): a
[named command](glossary.md#named-command), optional arguments, and a
[snapshot](glossary.md#snapshot). A request carries no commands, image names, or settings
of its own.

### named command
A command the [repository config](glossary.md#repository-config) defines under a name, such
as `test` or `lint`. A [request](glossary.md#request) chooses a command by name; it cannot
supply a command string.

### environment image
```glossary
short: A cached VM with the default branch set up in it.
confusable: run VM
```
A stopped virtual machine holding the [default branch](glossary.md#default-branch) source
with the setup commands already run. It is built once per default-branch commit and reused
by every [request](glossary.md#request) until the default branch moves.

### run VM
```glossary
short: The throwaway clone of an environment image that runs one request.
confusable: environment image
```
A clone of an [environment image](glossary.md#environment-image) created for one
[request](glossary.md#request), into which the [candidate](glossary.md#candidate) is
unpacked. It is deleted when the request ends.

### base manifest
The list of every file path in the [default branch](glossary.md#default-branch) commit an
[environment image](glossary.md#environment-image) was built from, stored inside the image.
A [run VM](glossary.md#run-vm) deletes those paths before unpacking the
[candidate](glossary.md#candidate), so a file the candidate deleted does not survive from
the default branch.

### network mode
One of `isolated`, `internet`, or `shared`: how much of the network a VM can reach during a
phase. Defined in [interfaces.md](interfaces.md#4-repository-config-taste-testtoml).

### policy ceiling
A limit the [operator](glossary.md#operator) sets when starting the
[broker](glossary.md#broker) that no [repository config](glossary.md#repository-config) may
exceed: the most open [network mode](glossary.md#network-mode) for each phase, the longest
time limit, and the largest VM.

### outcome
```glossary
short: The broker's one-word result for a request.
confusable: exit code
```
The [broker](glossary.md#broker)'s final classification of a
[request](glossary.md#request): `passed`, `failed`, `setup-failed`, `timed-out`, `error`,
`rejected`, or `cancelled`. Defined in [protocol.md](protocol.md#3-file-schemas).

### exit code
```glossary
confusable: outcome, command exit status
```
The process exit status of the [client](glossary.md#client). It is derived from the
[outcome](glossary.md#outcome) alone. Defined in [interfaces.md](interfaces.md#2-exit-codes).

### command exit status
```glossary
confusable: exit code
```
The exit status of the [named command](glossary.md#named-command) inside the
[guest](glossary.md#guest). It decides between the outcomes `passed` and `failed` and is
reported as `command-exit`, but it is never the client's own
[exit code](glossary.md#exit-code).

### exit-status probe
A guest command, `exit 7`, that the [broker](glossary.md#broker) runs after every boot
and again after every setup command and [named command](glossary.md#named-command),
whatever that command appeared to do (exit 0, exit non-zero, or run out of time). Only
if [Tart](glossary.md#tart) reports 7 does the broker trust what it saw; otherwise the
outcome is `error`. Specified in [vm-handling.md](vm-handling.md#2-guest-layout-and-steps).

### acknowledgement window
How long the [client](glossary.md#client) waits for a [broker](glossary.md#broker) to
acknowledge a new [request](glossary.md#request) with a `queued` status before it
withdraws the request and reports that nothing was run. Its length is in
[protocol.md](protocol.md#2-identifiers-and-limits).

### summary line
The last line the [client](glossary.md#client) prints on standard error for `run` and
`wait`: `key=value` pairs beginning with `outcome=` and `exit=`. Specified in
[interfaces.md](interfaces.md#output-streams).

### operator state
The directory, private to the [operator](glossary.md#operator), where the
[broker](glossary.md#broker) keeps the [registry](glossary.md#registry), the
[mirrors](glossary.md#mirror), staged snapshots, and the
[audit log](glossary.md#audit-log). An [agent](glossary.md#agent) must not be able to
write to it. Laid out in [protocol.md](protocol.md#5-operator-state).

### repository key
Twelve hex digits identifying a [registered repository](glossary.md#registered-repository),
derived from its path. It names the repository's [mirror](glossary.md#mirror) and appears
in the names of its [environment images](glossary.md#environment-image).

### state key
Eight hex digits identifying an [operator state](glossary.md#operator-state) directory,
derived from its path. Every VM name the [broker](glossary.md#broker) creates carries it,
so brokers with different state directories never touch each other's VMs.

### environment key
Twelve hex digits identifying one [environment image](glossary.md#environment-image) of a
repository: a hash of the [default branch](glossary.md#default-branch) commit and the
setup [network mode](glossary.md#network-mode). Defined in
[vm-handling.md](vm-handling.md#1-tart-invocations).

### heartbeat
The file `broker.json` the [broker](glossary.md#broker) rewrites every few seconds in each
[spool](glossary.md#spool) it serves. The [client](glossary.md#client) uses its age to tell
whether a broker is serving the [checkout](glossary.md#checkout).

### audit log
The [broker](glossary.md#broker)'s own record of every finished
[request](glossary.md#request), kept in the operator's state directory. Unlike the result
files in the [spool](glossary.md#spool), an [agent](glossary.md#agent) cannot alter it.

## Confusable Pairs

| Term | Confusable with | Distinction |
|---|---|---|
| [agent](glossary.md#agent) | [guest agent](glossary.md#guest-agent) | The agent writes code on the host; the guest agent is a service inside a VM. |
| [agent](glossary.md#agent) | [client](glossary.md#client) | The agent is the actor; the client is the program it runs. |
| [broker](glossary.md#broker) | [operator](glossary.md#operator) | The operator is a person; the broker is the process the operator starts. |
| [host](glossary.md#host) | [guest](glossary.md#guest) | The host is the Mac; a guest is inside a VM on it. |
| [checkout](glossary.md#checkout) | [registered repository](glossary.md#registered-repository) | A repository is registered once; each of its working trees is a checkout with its own spool. |
| [candidate](glossary.md#candidate) | [default branch](glossary.md#default-branch) | The candidate is untrusted and under test; the default branch is trusted and supplies the environment. |
| [environment image](glossary.md#environment-image) | [run VM](glossary.md#run-vm) | The image is cached and reused; the run VM is its single-use clone. |
| [outcome](glossary.md#outcome) | [exit code](glossary.md#exit-code) | The outcome is a word in the result file; the exit code is the number the client exits with. |
| [exit code](glossary.md#exit-code) | [command exit status](glossary.md#command-exit-status) | The exit code is the client's; the command exit status is the test command's inside the guest. |
| [repository key](glossary.md#repository-key) | [state key](glossary.md#state-key) | The repository key identifies a registered repository; the state key identifies the broker's own state directory. |
| [repository key](glossary.md#repository-key) | [environment key](glossary.md#environment-key) | One repository key per repository; one environment key per default-branch commit of it. |

## Renames and deprecated terms

None yet.

## Reserved Targets

None.
