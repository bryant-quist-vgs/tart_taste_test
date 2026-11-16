---
id: vm-handling
title: VM handling
is_source_of_truth: true
derives_from: [design, glossary, interfaces, protocol]
status: current
authority: >
  Canonical home for the exact Tart commands the broker runs, the names and
  time limits of its VMs, how its host processes are started and ended, how the
  broker stops, and every step it performs inside a guest. Code quotes these
  values and cites this file.
---

# VM handling

What the [broker](glossary.md#broker) does with [Tart](glossary.md#tart) and
inside a [guest](glossary.md#guest). The files the broker and the
[client](glossary.md#client) exchange, and the broker's private state, are in
[protocol.md](protocol.md).

The Tart command lines in §1 were checked for acceptance against Tart 2.41.1
without starting a VM. Everything in §2 runs in the test suite against a
stand-in for Tart; none of it has yet been observed against a real VM (see
[open-questions.md](open-questions.md)).

## 1. Tart invocations

The broker runs exactly these Tart commands. Every argument is passed as a
separate argument-vector item; no shell is involved on the host.

| Purpose | Command | Time limit |
|---|---|---|
| Version check | `tart --version` | 120 s |
| List local VMs | `tart list --source local --quiet` | 120 s |
| Create a VM from an image or another VM | `tart clone <source> <name>` | 3600 s (it may pull an image) |
| Size a VM | `tart set <name> [--cpu N] [--memory MB]` | 120 s |
| Start a VM | `tart run --no-graphics --no-audio --no-clipboard --no-usb-accessories <network flags> <name>` | ended by the broker when the phase ends |
| Run a guest command | `tart exec [-i] <name> <argv...>` | per step, §2 |
| Stop a VM | `tart stop <name> --timeout 20` | 120 s |
| Rename a finished build | `tart rename <build name> <environment name>` | 120 s |
| Delete a VM | `tart delete <name>` | 120 s |

The start command is exactly as shown: those four flags, the network flags of
the mode, and the name. Nothing else is ever passed, so no host directory, disk,
port, serial console, or USB device is attached, and clipboard sharing is off.

Network flags per [network mode](glossary.md#network-mode):

| Mode | Flags |
|---|---|
| `isolated` | `--net-softnet-block=0.0.0.0/0` |
| `internet` | `--net-softnet` |
| `shared` | none |

If Softnet is not installed, `tart run` exits; the broker reports outcome
`error` with the network mode and the end of Tart's own message, and never
starts the VM with a more open network instead.

VM names. Every name carries the state key, so two brokers with different state
directories never touch each other's VMs. These are the only names `prune` and
the broker's start-up cleanup will delete:

| Kind | Name | Deleted by |
|---|---|---|
| Environment image | `tastetest-<state key>-env-<repo key>-<environment key>` | a newer build for the same repository; `prune --environments` |
| Unfinished build | `tastetest-<state key>-build-<repo key>-<environment key>` | the build's own failure path; start-up cleanup; `prune` |
| Run VM | `tastetest-<state key>-run-<token>` | the end of its request; start-up cleanup; `prune` |
| Doctor VM | `tastetest-<state key>-doctor-<token>` | the end of `doctor --image`; start-up cleanup; `prune` |

`<token>` is 12 random hex digits chosen by the broker. `<environment key>` is
the first 12 hex digits of the SHA-256 over three UTF-8 strings joined by single
NUL bytes: the environment format number (`1`), the default-branch commit ID,
and the effective setup network mode. The repository config is part of the
commit, so any change to it changes the key.

Deleting a VM is: `tart stop`, then `tart delete`, issued whether or not the VM
is listed, so a listing that cannot be read never leaves a VM behind. The
listing is then used to confirm; if the VM is still listed, the same again after
2 seconds. A VM that survives, or whose deletion could not be confirmed, is
reported on the broker's output and left for `prune` or the next start-up
cleanup; the broker keeps serving. Only names with this state directory's
prefix are ever stopped or deleted. The `tart run` process of a phase is waited
for 30 seconds after `tart stop` and killed if it has not exited. A VM's console
log is deleted with the VM.

Every Tart and Git process is started as the leader of a session of its own.
When one has to be ended (its time limit, a cancel marker, a request to stop
the broker) the whole session is killed, so its helpers (`ssh`, Softnet) go
with it, and the broker waits up to 10 seconds for the process to be reaped
before it continues. (A process that a kill signal does not end within that
time is left to the operating system; the broker does not wait for it
further, and no spool scan runs during that wait.) This holds
even when the process itself has already exited but has not been reaped: a
helper it left behind, holding its output open, is killed with the session. A
process that exits by itself and is reaped is taken to have ended its own
helpers; nothing is signalled after that, because its process-group ID may by then
belong to something else.

**Stopping the broker.** SIGTERM and SIGINT (Ctrl-C) only set a flag. An idle
broker exits within about half a second (it finishes the pause or the scan it
is in), and a stop noticed after a scan claims no new request. If a request is
in flight, it is abandoned at its next wait: a wait looks for a stop every 0.2
seconds, after any spool scan that is due. Steps that do not wait (copying the
snapshot into staging, writing a result) run to their end first. Then its VM
is stopped and deleted in the ordinary way, its
result is written with outcome `error` and the reason "the broker was stopped
while this request ran", and then the broker exits with status 0. Stopping and
deleting a VM is never interrupted, by a second signal either; SIGKILL ends the
broker at once and leaves the VM for the next start-up cleanup. The same path
ends a request whose repository is unregistered, or whose spool is removed or
replaced, while it runs; the broker then keeps serving.

Other time limits: 300 seconds for a mirror fetch, 600 for `git archive`, 60 for
every other Git command; `vm.boot_timeout_seconds` for the guest agent to
answer, in attempts of 15 seconds; 60 seconds for each fixed guest script in §2,
or 600 when it receives a snapshot or archive on standard input.

## 2. Guest layout and steps

Everything lives under `$HOME/taste-test` in the guest.

```text
$HOME/taste-test/
├── src/            the source tree
└── base-manifest   NUL-separated paths of every entry in the default-branch commit
```

The fixed scripts below are run as `tart exec <name> /bin/sh -c '<script>'` and
contain no value from a request or a repository.

```text
Ready
    exit 0

Exit-status probe
    exit 7

Requirements
    [ -n "${HOME:-}" ] || exit 11; command -v tar >/dev/null 2>&1 || exit 12; command -v xargs >/dev/null 2>&1 || exit 13; exit 0

Unpack the default branch (archive on standard input)
    set -eu; root="$HOME/taste-test"; rm -rf "$root"; mkdir -p "$root/src"; tar -x -o -f - -C "$root/src"

Save the base manifest (manifest on standard input)
    set -eu; cat > "$HOME/taste-test/base-manifest"

Overlay the candidate (snapshot on standard input)
    set -eu; root="$HOME/taste-test"; cd "$root/src"; xargs -0 rm -rf -- < "$root/base-manifest"; tar -x -o -f - -C "$root/src"

Flush disks
    sync
```

A setup or named command is run with the configured shell instead:
`tart exec <name> <vm.shell...> '<script>' taste-test <args...>`, where the
script is `exec 2>&1; cd "$HOME/taste-test/src" && ` followed by the command
string exactly as the config wrote it. Nothing is appended. `taste-test` is the
shell's `$0`, so the first request argument is `$1` and none is lost; a command
that accepts arguments says where they go by containing `"$@"`
([interfaces.md §4](interfaces.md#4-repository-config-taste-testtoml)).

**After every boot**, before anything else is sent to the guest:

1. Wait until the Ready script succeeds.
2. Run the exit-status probe and require status 7 exactly. If Tart does not
   report 7, the outcome is `error`: the broker never reports a verdict it
   cannot observe.
3. Run the Requirements script. Status 11, 12, or 13 becomes outcome `error`
   with a reason naming the missing `HOME`, `tar`, or `xargs`.

**Building an [environment image](glossary.md#environment-image):**

1. Write the default-branch archive (`git archive --format=tar <commit>`) and
   the [base manifest](glossary.md#base-manifest)
   (`git ls-tree -r -z --name-only <commit>`) from the mirror to private staging.
2. Clone `vm.image` to the build name; apply `vm.cpu` and `vm.memory_mb`; start
   it with the setup network mode; perform the after-boot steps.
3. Unpack the archive and save the manifest.
4. Run each `setup.run` string with `vm.shell`, appending its output to
   `setup.log`. A non-zero status, or the budget running out, is
   `setup-failed`.
5. Flush disks, stop the VM, rename it to the environment name, and delete older
   environment images of the same repository.

Paths marked `export-ignore` in the default branch are absent from the archive
and therefore absent while setup runs; the candidate's own copies arrive with
the snapshot. The manifest lists submodule paths as single entries.

**Running a request:**

1. Clone the environment image to the run name, start it with the run network
   mode, and perform the after-boot steps.
2. Overlay the candidate: delete every path in the base manifest (recursively,
   so a submodule directory goes too), then unpack the snapshot. The result is
   the candidate's files plus whatever setup created that is not part of the
   default branch (installed dependencies, build caches). Directories the
   candidate removed may remain as empty directories.
3. Run the named command with `vm.shell`, appending its output to `output.log`.
4. Run the exit-status probe again, whatever the command appeared to do: exit
   0, exit non-zero, or run out of time. Only if the probe still reports 7 is
   what the broker saw taken as the command's own doing (`passed`, `failed`,
   `timed-out`); otherwise the VM or its guest agent went away and the outcome
   is `error`. The same holds for each setup command.
5. Stop and delete the run VM, whatever happened. The broker's staged copy of
   the snapshot is deleted too. The spool's `source.tar` was already deleted
   when the broker finished copying it, or when the request was refused after
   being claimed.

**Cancellation.** The `cancel` marker is checked when a request is claimed,
before an environment build starts a VM, before each setup command, every 0.2
seconds while a setup or named command runs, and after the run VM boots. A
cancelled build is deleted; a cancelled command is killed with its VM.
