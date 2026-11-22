"""Durable intake for saves that a busy mutation boundary would refuse.

A save that meets a busy boundary used to return ``MUTATION_BUSY`` and write
nothing, so it survived only if the agent remembered to send it again. In the
serving HTTP process an eligible save is accepted instead: its arguments go to
``.write-intake.sqlite`` in the vault's state directory and the caller gets a
``queued`` receipt. One drainer thread applies the queued saves in acceptance
order, through ``writer_lease.invoke_command``, as the principal that sent each
one, so every ordinary check still runs.

Exactly once: the drainer replays a save under its own explicit identity,
``intake:<id>``, which the idempotency store keeps for 24 hours. A save whose
commit happened before a process stop replays its stored terminal after the
restart instead of writing again. While a save is open, a resend with the
original identity returns the same receipt and runs nothing.

A save that fails for any reason other than contention stays ``failed`` with
its refusal and its arguments until the owner dismisses it
(``exomem intake dismiss``). An applied save keeps its compact outcome and
drops its arguments; applied rows are pruned after seven days.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import reserved_paths, state_paths
from .cli_ops import OpError

log = logging.getLogger(__name__)

STORE_ID = "write-intake-store"
OWNER = "write_intake"
REF_PREFIX = "exomem://queued-write/"
MODE = "queued-write"

QUEUED = "queued"
APPLYING = "applying"
APPLIED = "applied"
FAILED = "failed"
_OPEN_STATES = (QUEUED, APPLYING)

#: Refusals that prove nothing committed because the boundary or the store was
#: busy. Closed protocol codes: only these turn into a queued receipt.
ACCEPTED_REFUSALS = frozenset({"MUTATION_BUSY", "MUTATION_WARMING", "COLLECTION_STORE_BUSY"})  # nosemgrep: ep-word-set -- closed public error codes of the mutation protocol.
#: Replay outcomes that are not final: the drainer tries the same identity again
#: later. An outcome-unknown or acknowledgement-pending replay resolves on a
#: same-identity retry, which the idempotency store answers.
RETRY_REFUSALS = ACCEPTED_REFUSALS | {  # nosemgrep: ep-word-set -- closed public error codes of the mutation protocol.
    "MUTATION_OUTCOME_UNKNOWN",
    "MUTATION_ACKNOWLEDGEMENT_PENDING",
    # Committed, terminal not persisted: its own remediation is a same-identity retry.
    "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN",
}
_REQUEST_NAMESPACE = uuid.UUID("5d1f3c62-8a7e-4f0b-9c2d-6e4b1a7f3c90")

MAX_ARGUMENT_BYTES = 4 * 1024 * 1024
APPLIED_RETENTION_SECONDS = 7 * 24 * 60 * 60.0
FIRST_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
_DEFAULT_INTAKE_WAIT_SECONDS = 15.0
_MAX_INTAKE_WAIT_SECONDS = 600.0
_IDLE_POLL_SECONDS = 5.0
_OUTCOME_LIMIT_BYTES = 16 * 1024
_LIST_LIMIT = 50

#: The arguments `invoke_command` takes for itself, not for the command leaf.
_CONTROL_ARGUMENTS = frozenset({  # nosemgrep: ep-word-set -- invoke_command's own keyword parameters.
    "idempotency_key", "public_idempotency_key", "idempotency_principal_scope",
    "implicit_idempotency_scope", "mutation_request_id", "absorb_unkeyed",
})


def enabled() -> bool:
    """Whether acceptance is on. Off still drains what is queued."""
    return os.environ.get("EXOMEM_BUSY_INTAKE", "1").strip().lower() not in {"0", "false"}  # nosemgrep: ep-word-membership -- the env switch's closed off spellings.


def intake_wait_seconds() -> float:
    """How long an eligible save waits for the boundary before it is queued."""
    try:
        wait = float(os.environ.get("EXOMEM_BUSY_INTAKE_AFTER_SECONDS", _DEFAULT_INTAKE_WAIT_SECONDS))
    except ValueError:
        return _DEFAULT_INTAKE_WAIT_SECONDS
    if wait != wait or wait in (float("inf"), float("-inf")):
        return _DEFAULT_INTAKE_WAIT_SECONDS
    return min(max(0.0, wait), _MAX_INTAKE_WAIT_SECONDS)


def store_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / ".write-intake.sqlite"


def ref_for(intake_id: str) -> str:
    return f"{REF_PREFIX}{intake_id}"


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- store


@contextmanager
def _store(vault_root: Path, *, create: bool) -> Iterator[sqlite3.Connection]:
    """Open the intake store under the private-state ceremony; ``FileNotFoundError`` for a read of none."""
    root = Path(vault_root).resolve()
    path = store_path(root)
    with reserved_paths._subsystem_authority_scope(OWNER):  # noqa: SLF001
        with reserved_paths._identity_coordination_scope(  # noqa: SLF001
            root, descriptor_ids=(STORE_ID,), identity_may_change=create
        ):
            with reserved_paths._sqlite_owner_target_scope(  # noqa: SLF001
                root, path, STORE_ID, create=create
            ) as retained:
                conn = _open(root, path, retained, create)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write(vault_root: Path) -> Iterator[sqlite3.Connection]:
    with _store(vault_root, create=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _open(root: Path, path: Path, retained: Path, create: bool) -> sqlite3.Connection:
    if create:
        retained.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(retained, timeout=5.0)
    else:
        if not retained.exists():
            raise FileNotFoundError(retained)
        conn = sqlite3.connect(f"{retained.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            # A queued receipt promises custody: it must survive a power loss, not only a kill.
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS intake ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, origin TEXT, "
                "command TEXT NOT NULL, arguments TEXT, principal TEXT NOT NULL, surface TEXT, "
                "state TEXT NOT NULL, owner TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
                "accepted_at REAL NOT NULL, updated_at REAL NOT NULL, next_attempt_at REAL NOT NULL, "
                "error_code TEXT, error TEXT, outcome TEXT, finished_at REAL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS intake_state ON intake(state, seq)")
            # One open save per original identity: a resend while it is open gets its receipt.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS intake_open_origin ON intake(origin) "
                "WHERE origin IS NOT NULL AND state IN ('queued', 'applying')"
            )
            conn.commit()
        reserved_paths._publish_sqlite_owner_family(root, path, STORE_ID, conn)  # noqa: SLF001
    except BaseException:
        conn.close()
        raise
    return conn


# --------------------------------------------------------------------------- identity and eligibility


def origin_identity(command_name: str, arguments: Mapping[str, Any], control: Mapping[str, Any]) -> str | None:
    """The original call's replay identity, hashed; ``None`` for an unkeyed call."""
    explicit = control.get("idempotency_key")
    if explicit:
        material = ["explicit", control.get("idempotency_principal_scope") or "", explicit]
    elif control.get("implicit_idempotency_scope"):
        material = ["implicit", control["implicit_idempotency_scope"], command_name, _canonical(arguments)]
    else:
        return None
    return hashlib.sha256("\0".join(str(part) for part in material).encode("utf-8")).hexdigest()


