#!/usr/bin/env bash
# taste_test_install_test.sh — tests for taste_test_install.sh, built against
# contract v0 (taste_test_install_contract.md).
#
# Hermetic: `uv`, `git`, `taste-test`, and `taste-test-broker` are replaced by
# fake programs in a temporary directory, PATH holds only that directory and the
# system tools, and nothing outside "$TMPDIR" is written. No network, no real
# installation, and the state of the real checkout does not affect the result.
#
# Coverage (contract behaviours):
#   1  argument parsing: unknown option, repeated and conflicting options -> 2;
#      help wins wherever it appears
#   2  self-location through an absolute link, a relative link to a link, and a
#      path with spaces
#   3  --check: PASS, FAIL (not on PATH), FAIL (exit N); needs neither uv nor a
#      checkout                                                      [load-bearing]
#   4  preconditions: uv missing -> 1; not a checkout -> 1, nothing run
#   5  committed source: uncommitted changes, or a Git that cannot report the
#      checkout's state -> 1 unless --allow-dirty; not a Git working tree ->
#      warning                                                      [load-bearing]
#   6  install: the exact uv command line; --editable and its warning; uv
#      failure -> 1; both PATH hints                                [load-bearing]
#   7  uninstall: nothing installed -> 0; installed -> uv tool uninstall and the
#      hint; uv tool list or uninstall failing -> 1
#   8  optional-tool notes for tart and softnet
#   9  --dry-run prints the uv command and runs no uv command
#   capability boundary: sudo is never invoked; standard input is not read
#
# Not covered: a machine without git on PATH (the system directories these tests
# need also hold git), and the link-hop guard (contract behavior 2).
#
# Run with:  bash taste_test_install_test.sh

set -u
unset CDPATH

HERE="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$HERE/taste_test_install.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/taste-test-install-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

TESTS=0
FAILURES=0
OUT=""
ERR=""
STATUS=0

# --- fixtures ---------------------------------------------------------------

FAKE_BIN="$WORK/bin"
UV_LOG="$WORK/uv.log"
GIT_LOG="$WORK/git.log"
SYSTEM_PATH="/usr/bin:/bin"

SWITCHES="uv-list uv-fail uv-list-fail uv-dir-fail git-dirty git-not-repo git-refuse git-fail sudo-called"

reset_fixtures() {
  local switch
  rm -rf "$FAKE_BIN"
  mkdir -p "$FAKE_BIN"
  for switch in $SWITCHES; do
    rm -f "$WORK/$switch"
  done
  : >"$UV_LOG"
  : >"$GIT_LOG"
  make_git
}

# A fake uv that records each invocation, one argument per line after a marker.
make_uv() {
  cat >"$FAKE_BIN/uv" <<EOF
#!/bin/sh
{ echo "CALL"; for a in "\$@"; do echo "  \$a"; done; } >>"$UV_LOG"
if [ "\$1 \$2" = "tool list" ]; then
  [ -f "$WORK/uv-list-fail" ] && { echo "uv: cannot read the tool directory" >&2; exit 3; }
  [ -f "$WORK/uv-list" ] && cat "$WORK/uv-list"
  exit 0
fi
if [ "\$1 \$2 \$3" = "tool dir --bin" ]; then
  [ -f "$WORK/uv-dir-fail" ] && exit 3
  echo "$WORK/tool-bin"
  exit 0
fi
[ -f "$WORK/uv-fail" ] && { echo "uv: simulated failure" >&2; exit 3; }
exit 0
EOF
  chmod +x "$FAKE_BIN/uv"
}

# A fake git, so the real checkout's state never decides a test. By default the
# directory is a clean working tree. Switches: git-not-repo, git-refuse (Git
# will not read the repository), git-dirty, git-fail (git status fails).
make_git() {
  cat >"$FAKE_BIN/git" <<EOF
#!/bin/sh
echo "\$*" >>"$GIT_LOG"
case " \$* " in
  *" rev-parse "*)
    [ -f "$WORK/git-not-repo" ] && echo "fatal: not a git repository: .git" >&2 && exit 128
    [ -f "$WORK/git-refuse" ] && echo "fatal: detected dubious ownership" >&2 && exit 128
    echo true
    ;;
  *" status "*)
    [ -f "$WORK/git-fail" ] && exit 1
    [ -f "$WORK/git-dirty" ] && echo " M src/tart_taste_test/broker.py"
    ;;
esac
exit 0
EOF
  chmod +x "$FAKE_BIN/git"
}

make_command() { # name exit-status output
  cat >"$FAKE_BIN/$1" <<EOF
#!/bin/sh
echo "$3"
exit $2
EOF
  chmod +x "$FAKE_BIN/$1"
}

