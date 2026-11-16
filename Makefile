# tart-taste-test quality gates.
#
# `make commit` is the local pre-commit aggregate and the only thing CI runs, so
# a gate cannot exist in one place and not the other.
#
# Python gates run through `uv run --locked`, which syncs the dev group pinned
# in uv.lock (pytest, ruff, mypy, shellcheck-py) and fails if the lockfile is
# stale. The documentation gates use two external commands, glossarian and
# llm-size-guard; `make install-linters` installs both at the commits pinned
# below, and CI installs them the same way.

UV      ?= uv
UV_RUN  := $(UV) run --locked
PY_SRC  := src tests

# The repository's own files: tracked or not yet tracked, never ignored ones
# (virtual environments, caches, a developer's private notes).
REPO_FILES := git ls-files --cached --others --exclude-standard

# Every shell script in the repository, wherever it lives. Outside a Git working
# tree (a source archive, a VM guest) the scripts at the root are taken.
SHELL_SRC := $(or $(shell $(REPO_FILES) -- '*.sh' 2>/dev/null),$(wildcard *.sh))

# Gates keep throwaway files in .gate-scratch/, which Git ignores and which holds
# nothing else. The path is written out wherever it is used, and is not a
# variable, so that no setting from the command line or the environment can aim
# a removal at another directory.

# Pinned documentation linters. Bump a commit here, in one place, to upgrade.
# The repository and the commit are both fixed here: neither can be replaced
# from the environment.
GLOSSARIAN_URL      := git+ssh://git@github.com/bryant-quist-vgs/glossarian.git
GLOSSARIAN_REF      := 392ef31406ba156fcca0dee77e865ac30230c676
LLM_SIZE_GUARD_URL  := git+ssh://git@github.com/bryant-quist-vgs/llm_file_size_guard.git
LLM_SIZE_GUARD_REF  := 98271c1c7f0c6f603cc0d62977f7b780529aeae7

.DEFAULT_GOAL := help

.PHONY: help lint test format format-check docs-lint llm-size-guard commit \
        install-linters require-glossarian require-llm-size-guard

help:
	@echo "Targets:"
	@echo "  format           apply the code formatter (ruff format)"
	@echo "  format-check     verify formatting without changing files"
	@echo "  lint             static checks: ruff, mypy (src and tests), shellcheck"
	@echo "  test             the deterministic test suite (no VM, no network)"
	@echo "  docs-lint        validate docs/glossary.md and every glossary link in the repository"
	@echo "  llm-size-guard   fail when a repository file is too large for LLM maintenance"
	@echo "  commit           every gate above except format; run before committing"
	@echo "  install-linters  install glossarian and llm-size-guard at the pinned commits"

format:
	$(UV_RUN) ruff format $(PY_SRC)

format-check:
	$(UV_RUN) ruff format --check $(PY_SRC)

# shellcheck runs on every shell script the repository contains. The installer
# and its tests are shell, so an empty list means the list is wrong: the gate
# fails instead of passing without linting them.
lint:
	$(UV_RUN) ruff check $(PY_SRC)
	$(UV_RUN) mypy $(PY_SRC)
	@test -n "$(strip $(SHELL_SRC))" || { \
	  echo "Error: no shell script was found to lint; the installer is expected." >&2; \
	  exit 1; }
	$(UV_RUN) shellcheck $(SHELL_SRC)

# The suite never starts a VM: tests put a fake `tart` on PATH. The install
# script's shell suite runs unconditionally, so a missing or renamed suite fails
# the gate instead of being skipped; it is hermetic (fake `uv`, temporary
# directory only).
test:
	$(UV_RUN) pytest
	bash taste_test_install_test.sh

# The glossary governs the whole repository, so links are resolved in every
# Markdown file of the repository, not only under docs/. glossarian-gather scans
# a directory and knows nothing of .gitignore, so it is given a copy that holds
# the repository's Markdown and nothing else. The result then does not depend on
# ignored or private files that happen to sit in a developer's checkout. A file
# deleted from the working tree but not yet from the index is left out; a file
# whose name Git prints in quotes (unusual characters) fails the gate, because
# it could not be copied under that name and would otherwise go unchecked.
docs-lint: require-glossarian
	glossarian-check docs/glossary.md
	@rm -rf .gate-scratch/docs
	@mkdir -p .gate-scratch/docs
	@{ $(REPO_FILES) -- '*.md' 2>/dev/null || find . -name '*.md' -not -path './.*'; } | \
	while IFS= read -r file; do \
	  case "$$file" in \
	    \"*) echo "Error: docs-lint cannot read the file name $$file (Git quotes it); rename the file." >&2; exit 1 ;; \
	  esac; \
	  if [ -f "$$file" ]; then \
	    mkdir -p ".gate-scratch/docs/$$(dirname "$$file")" && \
	    cp "$$file" ".gate-scratch/docs/$$file" || exit 1; \
	  fi; \
	done
	glossarian-gather .gate-scratch/docs --repo-root .gate-scratch/docs

# The guard's accept/defer state is the tracked file named in
# .llm-file-size-guard.toml, so a suppression is a reviewable change and the
# result is the same on every machine and in CI.
#
# The guard takes its file list from the Git index, so on its own it would not
# see a file before that file's first commit. It is run against a throwaway
# index that names every repository file, tracked or not (`--intent-to-add`
# records names only). The real index is not touched.
llm-size-guard: require-llm-size-guard
	@mkdir -p .gate-scratch
	@rm -f .gate-scratch/index
	@GIT_INDEX_FILE=.gate-scratch/index git add --intent-to-add -- .
	GIT_INDEX_FILE=.gate-scratch/index llm-size-guard check

# Order: cheapest and most local first, so a formatting slip fails in seconds.
commit: format-check lint docs-lint test llm-size-guard
	@echo "commit gate: all checks passed"

install-linters:
	$(UV) tool install --force "$(GLOSSARIAN_URL)@$(GLOSSARIAN_REF)"
	$(UV) tool install --force "$(LLM_SIZE_GUARD_URL)@$(LLM_SIZE_GUARD_REF)"

# A missing documentation linter fails the gate. Skipping would let a broken
# glossary through on any machine that never installed the tool.
require-glossarian:
	@command -v glossarian-check >/dev/null 2>&1 && command -v glossarian-gather >/dev/null 2>&1 || { \
	  echo "Error: glossarian-check and glossarian-gather are not on PATH." >&2; \
	  echo "Install them with: make install-linters" >&2; \
	  exit 1; }

require-llm-size-guard:
	@command -v llm-size-guard >/dev/null 2>&1 || { \
	  echo "Error: llm-size-guard is not on PATH." >&2; \
	  echo "Install it with: make install-linters" >&2; \
	  exit 1; }