def principal_record(principal: Any) -> dict[str, Any] | None:
    """The stored form of an owner principal, or ``None`` for a caller whose authority may not be replayed.

    Only the owner (`raw_protection.is_owner`) is stored. A delegated authorization session can
    be narrowed or revoked after the save is accepted, so replaying it later could exceed what
    the owner still grants; such a caller keeps the busy refusal. The origin-session binding
    of a local or remote client is kept, so any live-session check runs against the login
    that sent the save.
    """
    from .governance.raw_protection import is_owner

    if principal is None or not is_owner(principal):
        return None
    if principal.verified_authorization_session is not None or principal.authorization_session_id is not None:
        return None
    binding = principal.client_binding
    origin = principal.origin_session
    return {
        "audience_id": principal.audience_id,
        "surface": principal.surface,
        "session_id": principal.session_id,
        "purpose": principal.purpose,
        "issuer_family": principal.issuer_family,
        "remote_owner": bool(principal.remote_owner),
        "local_owner": bool(principal.local_owner),
        "administrative_ingress": bool(principal.administrative_ingress),
        "client_binding": (
            {"issuer": binding.issuer, "client_id": binding.client_id} if binding is not None else None
        ),
        "origin_session": (
            {"session_id": origin.session_id, "generation": origin.generation, "audience": origin.audience}
            if origin is not None
            else None
        ),
    }


