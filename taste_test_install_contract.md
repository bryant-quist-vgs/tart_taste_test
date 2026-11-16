Contract version: v0

# taste_test_install.sh — installer for the taste-test and taste-test-broker commands: Contract

`taste_test_install.sh` is run by the operator from a checkout of this
repository. It installs the two commands, `taste-test` and `taste-test-broker`,
as one isolated tool by delegating to `uv tool install`, so that afterwards
starting a command needs no network and no `uv`, and (except after
`--editable`) writes no bytecode cache. It can also show what it would do,
verify an installation, and uninstall. It never uses `sudo`, never edits shell
start-up files or `PATH`, and does not install Tart, Softnet, or any VM image;
it only tells the operator when those are missing.

`taste-test-broker` runs with the operator's authority, outside any agent
sandbox, and this script and the source it installs are whatever the checkout
holds. The checkout must therefore be one that no agent can write, at a commit
the operator has reviewed. The script cannot enforce that. What it does do is
refuse source that differs from what Git has recorded (behavior 5), which
catches an accident, not an attacker: an agent that can write the checkout can
commit, or change this script.

The paired tests are in `taste_test_install_test.sh`. `make test` runs them
directly (`bash taste_test_install_test.sh`). `tests/install_script_test.py`
executes no shell: it checks that the three files exist, name the same contract
version, and are wired into `make test`, and that the script's `uv` and `git`
invocations are the ones declared here.

## Inputs

- **`--uninstall`** — flag, optional. CLI argument. **Trust:** operator-provided
  (trusted). Remove the installed tool instead of installing.
- **`--check`** — flag, optional. **Trust:** operator-provided. Verify that both
  commands are on `PATH` and report their versions.
- **`--dry-run`** — flag, optional. **Trust:** operator-provided. Print the
  `uv` command an install or uninstall would run, and run no `uv` command. An
  install's source check (behavior 5) still runs `git`. Not valid with
  `--check`.
- **`--editable`** — flag, optional. **Trust:** operator-provided. Install so
  that the commands run this checkout's source directly (for development); only
  valid when installing.
- **`--allow-dirty`** — flag, optional. **Trust:** operator-provided. Install
  even when Git reports uncommitted or untracked changes, or cannot report the
  state of the checkout; only valid when installing.
- **`-h` / `--help`** — flag, optional. Wherever it appears among the
  arguments, print usage to standard output and exit 0; no other argument is
  examined.
- **`${BASH_SOURCE[0]}`** — the installer's own path, resolved through symbolic
  links to the absolute directory that holds it, `REPO_ROOT`. **Trust:**
  operator-controlled.
- **`REPO_ROOT/pyproject.toml` and
  `REPO_ROOT/src/tart_taste_test/client_cli.py`** — existence only, as proof the
  installer is running from a checkout of this project. Never read or executed
  by this script.
- **The Git state of `REPO_ROOT`** — as reported by `git rev-parse` and
  `git status` (behavior 5). **Trust:** as trusted as the checkout itself; see
  the second paragraph above.
- **`$PATH`** — environment variable. Used to find `uv` and `git`, and, for
  hints and `--check`, `taste-test`, `taste-test-broker`, `tart`, and `softnet`.
- **`$CDPATH`** — unset at start-up so `cd` is literal. Never read.

Standard input is not read. No other environment variable is read by this
script.

## Outputs

- **Standard output** — the usage text for `--help`; one `running:` or
  `would run:` line for the command that changes the installation
  (`uv tool install ...` or `uv tool uninstall ...`; the queries
  `uv tool list`, `uv tool dir --bin`, and the `git` commands are not
  announced); `Warning:` lines (behaviors 5 and 6); `PASS`/`FAIL` lines for
  `--check`; `Note:` lines for missing optional tools; and closing hint and
  next-step lines.
- **Standard error** — `Error: ...` lines for refusals and usage errors (usage
  errors are followed by the usage text).
- **Exit code** — `0` success, including an uninstall when nothing was
  installed; `1` refusal or failure (`uv` missing, not a checkout of this
  project, uncommitted changes, `uv` or `git` failed, a `--check` item failed);
  `2` usage error (unknown option, conflicting or repeated options).
  `Load-bearing: the README tells operators and any wrapping automation to branch
  on 0, 1, and 2 as defined here.`
- **The installed tool** — written by `uv`, not by this script: an isolated
  environment in uv's tool directory and two executables in uv's tool bin
  directory.
  `Load-bearing: agents and operators invoke the bare names taste-test and
  taste-test-broker; the README and both --help texts assume those names.`

## Behaviors

