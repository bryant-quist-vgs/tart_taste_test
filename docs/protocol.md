---
id: protocol
title: Spool protocol and operator state
is_source_of_truth: true
derives_from: [design, glossary, interfaces]
status: current
authority: >
  Canonical home for the on-disk exchange between client and broker, the
  broker's private state, the identifiers and limits both sides enforce, and how
  the broker touches a directory an agent controls. Code quotes these values
  and cites this file.
---

# Spool protocol and operator state

The [client](glossary.md#client) and the [broker](glossary.md#broker) never talk
over a socket. They exchange files in a [spool](glossary.md#spool) inside the
[checkout](glossary.md#checkout), because the checkout is the one place a
sandboxed [agent](glossary.md#agent) can always write. See
[decision D3](decisions.md#d3-requests-travel-through-a-file-spool-in-the-checkout).

The Tart commands the broker runs, and what it does inside a
[guest](glossary.md#guest), are in [vm-handling.md](vm-handling.md). Everything
here runs in the test suite against a stand-in for Tart; none of it has yet been
observed against a real VM (see [open-questions.md](open-questions.md)).

## 1. Spool layout

```text
<checkout root>/.taste-test/
├── .gitignore                    "*" (written by the client; keeps the directory out of Git)
└── spool/
    ├── broker.json               heartbeat, written by the broker
    ├── requests/<id>/
    │   ├── source.tar            the candidate snapshot (client)
    │   ├── request.json          the request (client; written last)
    │   ├── request.claimed.json  request.json after the broker claims it (broker renames)
    │   ├── request.withdrawn.json  request.json while the client withdraws it (client renames)
    │   └── cancel                empty marker asking for cancellation (client)
    └── results/<id>/
        ├── status.json           current state (broker; replaced atomically)
        ├── setup.log             environment-build output, when a build ran (broker)
        ├── output.log            the command's output (broker; append only)
        └── result.json           the outcome (broker; written last)
```

Who creates what:

- The client creates `.taste-test/`, `.gitignore`, `spool/`, `requests/`,
  `results/`, and everything under `requests/<id>/`. Directories are created
  with mode 0700, and `source.tar` and `request.json` with mode 0600.
- The broker creates `broker.json`, `results/<id>/` and everything in it, and
  renames `request.json` to `request.claimed.json`. After staging the snapshot
  it removes `requests/<id>/source.tar`. It creates nothing else and never
  creates `.taste-test/`, `spool/`, `requests/`, or `results/`.

A file whose name ends in `.tmp` is a write in progress and is ignored by the
reader. Every JSON file is written to `<name>.tmp` and renamed into place, so a
reader never sees a partial document.

**How the client touches the spool.** Anything in the sandbox can write the
spool, so the client trusts none of it. It opens every directory from the
checkout root down (`.taste-test`, `spool`, `requests`, `results`, `<id>`)
relative to its parent's file descriptor without following a symbolic link, and
creates, reads, renames, and removes files only relative to those descriptors. A
link at any of those names is an error (exit code 5); it is never a way to read
or write outside `.taste-test/`. Files are opened without following a link and
without blocking, and must be regular files, so a FIFO cannot stall the client.
`.gitignore`, `source.tar`, and `request.json.tmp` are created exclusively;
nothing that already exists under those names is written through. The broker's
rules for the same directory are in [§4](#4-how-the-broker-touches-a-spool).

**Claiming and withdrawing.** A request is waiting from the moment
`request.json` appears and for as long as `results/<id>/result.json` does not
exist. The broker claims it by renaming `request.json` to
`request.claimed.json`, then reads the claimed file once; nothing written to
`request.json` afterwards is ever read. The client withdraws an unanswered
request by renaming `request.json` to `request.withdrawn.json` and then removing
the request directory. Both act by renaming the same file, so exactly one
succeeds: a withdrawn request never runs, and a claimed request is never
reported as withdrawn. A request whose directory is already gone when its turn
comes was withdrawn too: the broker drops it without a result. The client withdraws whenever it is about to stop waiting
for a request no broker has acknowledged: when the acknowledgement window ends,
when `--wait-timeout` runs out, and when it is interrupted. An unanswered
request therefore does not outlive the client that submitted it, short of that
client being killed outright. A request is handled once: when it ends, whatever its
outcome, it has a `result.json` and is no longer waiting.

## 2. Identifiers and limits

| Name | Value | Enforced by |
|---|---|---|
| Request ID | `[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}`: UTC time of creation, then 12 random hex digits | client (creates), broker (validates) |
| Command name | `[a-z][a-z0-9_-]{0,31}` | client, broker |
| Arguments per request | at most 64, each at most 4096 bytes, no control characters (Unicode category Cc: U+0000 to U+001F, U+007F to U+009F), and text that can be turned back into bytes (a lone surrogate that stands for no byte is refused) | client, broker |
| Commit ID in `client.head` | `[0-9a-f]{40,64}`, or `null` | broker |
| Client version in `client.version` | at most 64 printable characters | broker |
| Outcome name in `result.json` | `[a-z][a-z-]{0,31}` | client |
| Size of any JSON document read from the spool | at most 65536 bytes | client, broker |
| Snapshot size (`source.tar`) | at most 268435456 bytes (256 MiB) | client (early, exit code 6), broker (authoritative) |
| Stored output per log | at most 67108864 bytes (64 MiB); further output is read and discarded, and the result is marked truncated | broker |
| Bytes of one log the client reads when printing it | at most the stored-output limit; anything beyond it was not written by a broker and is not read | client |
| Age of a request when the broker first sees it | at most 3600 seconds since `request.json` was last modified; an older one is rejected. A request the broker has acknowledged is never rejected for its age, however long it queues. | broker |
| Heartbeat interval, and the longest time between two spool scans | 2 seconds. An idle broker scans more often. One wait is not interrupted by scans: the up to 10 seconds the broker waits for a process it has killed to be reaped ([vm-handling.md §1](vm-handling.md#1-tart-invocations)). | broker |
| Heartbeat considered stale after | 15 seconds. A heartbeat dated more than 15 seconds in the future is not a heartbeat at all. | client |
| Interval at which the client polls the spool while waiting | 0.25 seconds | client |
| Acknowledgement window | 20 seconds without a status for the request: the client withdraws it | client |
| Broker-lost period | 60 seconds of stale heartbeat after acknowledgement: the client exits 5 | client |
| Time limit of each Git command the client runs | 60 seconds | client |
| Retention of finished requests in the spool | 7 days, pruned by the client on `run` | client |
| Mode of directories the client creates | 0700 (files: 0600) | client |
| Entries considered in one spool directory per scan | 4096 | broker |
| Requests of one spool that hold a place in the queue | 32. Further requests are not seen, and so not acknowledged, until earlier ones end; a client that is not acknowledged withdraws its request (exit code 6). | broker |
| Shortest time between two requests of one spool starting | 2 seconds | broker |
| Worktree records read per repository | 256, each at most 4096 bytes, besides the records of linked worktrees already being served, which are read first | broker |
| Linked worktrees of one repository served at once | at most 16, first come first served; one being served keeps its place, and one over the limit is not opened. Main working trees do not count. The limit is per repository, so one repository's worktrees cannot take another's places. | broker |
| Time spent listing spools for new requests in one scan | 0.5 seconds, plus one spool. Spools not reached keep their last listing and are listed first in the next scan. Only the listing is bounded in time; the rest of a scan is bounded by the counts in this table. | broker |
| Different warnings about linked worktrees the broker prints | 64 in the life of the broker | broker |
| Lines saying a linked worktree started or stopped being served | 64 in any 3600 seconds; one warning says when lines were left out | broker |
| What an audit-log line keeps of a request | arguments up to 1024 bytes in total, otherwise their count, size, and SHA-256; a reason up to 2048 characters | broker |
| Interrupted requests of one spool that get an audit-log line at start-up | 8 | broker |
| Components in a checkout path | at most 64 | broker |
| Interval at which a wait in the broker looks for a cancel marker or a request to stop | 0.2 seconds | broker |

Requests are executed one at a time. A request's place is the order in which the
broker first saw it, and checkouts take turns: among the checkouts with a
request waiting, the one served longest ago goes next, so one checkout's backlog
cannot hold up another's. Request IDs are chosen by the client and are never
used for ordering, and the broker names its staging file and its
[run VM](glossary.md#run-vm) with a random token of its own, not with the
request ID. What the broker remembers about a request (its place, whether it was
too old when first seen, whether it has had its turn) is forgotten as soon as
the request is no longer waiting in its spool, so the broker's memory does not
grow with what agents write.

A waiting request's `status.json` says `queued` with the detail `next` or
`N ahead`, and the broker writes it again whenever that changes. `N`, and the
`pending` count in the heartbeat, count the requests waiting at this broker
from every checkout it serves, because that is what decides how long the wait
is. An agent can therefore see how busy the broker is, though not who is
keeping it busy.

The broker is a single process without threads. Heartbeats, spool scans, and
acknowledgements continue while a request executes because every wait in the
broker (for a Tart or Git command, for guest output, for a guest agent) calls
the same scan step at least every 2 seconds. Nothing the scan step meets in a
spool can stop the broker: a spool it cannot open or read is reported once and
not served. Nor can a spool slow the broker's waits without limit: listing
spools for new requests stops after the time in the table, and the spools that
were not reached are listed first in the next scan. A spool an agent has filled
with entries therefore delays how soon new requests are noticed, for every
checkout, but not the time limits or the cancel and stop checks of the request
that is running. The other steps of a scan (reading worktree records, opening
checkouts, rewriting the status of waiting requests) have no time limit of
their own. Each is bounded by a count in the table: records per repository,
components per path, linked worktrees per repository, waiting requests per
spool.

A linked worktree that is not served, because its repository is at the limit,
because it is on another volume, or because its record does not hold, gets no
heartbeat. Its client therefore reports that no broker is serving the checkout
(exit code 6); the broker's own output says why.

## 3. File schemas

All JSON documents are UTF-8 objects with `"schema": 1`. A reader rejects a
document whose `schema` it does not know, that is not an object, that is over
the size limit, that lacks a required key, or in which a value has the wrong
type. Every key shown in the examples below is required. Numbers must be finite:
`NaN`, `Infinity`, and values too large for a double are rejected. Any failure
to parse an untrusted document, of any kind, is confined to that one request.

What the client does with a document it rejects depends on the file. A rejected
`status.json` or `broker.json` is treated as absent: the request then counts as
unacknowledged, or the broker as not serving. A `result.json` that exists but is
rejected, names another request, or is not a regular file ends the client with
exit code 5: the broker puts a result in place with one rename, so an invalid
one is never a result still being written, and waiting for it would only hide
the problem.

### `request.json` (client → broker; untrusted input to the broker)

```json
{
  "schema": 1,
  "id": "20261115T183000Z-3f9a1c2b4d5e",
  "command": "test",
  "args": [],
  "source": {"sha256": "<64 hex digits>", "bytes": 1048576},
  "client": {"version": "0.1.0", "head": "<commit id or null>"}
}
```

`id` must equal the directory name. Unknown keys are rejected at every level.
`client` is informational; nothing is decided from it.

### `status.json` (broker → client)

```json
{"schema": 1, "id": "…", "state": "queued", "detail": "2 ahead", "updated": 1794767400.0}
```

`state` is one of:

| State | Meaning |
|---|---|
| `queued` | Seen by the broker; waiting for earlier requests. This is the acknowledgement. It is written before the request is validated. |
| `preparing` | Claimed. The snapshot is being staged, the default branch refreshed, and the environment image built or reused. |
| `running` | The named command is running in the run VM. |
| `finished` | `result.json` has been written. |

### `result.json` (broker → client)

```json
{
  "schema": 1,
  "id": "…",
  "outcome": "failed",
  "command_exit": 2,
  "reason": "",
  "command": {"name": "test", "run": "make test", "args": []},
  "default_branch": {"name": "main", "commit": "<commit id>"},
  "environment": {"vm": "tastetest-1a2b3c4d-env-0123456789ab-ba9876543210", "reused": true},
  "network": {"setup": "internet", "run": "isolated"},
  "source_sha256": "<64 hex digits>",
  "output": {"bytes": 20480, "truncated": false},
  "seconds": {"preparing": 3.4, "running": 78.9, "total": 82.3}
}
```

`outcome` is one of the following. The client maps it to an exit code with the
table in [interfaces.md §2](interfaces.md#2-exit-codes), which is the only
statement of that mapping.

`passed`, `failed`, `timed-out`, and `setup-failed` are set only when the
[exit-status probe](glossary.md#exit-status-probe) still passes after the
command they describe; otherwise the outcome is `error`.

| Outcome | Set when |
|---|---|
| `passed` | The command exited 0. |
| `failed` | The command exited non-zero. |
| `setup-failed` | A setup command exited non-zero or the setup budget ran out. |
| `timed-out` | The command exceeded its time limit. |
| `error` | Tart, the VM, the image, the mirror, the spool, or the broker itself failed. |
| `rejected` | The request or the repository config failed validation or exceeded a ceiling, or the request was too old when first seen. |
| `cancelled` | A `cancel` marker was seen before the command finished. |

`command_exit` is an integer only for `passed` (always 0) and `failed` (never
0), otherwise `null`; the client rejects a result in which the two disagree.
Keys describing work that never happened (`command`, `default_branch`,
`environment`, `network`, `source_sha256`) are `null` in a result produced
before that work. `reason` is one printable line. `seconds.total` is required
and is the time the broker spent on the request, which the client prints as
`duration`; `preparing` and `running` are present when those phases happened.
An outcome name that has the shape of one but is not in the table above (from a
newer broker) is not a verdict: the client reports it as `error`, exit code 5.

### `broker.json` (heartbeat)

```json
{"schema": 1, "pid": 4242, "version": "0.1.0", "updated": 1794767400.0, "pending": 0}
```

## 4. How the broker touches a spool

The spool lives in a directory an untrusted agent can write to, and the broker
runs as the same user with none of the agent's restrictions. The broker
therefore treats every name in the spool as hostile.

Which checkouts are served:

- The registered repository's main working tree, at the path recorded in the
  registry (which `register` resolved, so it contains no symbolic link).
- Each linked worktree that proves it belongs to that repository. The record
  `<repository>/.git/worktrees/<name>/gitdir` is only a claim, because an agent
  can write it. It must name a `.git` file; the directory holding that file is
  the claimed root. The claim is accepted only if the root's own `.git` is a
  regular file saying `gitdir: <repository>/.git/worktrees/<name>`. An agent
  cannot plant that in a directory it cannot write, so a forged record cannot
  aim the broker at someone else's checkout.
- When records in several registered repositories name the same root, each is a
  separate claim, tried in registration order, and the root is served for the
  first repository whose claim is accepted. A forged record in one repository
  therefore cannot take a worktree away from the repository it belongs to. A
  record that names another repository's main working tree is not a claim at
  all: the operator registered that directory.
- The number of linked worktrees served at once is limited (§2), because the
  broker keeps three file descriptors open per served spool. The records of
  worktrees already being served are read first, by name, so records added
  later cannot push one out, and a worktree waiting for a place is served when
  one frees up. The broker reports once when either limit is reached.
- Paths in both files may be absolute or relative. They are made absolute and
  normalised as text and compared as text with the registered path; no
  agent-chosen path is ever resolved through symbolic links. A repository
  registered under a path whose letter case differs from what Git recorded is
  therefore served, but its linked worktrees are not.
- A linked worktree must be on a volume that the repository's own path crosses
  (the volume of `/`, of the repository, or of a directory between them). A
  worktree on another volume, a network mount, or an automounted directory is
  not served; the broker reports it once. On the way to a claimed root, the
  first directory on any other volume is examined once (its device number is
  read from its entry in the parent) and refused; it is not opened, and nothing
  below it is touched.
- Both files are read as small regular files (no link followed, no blocking, at
  most 4096 bytes), and Git is never run in the repository.
- A checkout without an existing spool is not served. The broker never creates
  one.

How it opens things:

- A checkout root is opened one path component at a time from `/`, each
  relative to its parent's file descriptor and without following a symbolic
  link. A linked worktree's `.git` file is read through the descriptor of the
  root that was opened, so the directory that was checked is the directory
  that is served.
- From the checkout root down, each directory (`.taste-test`, `spool`,
  `requests`, `results`, `<id>`) is opened the same way. A link at any of those
  names is never looked through, whether to list, read, or write, and swapping a
  directory for a link after it was opened has no effect.
- Every scan, the broker checks that each checkout's path still leads to the
  directories it holds open. If the spool, its `requests` or `results`
  directory, or the checkout itself was removed or replaced, the broker stops
  using the old directories and opens the path again under all these rules. A
  request in flight from the old spool ends with outcome `error`, and its result
  is written to the directories it came from, never to whatever replaced them.
- Every file it reads is opened without following a link and without blocking
  (so a FIFO cannot stall the broker), and must be a regular file.
- The claimed request and the snapshot must each have exactly one hard link, so
  neither can be a second name for some other file of the operator's. For the
  snapshot the digest gives a second guard: the request must state the SHA-256
  of the bytes, which an agent cannot know for a file it cannot read.
- Directories and files must be owned by the user running the broker. This only
  excludes other users' files; it does nothing against the agent, which runs as
  the same user. The controls that matter against the agent are the others in
  this list.

How it writes:

- The broker never opens an existing spool file for writing. To create a file it
  removes any existing directory entry of that name and creates the file
  exclusively, relative to an open directory descriptor; a log is then written
  only through that one descriptor. A JSON document is created the same way
  under its `.tmp` name and renamed into place. A hard link or symbolic link
  planted under any of these names is therefore replaced, never written through.
- Every name is one of the fixed names in §1 under an ID that matched the
  pattern in §2.

What it never does with spool data:

- `source.tar` is never unpacked, listed, or parsed on the host. It is copied to
  private staging while being hashed and size-checked, and the private copy is
  later streamed into the guest.
- No value from a request is interpreted on the host. `command` is used only as
  a lookup key after matching the command-name pattern. `args` are appended, as
  separate items, to the guest argument vector of `tart exec`, after the VM
  name, the shell, the script, and the fixed word `taste-test`. Tart 2.41.1 was
  seen to accept such a command line without reading option-like items as its
  own options; that they then arrive in the guest unchanged has not been
  observed in a VM
  ([open question Q5](open-questions.md#q5-do-guest-arguments-arrive-unchanged)),
  and `doctor --image` checks it. They are never part of a host or guest script.
- Text taken from a request, a spool path, or guest output is printed by the
  broker and stored in a `reason` only after control characters are replaced.

## 5. Operator state

Default location: `~/Library/Application Support/tart-taste-test/`, overridable
with the environment variable `TASTE_TEST_STATE_DIR`. Created with mode 0700.
`serve`, `prune`, and `doctor --image` refuse to run if the directory has become
accessible to other users. It must not be writable by sandboxed agents.

```text
<state>/
├── registry.json          registered repositories
├── registry.lock          held briefly by register and unregister
├── broker.lock            held (flock) by serve, prune, and doctor --image
├── audit.log              one JSON object per line, one line per finished request
├── in-flight.json         the checkout and ID of the request being executed; removed when it ends
├── mirrors/<repo key>.git bare repository holding the fetched default branch
├── staging/               private copies of snapshots and default-branch archives; emptied at start-up
└── vm-logs/<vm>.log       console output of `tart run`; deleted with the VM
```

`<repo key>` is the first 12 hex digits of the SHA-256 of the registered
repository's resolved absolute path. `<state key>` is the first 8 hex digits of
the SHA-256 of the state directory's resolved absolute path.

`registry.json`:

```json
{
  "schema": 1,
  "repositories": [
    {
      "path": "/Users/me/src/project",
      "key": "0123456789ab",
      "branch": "main",
      "source": "remote",
      "url": "git@github.com:org/project.git",
      "registered": "2026-11-15T18:30:00Z"
    }
  ]
}
```

`source` is `remote` (fetch `url`) or `local` (fetch the repository's own
`path`; `url` is `null`). A remote URL must be `https://…`, `ssh://…`, or
`user@host:path`; it must not begin with `-`, contain whitespace or control
characters, or use a remote-helper form (`<name>::<address>`). A branch name
must match `[A-Za-z0-9][A-Za-z0-9._/-]{0,199}` and must not contain `..`. A
registry file that fails these checks is an error, not an empty registry.

Git is always started in the directory `/`, never in the directory the broker
was started from, and every repository it works on is named explicitly. So Git
never discovers, and never reads the configuration of, a checkout the operator
happened to be standing in (which an agent may be able to write). Inherited
`GIT_*` variables are removed, because they can redirect Git to another
repository or inject configuration; the exceptions are the operator's own
transport and configuration choices: `GIT_SSH`, `GIT_SSH_COMMAND`,
`GIT_ASKPASS`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, and
`GIT_CONFIG_NOSYSTEM`. Git is run with `GIT_TERMINAL_PROMPT=0`, with
`GIT_ALLOW_PROTOCOL` limited to `https:ssh` for a remote source and `file` for a
local one, and with SSH in batch mode (`GIT_SSH_COMMAND=ssh -o BatchMode=yes`)
unless the operator has chosen an SSH program with `GIT_SSH` or
`GIT_SSH_COMMAND`; either one is used as it is. A failed fetch ends the request
with outcome `error`; the broker does not fall back to a commit it fetched
earlier.

Each `audit.log` line is the request's `result.json` plus the time, the
repository key and path, the checkout root, and the run VM name. The audit log
is the tamper-resistant record: the spool copy of a result sits in an
agent-writable directory, the audit log does not.

What a line takes from the request is bounded (§2), because an agent chooses
its arguments and can make requests end at once. Arguments longer than the
limit in total are replaced by `{"count": N, "bytes": B, "sha256": "..."}`,
where the digest is over the arguments as UTF-8, each followed by one NUL byte;
`result.json` in the spool keeps them in full. While the broker serves, the
log grows by at most one line per checkout every 2 seconds (start-up recovery,
below, adds a bounded number at once), and it is never rotated or shortened by
the broker: removing old lines would let whoever can fill the log erase its history
([DW11](deferred-work.md#dw11-keep-the-audit-log-from-growing-without-limit)).

A broker that stops uncleanly leaves at most one claimed request without a
result. At start-up the next broker ends every such request it finds as
`error`. Finding more than a few in one spool means they were planted, so only
8 per spool get an audit-log line; the rest get a result only, and the broker
says once how many there were. Request IDs are chosen by agents, so their order
must not decide which 8: before it claims a request, the broker writes the
checkout and the ID to `in-flight.json` in operator state, and removes the file
when the request ends. The request named there is logged first at start-up,
then the others in the order of their IDs.