make_both_commands() {
  make_command taste-test 0 "taste-test 0.1.0"
  make_command taste-test-broker 0 "taste-test-broker 0.1.0"
}

make_sudo_trap() {
  cat >"$FAKE_BIN/sudo" <<EOF
#!/bin/sh
touch "$WORK/sudo-called"
exit 1
EOF
  chmod +x "$FAKE_BIN/sudo"
}

run_installer() { # [installer-path] -- args...
  local installer="$INSTALLER"
  if [ "${1:-}" != "--" ]; then
    installer="$1"
    shift
  fi
  shift
  OUT="$(PATH="$FAKE_BIN:$SYSTEM_PATH" bash "$installer" "$@" 2>"$WORK/stderr" </dev/null)"
  STATUS=$?
  ERR="$(cat "$WORK/stderr")"
}

uv_calls() { grep -c '^CALL$' "$UV_LOG" || true; }

# --- assertions ---------------------------------------------------------------

begin() {
  TESTS=$((TESTS + 1))
  CURRENT="$1"
  FAILED_HERE=0
  reset_fixtures
}

flunk() {
  if [ "$FAILED_HERE" -eq 0 ]; then
    FAILURES=$((FAILURES + 1))
    FAILED_HERE=1
    printf 'FAIL %s\n' "$CURRENT"
  fi
  printf '     %s\n' "$1"
}

expect_status() { [ "$STATUS" -eq "$1" ] || flunk "expected exit $1, got $STATUS (stderr: $ERR)"; }
expect_out() { case "$OUT" in *"$1"*) ;; *) flunk "stdout lacks: $1 (stdout: $OUT)" ;; esac; }
expect_no_out() { case "$OUT" in *"$1"*) flunk "stdout has: $1" ;; *) ;; esac; }
expect_err() { case "$ERR" in *"$1"*) ;; *) flunk "stderr lacks: $1 (stderr: $ERR)" ;; esac; }
expect_uv_calls() { [ "$(uv_calls)" -eq "$1" ] || flunk "expected $1 uv call(s), got $(uv_calls)"; }
expect_uv_log() {
  local expected="$1"
  [ "$(cat "$UV_LOG")" = "$expected" ] || flunk "uv was called as: $(tr '\n' '|' <"$UV_LOG")"
}

# --- tests ----------------------------------------------------------------------

begin "help prints usage with exit codes and exits 0 without needing uv"
run_installer -- --help
expect_status 0
expect_out "Usage: bash taste_test_install.sh"
expect_out "Exit codes: 0 success; 1 refusal or failure"
expect_out "2 usage error"
run_installer -- -h
expect_status 0

begin "help wins wherever it appears, even after an invalid option"
make_uv
for arguments in "--frobnicate --help" "--check --check -h" "--uninstall --help"; do
  # shellcheck disable=SC2086  # the list is split into separate options on purpose
  run_installer -- $arguments
  [ "$STATUS" -eq 0 ] || flunk "'$arguments' exited $STATUS, expected 0"
  expect_out "Usage: bash taste_test_install.sh"
done
expect_uv_calls 0

begin "unknown option is a usage error"
make_uv
run_installer -- --frobnicate
expect_status 2
expect_err "Error: unknown option: --frobnicate"
expect_err "Usage: bash taste_test_install.sh"
expect_uv_calls 0

begin "conflicting and repeated modes are usage errors"
make_uv
for arguments in "--uninstall --check" "--check --check" "--uninstall --uninstall" \
  "--check --dry-run" "--editable --uninstall" "--editable --check" \
  "--dry-run --dry-run" "--editable --editable" "--allow-dirty --allow-dirty" \
  "--allow-dirty --uninstall" "--allow-dirty --check" "extra"; do
  # shellcheck disable=SC2086  # the list is split into separate options on purpose
  run_installer -- $arguments
  [ "$STATUS" -eq 2 ] || flunk "'$arguments' exited $STATUS, expected 2"
done
expect_uv_calls 0

# Load-bearing: the exact delegated install command.
begin "install runs exactly the documented uv command"
make_uv
run_installer --
expect_status 0
expect_uv_log "CALL
  tool
  install
  --force
  --reinstall
  --compile-bytecode
  $HERE
CALL
  tool
  dir
  --bin"
expect_out "running: uv tool install --force --reinstall --compile-bytecode"
expect_no_out "Warning:" # the fake git reports a clean working tree
expect_out "installed taste-test and taste-test-broker in $WORK/tool-bin"
expect_out "taste-test is not on PATH yet"
expect_out "uv tool update-shell"
expect_out "taste-test-broker doctor"

