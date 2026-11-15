"""Lookup-only capture admission and closure under the canonical writer boundary."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError

from . import source_capture
from .cli_ops import OpError
from .governance.principal import OWNER_AUDIENCE, effective_principal, owner_principal

# A fixed per-operation work bound keeps collection independent of ledger size.
GC_BATCH_SIZE = 128
MAX_ATTEMPT_RESIDUE = 32  # Fixed bounded work for one text-capture attempt's owner locators.


class CaptureAttemptConfig(BaseModel):
    """Operator policy for new admission and retention deadlines; issued values stay fixed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    admission_seconds: Annotated[FiniteFloat, Field(gt=0)] = 600.0
    retention_seconds: Annotated[FiniteFloat, Field(gt=0)] = 7 * 24 * 60 * 60.0

    @classmethod
    def from_env(cls) -> CaptureAttemptConfig:
        values = {}
        # Environment names follow this configuration schema's fixed field names.
        for field in cls.model_fields:
            value = os.environ.get(f"EXOMEM_CAPTURE_ATTEMPT_{field.upper()}")
            if value is not None:
                values[field] = value
        try:
            return cls.model_validate(values)
        except ValidationError:
            raise OpError(
                "CAPTURE_ATTEMPT_CONFIG_UNAVAILABLE",
                "capture attempt policy requires finite positive seconds",
                "Set EXOMEM_CAPTURE_ATTEMPT_ADMISSION_SECONDS and EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS to finite positive seconds.",
            ) from None


class _WirePassage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str
    text: str


class CaptureWireV1(BaseModel):
    """Exact authored fields, before passage joining or taxonomy resolution."""

    model_config = ConfigDict(extra="forbid", strict=True)
    source_identity: source_capture.SourceIdentity
    title: str
    content: str | None = None
    passages: list[_WirePassage] | None = None
    source_type: str | None = None
    source_kind: str | None = None
    slug: str | None = None
    url: str | None = None
    tags: list[str] | None = None
    why_captured: str | None = None
    domain: str | None = None
    projects: list[str] | None = None
    raw_protection: bool = False

    def sha256(self) -> str:
        return hashlib.sha256(_json(self.model_dump(mode="json")).encode("utf-8")).hexdigest()


class AttemptScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cell_id: str | None
    replica_id: str
    ledger_id: str
    boundary_id: str


class CaptureReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol_version: Literal[1]
    ticket: Annotated[str, Field(min_length=32, max_length=128)]
    source_identity: source_capture.SourceIdentity
    request_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    admission_expires_at: FiniteFloat
    scope: AttemptScope


# Immutable ancestors remain visible through nested captures and terminal persistence.
_ACTIVE: ContextVar[tuple[tuple[str, str, str], ...]] = ContextVar("capture_attempt_ancestors", default=())
_DISPATCH: ContextVar[tuple[str, str, str, str | None] | None] = ContextVar("capture_attempt_dispatch", default=None)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _boundary_id(boundary) -> str:
    # These resolved coordinator properties select the actual OS lock, not a display cell name.
    return hashlib.sha256(_json((str(boundary.state_root), boundary.identity)).encode("utf-8")).hexdigest()


def _principal_binding() -> str:
    from . import cloud_cell

    who = effective_principal()
    if who.resolved and who.client_binding is not None:
        binding = (who.audience_id, who.client_binding.issuer, who.client_binding.client_id)
    elif (who.resolved and who.audience_id == OWNER_AUDIENCE and who.issuer_family
          and (who.issuer_family == owner_principal(surface=who.surface).issuer_family
               or cloud_cell.is_cloud_issuer_family(who.issuer_family))):
        binding = (who.audience_id, who.issuer_family)
    else:
        raise ValueError("CAPTURE_ATTEMPT_FORBIDDEN: authenticated stable principal binding is required")
    return hashlib.sha256(_json(binding).encode("utf-8")).hexdigest()


def _reservation(value: object) -> CaptureReservation:
    try:
        return CaptureReservation.model_validate(value)
    except ValidationError:
        raise ValueError("CAPTURE_ATTEMPT_INVALID: supply the unchanged server reservation") from None


def _actual_boundary(manager, root):
    boundary = manager._mutation_coordinator_for(root)
    if not boundary.current_thread_holds_boundary() or boundary.current_thread_borrows_boundary():
        raise ValueError("CAPTURE_ATTEMPT_LIFECYCLE_REQUIRED: use the command dispatcher")
    return boundary


