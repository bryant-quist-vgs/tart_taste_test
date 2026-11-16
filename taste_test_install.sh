#!/usr/bin/env bash
# taste_test_install.sh — install, verify, or remove the taste-test commands.
# Built against contract v0 (taste_test_install_contract.md).
# It delegates to `uv tool install`; the contract lists everything it may do.

set -u
unset CDPATH

readonly TOOL_NAME="tart-taste-test"
readonly COMMANDS="taste-test taste-test-broker"
# What Git says (with LC_ALL=C) when a directory is in no repository at all.
readonly NO_REPOSITORY="not a git repository"
# A guard, not a limit anyone meets: the operating system refuses a shorter chain
# of links before this script starts. It ends the loop below if links are
# changed while it runs.
readonly MAX_LINK_HOPS=40

usage() {
  cat <<'EOF'
Usage: bash taste_test_install.sh [--editable] [--allow-dirty] [--dry-run]
       bash taste_test_install.sh --uninstall [--dry-run]
       bash taste_test_install.sh --check
       bash taste_test_install.sh -h | --help

Installs the taste-test and taste-test-broker commands from this checkout as an
isolated tool (uv tool install). Re-run it to upgrade after pulling new source.

taste-test-broker runs with your authority, outside any agent sandbox. Install
it only from a checkout that no agent can write, at a commit you have reviewed:
this script and the source it installs are whatever is in the checkout.

  (no option)   install or upgrade; refused when Git reports uncommitted or
                untracked changes in the checkout
  --allow-dirty install even with uncommitted or untracked changes, or when
                Git cannot report the checkout's state
  --editable    install so the commands run this checkout's source directly;
                whatever can write the checkout then changes what the broker does
  --uninstall   remove the installed tool (operator state and VMs are kept)
  --check       verify both commands are on PATH and print their versions
  --dry-run     print the uv command an install or uninstall would run, and
                run no uv command (an install's source check still runs git)
  -h, --help    show this help

It never uses sudo, never edits shell start-up files or PATH, and does not
install Tart, Softnet, or VM images.

Exit codes: 0 success; 1 refusal or failure (uv missing, not a checkout of this
project, uncommitted changes, uv or git failed, a --check item failed);
2 usage error.
EOF
}

usage_error() {
  printf 'Error: %s\n' "$1" >&2
  usage >&2
  exit 2
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

# Print one shell-quoted command line.
show_command() {
  local prefix="$1" word line
  shift
  line=""
  for word in "$@"; do
    line="$line $(printf '%q' "$word")"
  done
  printf '%s%s\n' "$prefix" "$line"
}

# The absolute, physical directory that holds this script, through any symlinks.
script_directory() {
  local source="${BASH_SOURCE[0]}" directory target hops=0
  while [ -L "$source" ]; do
    hops=$((hops + 1))
    [ "$hops" -le "$MAX_LINK_HOPS" ] || return 1
    directory="$(cd -P -- "$(dirname -- "$source")" && pwd)" || return 1
    target="$(readlink -- "$source")" || return 1
    case "$target" in
      /*) source="$target" ;;
      *) source="$directory/$target" ;;
    esac
  done
  cd -P -- "$(dirname -- "$source")" && pwd
}

optional_tool_notes() {
  if ! command -v tart >/dev/null 2>&1; then
    echo "Note: tart is not on PATH. The broker needs Tart to start virtual machines."
  fi
  if ! command -v softnet >/dev/null 2>&1; then
    echo "Note: softnet is not on PATH. The isolated and internet network modes need Softnet;"
    echo "      without it, virtual machines asked for those modes fail to start."
  fi
}

check() {
  local name version status failed=0
  for name in $COMMANDS; do
    if ! command -v "$name" >/dev/null 2>&1; then
      echo "FAIL $name (not on PATH)"
      failed=1
      continue
    fi
    version="$("$name" --version 2>&1)"
    status=$?
    if [ "$status" -eq 0 ]; then
      echo "PASS $name ($version)"
    else
      echo "FAIL $name (exit $status)"
      failed=1
    fi
  done
  optional_tool_notes
  return "$failed"
}

# A Git working tree whose state Git would not report: refuse, unless told otherwise.
unchecked_source() {
  local repo_root="$1" allow_dirty="$2" why="$3"
  if [ "$allow_dirty" -eq 1 ]; then
    echo "Warning: $why in $repo_root, so the source was not compared"
    echo "         with a commit; installing it anyway (--allow-dirty)."
    return 0
  fi
  fail "$why in $repo_root, so the source cannot be compared with a commit.
See: git -C <checkout> status. Fix that, or pass --allow-dirty to install anyway"
}

# Refuse source that differs from what Git has recorded, unless told otherwise.
# This catches an accident (forgotten edits, an agent's uncommitted work). It is
# not a defence against a checkout an agent can write: such an agent can commit,
# or change this script.
require_committed_source() {
  local repo_root="$1" allow_dirty="$2" changes answer
  if ! command -v git >/dev/null 2>&1; then
    echo "Warning: git is not on PATH, so the source in $repo_root cannot be"
    echo "         compared with a commit. Install only source you have reviewed."
    return 0
  fi
  # LC_ALL=C so that Git's own words can be told apart: only "not a git
  # repository" means there is nothing to compare with. Any other refusal (a
  # checkout owned by someone else, a damaged repository) is a failed check.
  if ! answer="$(LC_ALL=C git -C "$repo_root" rev-parse --is-inside-work-tree 2>&1)"; then
    case "$answer" in
      *"$NO_REPOSITORY"*)
        echo "Warning: $repo_root is not a Git working tree, so its source cannot be"
        echo "         compared with a commit. Install only source you have reviewed."
        return 0
        ;;
    esac
    unchecked_source "$repo_root" "$allow_dirty" "Git could not read the repository"
    return 0
  fi
  # The settings are given here so that the operator's Git configuration cannot
  # hide untracked files or changed submodules from the check.
  if ! changes="$(git -C "$repo_root" -c core.fsmonitor=false status --porcelain \
    --untracked-files=normal --ignore-submodules=none 2>/dev/null)"; then
    unchecked_source "$repo_root" "$allow_dirty" "Git could not report the state of the working tree"
    return 0
  fi
  if [ -z "$changes" ]; then
    return 0
  fi
  if [ "$allow_dirty" -eq 1 ]; then
    echo "Warning: installing source with uncommitted or untracked changes (--allow-dirty)."
    return 0
  fi
  fail "$repo_root has uncommitted or untracked changes. The broker runs with your
authority, so install only reviewed, committed source: commit or remove the
changes (see: git status), or pass --allow-dirty to install them anyway"
}

install() {
  local repo_root="$1" editable="$2" dry_run="$3" bin_dir
  local -a cmd=(uv tool install --force --reinstall --compile-bytecode)
  if [ "$editable" -eq 1 ]; then
    cmd+=(--editable)
    echo "Warning: --editable makes both commands run the source in $repo_root directly."
    echo "         Whatever can write that directory can change what taste-test-broker"
    echo "         does with your authority. Do not use it for a checkout an agent works in."
  fi
  cmd+=("$repo_root")

  if [ "$dry_run" -eq 1 ]; then
    show_command "would run:" "${cmd[@]}"
    return 0
  fi
  show_command "running:" "${cmd[@]}"
  "${cmd[@]}" || fail "uv tool install failed; nothing was verified"

  bin_dir="$(uv tool dir --bin 2>/dev/null)" || bin_dir=""
  if [ -n "$bin_dir" ]; then
    echo "installed taste-test and taste-test-broker in $bin_dir"
  fi
  if ! command -v taste-test >/dev/null 2>&1; then
    if [ -n "$bin_dir" ]; then
      echo "taste-test is not on PATH yet. Add the directory above to PATH, or run:"
    else
      echo "taste-test is not on PATH yet. To add uv's tool directory to PATH, run:"
    fi
    echo "  uv tool update-shell"
  fi
  optional_tool_notes
  echo "Next:"
  echo "  taste-test-broker doctor                       check the installation"
  echo "  taste-test-broker register --remote-url URL    allow a repository (run in it)"
  echo "  taste-test-broker serve                        keep this running"
}

uninstall() {
  local dry_run="$1" listing
  local -a cmd=(uv tool uninstall "$TOOL_NAME")
  if [ "$dry_run" -eq 1 ]; then
    show_command "would run:" "${cmd[@]}"
    return 0
  fi
  listing="$(uv tool list 2>&1)" ||
    fail "uv tool list failed, so it is not known whether $TOOL_NAME is installed: $listing"
  if ! printf '%s\n' "$listing" | grep -q "^$TOOL_NAME "; then
    echo "$TOOL_NAME is not installed; nothing to do"
    return 0
  fi
  show_command "running:" "${cmd[@]}"
  "${cmd[@]}" || fail "uv tool uninstall failed"
  echo "Operator state and any cached virtual machines were left in place."
  echo "Removing the virtual machines needs the tool. To remove them now: install"
  echo "again, run 'taste-test-broker prune --environments', then uninstall."
}

main() {
  local mode="" editable=0 dry_run=0 allow_dirty=0 repo_root argument
  # Help wins wherever it appears, before any other argument is examined.
  for argument in "$@"; do
    case "$argument" in
      -h | --help)
        usage
        exit 0
        ;;
    esac
  done
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --uninstall | --check)
        [ -z "$mode" ] || usage_error "only one of --uninstall and --check may be given, once"
        mode="${1#--}"
        ;;
      --editable)
        [ "$editable" -eq 0 ] || usage_error "--editable was given more than once"
        editable=1
        ;;
      --dry-run)
        [ "$dry_run" -eq 0 ] || usage_error "--dry-run was given more than once"
        dry_run=1
        ;;
      --allow-dirty)
        [ "$allow_dirty" -eq 0 ] || usage_error "--allow-dirty was given more than once"
        allow_dirty=1
        ;;
      *) usage_error "unknown option: $1" ;;
    esac
    shift
  done
  if [ "$mode" = "check" ] && [ "$dry_run" -eq 1 ]; then
    usage_error "--check cannot be combined with --dry-run"
  fi
  if [ -n "$mode" ] && [ "$editable" -eq 1 ]; then
    usage_error "--editable is only valid when installing"
  fi
  if [ -n "$mode" ] && [ "$allow_dirty" -eq 1 ]; then
    usage_error "--allow-dirty is only valid when installing"
  fi

  repo_root="$(script_directory)" ||
    fail "cannot determine the directory of this script (too many symbolic links?)"

  if [ "$mode" = "check" ]; then
    check
    exit $?
  fi

  command -v uv >/dev/null 2>&1 ||
    fail "uv was not found on PATH. Install uv first (https://docs.astral.sh/uv/)"

  if [ "$mode" = "uninstall" ]; then
    uninstall "$dry_run"
    exit $?
  fi

  if [ ! -f "$repo_root/pyproject.toml" ] ||
    [ ! -f "$repo_root/src/tart_taste_test/client_cli.py" ]; then
    fail "$repo_root is not a tart-taste-test checkout (pyproject.toml or the package is missing)"
  fi
  require_committed_source "$repo_root" "$allow_dirty"
  install "$repo_root" "$editable" "$dry_run"
}

main "$@"