1. **Argument parsing.** If `-h` or `--help` is among the arguments, prints the
   usage text and exits 0 without examining anything else. Otherwise accepts
   the flags under Inputs in any order. An unknown option; any flag given
   twice; `--uninstall` together with `--check`; `--check` with `--dry-run`; or
   `--editable` or `--allow-dirty` with `--uninstall` or `--check` is a usage
   error: the error and the usage text go to standard error and the exit code
   is 2. Nothing else is done.
2. **Self-location.** Unsets `CDPATH`, resolves `${BASH_SOURCE[0]}` through its
   chain of symbolic links, and takes the absolute physical directory as
   `REPO_ROOT`. The loop stops with exit 1 after 40 links. That is a guard
   against links changed while the script runs, not a behavior a caller can
   meet: the operating system refuses a shorter chain before the script starts.
3. **Check mode.** With `--check`, for each of `taste-test` and
   `taste-test-broker`: if the name is not on `PATH` prints
   `FAIL <name> (not on PATH)`; otherwise runs `<name> --version` and prints
   `PASS <name> (<its output>)`, or `FAIL <name> (exit N)` when it exits
   non-zero. Then prints the optional-tool notes of behavior 8. Exits 0 only if
   both passed. Needs neither `uv`, nor `git`, nor the checkout files, and runs
   neither `uv` nor `git`.
   `Load-bearing: the README names --check as the operator's verification step.`
4. **Preconditions (install and uninstall).** Requires `uv` on `PATH`; without
   it prints an error naming `uv` and exits 1. For install, also requires the
   two checkout files under `REPO_ROOT`; without them exits 1 and runs nothing.
5. **Committed source (install, also with `--dry-run`).** If `git` is on
   `PATH`, runs `git -C REPO_ROOT rev-parse --is-inside-work-tree` with
   `LC_ALL=C`, and if that succeeds runs
   `git -C REPO_ROOT -c core.fsmonitor=false status --porcelain
   --untracked-files=normal --ignore-submodules=none`. The two settings are
   given on the command line so that the operator's Git configuration cannot
   hide untracked files or changed submodules. Then:
   - `git status` prints anything (uncommitted or untracked changes): without
     `--allow-dirty` prints an error naming `--allow-dirty` and exits 1,
     running no `uv` command; with it prints a `Warning:` line and continues.
   - `git` is missing, or `rev-parse` fails saying `not a git repository`:
     prints a `Warning:` that the source could not be compared with a commit,
     and continues.
   - `rev-parse` fails for any other reason (for example Git refuses a
     checkout owned by another user), or `git status` fails: the check could
     not be made. Without `--allow-dirty` prints an error naming
     `--allow-dirty` and exits 1, running no `uv` command; with it prints a
     `Warning:` line and continues.
   `Load-bearing: the README tells operators that forgotten or an agent's
   uncommitted edits are not installed silently.`
6. **Install (default mode).** With `--editable`, first prints a `Warning:`
   that both commands will run the checkout's source directly and that whatever
   can write the checkout then controls what the broker does. Runs exactly
   `uv tool install --force --reinstall --compile-bytecode REPO_ROOT`, with
   `--editable` inserted before `REPO_ROOT` when requested, after printing that
   command line. If `uv` fails, prints an error and exits 1. On success prints
   the directory `uv tool dir --bin` reports, when it reports one, and, when
   `taste-test` is still not found on `PATH`, a hint to add uv's tool bin
   directory to `PATH` (or run `uv tool update-shell`); then the optional-tool
   notes of behavior 8 and the next steps (`taste-test-broker doctor`,
   `register`, `serve`).
   `Load-bearing: re-running the installer is the documented upgrade path;
   --force --reinstall make a second run replace the first.`
7. **Uninstall.** With `--uninstall`, runs `uv tool list`. If that fails,
   prints an error with its output and exits 1. If its output has no line
   beginning `tart-taste-test `, prints that nothing is installed and exits 0.
   Otherwise prints and runs `uv tool uninstall tart-taste-test`, exiting 1 if
   it fails. Then prints that operator state and VMs were left in place, and
   that removing the VMs needs the tool: install again, run
   `taste-test-broker prune --environments`, then uninstall.
8. **Optional-tool notes.** Prints `Note: tart is not on PATH ...` when `tart`
   is not found and `Note: softnet is not on PATH ...` when `softnet` is not
   found. These are information, not failures.
9. **Dry run.** With `--dry-run`, performs behaviors 1, 2, 4, and (for an
   install) 5 and the `--editable` warning, prints `would run: <command line>`
   for the install or uninstall command, and runs no `uv` command at all.

Any behavior the script performs that is not listed here is **undeclared** and
constitutes drift. The script must not perform undeclared behaviors. An
adversarial reviewer treats undeclared behaviors as suspect.

## Declared capability surface