begin "install does not print the PATH hint when the command is found"
make_uv
make_command taste-test 0 "taste-test 0.1.0"
run_installer --
expect_status 0
expect_no_out "is not on PATH yet"

begin "the PATH hint does not point at a directory that was never printed"
make_uv
touch "$WORK/uv-dir-fail"
run_installer --
expect_status 0
expect_no_out "installed taste-test and taste-test-broker in"
expect_no_out "the directory above"
expect_out "To add uv's tool directory to PATH, run:"
expect_out "uv tool update-shell"

# Load-bearing: source that differs from what Git recorded is not installed by accident.
begin "a checkout with uncommitted changes is refused unless --allow-dirty is given"
make_uv
touch "$WORK/git-dirty"
run_installer --
expect_status 1
expect_err "has uncommitted or untracked changes"
expect_err "--allow-dirty"
expect_uv_calls 0
run_installer -- --dry-run
expect_status 1
expect_uv_calls 0
run_installer -- --allow-dirty
expect_status 0
expect_out "Warning: installing source with uncommitted or untracked changes"
expect_out "running: uv tool install"
asked="status --porcelain --untracked-files=normal --ignore-submodules=none"
case "$(cat "$GIT_LOG")" in *"$asked"*) ;; *) flunk "git status was not asked: $asked" ;; esac

begin "when Git cannot report the checkout's state nothing is installed unless --allow-dirty"
for switch in git-fail git-refuse; do
  reset_fixtures
  make_uv
  touch "$WORK/$switch"
  run_installer --
  expect_status 1
  expect_err "so the source cannot be compared with a commit"
  expect_err "--allow-dirty"
  expect_uv_calls 0
  run_installer -- --allow-dirty
  expect_status 0
  expect_out "so the source was not compared"
done

begin "a directory that is not a Git working tree is installed with a warning"
make_uv
touch "$WORK/git-not-repo"
run_installer --
expect_status 0
expect_out "is not a Git working tree"
expect_out "running: uv tool install"

begin "editable install adds --editable before the checkout path and warns"
make_uv
run_installer -- --editable
expect_status 0
expect_out "Warning: --editable makes both commands run the source in"
expect_out "Do not use it for a checkout an agent works in"
expect_uv_log "CALL
  tool
  install
  --force
  --reinstall
  --compile-bytecode
  --editable
  $HERE
CALL
  tool
  dir
  --bin"

begin "a failing uv makes the install fail"
make_uv
touch "$WORK/uv-fail"
run_installer --
expect_status 1
expect_err "Error: uv tool install failed"
expect_no_out "installed taste-test"

begin "missing uv is a refusal naming uv"
run_installer --
expect_status 1
expect_err "Error: uv was not found on PATH"
run_installer -- --uninstall
expect_status 1
expect_err "uv was not found on PATH"

begin "dry run prints the uv command and runs no uv command"
make_uv
run_installer -- --dry-run
expect_status 0
expect_out "would run: uv tool install --force --reinstall --compile-bytecode"
expect_uv_calls 0
run_installer -- --dry-run --editable
expect_out "--compile-bytecode --editable"
expect_uv_calls 0
run_installer -- --uninstall --dry-run
expect_status 0
expect_out "would run: uv tool uninstall tart-taste-test"
expect_uv_calls 0

begin "a directory that is not a checkout is refused and nothing is run"
make_uv
mkdir -p "$WORK/elsewhere"
cp "$INSTALLER" "$WORK/elsewhere/taste_test_install.sh"
run_installer "$WORK/elsewhere/taste_test_install.sh" --
expect_status 1
expect_err "is not a tart-taste-test checkout"
expect_uv_calls 0

begin "the checkout is found through symbolic links and a path with spaces"
make_uv
# The dry run prints the checkout path shell-quoted, so compare it quoted.
quoted_here="$(printf '%q' "$HERE")"
mkdir -p "$WORK/a link dir"
ln -s "$INSTALLER" "$WORK/a link dir/install link.sh"
run_installer "$WORK/a link dir/install link.sh" -- --dry-run
expect_status 0
expect_out "--compile-bytecode $quoted_here"
# A relative link that points at another link, which points at the installer.
ln -s "install link.sh" "$WORK/a link dir/relative link.sh"
run_installer "$WORK/a link dir/relative link.sh" -- --dry-run
expect_status 0
expect_out "--compile-bytecode $quoted_here"
mkdir -p "$WORK/spaced checkout/src/tart_taste_test"
cp "$INSTALLER" "$WORK/spaced checkout/"
: >"$WORK/spaced checkout/pyproject.toml"
: >"$WORK/spaced checkout/src/tart_taste_test/client_cli.py"
run_installer "$WORK/spaced checkout/taste_test_install.sh" --
expect_status 0
resolved="$(cd -P -- "$WORK/spaced checkout" && pwd)"
case "$(cat "$UV_LOG")" in *"  $resolved"*) ;; *) flunk "the spaced path was not passed as one argument" ;; esac

