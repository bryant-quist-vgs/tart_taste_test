"""The broker loop: watch spools, claim and validate requests, write results.

Built against contract v1 (contracts/taste_test_broker_contract.md).

The broker runs with the operator's authority and reads directories an untrusted
agent controls. It decides which spools to serve only through `served`, touches
them only through `spool.Spool`, admits request data in exactly one place
(`_admit`, from the bytes of the file it claimed), and prints only through
`notices`, which makes agent-derived text safe first.

Stopping is cooperative. A signal, `request_stop`, or the loss of the spool a
request came from only sets a flag; `tick`, which every wait in the broker
calls, turns the flag into `_Abort` when a request is in flight and no clean-up
is running. So a request is abandoned at a point where its VM is then stopped
and deleted in the ordinary way, and it still gets a result (`error`).
"""

import contextlib
import os
import secrets
import signal
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from tart_taste_test import __version__, audit, engine, protocol, repo_config, tart
from tart_taste_test.audit import Report
from tart_taste_test.engine import Cancelled, Engine, EngineError, SetupFailed, environment_key
from tart_taste_test.fairqueue import FairQueue, Key
from tart_taste_test.mirror import Mirror, MirrorError
from tart_taste_test.notices import Notices
from tart_taste_test.registry import Repository, State
from tart_taste_test.served import ServedSpools
from tart_taste_test.spool import SnapshotTooLarge, Spool, SpoolError

IDLE_SLEEP = 0.5
STOPPED = "the broker was stopped while this request ran"
LEFT_BEHIND = "a previous broker stopped while this request ran"
# A broker that stops uncleanly leaves at most one claimed request without a
# result. Any more in one spool were planted, so only this many per spool get an
# audit-log line and an event when the next broker starts: first the one the
# previous broker recorded as in flight, then the others in the order of their IDs.
MAX_RECOVERIES_LOGGED = 8


@dataclass(frozen=True)
class Pace:
    """The broker's pauses. Tests set them to zero."""

    ready_interval: float = engine.READY_INTERVAL
    delete_retry_pause: float = engine.DELETE_RETRY_PAUSE
    # The shortest time between two requests of one checkout starting.
    turn_interval: float = protocol.HEARTBEAT_INTERVAL_SECONDS


class Rejected(Exception):
    """The request or the repository config is not acceptable: outcome `rejected`."""


class _Withdrawn(Exception):
    """The client took its request back before the broker claimed it."""


class _Abort(BaseException):
    """Raised by `tick` to abandon the request in flight. Not an `Exception`, so
    nothing between the wait that raised it and `_process` mistakes it for a failure
    of its own."""


