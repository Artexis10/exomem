"""Linux managed-service lifecycle; public requests are forwarded by the ingress.

This module owns processes and deployment records, never vault or OAuth stores.
The supported daemon runs as a verified systemd user-unit MainPID.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def private_directory(path: Path) -> Path:
    """Create an owner-only directory without following its final symlink."""
    path = Path(os.path.abspath(path))
    if path.is_symlink():
        raise ValueError("managed directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("managed directory must be private to its owner (0700)")
    return path


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ReleaseRecords:
    """Atomic, content-free deployment records outside the vault."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _read(self, name: str) -> dict[str, Any] | None:
        path = self.directory / name
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                raise ValueError("deployment record must be private and bounded")
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("invalid deployment record")
        return value

    def _write(self, name: str, value: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".release-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / name)
            _sync_directory(self.directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def active(self) -> dict[str, Any] | None:
        return self._read("active.json")

    def pending(self) -> dict[str, Any] | None:
        return self._read("transition.json")

    def begin(
        self, target: dict[str, Any], *, worker_pid: int, identity: dict[str, Any] | None = None
    ) -> None:
        if self.pending() is not None:
            raise RuntimeError("an unfinished transition already exists; resume it")
        self._write(
            "transition.json",
            {
                "target": target,
                "worker_pid": worker_pid,
                "phase": "drained",
                "identity": identity or {},
            },
        )

    def phase(self, phase: str, **fields: Any) -> None:
        pending = self.pending()
        if pending is None:
            raise RuntimeError("no transition to update")
        self._write("transition.json", {**pending, **fields, "phase": phase})

    def accept(self, target: dict[str, Any]) -> None:
        # Active is published first. A crash before receipt removal requires
        # explicit roll-forward; it never silently selects the prior release.
        self._write("active.json", target)
        (self.directory / "transition.json").unlink(missing_ok=True)
        _sync_directory(self.directory)


@contextmanager
def deployment_lock(directory: Path) -> Iterator[None]:
    import fcntl

    descriptor = os.open(directory / "manager.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("deployment lock must be owner-only")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("a managed supervisor already holds the deployment lock") from error
        yield
    finally:
        os.close(descriptor)


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("deadline must be a positive finite number")
        self.clock = clock
        self.ends = clock() + seconds

    def remaining(self, maximum: float) -> float:
        remaining = min(maximum, self.ends - self.clock())
        if remaining <= 0:
            raise TimeoutError("managed upgrade deadline expired")
        return remaining


def _descendants(
    *, ignore_sessions: frozenset[int] = frozenset()
) -> dict[int, tuple[int, str]]:
    """Read the current descendant tree, including subreaper-adopted children.

    ``ignore_sessions`` excludes the session of a deliberately co-owned tree so
    one worker's exit can be proven while another is still running. Every worker
    and migrator is spawned with ``start_new_session=True``, so a session id is
    exactly one spawned tree and nothing in it calls ``setsid`` again. A standby
    is the only tree this supervisor ever runs beside the serving worker, and
    each one's stop proof is still exhaustive within its own session.
    """
    processes: dict[int, tuple[int, str]] = {}
    sessions: dict[int, int] = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            pid = int(path.name)
            processes[pid] = (int(fields[1]), fields[0])
            sessions[pid] = int(fields[3])
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            continue
    owned: dict[int, tuple[int, str]] = {}
    parents = {os.getpid()}
    while True:
        new = {
            pid for pid, (parent, _) in processes.items() if parent in parents and pid not in owned
        }
        if not new:
            break
        owned.update({pid: processes[pid] for pid in new})
        parents = new
    if ignore_sessions:
        owned = {
            pid: value
            for pid, value in owned.items()
            if sessions.get(pid) not in ignore_sessions
        }
    return owned


async def stop_owned_process(
    child: asyncio.subprocess.Process,
    *,
    timeout: float,
    include_adopted: bool = False,
    ignore_sessions: frozenset[int] = frozenset(),
) -> None:
    """Stop an owned process tree and require observed exit before returning.

    ``include_adopted`` is only for the single-purpose daemon after enabling
    Linux subreaper mode. A caller must never use it in a shared application.
    """
    deadline = Deadline(timeout)
    signalled: set[int] = set()
    if child.returncode is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    while True:
        owned = _descendants(ignore_sessions=ignore_sessions) if include_adopted else {}
        for pid, (_, state) in owned.items():
            if pid == child.pid:
                continue  # asyncio owns reaping this direct child
            if state == "Z":
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            elif pid not in signalled:
                try:
                    os.kill(pid, signal.SIGTERM)
                    signalled.add(pid)
                except ProcessLookupError:
                    pass
        group_alive = False
        try:
            os.killpg(child.pid, 0)
            group_alive = True
        except ProcessLookupError:
            pass
        if (
            child.returncode is not None
            and not group_alive
            and not _live_descendants(include_adopted, ignore_sessions)
        ):
            return
        await asyncio.sleep(min(0.025, deadline.remaining(0.025)))


def _live_descendants(
    include_adopted: bool, ignore_sessions: frozenset[int] = frozenset()
) -> bool:
    return bool(_descendants(ignore_sessions=ignore_sessions)) if include_adopted else False


#: A standby warms while the previous worker still serves, so its budget is
#: separate from the cutover budget and is measured in minutes, not seconds.
STANDBY_WARM_ENV = "EXOMEM_STANDBY_WARM_SECONDS"
DEFAULT_STANDBY_WARM_SECONDS = 300.0
#: A cold worker warms the same catalogs a standby does, so its default budget
#: matches. It is a separate knob because it answers a separate question: how
#: long a hung cold worker stays invisible, with nothing else serving.
COLD_START_ENV = "EXOMEM_COLD_START_SECONDS"
DEFAULT_COLD_START_SECONDS = 300.0
COLD_START_FLOOR_SECONDS = 120.0


def standby_warm_budget() -> float:
    """How long a candidate may warm beside the serving worker."""
    raw = os.environ.get(STANDBY_WARM_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return DEFAULT_STANDBY_WARM_SECONDS


def cold_start_budget() -> float:
    """How long a cold worker may warm before its readiness window closes."""
    raw = os.environ.get(COLD_START_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return DEFAULT_COLD_START_SECONDS


def unready_reason(payload: Any) -> str | None:
    """Name what an unready worker last reported waiting on, or ``None``.

    Observation only. Nothing in the handoff branches on this, no budget is
    derived from it, and no wait ends earlier or later because it is present,
    absent or unchanged. It exists so a failed handoff record says what the
    replacement was doing instead of only that it ran out of time.

    Every value it can return comes from a fixed vocabulary the readiness
    contract defines -- `reasons` entries, the lexical repair phase, the
    cutover component names -- so no configuration or vault content can reach
    a record through it.
    """
    if not isinstance(payload, dict):
        return None
    raw_reasons = payload.get("reasons")
    reason = (
        next((entry for entry in raw_reasons if isinstance(entry, str) and entry), None)
        if isinstance(raw_reasons, list)
        else None
    )
    phase = None
    retrieval = payload.get("retrieval")
    if isinstance(retrieval, dict):
        repair = retrieval.get("repair")
        if isinstance(repair, dict):
            raw_phase = repair.get("phase")
            if isinstance(raw_phase, str) and raw_phase and raw_phase != "idle":
                phase = raw_phase
    if reason and phase:
        return f"{reason} (repair: {phase})"
    if reason or phase:
        return reason or phase
    # A standby answers with cutover components rather than serving reasons.
    cutover = payload.get("cutover")
    components = cutover.get("components") if isinstance(cutover, dict) else None
    if isinstance(components, dict):
        waiting = [
            name
            for name, state in components.items()
            if isinstance(name, str) and state != "ready"
        ]
        if waiting:
            return waiting[0]
    return None


def cold_start_window(timeout: float | None = None, floor: float | None = None) -> float:
    """The window a replacement gets to report ready while nothing else serves.

    One source of truth for every wait that begins after the previous worker
    has stopped: the supervisor's cold start, its promotion and cold-start
    waits during a handoff, and the operator client's polling deadline. The
    floor is a parameter so a test can shrink the window without shortening the
    production one.
    """
    return max(
        COLD_START_FLOOR_SECONDS if floor is None else floor,
        cold_start_budget() if timeout is None else timeout,
    )


class Supervisor:
    """Serialize replacement while the ingress retains client connections."""

    def __init__(
        self,
        directory: Path,
        *,
        initial_target: dict[str, Any],
        ingress: Any,
        runtime: Any,
        identity: dict[str, Any],
        transition_timeout: float = 40,
        standby_warm_timeout: float | None = None,
        cold_start_timeout: float | None = None,
        cold_start_floor: float | None = None,
    ):
        self.records = ReleaseRecords(directory)
        self.initial_target = initial_target
        self.ingress = ingress
        self.runtime = runtime
        self.identity = identity
        self.transition_timeout = transition_timeout
        self.standby_warm_timeout = (
            standby_warm_budget() if standby_warm_timeout is None else standby_warm_timeout
        )
        self.cold_start_timeout = (
            cold_start_budget() if cold_start_timeout is None else cold_start_timeout
        )
        self.cold_start_floor = (
            COLD_START_FLOOR_SECONDS if cold_start_floor is None else cold_start_floor
        )
        self.lock = asyncio.Lock()
        self.phase = "unavailable"
        self.transition_task: asyncio.Task | None = None
        #: Identifies the in-flight transition an operator is polling, and
        #: retains the finished one's outcome so a client that acknowledged and
        #: went away can still read what happened.
        self.transition_id: str | None = None
        self.last_transition: dict[str, Any] | None = None

    def _replacement_budget(self) -> float:
        """How long a replacement may take to report ready with nothing serving."""
        return cold_start_window(self.cold_start_timeout, self.cold_start_floor)

    async def start(self) -> None:
        if self.records.pending() is not None:
            self.phase = "recovery-required"
            self.ingress.unavailable()
            return
        target = self.records.active() or self.initial_target
        target = await self.runtime.inspect(target)
        # A window shorter than the cold worker's warm stops it mid-warm, and
        # the unit restarts into the same cold catalog.
        client = await self.runtime.start(target, timeout=self._replacement_budget())
        self.records.accept(target)
        self.ingress.resume(client)
        self.phase = "ready"

    def status(self) -> dict[str, Any]:
        import sys

        return {
            "ok": True,
            "phase": self.phase,
            "active": self.records.active(),
            "pending": self.records.pending(),
            "worker_pid": self.runtime.pid,
            "launcher_python": sys.executable,
            "unit": self.identity.get("unit"),
            "ingress": self.ingress.stats,
            "port": getattr(self.runtime, "port", None),
            "transition": self.transition_id,
            "last_transition": self.last_transition,
        }

    def _environment_notes(self, handoff: dict[str, Any]) -> dict[str, Any]:
        """Carry an unreadable environment file into every handoff record.

        A child that silently inherited a stale environment is the failure this
        read exists to prevent, so the note belongs on the discarded and
        unsupported paths as much as on the one that promoted.
        """
        warnings = getattr(self.runtime, "environment_warnings", None)
        if warnings:
            handoff["environment"] = list(warnings)
        return handoff

    async def _warm_standby(
        self, target: dict[str, Any], *, resume: bool
    ) -> tuple[dict[str, Any], Any]:
        """Warm the candidate beside the serving worker under its own budget.

        Nothing here pauses, drains or signals the worker that is serving. A
        candidate that cannot reach cutover readiness inside the warm budget is
        discarded with the component it waited on recorded, and the upgrade
        continues through the one-worker sequence — reported, never silent
        (`seamless-managed-worker-handoff` D7).
        """
        if resume:
            # A recorded transition has already stopped its worker; there is
            # nothing left to warm beside.
            return self._environment_notes(
                {"standby": "unsupported", "reason": "resuming a recorded transition"}
            ), None
        if not getattr(self.runtime, "standby_capable", False):
            return self._environment_notes(
                {"standby": "unsupported", "reason": "target release has no standby mode"}
            ), None
        try:
            standby = await self.runtime.start_standby(
                target, timeout=self.standby_warm_timeout
            )
        except TimeoutError:
            await self._discard_standby("pending")
            return self._environment_notes(
                {
                    "standby": "discarded",
                    "reason": "warm budget expired",
                    "waiting": getattr(self.runtime, "standby_waiting", None),
                }
            ), None
        except Exception:  # noqa: BLE001 - a candidate failure never touches the serving worker
            await self._discard_standby("pending")
            return self._environment_notes(
                {
                    "standby": "discarded",
                    "reason": "candidate could not warm",
                    "waiting": getattr(self.runtime, "standby_waiting", None),
                }
            ), None
        return self._environment_notes({"standby": "ready"}), standby

    async def _discard_standby(self, candidate: Any) -> None:
        """Stop a candidate without ever signalling the worker that is serving.

        Any truthy ``candidate`` means one may exist; discarding when none does
        is a no-op in the runtime.
        """
        if not candidate:
            return
        discard = getattr(self.runtime, "discard_standby", None)
        if discard is None:
            return
        try:
            await discard(timeout=10)
        except Exception:  # noqa: BLE001 - a stuck candidate must not mask the outcome
            pass

    def _migration_declared(self, target: dict[str, Any]) -> tuple[bool, str]:
        """Whether the staged target declares a state migration."""
        declared = getattr(self.runtime, "migration_required", None)
        if declared is None:
            return True, "target declaration unavailable"
        return declared(target)

    async def upgrade(
        self, target: dict[str, Any] | None, *, resume: bool = False
    ) -> dict[str, Any]:
        if self.lock.locked():
            return {"ok": False, "error": "an upgrade is already in progress"}
        async with self.lock:
            # A promotion record belongs to the transition that produced it.
            # Carried into a later attempt's handoff it would tell whoever
            # resumes that state was handed over when this attempt never
            # reached promotion -- the difference between a restart and a
            # rollback.
            if getattr(self.runtime, "promotion_record", None) is not None:
                self.runtime.promotion_record = None
            # Same reason: an earlier attempt's note would tell whoever resumes
            # that this replacement was waiting on something it never reached.
            if getattr(self.runtime, "replacement_waiting", None) is not None:
                self.runtime.replacement_waiting = None
            pending = self.records.pending()
            if bool(pending) != resume:
                return {
                    "ok": False,
                    "error": "resume the recorded transition"
                    if pending
                    else "no transition to resume",
                }
            if resume:
                target = pending["target"]
            try:
                target = await self.runtime.inspect(target)
            except Exception:  # noqa: BLE001 - reject candidate failures before changing admission
                return {
                    "ok": False,
                    "error": "target interpreter verification failed; current admission unchanged",
                }
            # A standby warm is minutes long and runs while the old worker is
            # still serving. The transition is in progress from here, which is
            # what a polling operator has to be able to see.
            self.phase = "upgrading"
            handoff, standby = await self._warm_standby(target, resume=resume)
            deadline = Deadline(self.transition_timeout)
            # Bound before the try so the failure path can tell a handoff that
            # burned the whole replacement window from one that died at the
            # stop, without claiming a measurement it never took.
            stopped_at: float | None = None
            # The window operators care about: nobody is served between here and
            # the resume below.
            paused_at = time.monotonic()
            self.ingress.pause()
            if not resume:
                try:
                    drained = await self.ingress.drain(deadline.remaining(30))
                except TimeoutError:
                    drained = False
                if not drained:
                    await self._discard_standby(standby)
                    self.ingress.resume()
                    self.phase = "ready"
                    return {
                        "ok": False,
                        "error": "active requests exceeded the drain budget; current worker is still serving",
                    }
                try:
                    self.records.begin(target, worker_pid=self.runtime.pid, identity=self.identity)
                except OSError:
                    await self._discard_standby(standby)
                    self.ingress.resume()
                    self.phase = "ready"
                    return {
                        "ok": False,
                        "error": "could not record the transition; current worker is still serving",
                    }
            try:
                # The cutover budget bounds everything that happens while
                # abandoning the upgrade is still cheap. Past the stop there is
                # no worker to give admission back to, so it ends there.
                async with asyncio.timeout(deadline.remaining(40)):
                    await self.ingress.detach_streams()
                    # Resume always repeats the stop proof, including a timed-out
                    # migrator or failed candidate retained by this supervisor.
                    self.records.phase("stopping")
                    await self.runtime.stop(timeout=deadline.remaining(10))
                    stopped_at = time.monotonic()
                    # The migrator is the only writer between the two workers,
                    # and it runs only when the target declares a state
                    # migration. A skipped step is recorded, never silent.
                    migrate, reason = self._migration_declared(target)
                    handoff["migration"] = {
                        "state": "ran" if migrate else "skipped",
                        "reason": reason,
                    }
                    if migrate:
                        self.records.phase("migrating", worker_pid=0)
                        await self.runtime.migrate(target, timeout=deadline.remaining(15))
                # Nothing is serving from here, and no rollback remains: the only
                # question left is whether the replacement becomes ready, so it
                # gets the same window a cold start gets. Ending the wait sooner
                # buys nothing -- it converts "unavailable for another minute"
                # into "unavailable until an operator resumes", and the resume
                # runs into the same wall, killing the background repair it is
                # waiting on and restarting it from zero. Ingress stays paused:
                # `_queue` already answers a request that outlasts its own
                # budget with a bounded, explicit, undispatched refusal.
                replacement_budget = self._replacement_budget()
                if standby is not None:
                    self.records.phase("promoting", worker_pid=0)
                    client, promotion = await self.runtime.promote_standby(
                        migrated=migrate, timeout=replacement_budget
                    )
                    handoff["promotion"] = promotion
                else:
                    self.records.phase("starting", worker_pid=0)
                    client = await self.runtime.start(target, timeout=replacement_budget)
                handoff["ready_after_ms"] = round((time.monotonic() - stopped_at) * 1000.0, 1)
                self.records.phase("ready", worker_pid=self.runtime.pid)
                self.records.accept(target)
                self.ingress.resume(client)
                self.phase = "ready"
                handoff["unavailable_ms"] = round(
                    (time.monotonic() - paused_at) * 1000.0, 1
                )
                return {
                    "ok": True,
                    "phase": "ready",
                    "active": target,
                    "worker_pid": self.runtime.pid,
                    "handoff": handoff,
                }
            # `CancelledError` deliberately does not reach this handler: it is
            # not an `Exception`, and a cancelled transition is a supervisor
            # shutdown, not a failed upgrade. `serve()`'s `finally` owns that
            # case -- it cancels the transition task, makes ingress
            # unavailable, discards the standby and stops the worker -- and
            # writing a `failed` record here would misreport a shutdown as one.
            except Exception:  # noqa: BLE001 - every post-stop failure must retain recovery state
                self.phase = "recovery-required"
                self.ingress.unavailable()
                await self._discard_standby(standby)
                # A promotion that was accepted before the failure is the single
                # most useful fact for whoever resumes: it says the state was
                # handed over, so the next start is a restart, not a rollback.
                promotion = getattr(self.runtime, "promotion_record", None)
                if promotion is not None:
                    handoff["promotion"] = promotion
                # How long the replacement was given, and its own account of
                # what it was doing with it. A handoff that burned the whole
                # window on a background repair and one that died two seconds
                # after the stop need entirely different responses.
                if stopped_at is not None:
                    handoff["ready_after_ms"] = round(
                        (time.monotonic() - stopped_at) * 1000.0, 1
                    )
                # Its own key: `waiting` belongs to a discarded standby's
                # component, and the handoff that discards a candidate and then
                # fails to start its replacement is the incident shape -- both
                # facts matter to whoever reads the record, so neither
                # displaces the other.
                waiting = getattr(self.runtime, "replacement_waiting", None)
                if waiting:
                    handoff["replacement_waiting"] = waiting
                # Error text from subprocesses can contain configuration or
                # vault content. Retain phase and identity, not arbitrary text.
                self.records.phase("failed", worker_pid=self.runtime.pid)
                try:
                    await self.runtime.stop(timeout=5)
                except Exception:  # noqa: BLE001 - never claim an unproven process exit
                    return {
                        "ok": False,
                        "error": "upgrade failed; owned process exit is unproven; resume required",
                        "handoff": handoff,
                    }
                self.records.phase("failed", worker_pid=0)
                return {
                    "ok": False,
                    "error": "upgrade failed after shutdown; worker is stopped; resume required",
                    "handoff": handoff,
                }


def verify_systemd_identity(
    unit: str,
    *,
    properties: dict[str, str] | None = None,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, str]:
    """Require systemd's cleanup boundary and an empty prior invocation."""
    import re
    import subprocess

    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit):
        raise ValueError("a systemd user service unit name is required")
    if properties is None:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=MainPID,ControlGroup,InvocationID,KillMode,SendSIGKILL,"
                "TimeoutStopUSec,EnvironmentFiles",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=5,
            check=True,
        )
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties.get("MainPID") != str(os.getpid()):
        raise RuntimeError("managed serve must be the selected systemd unit MainPID")
    if (
        properties.get("KillMode") != "control-group"
        or properties.get("SendSIGKILL") != "yes"
        or properties.get("TimeoutStopUSec", "") in {"", "infinity", "0"}
    ):
        raise RuntimeError("systemd must enforce bounded control-group cleanup")
    invocation = properties.get("InvocationID", "")
    if not re.fullmatch("[0-9a-f]{32}", invocation) or invocation != os.environ.get(
        "INVOCATION_ID"
    ):
        raise RuntimeError("systemd invocation identity does not match this supervisor")
    group = properties.get("ControlGroup", "")
    if not group.startswith("/") or ".." in Path(group).parts or group == "/":
        raise RuntimeError("invalid service control group")
    own_groups = (proc_root / "self/cgroup").read_text().splitlines()
    if f"0::{group}" not in own_groups:
        raise RuntimeError("supervisor does not belong to the selected service cgroup")
    directory = cgroup_root / group.lstrip("/")
    if not (directory / "cgroup.procs").is_file():
        raise RuntimeError("service cgroup cannot be inspected")
    members: set[int] = set()
    for path in directory.rglob("cgroup.procs"):
        members.update(int(pid) for pid in path.read_text().split())
    if members != {os.getpid()}:
        raise RuntimeError("service cgroup has residual processes; prior cleanup is unproven")
    identity = {
        "unit": unit,
        "invocation": invocation,
        "cgroup": group,
        "boot": (proc_root / "sys/kernel/random/boot_id").read_text().strip(),
    }
    # systemd renders this as `path (ignore_errors=...)` per file, and a unit may
    # declare several. Match every entry rather than anchoring on one, or two
    # files collapse into a single unusable path. Only paths are retained; the
    # files' values never enter this process.
    rendered = [
        path.lstrip("-")
        for path in re.findall(
            r"(\S+)\s*\(ignore_errors=[^)]*\)", properties.get("EnvironmentFiles", "")
        )
    ]
    files = [path for path in rendered if path and Path(path).is_absolute()]
    if files:
        identity["environment_files"] = files
        # Retained for the single-file case every managed unit renders today.
        identity["environment_file"] = files[0]
    return identity


def enable_subreaper() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "could not establish worker child ownership")


WORKER_PROTOCOL = 1


def remove_stale_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("managed socket path contains an unexpected file")
    path.unlink()


class WorkerRuntime:
    """Own every state-touching child through stop, migration and readiness.

    At most two owned worker trees exist at once: the one serving and, during an
    upgrade, the standby warming beside it. Each is its own session, so each
    one's exit proof stays exhaustive inside its session while the other runs.
    Only one of them ever owns state, and promotion is the single moment that
    changes.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        host: str,
        port: int,
        environment_file: Path | str | None = None,
        environment_files: Any = None,
    ):
        self.socket_path = socket_path
        self.host = host
        self.port = port
        self.child: asyncio.subprocess.Process | None = None
        self.client: Any = None
        self.standby: asyncio.subprocess.Process | None = None
        self.standby_client: Any = None
        self.standby_socket = socket_path.with_name(
            f"{socket_path.stem}-standby{socket_path.suffix}"
        )
        self.standby_capable = False
        self.standby_waiting: str | None = None
        #: What the replacement last reported waiting on after the old worker
        #: stopped. Recorded for the handoff; it gates nothing.
        self.replacement_waiting: str | None = None
        self.promotion_record: dict[str, Any] | None = None
        declared = environment_files if environment_files else (
            [environment_file] if environment_file else []
        )
        self.environment_files = tuple(Path(entry) for entry in declared)
        self.environment_warnings: list[str] = []

    @property
    def pid(self) -> int:
        return self.child.pid if self.child is not None and self.child.returncode is None else 0

    def _note_replacement_waiting(self, response: Any) -> None:
        """Remember an unready replacement's own account of what it is doing.

        Deliberately total: any malformed, absent or unparseable answer leaves
        the last note alone rather than raising. An observation that could fail
        a handoff would be worse than no observation.
        """
        try:
            if response.status_code not in {200, 503}:
                return
            reason = unready_reason(response.json())
        except Exception:  # noqa: BLE001 - observing must never fail a handoff
            return
        if reason:
            self.replacement_waiting = reason

    def _other_sessions(self, *, standby: bool) -> frozenset[int]:
        """The co-owned session a stop or spawn proof must leave alone."""
        other = self.standby if not standby else self.child
        # ``start_new_session=True`` makes the session id the spawned pid, and it
        # stays valid for members that outlive the direct child.
        return frozenset({other.pid}) if other is not None else frozenset()

    def _child_environment(self) -> dict[str, str] | None:
        """Overlay the unit's current environment file for the child only.

        systemd read ``EnvironmentFile=`` once, when this supervisor started, so
        an edited service environment would otherwise reach a worker only after
        a full restart — which is exactly what a seamless upgrade avoids. The
        file is re-read at spawn time and applied to the child's environment;
        this process's own environment is never changed and no value is logged.
        """
        if not self.environment_files:
            return None
        self.environment_warnings = []
        values: dict[str, str] = {}
        for path in self.environment_files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # An operator has to see this: the child silently inheriting a
                # stale environment is what this read exists to prevent.
                self.environment_warnings.append(f"unreadable: {path.name}")
                log.warning("managed service environment file could not be read")
                continue
            values.update(self._parse_environment(text))
        if not values:
            return None
        return {**os.environ, **values}

    @staticmethod
    def _parse_environment(text: str) -> dict[str, str]:
        """Read systemd `KEY=value` assignments; never log a value."""
        values: dict[str, str] = {}
        for line in text.splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#") or "=" not in entry:
                continue
            name, _, value = entry.partition("=")
            name = name.strip()
            if not name or not (name[0].isalpha() or name[0] == "_"):
                continue
            if not all(character.isalnum() or character == "_" for character in name):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            values[name] = value
        return values

    async def inspect(self, target: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(target, dict) or not {"python", "version"} <= set(target):
            raise ValueError("target must identify an interpreter and exact release")
        if set(target) - {"python", "version", "state_descriptors"}:
            raise ValueError("target carries fields the managed protocol does not define")
        interpreter, version = target["python"], target["version"]
        if (
            not isinstance(interpreter, str)
            or not Path(interpreter).is_absolute()
            or not os.access(interpreter, os.X_OK)
            or not isinstance(version, str)
            or not version
            or len(version) > 80
        ):
            raise ValueError("invalid target interpreter or version")
        # The probe also reports what this release can do and what its state
        # descriptors are, so the supervisor never has to run the migrator to
        # learn whether the target declares a migration.
        code = (
            "import json; from importlib.metadata import version; "
            "from exomem.service_manager import WORKER_PROTOCOL\n"
            "try:\n"
            "    from exomem.service_standby import STANDBY_ENV as _s\n"
            "    standby = True\n"
            "except Exception:\n"
            "    standby = False\n"
            "try:\n"
            "    from exomem.state_migration import declared_descriptor_ids\n"
            "    descriptors = list(declared_descriptor_ids())\n"
            "except Exception:\n"
            "    descriptors = []\n"
            'print(json.dumps({"version":version("exomem"),"protocol":WORKER_PROTOCOL,'
            '"standby":standby,"state_descriptors":descriptors}))'
        )
        probe = await asyncio.create_subprocess_exec(
            interpreter,
            "-I",
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(probe.communicate(), 10)
        except BaseException:
            if probe.returncode is None:
                probe.kill()
                await probe.wait()
            raise
        if probe.returncode != 0 or len(output) > 4096:
            raise ValueError("target cannot run the managed worker protocol")
        actual = json.loads(output)
        if not isinstance(actual, dict) or actual.get("version") != version:
            raise ValueError("target version differs")
        if actual.get("protocol") != WORKER_PROTOCOL:
            raise ValueError("target managed worker protocol differs")
        descriptors = actual.get("state_descriptors")
        if not isinstance(descriptors, list) or not all(
            isinstance(entry, str) and entry for entry in descriptors
        ):
            raise ValueError("target did not declare its state descriptors")
        declared = target.get("state_descriptors")
        if declared is not None and list(declared) != descriptors:
            raise ValueError("staged state-migration declaration does not match the target")
        self.standby_capable = actual.get("standby") is True
        return {
            "python": interpreter,
            "version": version,
            "state_descriptors": descriptors,
        }

    async def _spawn(self, command: list[str], *, standby: bool = False) -> None:
        ignore = self._other_sessions(standby=standby)
        occupied = self.standby if standby else self.child
        if occupied is not None or _descendants(ignore_sessions=ignore):
            raise RuntimeError("previous owned process exit has not been proven")
        pending = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                start_new_session=True,
                env=self._child_environment(),
            )
        )
        try:
            spawned = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Retain ownership even if cancellation arrives between fork and
            # subprocess construction. The failure path must stop this tree.
            spawned = await asyncio.shield(pending)
            if standby:
                self.standby = spawned
            else:
                self.child = spawned
            raise
        if standby:
            self.standby = spawned
        else:
            self.child = spawned

    async def stop(self, timeout: float) -> None:
        ignore = self._other_sessions(standby=False)
        if self.child is not None:
            await stop_owned_process(
                self.child, timeout=timeout, include_adopted=True, ignore_sessions=ignore
            )
            self.child = None
        elif _descendants(ignore_sessions=ignore):
            raise RuntimeError("untracked owned children require a service-manager cleanup")
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        remove_stale_socket(self.socket_path)

    async def migrate(self, target: dict[str, Any], timeout: float) -> None:
        vault = os.environ.get("EXOMEM_VAULT_PATH", "")
        if not vault or not Path(vault).is_absolute():
            raise ValueError("managed service requires its installed absolute vault binding")
        deadline = Deadline(timeout)
        await self._spawn(
            [
                target["python"],
                "-I",
                "-m",
                "exomem",
                "maintain",
                "--vault",
                vault,
                "--migrate-state",
                "--offline",
                "--json",
            ]
        )
        await asyncio.wait_for(self.child.wait(), deadline.remaining(timeout))
        if self.child.returncode != 0:
            raise RuntimeError("offline target preparation failed")
        # A completed migrator is not proof its children have stopped.
        await self.stop(timeout=deadline.remaining(timeout))

    async def start(self, target: dict[str, Any], timeout: float):
        import httpx

        deadline = Deadline(timeout)
        # The record always names something: a replacement that never answered
        # its readiness probe is a fact an operator needs, not an absent field.
        self.replacement_waiting = "unreachable"
        remove_stale_socket(self.socket_path)
        await self._spawn(
            [
                target["python"],
                "-I",
                "-m",
                "exomem.service_manager",
                "worker",
                "--socket",
                str(self.socket_path),
                "--host",
                self.host,
                "--port",
                str(self.port),
            ]
        )
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path), retries=0),
            base_url="http://localhost",
            trust_env=False,
            follow_redirects=False,
            timeout=None,
        )
        while True:
            if self.child.returncode is not None:
                raise RuntimeError("candidate worker exited before readiness")
            try:
                async with asyncio.timeout(deadline.remaining(2)):
                    health = await self.client.get("/health")
                    ready = await self.client.get("/health/ready")
                if health.status_code == 200 and health.json().get("version") != target["version"]:
                    raise RuntimeError("candidate is serving a different release")
                if (
                    health.status_code == 200
                    and ready.status_code == 200
                    and ready.json().get("status") == "ready"
                ):
                    self.replacement_waiting = None
                    return self.client
                self._note_replacement_waiting(ready)
            except (httpx.HTTPError, TimeoutError):
                pass
            await asyncio.sleep(min(0.1, deadline.remaining(0.1)))


    def _worker_command(self, target: dict[str, Any], socket_path: Path, *, standby: bool):
        command = [
            target["python"],
            "-I",
            "-m",
            "exomem.service_manager",
            "worker",
            "--socket",
            str(socket_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        if standby:
            command.append("--standby")
        return command

    async def _probe(self, client: Any, target: dict[str, Any]) -> dict[str, Any]:
        """Read one worker's liveness and readiness pair."""
        health = await client.get("/health")
        ready = await client.get("/health/ready")
        if health.status_code == 200 and health.json().get("version") != target["version"]:
            raise RuntimeError("candidate is serving a different release")
        if health.status_code != 200 or ready.status_code not in {200, 503}:
            return {}
        payload = ready.json()
        return payload if isinstance(payload, dict) else {}

    def migration_required(self, target: dict[str, Any]) -> tuple[bool, str]:
        """Whether the staged target declares a state migration for this vault.

        Derived entirely from the state manifest the running system already
        maintains: a manifest that is not complete needs one, and so does a
        target whose descriptor set differs from the one the manifest was
        published with. Nothing here runs the migrator to find out
        (`seamless-managed-worker-handoff` D8).
        """
        vault = os.environ.get("EXOMEM_VAULT_PATH", "")
        if not vault or not Path(vault).is_absolute():
            return True, "vault binding unavailable"
        declared = target.get("state_descriptors")
        if not declared:
            return True, "target declares no descriptor set"
        from . import state_migration

        try:
            status = state_migration.migration_status(Path(vault))
        except Exception:  # noqa: BLE001 - an unreadable manifest is the migrator's problem
            return True, "state manifest unreadable"
        if status != "complete":
            return True, f"state manifest {status}"
        recorded = state_migration.recorded_descriptor_ids(Path(vault))
        if recorded is None:
            return True, "state manifest records no descriptor set"
        if tuple(declared) != tuple(recorded):
            return True, "descriptors_changed"
        return False, "declared_none"

    async def start_standby(self, target: dict[str, Any], timeout: float):
        """Warm a candidate beside the serving worker until it is cutover-ready.

        The standby binds its own socket and owns nothing: it takes no writer
        lease, publishes nothing and schedules no work until promotion. This
        never signals, pauses or inspects the worker that is serving.
        """
        import httpx

        if not self.standby_capable:
            raise RuntimeError("target release cannot run as a standby")
        deadline = Deadline(timeout)
        # The record always names something: a candidate that never answered its
        # readiness probe is a fact an operator needs, not an absent field.
        self.standby_waiting = "unreachable"
        remove_stale_socket(self.standby_socket)
        await self._spawn(
            self._worker_command(target, self.standby_socket, standby=True), standby=True
        )
        self.standby_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.standby_socket), retries=0),
            base_url="http://localhost",
            trust_env=False,
            follow_redirects=False,
            timeout=None,
        )
        while True:
            if self.standby.returncode is not None:
                raise RuntimeError("standby worker exited before cutover readiness")
            try:
                async with asyncio.timeout(deadline.remaining(2)):
                    payload = await self._probe(self.standby_client, target)
                cutover = payload.get("cutover") or {}
                components = cutover.get("components") or {}
                waiting = [name for name, state in components.items() if state != "ready"]
                if waiting:
                    self.standby_waiting = waiting[0]
                if cutover.get("cutover_ready") is True:
                    self.standby_waiting = None
                    return self.standby_client
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            await asyncio.sleep(min(0.1, deadline.remaining(0.1)))

    async def discard_standby(self, timeout: float = 10) -> None:
        """Stop the candidate's tree without touching the serving worker's."""
        if self.standby is not None:
            await stop_owned_process(
                self.standby,
                timeout=timeout,
                include_adopted=True,
                ignore_sessions=self._other_sessions(standby=True),
            )
            self.standby = None
        if self.standby_client is not None:
            await self.standby_client.aclose()
            self.standby_client = None
        remove_stale_socket(self.standby_socket)

    def _assume_promoted(self) -> None:
        """One owner at a time: the promoted tree becomes the serving one.

        Idempotent, because the readiness-timeout path calls it before raising.
        """
        if self.standby is None:
            return
        self.child, self.standby = self.standby, None
        self.client, self.standby_client = self.standby_client, None
        self.socket_path, self.standby_socket = self.standby_socket, self.socket_path
        self.standby_waiting = None

    async def promote_standby(self, *, migrated: bool, timeout: float):
        """Hand state ownership to the warmed candidate and serve from it.

        Called only after the previous worker and its descendants have provably
        exited and after the migrator has run or been recorded as skipped.
        """
        import httpx

        if self.standby is None or self.standby_client is None:
            raise RuntimeError("no standby worker to promote")
        if self.child is not None or _descendants(
            ignore_sessions=frozenset({self.standby.pid})
        ):
            raise RuntimeError("previous owned process exit has not been proven")
        deadline = Deadline(timeout)
        # A migrated promotion re-runs the whole source proof inside this POST
        # -- seconds on a large vault, and it grows with the corpus -- so the
        # call gets the cold-start budget this method is handed rather than a
        # fixed ten seconds. Capping it lower would time out the request while
        # the promotion it asked for was still running, and discard a standby
        # that was about to succeed. Without a migration the POST only acquires
        # ownership, so it keeps the tight cap that surfaces an unresponsive
        # candidate quickly; the readiness wait below gets the whole budget
        # either way.
        promote_budget = deadline.remaining(timeout if migrated else 10)
        async with asyncio.timeout(promote_budget):
            response = await self.standby_client.post(
                "/control/promote", json={"migrated": bool(migrated)}
            )
        if response.status_code != 200:
            raise RuntimeError("standby refused promotion")
        # Ownership has already changed hands. Record that before waiting on
        # readiness so an exhausted wait cannot lose the fact that it happened.
        record = response.json()
        self.promotion_record = record
        self.replacement_waiting = "unreachable"
        try:
            while True:
                if self.standby.returncode is not None:
                    raise RuntimeError("promoted worker exited before readiness")
                try:
                    async with asyncio.timeout(deadline.remaining(2)):
                        health = await self.standby_client.get("/health")
                        ready = await self.standby_client.get("/health/ready")
                    if (
                        health.status_code == 200
                        and ready.status_code == 200
                        and ready.json().get("status") == "ready"
                    ):
                        self.replacement_waiting = None
                        break
                    self._note_replacement_waiting(ready)
                except (httpx.HTTPError, TimeoutError, ValueError):
                    pass
                await asyncio.sleep(min(0.1, deadline.remaining(0.1)))
        except TimeoutError:
            # The promotion itself was accepted, so this process already holds
            # the writer lease: it is the serving worker that has not answered
            # yet, not a candidate to throw away. Track it as such so the
            # failure path stops the right tree, and say which of the two
            # happened rather than reporting a generic cutover failure.
            self._assume_promoted()
            raise RuntimeError(
                "promotion was accepted but the worker did not report readiness "
                "inside the cold-start budget"
            ) from None
        self._assume_promoted()
        return self.client, record