def _restore_principal(record: Mapping[str, Any]) -> Any:
    from .governance import principal as principal_module

    binding = record.get("client_binding")
    origin = record.get("origin_session")
    return principal_module.RequestPrincipal(
        audience_id=record["audience_id"],
        surface=record.get("surface") or "library",
        session_id=record.get("session_id"),
        purpose=record.get("purpose"),
        issuer_family=record.get("issuer_family"),
        remote_owner=bool(record.get("remote_owner")),
        local_owner=bool(record.get("local_owner")),
        administrative_ingress=bool(record.get("administrative_ingress")),
        client_binding=(
            principal_module.ClientBinding(issuer=binding["issuer"], client_id=binding["client_id"])
            if isinstance(binding, Mapping)
            else None
        ),
        origin_session=(
            principal_module.OriginSessionBinding(
                session_id=origin["session_id"], generation=origin["generation"], audience=origin["audience"]
            )
            if isinstance(origin, Mapping)
            else None
        ),
    )


def _surface_record() -> dict[str, Any] | None:
    from . import capabilities

    descriptor = capabilities.current_active_surface()
    if descriptor is None:
        return None
    return {
        "surface": descriptor.surface,
        "profile": descriptor.profile,
        "tier2_enabled": descriptor.tier2_enabled,
        "tool_shape": descriptor.tool_shape,
    }


def _restore_surface(record: Mapping[str, Any] | None, command_name: str) -> Any:
    from . import capabilities

    if record is None:
        return None
    return capabilities.ActiveSurfaceDescriptor(
        surface=record["surface"],
        profile=record["profile"],
        tier2_enabled=bool(record["tier2_enabled"]),
        product_commands=(command_name,),
        tool_shape=record.get("tool_shape") or "grouped",
    )


_REPLAYING: contextvars.ContextVar[bool] = contextvars.ContextVar("exomem_intake_replaying", default=False)
_WAIT_CAP: contextvars.ContextVar[float | None] = contextvars.ContextVar("exomem_intake_wait_cap", default=None)


def busy_wait_cap() -> float | None:
    """The deadline (monotonic) by which an eligible save stops waiting and is queued, if any."""
    return _WAIT_CAP.get()


# --------------------------------------------------------------------------- drainers


_DRAINERS: dict[Path, Drainer] = {}
_DRAINERS_LOCK = threading.Lock()


def drainer_for(vault_root: Any) -> Drainer | None:
    try:
        key = Path(vault_root).resolve()
    except (TypeError, OSError):
        return None
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.get(key)
    return drainer if drainer is not None and drainer.running else None


def start(vault_root: Path) -> Drainer:
    """Start this process's drainer for one vault. The serving HTTP runtime calls it once."""
    key = Path(vault_root).resolve()
    with _DRAINERS_LOCK:
        existing = _DRAINERS.get(key)
        if existing is not None and existing.running:
            return existing
        drainer = Drainer(key)
        _DRAINERS[key] = drainer
    drainer.start()
    return drainer


def stop(vault_root: Path, *, timeout: float = 10.0) -> None:
    key = Path(vault_root).resolve()
    with _DRAINERS_LOCK:
        drainer = _DRAINERS.pop(key, None)
    if drainer is not None:
        drainer.stop(timeout=timeout)


def _owner_identity() -> tuple[str | None, Path]:
    from . import writer_lease

    store = writer_lease.get_manager().idempotency
    return store.owner_id, store.state_dir


def _owner_alive(owner: str | None, mine: str | None, state_dir: Path) -> bool:
    from . import writer_lease

    if owner is None:
        return False
    if owner == mine:
        return True
    return writer_lease._probe_owner_liveness(state_dir, owner)  # noqa: SLF001


