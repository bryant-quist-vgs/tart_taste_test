"""Operator state: where it lives and which repositories are registered.

Specified in docs/protocol.md §5. Everything here is written only by the broker
executable, in a directory sandboxed agents must not be able to write to.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_ENV = "TASTE_TEST_STATE_DIR"
DEFAULT_STATE = Path("Library") / "Application Support" / "tart-taste-test"
SCHEMA = 1

BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_REMOTE_HELPER_RE = re.compile(r"[A-Za-z0-9+.-]+::")
# A remote is reached over HTTPS or SSH: a URL with one of those schemes, or the
# scp-like form `user@host:path`. Local paths and file:// URLs are refused, so a
# "remote" can never be a repository inside an agent-writable checkout.
_REMOTE_RE = re.compile(r"(https://|ssh://|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:(?!/))\S+")
MAX_URL_LENGTH = 2048


class RegistryError(Exception):
    """Operator state is unreadable, or a value offered for it is not acceptable."""


@dataclass(frozen=True)
class Repository:
    path: str
    key: str
    branch: str
    source: str  # "remote" or "local"
    url: str | None
    registered: str

    def fetch_source(self) -> str:
        """What `git fetch` reads from: the recorded URL, or the repository itself."""
        return self.url if self.source == "remote" and self.url is not None else self.path


@dataclass(frozen=True)
class State:
    """Paths inside the operator state directory."""

    root: Path

    @property
    def registry(self) -> Path:
        return self.root / "registry.json"

    @property
    def lock(self) -> Path:
        return self.root / "broker.lock"

    @property
    def audit(self) -> Path:
        return self.root / "audit.log"

    @property
    def mirrors(self) -> Path:
        return self.root / "mirrors"

    @property
    def in_flight(self) -> Path:
        return self.root / "in-flight.json"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def vm_logs(self) -> Path:
        return self.root / "vm-logs"

    @property
    def registry_lock(self) -> Path:
        return self.root / "registry.lock"

    @property
    def key(self) -> str:
        """First 8 hex digits of the SHA-256 of this state directory's resolved path."""
        return hashlib.sha256(os.fsencode(self.root.resolve())).hexdigest()[:8]

    def ensure(self) -> None:
        """Create the state directories, private to the current user.

        An existing directory is left as it is: if it has become accessible to
        other users, `require_private` and `doctor` report that instead of
        quietly changing it.
        """
        for directory in (self.root, self.mirrors, self.staging, self.vm_logs):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def require_private(self) -> None:
        """Refuse to work in a state directory other users can read or write."""
        if not self.is_private():
            raise RegistryError(
                f"operator state {self.root} is accessible to other users; "
                f"run `chmod 700 {self.root}` (it must be owned by you)"
            )

    def is_private(self) -> bool:
        status = os.stat(self.root)
        return status.st_uid == os.getuid() and (status.st_mode & 0o077) == 0


def state_from_environment() -> State:
    override = os.environ.get(STATE_ENV)
    if override:
        return State(Path(override).expanduser().resolve())
    home = os.environ.get("HOME")
    if not home:
        raise RegistryError(f"HOME is not set; set {STATE_ENV} to choose a state directory")
    return State(Path(home) / DEFAULT_STATE)


def repo_key(path: Path) -> str:
    """First 12 hex digits of the SHA-256 of the resolved repository path."""
    return hashlib.sha256(os.fsencode(path.resolve())).hexdigest()[:12]


def check_branch(branch: str) -> str:
    if BRANCH_RE.fullmatch(branch) is None or ".." in branch:
        raise RegistryError(f"{branch!r} is not an acceptable branch name")
    return branch


def check_url(url: str) -> str:
    """Refuse URLs that Git could read as an option or as a remote-helper command."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise RegistryError("the remote URL is empty or too long")
    if url.startswith("-"):
        raise RegistryError("the remote URL must not begin with '-'")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise RegistryError("the remote URL contains a control character")
    if _REMOTE_HELPER_RE.match(url):
        raise RegistryError("remote-helper URLs (<name>::<address>) are not accepted")
    if _REMOTE_RE.fullmatch(url) is None:
        raise RegistryError(
            "the remote URL must be https://..., ssh://..., or user@host:path; local paths "
            "and file:// URLs are not accepted (use --trust-local for a local branch)"
        )
    return url


def new_repository(path: Path, branch: str, url: str | None) -> Repository:
    """Build a registry entry. `url` None means the local branch is trusted."""
    resolved = path.resolve()
    return Repository(
        path=str(resolved),
        key=repo_key(resolved),
        branch=check_branch(branch),
        source="local" if url is None else "remote",
        url=None if url is None else check_url(url),
        registered=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _entry(raw: Any) -> Repository:
    if not isinstance(raw, dict):
        raise RegistryError("a registry entry is not an object")
    try:
        entry = Repository(**{key: raw[key] for key in Repository.__dataclass_fields__})
    except KeyError as error:
        raise RegistryError(f"a registry entry lacks {error.args[0]!r}") from None
    for name in ("path", "key", "branch", "source", "registered"):
        if not isinstance(getattr(entry, name), str):
            raise RegistryError(f"registry entry field {name!r} is not a string")
    if entry.source not in ("remote", "local"):
        raise RegistryError(f"registry entry has unknown source {entry.source!r}")
    if re.fullmatch(r"[0-9a-f]{12}", entry.key) is None or not os.path.isabs(entry.path):
        raise RegistryError("registry entry has a malformed key or path")
    check_branch(entry.branch)
    if entry.source == "remote":
        if not isinstance(entry.url, str):
            raise RegistryError("a remote registry entry has no URL")
        check_url(entry.url)
    return entry


def load(state: State) -> list[Repository]:
    """Read the registry. A missing file is an empty registry."""
    try:
        text = state.registry.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        raise RegistryError(f"cannot read {state.registry}: {error.strerror}") from None
    try:
        document = json.loads(text)
    except ValueError as error:
        raise RegistryError(f"{state.registry} is not valid JSON: {error}") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise RegistryError(f"{state.registry} has an unknown schema")
    entries = document.get("repositories")
    if not isinstance(entries, list):
        raise RegistryError(f"{state.registry} has no repository list")
    return [_entry(raw) for raw in entries]


def save(state: State, repositories: list[Repository]) -> None:
    """Write the registry atomically."""
    state.ensure()
    ordered = sorted(repositories, key=lambda repository: repository.path)
    document = {"schema": SCHEMA, "repositories": [asdict(r) for r in ordered]}
    temporary = state.registry.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, state.registry)


@contextlib.contextmanager
def exclusive(state: State) -> Iterator[None]:
    """Hold the broker lock. Raises `RegistryError` if another process holds it.

    Also refuses a state directory that is not private: everything that starts
    VMs goes through this lock.
    """
    state.ensure()
    state.require_private()
    descriptor = os.open(state.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RegistryError(
                "another taste-test-broker is using this state directory; stop it first"
            ) from None
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def editing(state: State) -> Iterator[list[Repository]]:
    """Load the registry under its own lock and save whatever the block leaves in the list.

    `register` and `unregister` use this, so two of them never interleave. A
    running broker does not hold this lock; it re-reads the registry on its next
    scan and sees either the old file or the new one, never a partial one.
    """
    state.ensure()
    descriptor = os.open(state.registry_lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        repositories = load(state)
        yield repositories
        save(state, repositories)
    finally:
        os.close(descriptor)