async def control_server(path: Path, supervisor: Supervisor) -> asyncio.Server:
    """Serve a bounded local-only JSON protocol; no public ASGI control route."""
    import socket
    import struct

    remove_stale_socket(path)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid():
                raise ValueError("control peer is not the service owner")
            line = await asyncio.wait_for(reader.readline(), 5)
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid control request")
            command = request.get("command")
            if command == "status":
                result = supervisor.status()
            elif command in {"upgrade", "resume"}:
                if supervisor.transition_task is not None and not supervisor.transition_task.done():
                    result = {"ok": False, "error": "an upgrade is already in progress"}
                else:
                    # A standby warm is minutes long, so the transition is
                    # acknowledged rather than awaited: holding the control
                    # connection open for it turns any client read timeout into
                    # a false failure while promotion proceeds regardless. The
                    # client polls `status` for the outcome.
                    transition = uuid.uuid4().hex
                    supervisor.transition_id = transition
                    supervisor.last_transition = None
                    task = asyncio.create_task(
                        supervisor.upgrade(
                            request.get("target"),
                            resume=command == "resume",
                        )
                    )

                    def _record(finished: asyncio.Task, identifier: str = transition) -> None:
                        if finished.cancelled():
                            outcome: dict[str, Any] = {
                                "ok": False,
                                "error": "the transition was cancelled",
                            }
                        elif finished.exception() is not None:
                            # Error text from a transition can carry
                            # configuration or vault content; keep the shape.
                            outcome = {"ok": False, "error": "the transition failed"}
                        else:
                            outcome = finished.result()
                        supervisor.last_transition = {"transition": identifier, **outcome}

                    task.add_done_callback(_record)
                    supervisor.transition_task = task
                    result = {
                        "ok": True,
                        "accepted": True,
                        "transition": transition,
                        "phase": supervisor.phase,
                    }
            else:
                result = {"ok": False, "error": "unknown control command"}
        except (ValueError, TimeoutError, ConnectionError):
            result = {"ok": False, "error": "invalid or incomplete control request"}
        try:
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_unix_server(handle, path=str(path), limit=65536)
    os.chmod(path, 0o600)
    return listener