class Broker:
    def __init__(
        self,
        state: State,
        policy: repo_config.Policy,
        vms: tart.Tart | None = None,
        say: Callable[[str], None] | None = None,
        pace: Pace | None = None,
    ) -> None:
        pace = pace if pace is not None else Pace()
        self._state = state
        self._policy = policy
        self._notices = Notices(say)
        self._vms = vms if vms is not None else tart.Tart()
        self._vms.tick = self.tick
        self._engine = Engine(
            self._vms,
            state,
            self.tick,
            self._notices.warn,
            pace.ready_interval,
            pace.delete_retry_pause,
            self._shield,
        )
        self._spools = ServedSpools(state, self._notices)
        self._queue = FairQueue(pace.turn_interval)
        # What each waiting request was last told about its place.
        self._told: dict[Key, str] = {}
        self._last_beat = 0.0
        self._stopping = False
        # False only while a request is in flight and can still be abandoned.
        self._settled = True
        self._shielded = 0

    # -- scanning --------------------------------------------------------------

    def _is_stale(self, root: Path, request_id: str) -> bool:
        """Whether a request, seen now for the first time, has already waited too long."""
        held = self._spools.get(root)
        modified = None if held is None else held[1].request_modified(request_id)
        return (
            held is not None
            and modified is not None
            and not held[1].acknowledged(request_id)
            and time.time() - modified > protocol.MAX_REQUEST_AGE_SECONDS
        )

    def _scan(self) -> None:
        """Refresh the spools, tell waiting requests their place, and write the heartbeat."""
        try:
            self._spools.refresh()
        finally:
            # Even after a refresh that failed part-way, the queue names only
            # requests in spools that are still served.
            self._queue.observe(self._spools.unclaimed(), self._is_stale)
        order = self._queue.order()
        waiting = set(order)
        self._told = {key: told for key, told in self._told.items() if key in waiting}
        for position, key in enumerate(order):
            ahead = "next" if position == 0 else f"{position} ahead"
            held = self._spools.get(key[0])
            if held is not None and self._told.get(key) != ahead:
                with contextlib.suppress(SpoolError, OSError):
                    self._status(held[1], key[1], protocol.STATE_QUEUED, ahead)
                    self._told[key] = ahead
        beat = protocol.heartbeat_document(os.getpid(), __version__, time.time(), len(order))
        for _, spool in self._spools:
            with contextlib.suppress(SpoolError):
                spool.write_heartbeat(beat)

    def tick(self, force: bool = False) -> None:
        """Scan spools, acknowledge new requests, write the heartbeat; maybe abandon.

        Called from every wait loop in the broker, the engine, and the Tart and
        Git runners, so it keeps running while a request executes. The scan runs
        at most once per heartbeat interval unless `force` is set, and nothing
        it meets in a spool can make it raise. The one thing `tick` raises is
        `_Abort`, once per request, and only while that request is in flight and
        no clean-up is running.
        """
        now = time.monotonic()
        if force or now - self._last_beat >= protocol.HEARTBEAT_INTERVAL_SECONDS:
            self._last_beat = now
            try:
                self._scan()
            except Exception as error:
                # Once per kind of failure: the text may quote what an agent wrote.
                kind = type(error).__name__
                self._notices.warn_once(
                    f"a spool scan failed ({kind}): {error}", key=f"scan failed: {kind}"
                )
        if not self._settled and not self._shielded:
            why = self._spools.lost or (STOPPED if self._stopping else None)
            if why is not None:
                self._settled = True
                raise _Abort(why)

    @contextlib.contextmanager
    def _shield(self) -> Iterator[None]:
        """While entered, `tick` does not abandon the request: clean-up runs to its end."""
        self._shielded += 1
        try:
            yield
        finally:
            self._shielded -= 1

    def _status(self, spool: Spool, request_id: str, state: str, detail: str) -> None:
        document = protocol.status_document(request_id, state, detail, time.time())
        spool.write_document(request_id, protocol.STATUS_FILE, document)

    # -- one request -----------------------------------------------------------

    def _process(self, key: Key) -> None:
        """Take one request from claim to result. Never raises for a bad request."""
        root, request_id = key
        stale = self._queue.take(key)
        held = self._spools.get(root)
        if held is None:
            return  # its spool stopped being served after the queue last saw it
        repository, spool = held
        report = Report(request_id)
        started = time.monotonic()
        token = secrets.token_hex(6)
        staged = self._state.staging / f"{token}.tar"
        self._spools.in_use, self._spools.lost, self._settled = spool, None, False
        try:
            try:
                audit.note_in_flight(self._state, root, request_id)
            except OSError as error:
                self._notices.warn_once(
                    f"the record of the request in flight could not be written: {error}",
                    key="in-flight record",
                )
            try:
                self._execute(request_id, repository, spool, report, token, staged, started, stale)
            except _Withdrawn:
                self._notices.event(f"{request_id} was withdrawn before it was claimed")
                return
            except _Abort as abandoned:
                report.outcome, report.reason = "error", str(abandoned)
            except Rejected as error:
                report.outcome, report.reason = "rejected", str(error)
            except SetupFailed as error:
                report.outcome, report.reason = "setup-failed", str(error)
            except Cancelled:
                report.outcome, report.reason = "cancelled", "cancelled on request"
            except (EngineError, MirrorError, SpoolError, tart.TartError, OSError) as error:
                report.outcome, report.reason = "error", str(error)
            except Exception as error:
                # A defect in handling one request must not stop the broker or be
                # mistaken for a verdict.
                report.outcome = "error"
                report.reason = f"internal error ({type(error).__name__}): {error}"
            finally:
                self._settled = True
                with contextlib.suppress(OSError):
                    staged.unlink()
            report.reason = protocol.printable(report.reason)
            report.seconds.setdefault("total", time.monotonic() - started)
            self._finish(repository, spool, report, token)
        finally:
            # A spool that stopped being served mid-request was kept open until here.
            self._spools.release()
            audit.clear_in_flight(self._state)

    def _admit(self, data: bytes, request_id: str, stale: bool) -> protocol.Request:
        """Validate the bytes of a claimed request. The only place request data enters."""
        if stale:
            raise Rejected("the request waited too long for a broker; submit it again")
        try:
            return protocol.parse_request(data, request_id)
        except protocol.ProtocolError as error:
            raise Rejected(f"invalid request: {error}") from None

    def _stage(self, request: protocol.Request, spool: Spool, staged: Path) -> str:
        """Copy the snapshot to private staging and check it is the one the request names."""
        try:
            digest, size = spool.stage_source(request.id, staged, protocol.MAX_SOURCE_BYTES)
        except SnapshotTooLarge as error:
            raise Rejected(str(error)) from None
        if digest != request.sha256 or size != request.size:
            raise Rejected("the snapshot does not match the digest and size in the request")
        return digest

    def _config(
        self, repository: Repository, mirror: Mirror, commit: str
    ) -> repo_config.RepoConfig:
        try:
            raw = mirror.read_config(commit)
            if raw is None:
                raise Rejected(
                    f"no {repo_config.CONFIG_PATH} on branch {repository.branch} at "
                    f"{commit[:12]}; merge one to the default branch first"
                )
            return repo_config.parse(raw, self._policy)
        except repo_config.ConfigError as error:
            raise Rejected(f"{repo_config.CONFIG_PATH} on {repository.branch}: {error}") from None

    def _execute(
        self,
        request_id: str,
        repository: Repository,
        spool: Spool,
        report: Report,
        token: str,
        staged: Path,
        started: float,
        stale: bool,
    ) -> None:
        def cancelled() -> bool:
            return spool.cancel_requested(request_id)

        def status(state: str, detail: str) -> None:
            self._status(spool, request_id, state, detail)

        data = spool.claim(request_id)
        if data is None:
            raise _Withdrawn()
        try:
            request = self._admit(data, request_id, stale)
            report.command = {"name": request.command, "run": None, "args": list(request.args)}
            if cancelled():
                raise Cancelled()
            self._notices.event(f"{request_id} claimed ({spool.root}, command {request.command})")
            status(protocol.STATE_PREPARING, "staging the snapshot")
            report.source_sha256 = self._stage(request, spool, staged)
        finally:
            # From the claim on, the broker's own copy is the only one it uses.
            spool.remove_source(request_id)

        status(protocol.STATE_PREPARING, "refreshing the default branch")
        mirror = Mirror(self._state, repository, self.tick)
        commit = mirror.refresh()
        report.default_branch = {"name": repository.branch, "commit": commit}
        config = self._config(repository, mirror, commit)
        command = config.commands.get(request.command)
        if command is None:
            defined = ", ".join(sorted(config.commands))
            raise Rejected(f"unknown command {request.command!r}; defined commands: {defined}")
        if request.args and not command.allow_args:
            raise Rejected(f"command {command.name!r} does not accept arguments")
        report.command = {"name": command.name, "run": command.run, "args": list(request.args)}
        report.network = {"setup": config.setup_network, "run": config.run_network}

        key = environment_key(commit, config.setup_network)
        environment = self._engine.names.environment(repository.key, key)
        reused = self._engine.environment_exists(environment)
        if cancelled():
            raise Cancelled()
        if not reused:
            self._notices.event(f"{request_id} building environment {environment}")
            status(
                protocol.STATE_PREPARING,
                f"building the environment image for {commit[:12]} (first request for this commit)",
            )
            with os.fdopen(spool.create_log(request_id, protocol.SETUP_LOG), "wb") as setup_log:
                self._engine.build_environment(
                    repository.key, commit, config, mirror, setup_log, cancelled
                )
        report.environment = {"vm": environment, "reused": reused}
        prepared = time.monotonic()
        report.seconds["preparing"] = prepared - started

        status(protocol.STATE_PREPARING, "starting the run VM")
        with os.fdopen(spool.create_log(request_id, protocol.OUTPUT_LOG), "wb") as output:
            done = self._engine.run_request(
                token,
                environment,
                config,
                command,
                request.args,
                staged,
                output,
                lambda: status(protocol.STATE_RUNNING, command.name),
                cancelled,
            )
        report.seconds["running"] = time.monotonic() - prepared
        report.outcome = done.outcome
        report.command_exit = done.command_exit
        report.reason = done.reason
        report.output_bytes = done.output_bytes
        report.truncated = done.truncated

    def _finish(
        self,
        repository: Repository,
        spool: Spool,
        report: Report,
        token: str | None,
        logged: bool = True,
    ) -> None:
        """Write result.json, mark the request finished, and append the audit line.

        Without `logged` (start-up recovery past its limit) only the result is written.
        """
        document = report.document()
        try:
            spool.write_document(report.request_id, protocol.RESULT_FILE, document)
            self._status(spool, report.request_id, protocol.STATE_FINISHED, report.outcome)
        except SpoolError as error:
            self._notices.warn(f"{report.request_id} result could not be written: {error}")
        if not logged:
            return
        run_vm = None if token is None else self._engine.names.run(token)
        try:
            audit.append(self._state, document, repository, spool.root, run_vm)
        except OSError as error:
            self._notices.warn(f"the audit log could not be written: {error}")
        detail = f" ({report.reason})" if report.reason else ""
        self._notices.event(f"{report.request_id} {report.outcome}{detail}")

    # -- the loop ----------------------------------------------------------------

    def _recover(self) -> None:
        """Finish what a previous broker left behind."""
        for name in self._engine.cleanup_transient():
            self._notices.event(f"deleted leftover VM {name}")
        for leftover in self._state.staging.iterdir():
            with contextlib.suppress(OSError):
                leftover.unlink()
        recorded = audit.in_flight(self._state)
        for repository, spool in self._spools:
            with contextlib.suppress(OSError):
                left = spool.interrupted()
                if recorded is not None and recorded[0] == str(spool.root) and recorded[1] in left:
                    left.remove(recorded[1])
                    left.insert(0, recorded[1])
                for count, request_id in enumerate(left):
                    report = Report(request_id, "error", LEFT_BEHIND)
                    self._finish(repository, spool, report, None, count < MAX_RECOVERIES_LOGGED)
                if len(left) > MAX_RECOVERIES_LOGGED:
                    self._notices.warn(
                        f"{spool.root} held {len(left)} claimed requests without a result; a "
                        f"broker leaves at most one. All were ended as errors; only "
                        f"{MAX_RECOVERIES_LOGGED} have an audit-log line"
                    )
        audit.clear_in_flight(self._state)

    def request_stop(self) -> None:
        """Ask `serve` to return. A request in flight is abandoned with outcome `error`,
        its VM is stopped and deleted, and its result is written first."""
        self._stopping = True

    def _stop_asked(self) -> bool:
        """Whether a stop was asked for. `serve` reads the flag through this call
        because a signal handler sets it at any moment, which a type checker
        reading the loop cannot know."""
        return self._stopping

    def serve(self, once: bool = False) -> int:
        """Run until asked to stop; with `once`, until no request is waiting."""

        def stop(_number: int, _frame: FrameType | None) -> None:
            self._stopping = True

        # Signal handlers can only be set from the main thread; elsewhere the
        # embedding code stops the broker with `request_stop`.
        handled = (signal.SIGTERM, signal.SIGINT)
        in_main = threading.current_thread() is threading.main_thread()
        previous = [signal.signal(number, stop) for number in handled] if in_main else []
        try:
            self._spools.refresh()
            self._recover()
            policy = self._policy
            self._notices.event(
                f"broker {__version__} ready (max setup network {policy.max_setup_network}, "
                f"max run network {policy.max_run_network}, max timeout {policy.max_timeout}s)"
            )
            while not self._stop_asked():
                self.tick(force=True)
                if self._stop_asked():
                    break  # a stop that arrived during the scan claims nothing
                key = self._queue.next()
                if key is not None:
                    self._process(key)
                elif once and not len(self._queue):
                    break
                else:
                    time.sleep(IDLE_SLEEP)
            if self._stop_asked():
                self._notices.event("stopped")
            return 0
        finally:
            for number, handler in zip(handled, previous, strict=False):
                signal.signal(number, handler)
            self._spools.close()