def _consume_dispatch(manager, root, command: str, ticket: str | None = None):
    boundary = _actual_boundary(manager, root)
    expected = (manager.idempotency.capture_attempts.ledger_id, _boundary_id(boundary), command, ticket)
    if _DISPATCH.get() != expected:
        raise ValueError("CAPTURE_ATTEMPT_LIFECYCLE_REQUIRED: use the command dispatcher")
    _DISPATCH.set(None)
    return boundary


def wire_from_arguments(arguments) -> CaptureWireV1:
    if (arguments.get("source_identity") is None or arguments.get("files")
            or arguments.get("adoption") is not None or arguments.get("correction") is not None):
        raise ValueError("CAPTURE_ATTEMPT_INVALID: reservation requires identity-bearing text or passages without files, adoption or correction")
    return CaptureWireV1.model_validate({field: arguments[field] for field in CaptureWireV1.model_fields if field in arguments})


def _capture_receipt(terminal: dict) -> dict:
    return terminal.get("leaf_result", terminal)


class CaptureAttemptLedger:
    """Private attempt rows share runtime custody, never generic claim recovery."""

    def __init__(self, store):
        self.store = store
        with store._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS capture_attempt_ledger (singleton INTEGER PRIMARY KEY CHECK(singleton=1), ledger_id TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO capture_attempt_ledger VALUES (1, ?)", (secrets.token_hex(32),))
            conn.execute("CREATE TABLE IF NOT EXISTS capture_attempts (ticket TEXT PRIMARY KEY, boundary_id TEXT NOT NULL, reservation TEXT NOT NULL, principal TEXT NOT NULL, state TEXT NOT NULL, expires_at REAL NOT NULL, retention_seconds REAL NOT NULL, gc_after REAL, past_outcome TEXT, receipt TEXT, custody_pending INTEGER NOT NULL DEFAULT 0)")
            # Earlier unreleased candidates never recorded retention; inventing it would change issued policy.
            if "retention_seconds" not in {row[1] for row in conn.execute("PRAGMA table_info(capture_attempts)")}:
                raise OpError(
                    "CAPTURE_ATTEMPT_LEDGER_UNAVAILABLE",
                    "capture attempt ledger lacks its issued retention policy",
                    "Use fresh isolated candidate state; earlier candidate retention cannot be inferred.",
                )
            if "custody_pending" not in {row[1] for row in conn.execute("PRAGMA table_info(capture_attempts)")}:
                raise OpError(
                    "CAPTURE_ATTEMPT_LEDGER_UNAVAILABLE",
                    "capture attempt ledger lacks custody eligibility",
                    "Use fresh isolated candidate state; earlier candidate custody cannot be inferred.",
                )
            conn.execute("CREATE INDEX IF NOT EXISTS capture_attempts_expiry ON capture_attempts (boundary_id, state, expires_at, ticket) WHERE custody_pending=0")
            conn.execute("CREATE INDEX IF NOT EXISTS capture_attempts_gc ON capture_attempts (boundary_id, gc_after, ticket) WHERE gc_after IS NOT NULL AND custody_pending=0")
            conn.execute("CREATE TABLE IF NOT EXISTS capture_attempt_custody (ticket TEXT PRIMARY KEY, allocation TEXT, observed_ref TEXT, publication_state TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS capture_attempt_residue (ticket TEXT NOT NULL, owner TEXT NOT NULL, locator TEXT NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(ticket,owner,locator,version))")
            self.ledger_id = conn.execute("SELECT ledger_id FROM capture_attempt_ledger WHERE singleton=1").fetchone()[0]

    def scope(self, manager, boundary) -> dict:
        from .cloud_cell import CloudCellCredentials, cloud_mode_enabled

        cell_id = CloudCellCredentials.from_env().cell_id if cloud_mode_enabled() else None
        return {"cell_id": cell_id, "replica_id": manager.config.replica_id or "standalone",
                "ledger_id": self.ledger_id, "boundary_id": _boundary_id(boundary)}

    def _active_handles(self, boundary) -> frozenset[str]:
        if not boundary.current_thread_holds_boundary() or boundary.current_thread_borrows_boundary():
            return frozenset()
        identity = _boundary_id(boundary)
        return frozenset(ticket for ledger, bound, ticket in _ACTIVE.get() if ledger == self.ledger_id and bound == identity)

    def collect(self, boundary) -> int:
        """Bound indexed collection; admission expiry never collects accepted execution."""
        if not boundary.current_thread_holds_boundary() or boundary.current_thread_borrows_boundary():
            raise RuntimeError("capture attempt collection requires the actual canonical boundary")
        now = self.store.clock()
        active = self._active_handles(boundary)
        identity = _boundary_id(boundary)
        with self.store._connect() as conn:
            expired = conn.execute("SELECT ticket FROM capture_attempts WHERE boundary_id=? AND state='reserved' AND custody_pending=0 AND expires_at<=? ORDER BY expires_at,ticket LIMIT ?", (identity, now, GC_BATCH_SIZE)).fetchall()
            terminal = conn.execute("SELECT ticket FROM capture_attempts WHERE boundary_id=? AND custody_pending=0 AND gc_after<=? ORDER BY gc_after,ticket LIMIT ?", (identity, now, GC_BATCH_SIZE - len(expired))).fetchall()
            tickets = [row for row in (*expired, *terminal) if row[0] not in active]
            from .source_result_custody import close_rows

            close_rows(conn, kind="capture", keys=tuple(row[0] for row in tickets))
            conn.executemany("DELETE FROM capture_attempts WHERE ticket=?", tickets)
            conn.executemany("DELETE FROM capture_attempt_custody WHERE ticket=?", tickets)
            conn.executemany("DELETE FROM capture_attempt_residue WHERE ticket=?", tickets)
        return len(tickets)

    def record_allocation(self, ticket: str, allocation: dict) -> None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT allocation FROM capture_attempt_custody WHERE ticket=?", (ticket,)).fetchone()
            if row is not None and row[0] is not None and row[0] != _json(allocation):
                raise ValueError("CAPTURE_ATTEMPT_CUSTODY_CONFLICT: this wire attempt owns one Source allocation")
            conn.execute("INSERT INTO capture_attempt_custody(ticket,allocation,publication_state) VALUES (?,?,'allocated') ON CONFLICT(ticket) DO UPDATE SET allocation=excluded.allocation",
                         (ticket, _json(allocation)))
            conn.execute("UPDATE capture_attempts SET custody_pending=1 WHERE ticket=?", (ticket,))

    def record_observed(self, ticket: str, reference: str) -> None:
        with self.store._connect() as conn:
            conn.execute("INSERT INTO capture_attempt_custody(ticket,observed_ref,publication_state) VALUES (?,?,'observed') ON CONFLICT(ticket) DO UPDATE SET observed_ref=excluded.observed_ref",
                         (ticket, reference))

    def record_published(self, ticket: str, reference: str) -> None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT allocation FROM capture_attempt_custody WHERE ticket=?", (ticket,)).fetchone()
            if row is not None and row[0] is not None and json.loads(row[0])["source_ref"] == reference:
                conn.execute("UPDATE capture_attempt_custody SET publication_state='published' WHERE ticket=?", (ticket,))
                self._refresh_custody_pending(conn, ticket)

    def reserve_residue(self, ticket: str, owner) -> None:
        with self.store._connect() as conn:
            if conn.execute("SELECT 1 FROM capture_attempt_residue WHERE ticket=? AND owner=? AND locator=? AND version=?",
                            (ticket, owner.owner, owner.locator, owner.version)).fetchone() is not None:
                return
            count = conn.execute("SELECT count(*) FROM capture_attempt_residue WHERE ticket=?", (ticket,)).fetchone()[0]
            if count >= MAX_ATTEMPT_RESIDUE:
                raise ValueError("CAPTURE_ATTEMPT_CUSTODY_UNAVAILABLE: attempt residue exceeds its bounded owner set")
            conn.execute("INSERT OR IGNORE INTO capture_attempt_custody(ticket,publication_state) VALUES (?,'unallocated')", (ticket,))
            conn.execute("INSERT OR IGNORE INTO capture_attempt_residue(ticket,owner,locator,version,state) VALUES (?,?,?,?,'pending')",
                         (ticket, owner.owner, owner.locator, owner.version))
            conn.execute("UPDATE capture_attempts SET custody_pending=1 WHERE ticket=?", (ticket,))

    @staticmethod
    def _refresh_custody_pending(conn, ticket: str) -> None:
        # Eligibility changes with its owning rows, so collection never scans unresolved predecessors.
        conn.execute(
            "UPDATE capture_attempts SET custody_pending="
            "EXISTS(SELECT 1 FROM capture_attempt_residue WHERE ticket=? AND state!='retired') OR "
            "EXISTS(SELECT 1 FROM capture_attempt_custody WHERE ticket=? AND allocation IS NOT NULL AND publication_state!='published') "
            "WHERE ticket=?", (ticket, ticket, ticket),
        )

    def custody(self, ticket: str) -> dict:
        """Report attempt ownership, separately from canonical dependency and erasure coverage."""
        with self.store._connect() as conn:
            row = conn.execute("SELECT allocation,observed_ref,publication_state FROM capture_attempt_custody WHERE ticket=?", (ticket,)).fetchone()
            residues = conn.execute("SELECT owner,locator,version,state FROM capture_attempt_residue WHERE ticket=? ORDER BY owner,locator,version LIMIT ?", (ticket, MAX_ATTEMPT_RESIDUE + 1)).fetchall()
        if row is None or len(residues) > MAX_ATTEMPT_RESIDUE:
            return _unknown_custody()
        targets = []
        state = "complete"
        if row[0] is not None:
            allocation = json.loads(row[0])
            targets.append({"source_ref": allocation["source_ref"], "source_hash": allocation["source_hash"]})
            if row[2] != "published":
                state = "unknown"
        unresolved = [{"owner": item[0], "locator": item[1], "version": item[2]} for item in residues if item[3] != "retired"]
        return {"custody_state": "pending" if unresolved else state, "owned_targets": targets,
                "attempt_residue": {"state": "pending" if unresolved else "clear", "owners": unresolved,
                                    "retirement_requested": any(item[3] == "retire_requested" for item in residues)}}

    def acknowledge_residue(self, reservation: CaptureReservation, owner) -> None:
        row = self._lookup(reservation)
        if row is None or row[2] not in ("closed", "completed"):
            raise ValueError("CAPTURE_ATTEMPT_ADMISSION_OPEN: close admission before residue retirement")
        with self.store._connect() as conn:
            conn.execute("UPDATE capture_attempt_residue SET state='retired' WHERE ticket=? AND owner=? AND locator=? AND version=?",
                         (reservation.ticket, owner.owner, owner.locator, owner.version))
            self._refresh_custody_pending(conn, reservation.ticket)

    def reserve(self, manager, boundary, source_identity, request_sha256) -> dict:
        config = CaptureAttemptConfig.from_env()
        self.collect(boundary)
        reservation = CaptureReservation(protocol_version=1, ticket=secrets.token_hex(32),
                                         source_identity=source_identity, request_sha256=request_sha256,
                                         admission_expires_at=self.store.clock() + config.admission_seconds,
                                         scope=AttemptScope(**self.scope(manager, boundary))).model_dump(mode="json")
        with self.store._connect() as conn:
            conn.execute("INSERT INTO capture_attempts (ticket,boundary_id,reservation,principal,state,expires_at,retention_seconds) VALUES (?,?,?,?,'reserved',?,?)",
                         (reservation["ticket"], reservation["scope"]["boundary_id"], _json(reservation), _principal_binding(), reservation["admission_expires_at"], config.retention_seconds))
        return {"reservation": reservation}

    def _lookup(self, reservation: CaptureReservation):
        with self.store._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT reservation, principal, state, past_outcome, receipt, retention_seconds FROM capture_attempts WHERE ticket=?", (reservation.ticket,)).fetchone()
            if row is not None and (row[0] != _json(reservation.model_dump(mode="json")) or row[1] != _principal_binding()):
                raise ValueError("CAPTURE_ATTEMPT_INVALID: reservation or principal binding differs")
            from .source_result_custody import current

            invocation = current(self.store)
            if row is not None and invocation is not None:
                from .source_result_custody import capture_pointer

                invocation.pin(conn, capture_pointer(reservation), row[2])
        return row

    def complete(self, ticket: str, terminal: dict, *, retention_seconds: float) -> None:
        receipt = _capture_receipt(terminal)
        capture_result = receipt.get("capture_outcome")
        # These outcomes are the canonical Source capture protocol's closed enum.
        if capture_result not in ("created", "reused"):
            raise RuntimeError("capture attempt lacks a canonical capture outcome")
        from .source_result_custody import current

        invocation = current(self.store)
        if invocation is not None:
            with self.store._connect() as conn:
                stored = conn.execute("SELECT reservation FROM capture_attempts WHERE ticket=?", (ticket,)).fetchone()
            if stored is not None and invocation.persist_capture(terminal,
                    reservation=CaptureReservation.model_validate_json(stored[0]), retention_seconds=retention_seconds) is not None:
                return
        with self.store._connect() as conn:
            conn.execute("UPDATE capture_attempts SET state='completed',past_outcome=?,receipt=?,gc_after=? WHERE ticket=? AND state='accepted'",
                         (
                             capture_result,
                             _json(terminal),
                             self.store.clock() + retention_seconds,
                             ticket,
                         ))

    def commit(self, manager, root, reservation: CaptureReservation, wire: CaptureWireV1, operation: Callable[[], dict], replay: Callable[[dict], dict]) -> dict:
        boundary = _actual_boundary(manager, root)
        source_capture.require_authority(root)
        if reservation.scope.model_dump() != self.scope(manager, boundary):
            raise ValueError("CAPTURE_ATTEMPT_SCOPE_CHANGED: reservation custody is unavailable")
        if reservation.source_identity != wire.source_identity or reservation.request_sha256 != wire.sha256():
            raise ValueError("CAPTURE_ATTEMPT_INPUT_MISMATCH: exact authored input differs")
        if reservation.ticket in self._active_handles(boundary):
            raise ValueError("CAPTURE_ATTEMPT_CLOSED: execution is already active")
        row = self._lookup(reservation)
        if row is not None and row[2] == "completed":
            source_capture.require_authority(root)
            from .source_result_custody import current

            terminal = json.loads(row[4])
            invocation = current(self.store)
            if invocation is not None:
                terminal = invocation.decode(terminal)
            return replay(_capture_receipt(terminal))
        if row is None or row[2] != "reserved" or reservation.admission_expires_at <= self.store.clock():
            raise ValueError("CAPTURE_ATTEMPT_CLOSED: admission is closed; use a fresh reservation")
        with self.store._connect() as conn:
            conn.execute("UPDATE capture_attempts SET state='accepted' WHERE ticket=? AND state='reserved'", (reservation.ticket,))
        token = _ACTIVE.set((*_ACTIVE.get(), (self.ledger_id, reservation.scope.boundary_id, reservation.ticket)))
        try:
            terminal = operation()
            self.complete(reservation.ticket, terminal, retention_seconds=row[5])
            source_capture.require_authority(root)
            return terminal
        finally:
            # An interrupted accepted row remains lookup-only. Settlement closes it with unknown past outcome.
            _ACTIVE.reset(token)

    def settle(self, manager, root, reservation: CaptureReservation, *, retire_owned: bool = False) -> dict:
        boundary = _actual_boundary(manager, root)
        observed = self.scope(manager, boundary)
        result = {"state": "unknown", "admission_closed_here": None, "past_outcome": "unknown",
                  "observed_scope": observed, "capture_receipt": None, **_unknown_custody()}
        if reservation.scope.model_dump() != observed:
            return result
        if reservation.ticket in self._active_handles(boundary):
            return {**result, "state": "pending"}
        source_capture.require_authority(root)
        row = self._lookup(reservation)
        result.update(state="closed", admission_closed_here=True)
        if row is not None:
            if row[2] == "completed":
                from .source_result_custody import current

                terminal = json.loads(row[4])
                invocation = current(self.store)
                if invocation is not None:
                    terminal = invocation.decode(terminal)
                result.update(past_outcome=row[3], capture_receipt=_capture_receipt(terminal))
            else:
                past = "not_started" if row[2] == "reserved" else row[3] or "unknown"
                with self.store._connect() as conn:
                    conn.execute("UPDATE capture_attempts SET state='closed',past_outcome=?,gc_after=COALESCE(gc_after,?) WHERE ticket=?", (past, self.store.clock() + row[5], reservation.ticket))
                result["past_outcome"] = past
        if retire_owned and row is not None:
            # This records withdrawal of pre-reference residue; only its producer can acknowledge removal.
            with self.store._connect() as conn:
                conn.execute("UPDATE capture_attempt_residue SET state='retire_requested' WHERE ticket=? AND state='pending'", (reservation.ticket,))
        result.update(self.custody(reservation.ticket))
        if result["past_outcome"] == "not_started":
            # Only accepted admissions can enter producers; a closed unused ticket never owned bytes.
            result.update(custody_state="complete", owned_targets=[],
                          attempt_residue={"state": "clear", "owners": [], "retirement_requested": False})
        self.collect(boundary)
        source_capture.require_authority(root)
        return result


def reserve(root, source_identity, request_sha256, capture_wire_version=1) -> dict:
    from .writer_lease import active_manager

    source_capture.require_authority(root)
    _principal_binding()
    source_identity = source_capture.validate_identity(source_identity)
    source_capture.validate_sha256(request_sha256)
    if type(capture_wire_version) is not int or capture_wire_version != 1:
        raise ValueError("CAPTURE_ATTEMPT_INVALID: capture_wire_version must be 1")
    manager = active_manager()
    boundary = _consume_dispatch(manager, root, "reserve_source_capture")
    result = manager.idempotency.capture_attempts.reserve(manager, boundary, source_identity, request_sha256)
    source_capture.require_authority(root)
    return result


def settle(root, reservation, *, retire_owned: bool = False) -> dict:
    from .writer_lease import active_manager

    source_capture.require_authority(root)
    _principal_binding()
    manager = active_manager()
    _consume_dispatch(manager, root, "settle_source_capture")
    result = manager.idempotency.capture_attempts.settle(manager, root, _reservation(reservation), retire_owned=retire_owned)
    source_capture.require_authority(root)
    return result


def require_active_capture(root, reservation) -> None:
    from .writer_lease import active_manager

    manager = active_manager()
    _consume_dispatch(manager, root, "capture_source", _reservation(reservation).ticket)


def invoke(manager, root, command: str, arguments, operation: Callable[[], dict], *, wire: CaptureWireV1 | None, replay: Callable[[dict], dict]) -> dict:
    """Run only inside the dispatcher's actual guard; hooks never acquire authority or hop threads."""
    boundary = _actual_boundary(manager, root)
    source_capture.require_authority(root)
    _principal_binding()
    ledger = manager.idempotency.capture_attempts
    reservation = _reservation(arguments["capture_reservation"]) if wire is not None else None
    token = _DISPATCH.set((ledger.ledger_id, _boundary_id(boundary), command, reservation.ticket if reservation is not None else None))
    try:
        if reservation is not None:
            return ledger.commit(manager, root, reservation, wire, operation, replay)
        return operation()
    finally:
        _DISPATCH.reset(token)


def pending(manager, root, reservation) -> dict:
    """An unsuccessful boundary acquisition observes custody without looking up an attempt."""
    source_capture.require_authority(root)
    _principal_binding()
    reserved = _reservation(reservation)
    observed = manager.idempotency.capture_attempts.scope(manager, manager._mutation_coordinator_for(root))
    source_capture.require_authority(root)
    return {"state": "pending" if reserved.scope.model_dump() == observed else "unknown",
            "admission_closed_here": None, "past_outcome": "unknown", "observed_scope": observed,
            "capture_receipt": None, **_unknown_custody()}


def _unknown_custody() -> dict:
    return {"custody_state": "unknown", "owned_targets": None,
            "attempt_residue": {"state": "unknown", "owners": None}}


def _custody_attempt(root):
    from .writer_lease import active_manager

    if not _ACTIVE.get():
        return None
    manager = active_manager()
    boundary = _actual_boundary(manager, root)
    ledger = manager.idempotency.capture_attempts
    for ledger_id, boundary_id, ticket in reversed(_ACTIVE.get()):
        if ledger_id == ledger.ledger_id and boundary_id == _boundary_id(boundary):
            return ledger, ticket
    return None


def record_allocation(root, *, reference: str, source_hash: str, path: str) -> bool:
    active = _custody_attempt(root)
    if active is not None:
        active[0].record_allocation(active[1], {"source_ref": reference, "source_hash": source_hash, "canonical_path": path})
        return True
    return False


def record_observed(root, reference: str) -> None:
    active = _custody_attempt(root)
    if active is not None:
        active[0].record_observed(active[1], reference)


def record_published(root, reference: str) -> None:
    active = _custody_attempt(root)
    if active is not None:
        active[0].record_published(active[1], reference)


def reserve_residue(root, owner) -> None:
    active = _custody_attempt(root)
    if active is None:
        raise ValueError("CAPTURE_ATTEMPT_LIFECYCLE_REQUIRED: residue requires an active capture attempt")
    active[0].reserve_residue(active[1], owner)


def acknowledge_residue(root, reservation, owner) -> None:
    from .writer_lease import active_manager

    source_capture.require_authority(root)
    manager = active_manager()
    boundary = _actual_boundary(manager, root)
    reserved = _reservation(reservation)
    ledger = manager.idempotency.capture_attempts
    if reserved.scope.model_dump() != ledger.scope(manager, boundary):
        raise ValueError("CAPTURE_ATTEMPT_SCOPE_CHANGED: residue custody is unavailable")
    ledger.acknowledge_residue(reserved, owner)