async def serve(supervisor: Supervisor, *, host: str, port: int) -> None:
    """Keep ingress and control alive until the owning unit stops this daemon."""
    import uvicorn

    async with await control_server(supervisor.records.directory / "control.sock", supervisor):
        await supervisor.start()
        server = uvicorn.Server(
            uvicorn.Config(
                supervisor.ingress,
                host=host,
                port=port,
                lifespan="off",
                access_log=False,
                timeout_graceful_shutdown=10,
            )
        )

        async def monitor() -> None:
            while True:
                await asyncio.sleep(0.25)
                if supervisor.phase == "ready" and not supervisor.runtime.pid:
                    # A crash is not a managed handoff. Stop the supervisor so
                    # the unit cleans the whole cgroup and applies Restart=.
                    supervisor.ingress.unavailable()
                    server.should_exit = True
                    return

        monitoring = asyncio.create_task(monitor())
        try:
            await server.serve()
        finally:
            monitoring.cancel()
            await asyncio.gather(monitoring, return_exceptions=True)
            if supervisor.transition_task is not None and not supervisor.transition_task.done():
                supervisor.transition_task.cancel()
                await asyncio.gather(supervisor.transition_task, return_exceptions=True)
            try:
                await asyncio.wait_for(supervisor.ingress.aclose(), 10)
            except TimeoutError:
                pass
            discard = getattr(supervisor.runtime, "discard_standby", None)
            if discard is not None:
                try:
                    await discard(timeout=5)
                except Exception:  # noqa: BLE001 - shutdown still has to stop the worker
                    pass
            await supervisor.runtime.stop(timeout=5)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from importlib.metadata import version

    parser = argparse.ArgumentParser(prog="python -m exomem.service_manager")
    commands = parser.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("serve", help="run the installed systemd managed service")
    daemon.add_argument("--runtime-dir", type=Path, required=True)
    daemon.add_argument("--worker-python", required=True)
    daemon.add_argument("--unit-name", required=True)
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--port", type=int, default=8765)
    worker = commands.add_parser("worker", help="run the supervisor-owned private HTTP worker")
    worker.add_argument("--socket", type=Path, required=True)
    worker.add_argument("--host", required=True)
    worker.add_argument("--port", type=int, required=True)
    worker.add_argument(
        "--standby",
        action="store_true",
        help="warm beside the serving worker and own no state until promoted",
    )
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("managed services currently require Linux systemd user units")
    if args.command == "worker":
        from . import server

        private_directory(args.socket.parent)
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            worker_socket=args.socket,
            standby=args.standby,
        )
        return 0
    try:
        directory = private_directory(args.runtime_dir)
        # The standby socket is the longest name this directory has to hold.
        if len(os.fsencode(directory / "worker-standby.sock")) >= 104:
            raise ValueError("managed runtime directory is too long for Unix sockets")
        if not 0 < args.port < 65536:
            raise ValueError("port must be between 1 and 65535")
        with deployment_lock(directory):
            identity = verify_systemd_identity(args.unit_name)
            enable_subreaper()
            from .service_ingress import ServiceIngress

            host = os.environ.get("EXOMEM_HOST") or args.host
            runtime = WorkerRuntime(
                directory / "worker.sock",
                host=host,
                port=args.port,
                environment_files=identity.get("environment_files"),
            )
            supervisor = Supervisor(
                directory,
                initial_target={"python": args.worker_python, "version": version("exomem")},
                ingress=ServiceIngress(),
                runtime=runtime,
                identity=identity,
            )
            asyncio.run(serve(supervisor, host=host, port=args.port))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"managed service: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