begin "uninstall with nothing installed does nothing and succeeds"
make_uv
run_installer -- --uninstall
expect_status 0
expect_out "tart-taste-test is not installed; nothing to do"
expect_uv_log "CALL
  tool
  list"

begin "uninstall removes the tool and keeps operator state"
make_uv
printf 'other-tool v1.0\ntart-taste-test v0.1.0\n- taste-test\n' >"$WORK/uv-list"
run_installer -- --uninstall
expect_status 0
expect_uv_log "CALL
  tool
  list
CALL
  tool
  uninstall
  tart-taste-test"
expect_out "Operator state and any cached virtual machines were left in place"
expect_out "taste-test-broker prune --environments"

begin "a failing uv makes the uninstall fail"
make_uv
printf 'tart-taste-test v0.1.0\n' >"$WORK/uv-list"
touch "$WORK/uv-fail"
run_installer -- --uninstall
expect_status 1
expect_err "Error: uv tool uninstall failed"
expect_no_out "were left in place"

begin "when uv cannot list its tools the uninstall fails instead of reporting nothing to do"
make_uv
printf 'tart-taste-test v0.1.0\n' >"$WORK/uv-list"
touch "$WORK/uv-list-fail"
run_installer -- --uninstall
expect_status 1
expect_err "Error: uv tool list failed"
expect_no_out "nothing to do"
expect_uv_calls 1

begin "uninstall is not fooled by a tool with a longer name"
make_uv
printf 'tart-taste-test-extras v1.0\n' >"$WORK/uv-list"
run_installer -- --uninstall
expect_status 0
expect_out "is not installed"
expect_uv_calls 1

# Load-bearing: --check is the operator's verification step.
begin "check passes when both commands run, and needs no uv"
make_both_commands
run_installer -- --check
expect_status 0
expect_out "PASS taste-test (taste-test 0.1.0)"
expect_out "PASS taste-test-broker (taste-test-broker 0.1.0)"

begin "check needs no checkout and calls neither uv nor git when they are present"
make_uv
make_both_commands
mkdir -p "$WORK/elsewhere"
cp "$INSTALLER" "$WORK/elsewhere/taste_test_install.sh"
run_installer "$WORK/elsewhere/taste_test_install.sh" -- --check
expect_status 0
expect_out "PASS taste-test-broker"
expect_uv_calls 0
[ ! -s "$GIT_LOG" ] || flunk "git was called: $(tr '\n' '|' <"$GIT_LOG")"

begin "check fails for a command that is missing or exits non-zero"
make_command taste-test 4 "broken"
run_installer -- --check
expect_status 1
expect_out "FAIL taste-test (exit 4)"
expect_out "FAIL taste-test-broker (not on PATH)"

begin "missing tart and softnet are notes, not failures"
make_both_commands
run_installer -- --check
expect_status 0
expect_out "Note: tart is not on PATH"
expect_out "Note: softnet is not on PATH"
make_command tart 0 "2.41.1"
make_command softnet 0 "softnet"
run_installer -- --check
expect_status 0
expect_no_out "Note:"

begin "sudo is never invoked in any mode"
make_uv
make_sudo_trap
make_both_commands
printf 'tart-taste-test v0.1.0\n' >"$WORK/uv-list"
run_installer --
run_installer -- --editable
run_installer -- --check
run_installer -- --uninstall
[ ! -e "$WORK/sudo-called" ] || flunk "sudo was invoked"

# The installer and a following `read` share one open file. If the installer, or
# anything it starts, consumed input, the `read` would not get the first line.
begin "standard input is not read in any mode"
make_uv
make_both_commands
printf 'tart-taste-test v0.1.0\n' >"$WORK/uv-list"
printf 'first line\nsecond line\n' >"$WORK/stdin"
for arguments in "" "--editable" "--allow-dirty" "--dry-run" "--check" "--uninstall" "--help"; do
  remaining=""
  {
    # shellcheck disable=SC2086  # the list is split into separate options on purpose
    PATH="$FAKE_BIN:$SYSTEM_PATH" bash "$INSTALLER" $arguments >/dev/null 2>&1
    IFS= read -r remaining
  } <"$WORK/stdin"
  [ "$remaining" = "first line" ] || flunk "'$arguments' consumed standard input"
done

printf '%d tests, %d failure(s)\n' "$TESTS" "$FAILURES"
[ "$FAILURES" -eq 0 ]