- **Filesystem reads:** existence tests on `REPO_ROOT/pyproject.toml` and
  `REPO_ROOT/src/tart_taste_test/client_cli.py`; `readlink` on
  `${BASH_SOURCE[0]}` and its link chain. No other reads by this script.
  **Delegated reads:** `git status` reads the working tree and the repository
  under `REPO_ROOT`, including its configuration.
- **Filesystem writes:** none by this script. **Delegated writes:**
  `uv tool install` and `uv tool uninstall` create, replace, or remove the tool
  environment in uv's tool directory and the two executables in uv's tool bin
  directory, and may write uv's cache. With `--editable`, running the installed
  commands may write bytecode caches inside `REPO_ROOT/src`. `git status` may
  refresh the index file of the repository under `REPO_ROOT`.
- **Network access:** none performed directly. `uv tool install` may download
  the build backend named in `pyproject.toml` and, when no suitable interpreter
  is installed, a Python interpreter.
- **Subprocess invocation:** `uv tool install ...`, `uv tool uninstall
  tart-taste-test`, `uv tool list`, `uv tool dir --bin`;
  `git -C REPO_ROOT rev-parse --is-inside-work-tree` and
  `git -C REPO_ROOT -c core.fsmonitor=false status --porcelain --untracked-files=normal --ignore-submodules=none`;
  in check mode
  `taste-test --version` and `taste-test-broker --version`; `command -v` lookups
  for `uv`, `git`, `taste-test`, `taste-test-broker`, `tart`, and `softnet`;
  `readlink`, `dirname`, `cat` (usage text), and `grep` (on the output of
  `uv tool list`). No other commands. Git may itself run programs named in the
  repository's configuration; that is part of why the checkout must be one no
  agent can write.
- **Environment variables read:** `PATH`. **Environment variables set:**
  `CDPATH` is unset at start-up.
- **Secrets handled:** No secrets handled.
- **Privilege escalation:** No privilege escalation; never invokes `sudo`.

## Assumptions for operation

- Runtime: `bash >= 3.2` (the macOS system bash is sufficient).
- Dependencies: `uv` on `PATH` for install and uninstall; POSIX `readlink`,
  `dirname`, `cat`, and `grep`; `git` when the checkout is a Git working tree.
- The installer is run from (or through a symbolic link to) the root of a
  checkout of this repository.
- No agent can write the checkout, and the operator has reviewed the commit it
  is at. The script, the source it installs, and the repository configuration
  Git reads all come from the checkout.
- The installed tool requires Python >= 3.14, which `uv` provides.
- A non-editable install is a snapshot of the source at install time;
  re-running the installer is the upgrade path.

## Anti-patterns this script must not do

- Do **not** use `sudo`, install system-wide, or write anywhere itself.
- Do **not** edit shell start-up files or change `PATH`; print the hint and let
  the operator decide.
- Do **not** install Tart, Softnet, or VM images, and do **not** start a VM.
- Do **not** start the broker or register a repository; those are deliberate
  operator actions.
- Do **not** remove operator state or VMs on uninstall.
- Do **not** prompt or read standard input.
- Do **not** install source with uncommitted or untracked changes unless the
  operator passed `--allow-dirty`.
- Do **not** describe the committed-source check as protection against an agent
  that can write the checkout. It is not.

## Revision rules

- Removing a behavior, an input, an output, or an assumption requires a high
  threshold and a deliberate version bump. Removals are recheck signals for
  everything that depends on the script.
- Added assumptions never relax previous assumptions.
- Added capabilities to the declared capability surface require operator
  review; a maintenance session that adds a capability silently is producing
  drift.
- A version is ratified only after review; one change is one bump.
- The script's file header references the contract version it was built against.

## Change log

- **v0** — 2026-11-15: draft, not ratified. The script and its tests exist and
  were reviewed, but the shell tests have never been executed (the authoring
  session was not permitted to run shell scripts), so the first condition for
  ratifying v1 is not met.
  Review: 2026-11-15, three fresh subagents with each finding verified by a
  fourth, verdict "changes required"; 2 high, 12 medium, 22 low or
  informational findings across the installer, README, Makefile, and CI, all
  resolved in this draft (the install source is now checked against Git and
  `--editable` warns; help wins anywhere; `uv tool list` failing is an error;
  the uninstall hint, the PATH hint, and this contract's wording were
  corrected; tests were added for each).
  A second round (same date and reviewer kinds, on those fixes) returned 5 low
  findings: every Git failure was read as "not a working tree"; the operator's
  Git settings could hide untracked files; `--dry-run` was described as running
  nothing; the link limit cannot be met; and the Python checks of the script's
  commands had blind spots. All were resolved in this draft. Those fixes have
  not been re-reviewed.