class Drainer:
    """The one thread that applies one vault's queued saves, oldest first."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._open = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        self._refresh_open()
        thread = threading.Thread(
            target=contextvars.Context().run, args=(self._loop,), name="exomem-write-intake", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float) -> None:
        self._stop.set()
        self.notify()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def notify(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def backlog(self) -> int:
        with self._condition:
            return self._open

    def wait_for_empty_queue(self, deadline: float) -> bool:
        """Wait until no save is open or `deadline` (monotonic) passes; return whether it emptied."""
        with self._condition:
            while self._open > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return False
                self._condition.wait(min(remaining, 1.0))
            return True

    def _refresh_open(self) -> None:
        try:
            with _store(self.vault_root, create=False) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM intake WHERE state IN (?, ?)", _OPEN_STATES
                ).fetchone()[0]
        except FileNotFoundError:
            count = 0
        with self._condition:
            self._open = int(count)
            self._condition.notify_all()

    def note_accepted(self) -> None:
        with self._condition:
            self._open += 1
            self._condition.notify_all()

    def _loop(self) -> None:
        backoff_failures = 0
        while not self._stop.is_set():
            try:
                due_in = self._drain_one()
                backoff_failures = 0
            except Exception:  # noqa: BLE001 - the drainer must outlive a store fault
                log.warning("write intake drain pass failed", exc_info=True)
                backoff_failures += 1
                due_in = min(MAX_BACKOFF_SECONDS, FIRST_BACKOFF_SECONDS * 2**backoff_failures)
            if due_in is None:
                continue
            with self._condition:
                if self._stop.is_set():
                    return
                self._condition.wait(due_in)

    def _drain_one(self) -> float | None:
        """Apply the oldest open save if it is due. Return seconds to wait, or None to go again at once."""
        claimed = _with_store_retry(lambda: _claim_head(self.vault_root))
        if claimed is None:
            self._refresh_open()
            return _IDLE_POLL_SECONDS
        if isinstance(claimed, float):
            return claimed
        _apply(self.vault_root, claimed)
        self._refresh_open()
        return None


@dataclass(frozen=True)
class _Claim:
    id: str
    command: str
    arguments: dict[str, Any]
    principal: dict[str, Any]
    surface: dict[str, Any] | None
    attempts: int


def _claim_head(vault_root: Path) -> _Claim | float | None:
    """Claim the oldest open save. A float is the wait until it is due; None means nothing is open.

    Order is strict: a save in back-off holds the saves behind it, so a later edit never
    overtakes the create it depends on. A save left applying by a live other process is
    that process's; this drainer waits for it.
    """
    mine, state_dir = _owner_identity()
    now = time.time()
    try:
        with _write(vault_root) as conn:
            row = conn.execute(
                "SELECT * FROM intake WHERE state IN (?, ?) ORDER BY seq LIMIT 1", _OPEN_STATES
            ).fetchone()
            if row is None:
                return None
            if row["state"] == APPLYING and row["owner"] != mine and _owner_alive(row["owner"], mine, state_dir):
                return _IDLE_POLL_SECONDS
            if row["state"] == QUEUED and row["next_attempt_at"] > now:
                return max(0.05, row["next_attempt_at"] - now)
            conn.execute(
                "UPDATE intake SET state = ?, owner = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (APPLYING, mine, now, row["id"]),
            )
            return _Claim(
                id=row["id"],
                command=row["command"],
                arguments=json.loads(row["arguments"]),
                principal=json.loads(row["principal"]),
                surface=json.loads(row["surface"]) if row["surface"] else None,
                attempts=int(row["attempts"]) + 1,
            )
    except FileNotFoundError:
        return None


def _command_named(name: str) -> Any:
    from .commands import PRODUCT_COMMANDS

    for command in PRODUCT_COMMANDS:
        if command.name == name:
            return command
    return None


def _apply(vault_root: Path, claim: _Claim) -> None:
    """Replay one claimed save in a fresh context and record its outcome."""
    contextvars.Context().run(_apply_in_context, vault_root, claim)


def _apply_in_context(vault_root: Path, claim: _Claim) -> None:
    from . import capabilities, cli_ops, writer_lease
    from .governance import principal as principal_module

    command = _command_named(claim.command)
    if command is None:
        _finish_failed(vault_root, claim.id, "INTAKE_COMMAND_UNKNOWN",
                       {"code": "INTAKE_COMMAND_UNKNOWN", "message": f"no command named {claim.command}"})
        return
    principal = _restore_principal(claim.principal)
    descriptor = _restore_surface(claim.surface, command.name)
    if command.needs_schema:
        from . import schema as schema_module

        injected: tuple[Any, ...] = (vault_root, schema_module.load_source_schema(vault_root))
    else:
        injected = (vault_root,)
    token = _REPLAYING.set(True)
    try:
        with (
            principal_module.request_scope(principal),
            capabilities.active_surface(descriptor) if descriptor is not None else _nothing(),
            principal_module.invocation_scope(),
            writer_lease.caller_holds_no_request_worker(),
        ):
            result = writer_lease.invoke_command(
                command,
                *injected,
                idempotency_key=f"intake:{claim.id}",
                idempotency_principal_scope=principal.audience_id,
                # Receipts require a UUID request id; one per attempt, derived from the save.
                mutation_request_id=str(uuid.uuid5(_REQUEST_NAMESPACE, f"{claim.id}:{claim.attempts}")),
                **claim.arguments,
            )
    except Exception as error:  # noqa: BLE001 - every replay ends in a recorded outcome
        refusal = cli_ops.public_refusal(error)
        code = str(refusal.get("code")) if refusal else "INTERNAL"
        if code in RETRY_REFUSALS:
            _with_store_retry(lambda: _requeue(vault_root, claim, code))
            return
        if refusal is None:
            log.warning("write intake replay of %s failed", claim.command, exc_info=True)
            refusal = {"code": "INTERNAL", "message": type(error).__name__}
        _with_store_retry(lambda: _finish_failed(vault_root, claim.id, code, refusal))
        return
    finally:
        _REPLAYING.reset(token)
    _with_store_retry(lambda: _finish_applied(vault_root, claim.id, result))


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def _requeue(vault_root: Path, claim: _Claim, code: str) -> None:
    delay = min(MAX_BACKOFF_SECONDS, FIRST_BACKOFF_SECONDS * 2 ** max(0, claim.attempts - 1))
    now = time.time()
    with _write(vault_root) as conn:
        conn.execute(
            "UPDATE intake SET state = ?, owner = NULL, updated_at = ?, next_attempt_at = ?, error_code = ? "
            "WHERE id = ? AND state = ?",
            (QUEUED, now, now + delay, code, claim.id, APPLYING),
        )


def _bounded_outcome(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"result": str(result)[:1024]}
    text = _canonical(dict(result))
    if len(text.encode("utf-8")) <= _OUTCOME_LIMIT_BYTES:
        return json.loads(text)
    # A large reply keeps its top-level scalars: the summary, state, title and ids.
    return {
        key: value
        for key, value in result.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }


def _finish_applied(vault_root: Path, intake_id: str, result: Any) -> None:
    now = time.time()
    with _write(vault_root) as conn:
        conn.execute(
            "UPDATE intake SET state = ?, owner = NULL, arguments = NULL, outcome = ?, error_code = NULL, "
            "updated_at = ?, finished_at = ? WHERE id = ?",
            (APPLIED, _canonical(_bounded_outcome(result)), now, now, intake_id),
        )
        conn.execute(
            "DELETE FROM intake WHERE state = ? AND finished_at < ?",
            (APPLIED, now - APPLIED_RETENTION_SECONDS),
        )
    log.info("write intake applied %s", intake_id)


def _finish_failed(vault_root: Path, intake_id: str, code: str, refusal: Mapping[str, Any]) -> None:
    now = time.time()
    with _write(vault_root) as conn:
        conn.execute(
            "UPDATE intake SET state = ?, owner = NULL, error_code = ?, error = ?, updated_at = ?, "
            "finished_at = ? WHERE id = ?",
            (FAILED, code, _canonical(dict(refusal)), now, now, intake_id),
        )
    log.warning("write intake save %s failed with %s", intake_id, code)


# --------------------------------------------------------------------------- acceptance


@dataclass
class Admission:
    """One eligible save on its way through the dispatcher."""

    drainer: Drainer
    command: Any
    arguments: dict[str, Any]
    control: dict[str, Any]
    principal: dict[str, Any]
    surface: dict[str, Any] | None
    origin: str | None

    def run(self, call: Callable[[], Any]) -> Any:
        started = time.monotonic()
        deadline = started + intake_wait_seconds()
        open_receipt = self._open_receipt()
        if open_receipt is not None:
            return open_receipt
        if self.drainer.backlog() > 0 and not self.drainer.wait_for_empty_queue(deadline):
            # Behind earlier queued saves: run after them, never before.
            return self._accept(reason="queue_not_empty", started=started)
        token = _WAIT_CAP.set(deadline)
        try:
            return call()
        except OpError as error:
            if error.code not in ACCEPTED_REFUSALS or error.details.get("committed") is not False:
                raise
            try:
                return self._accept(reason=error.code, started=started)
            except Exception:  # noqa: BLE001 - an intake that cannot write leaves today's refusal
                log.warning("write intake could not accept a %s save", self.command.name, exc_info=True)
                raise error from None
        finally:
            _WAIT_CAP.reset(token)

    def _open_receipt(self) -> dict[str, Any] | None:
        if self.origin is None:
            return None
        return _with_store_retry(self._open_receipt_once)

    def _open_receipt_once(self) -> dict[str, Any] | None:
        try:
            with _store(self.drainer.vault_root, create=False) as conn:
                row = conn.execute(
                    "SELECT * FROM intake WHERE origin = ? AND state IN (?, ?, ?) ORDER BY seq DESC LIMIT 1",
                    (self.origin, *_OPEN_STATES, APPLIED),
                ).fetchone()
                if row is None:
                    return None
                if row["state"] == APPLIED:
                    # The original identity never committed; the queued replay did. Answer
                    # the resend with that outcome instead of writing a second time.
                    outcome = json.loads(row["outcome"]) if row["outcome"] else {}
                    return {**outcome, "queued_write": {"ref": ref_for(row["id"]), "state": APPLIED}}
                return _receipt(conn, row)
        except FileNotFoundError:
            return None

    def _accept(self, *, reason: str, started: float) -> dict[str, Any]:
        return _with_store_retry(lambda: self._accept_once(reason=reason, started=started))

    def _accept_once(self, *, reason: str, started: float) -> dict[str, Any]:
        intake_id = uuid.uuid4().hex
        now = time.time()
        with _write(self.drainer.vault_root) as conn:
            if self.origin is not None:
                row = conn.execute(
                    "SELECT * FROM intake WHERE origin = ? AND state IN (?, ?)", (self.origin, *_OPEN_STATES)
                ).fetchone()
                if row is not None:
                    return _receipt(conn, row)
            conn.execute(
                "INSERT INTO intake (id, origin, command, arguments, principal, surface, state, "
                "accepted_at, updated_at, next_attempt_at, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intake_id, self.origin, self.command.name, _canonical(self.arguments),
                    _canonical(self.principal), _canonical(self.surface) if self.surface else None,
                    QUEUED, now, now, now, reason,
                ),
            )
            row = conn.execute("SELECT * FROM intake WHERE id = ?", (intake_id,)).fetchone()
            receipt = _receipt(conn, row)
        self.drainer.note_accepted()
        _journal_queued(self, intake_id, reason, started)
        return receipt


_STORE_RETRIES = 5


def _with_store_retry(operation: Callable[[], Any]) -> Any:
    """Run one intake-store operation, retrying a busy private-state identity domain briefly.

    Every open of the store takes its reserved-identity domain for milliseconds; a drainer
    pass and an acceptance can meet there. That contention must not refuse a save.
    """
    for attempt in range(_STORE_RETRIES):
        try:
            return operation()
        except OpError as error:
            if error.code != "MUTATION_BUSY" or attempt == _STORE_RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _journal_queued(admission: Admission, intake_id: str, reason: str, started: float) -> None:
    from . import writer_lease

    try:
        writer_lease.get_manager()._record_mutation_journal(  # noqa: SLF001
            request_id=str(admission.control.get("mutation_request_id") or intake_id),
            command=admission.command.name,
            receipt=intake_id[:16],
            outcome="queued",
            error_code=reason,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            scope=admission.control.get("implicit_idempotency_scope")
            or admission.control.get("idempotency_principal_scope"),
            targets=[str(admission.drainer.vault_root)],
            boundary_timing=(None, None),
        )
    except Exception:  # noqa: BLE001 - the journal is observability; the save is already held
        log.debug("write intake journal row failed", exc_info=True)


def _position(conn: sqlite3.Connection, seq: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM intake WHERE state IN (?, ?) AND seq <= ?", (*_OPEN_STATES, seq)
        ).fetchone()[0]
    )


def _receipt(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    ref = ref_for(row["id"])
    position = _position(conn, row["seq"])
    return {
        "success": True,
        "state": QUEUED,
        "committed": False,
        "user_summary": (
            "Queued, not saved yet: Exomem was busy, so it is holding this save durably and will "
            f"apply it in arrival order (position {position}). Do not resend it; check its outcome "
            "with the route below."
        ),
        "queued_write": {"ref": ref, "position": position, "accepted_at": _iso(row["accepted_at"])},
        "route": {"tool": "review_memory", "args": {"mode": MODE, "ref": ref}},
    }


def admission(command: Any, injected: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Admission | None:
    """The intake admission for one dispatched call, or ``None`` when the intake does not apply."""
    if _REPLAYING.get() or not getattr(command, "busy_intake", False) or not injected or not enabled():
        return None
    drainer = drainer_for(injected[0])
    if drainer is None:
        return None
    arguments = {key: value for key, value in kwargs.items() if key not in _CONTROL_ARGUMENTS}
    from .commands import invocation_is_read_only
    from .governance import principal as principal_module
    from .governance.egress import SelectorCoverageError

    try:
        if invocation_is_read_only(command, arguments):
            return None
    except SelectorCoverageError:
        return None
    principal = principal_record(principal_module.current_principal())
    if principal is None:
        return None
    try:
        encoded = _canonical(arguments)
        json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        return None
    control = {key: kwargs[key] for key in _CONTROL_ARGUMENTS if key in kwargs}
    return Admission(
        drainer=drainer,
        command=command,
        arguments=arguments,
        control=control,
        principal=principal,
        surface=_surface_record(),
        origin=origin_identity(command.name, arguments, control),
    )


# --------------------------------------------------------------------------- reads and owner operations


def _view(row: sqlite3.Row, conn: sqlite3.Connection, *, full: bool) -> dict[str, Any]:
    view: dict[str, Any] = {
        "ref": ref_for(row["id"]),
        "command": row["command"],
        "state": row["state"],
        "attempts": row["attempts"],
        "accepted_at": _iso(row["accepted_at"]),
        "age_seconds": round(time.time() - row["accepted_at"], 1),
    }
    if row["state"] in _OPEN_STATES:
        view["position"] = _position(conn, row["seq"])
        if row["error_code"]:
            view["last_wait_reason"] = row["error_code"]
    if row["state"] == FAILED:
        view["error"] = json.loads(row["error"]) if row["error"] else {"code": row["error_code"]}
    if row["state"] == APPLIED:
        view["finished_at"] = _iso(row["finished_at"])
        if full:
            view["outcome"] = json.loads(row["outcome"]) if row["outcome"] else None
    if full and row["arguments"] is not None:
        view["arguments"] = json.loads(row["arguments"])
    return view


def _summary_line(state_counts: Mapping[str, int]) -> str:
    queued = state_counts.get(QUEUED, 0) + state_counts.get(APPLYING, 0)
    failed = state_counts.get(FAILED, 0)
    return f"{queued} queued save(s) waiting, {failed} failed save(s) to review."


def read(vault_root: Path, ref: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """`review_memory(mode="queued-write")`: one save by `ref`, or the open and failed saves."""
    if ref is not None:
        if not ref.startswith(REF_PREFIX) or not ref[len(REF_PREFIX):]:
            raise OpError(
                "INVALID_REVIEW",
                f"queued-write mode takes a ref of the form {REF_PREFIX}<id>",
                "Pass the queued_write.ref from the receipt, or omit ref to list saves.",
            )
        try:
            with _store(vault_root, create=False) as conn:
                row = conn.execute("SELECT * FROM intake WHERE id = ?", (ref[len(REF_PREFIX):],)).fetchone()
                if row is None:
                    raise OpError(
                        "QUEUED_WRITE_NOT_FOUND",
                        "no queued save has this ref; an applied save is kept for 7 days",
                        "Check the page itself, or list the open saves without ref.",
                    )
                view = _view(row, conn, full=True)
        except FileNotFoundError:
            raise OpError(
                "QUEUED_WRITE_NOT_FOUND",
                "no save has been queued on this vault",
                "Check the page itself.",
            ) from None
        summaries = {  # nosemgrep: ep-word-dict -- keyed by this store's own closed row states.
            QUEUED: "Queued, not saved yet.",
            APPLYING: "Being applied now, not confirmed yet.",
            APPLIED: "Saved: the queued save was applied.",
            FAILED: "Not saved: the queued save failed. Its arguments are below; correct and send it again.",
        }
        return {"user_summary": summaries[view["state"]], "queued_write": view}
    cap = max(1, min(int(limit or _LIST_LIMIT), _LIST_LIMIT))
    try:
        with _store(vault_root, create=False) as conn:
            counts = {
                row["state"]: row["n"]
                for row in conn.execute("SELECT state, COUNT(*) AS n FROM intake GROUP BY state")
            }
            rows = conn.execute(
                "SELECT * FROM intake WHERE state IN (?, ?, ?) ORDER BY seq LIMIT ?",
                (*_OPEN_STATES, FAILED, cap),
            ).fetchall()
            items = [_view(row, conn, full=False) for row in rows]
    except FileNotFoundError:
        counts, items = {}, []
    listed = sum(counts.get(state, 0) for state in (*_OPEN_STATES, FAILED))
    return {
        "user_summary": _summary_line(counts),
        "items": items,
        "counts": {state: counts.get(state, 0) for state in (QUEUED, APPLYING, FAILED, APPLIED)},
        "truncated": listed > len(items),
    }


def status(vault_root: Path) -> dict[str, Any]:
    """Counts for `coordination_status`; ``unknown`` when the store cannot be read."""
    try:
        with _store(vault_root, create=False) as conn:
            counts = {
                row["state"]: row["n"]
                for row in conn.execute("SELECT state, COUNT(*) AS n FROM intake GROUP BY state")
            }
            oldest = conn.execute(
                "SELECT MIN(accepted_at) FROM intake WHERE state IN (?, ?)", _OPEN_STATES
            ).fetchone()[0]
    except FileNotFoundError:
        counts, oldest = {}, None
    except Exception:  # noqa: BLE001 - an unreadable store is a visible unknown, never zero
        log.warning("write intake status read failed", exc_info=True)
        return {"state": "unknown"}
    return {
        "state": "ready",
        "queued": counts.get(QUEUED, 0),
        "applying": counts.get(APPLYING, 0),
        "failed": counts.get(FAILED, 0),
        "oldest_open_age_seconds": None if oldest is None else round(time.time() - oldest, 1),
    }


def dismiss(vault_root: Path, intake_id: str) -> dict[str, Any]:
    """Remove one failed save. Open or applied saves are not dismissed."""
    if intake_id.startswith(REF_PREFIX):
        intake_id = intake_id[len(REF_PREFIX):]
    with _write(vault_root) as conn:
        row = conn.execute("SELECT state FROM intake WHERE id = ?", (intake_id,)).fetchone()
        if row is None:
            raise OpError("QUEUED_WRITE_NOT_FOUND", f"no queued save {intake_id}", "List saves with `exomem intake list`.")
        if row["state"] != FAILED:
            raise OpError(
                "QUEUED_WRITE_NOT_FAILED",
                f"save {intake_id} is {row['state']}; only a failed save can be dismissed",
                "Wait for it to be applied, or list saves with `exomem intake list`.",
            )
        conn.execute("DELETE FROM intake WHERE id = ?", (intake_id,))
    return {"dismissed": ref_for(intake_id)}
