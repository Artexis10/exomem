"""Crash-recoverable receipt-first execution for consolidation effects.

The receipt chain, effect journal, and mutated state are separate durable stores.
This module therefore never infers one store from another: it binds the exact
receipt records into an owner-only prepared journal, classifies the effect, and
only then appends a terminal and finalizes the journal.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, NoReturn

from .. import mutation_lock, reserved_paths, writer_lease
from ..cli_ops import OpError
from ..kbdir import kb_dirname
from . import consolidation_plan, consolidation_receipts, receipts

JOURNAL_SCHEMA = "exomem.consolidation-effect-journal/v1"

_OWNER = "consolidation.run"
_DESCRIPTOR_ID = "consolidation-tree"
_BINDING_SCHEMA = "exomem.consolidation-effect-journal-binding/v1"
_BINDING_DOMAIN = _BINDING_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}(?::(?:committed|aborted))?\Z")
_INSTANCE_ID = re.compile(r"[0-9a-f]{32}\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_JOURNAL_BYTES = 512 * 1024
_STATES = frozenset({"prior", "prepared", "target", "mixed"})
_TERMINAL_ROLES = frozenset({"committed", "aborted"})
_REFERENCE_FIELDS = frozenset(
    {
        "event_id",
        "instance_id",
        "payload_digest",
        "receipt_head_digest",
        "record_hash",
        "sequence",
    }
)
_PREPARED_FIELDS = frozenset(
    {
        "binding_digest",
        "effect_ordinal",
        "intent",
        "intent_payload",
        "kind",
        "operation_id",
        "revision",
        "run_id",
        "schema",
        "status",
    }
)
_FINAL_FIELDS = _PREPARED_FIELDS | frozenset(
    {"observed_digest", "observed_state", "terminal"}
)

__all__ = [
    "JOURNAL_SCHEMA",
    "ConsolidationEffectJournalState",
    "ConsolidationEffectJournalStore",
    "ConsolidationEffectUnavailable",
    "EffectExecutionResult",
    "EffectObservation",
    "ReceiptReference",
]


class ConsolidationEffectUnavailable(RuntimeError):
    """Stable content-free refusal for ambiguous or contradictory effect state."""

    code = "CONSOLIDATION_EFFECT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ReceiptReference:
    event_id: str
    instance_id: str
    payload_digest: str
    receipt_head_digest: str
    record_hash: str
    sequence: int


@dataclass(frozen=True, slots=True)
class EffectObservation:
    state: Literal["prior", "prepared", "target", "mixed"]
    digest: str


@dataclass(frozen=True, slots=True)
class ConsolidationEffectJournalState:
    schema: str
    run_id: str
    operation_id: str
    kind: str
    effect_ordinal: int
    binding_digest: str
    revision: int
    status: Literal["prepared", "final"]
    intent: ReceiptReference
    intent_payload: Mapping[str, object]
    terminal: ReceiptReference | None
    observed_state: str | None
    observed_digest: str | None
    state_digest: str


@dataclass(frozen=True, slots=True)
class EffectExecutionResult:
    role: Literal["committed", "aborted"]
    observed_state: str
    observed_digest: str
    intent: ReceiptReference
    terminal: ReceiptReference
    journal_digest: str


def _fail() -> NoReturn:
    raise ConsolidationEffectUnavailable from None


def _crash_point(_point: str) -> None:
    """Narrow test seam for the six durable cross-store boundaries."""


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _event_id(value: object) -> str:
    if not isinstance(value, str) or _EVENT_ID.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SAFE_INTEGER:
        _fail()
    return value


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail()
        value[key] = item
    return value


def _reject_number(_value: str) -> NoReturn:
    _fail()


def _parse_integer(value: str) -> int:
    if value.startswith("-"):
        _fail()
    try:
        return _integer(int(value))
    except ValueError:
        _fail()


def _decode(raw: bytes) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_JOURNAL_BYTES:
        _fail()
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ConsolidationEffectUnavailable,
    ):
        _fail()
    if not isinstance(value, Mapping):
        _fail()
    try:
        canonical = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if canonical != raw:
        _fail()
    return value


def _frame(domain: bytes, payload: bytes) -> bytes:
    return (
        len(domain).to_bytes(4, "big")
        + domain
        + len(payload).to_bytes(8, "big")
        + payload
    )


def _binding_digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = consolidation_plan.canonical_closed_jcs(payload)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    return hashlib.sha256(_frame(_BINDING_DOMAIN, encoded)).hexdigest()


def _reference_value(reference: ReceiptReference) -> dict[str, object]:
    return {
        "event_id": reference.event_id,
        "instance_id": reference.instance_id,
        "payload_digest": reference.payload_digest,
        "receipt_head_digest": reference.receipt_head_digest,
        "record_hash": reference.record_hash,
        "sequence": reference.sequence,
    }


def _parse_reference(value: object) -> ReceiptReference:
    row = _mapping(value, _REFERENCE_FIELDS)
    instance_id = row["instance_id"]
    if not isinstance(instance_id, str) or _INSTANCE_ID.fullmatch(instance_id) is None:
        _fail()
    record_hash = _digest(row["record_hash"])
    head = _digest(row["receipt_head_digest"])
    if record_hash != head:
        _fail()
    return ReceiptReference(
        event_id=_event_id(row["event_id"]),
        instance_id=instance_id,
        payload_digest=_digest(row["payload_digest"]),
        receipt_head_digest=head,
        record_hash=record_hash,
        sequence=_integer(row["sequence"], minimum=1),
    )


def _intent_from_payload(
    payload: object,
    *,
    event_id: str,
) -> consolidation_receipts.ConsolidationEvent:
    try:
        checked = consolidation_receipts.validate_nested(
            payload,
            outer_phase="intent",
        )
        expected_id = consolidation_receipts._intent_identity(checked)  # noqa: SLF001
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    if event_id != expected_id:
        _fail()
    return consolidation_receipts.ConsolidationEvent(
        event_id=event_id,
        phase="intent",
        payload=checked,
        payload_digest=_digest(checked["payload_digest"]),
    )


def _state_value(state: ConsolidationEffectJournalState) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": state.schema,
        "run_id": state.run_id,
        "operation_id": state.operation_id,
        "kind": state.kind,
        "effect_ordinal": state.effect_ordinal,
        "binding_digest": state.binding_digest,
        "revision": state.revision,
        "status": state.status,
        "intent": _reference_value(state.intent),
        "intent_payload": dict(state.intent_payload),
    }
    if state.status == "final":
        if (
            state.terminal is None
            or state.observed_state is None
            or state.observed_digest is None
        ):
            _fail()
        value.update(
            {
                "terminal": _reference_value(state.terminal),
                "observed_state": state.observed_state,
                "observed_digest": state.observed_digest,
            }
        )
    elif any(
        item is not None
        for item in (state.terminal, state.observed_state, state.observed_digest)
    ):
        _fail()
    return value


def _state_bytes(state: ConsolidationEffectJournalState) -> bytes:
    try:
        return consolidation_plan.canonical_closed_jcs(_state_value(state))
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()


def _parse_state(raw: bytes) -> ConsolidationEffectJournalState:
    value = _decode(raw)
    status = value.get("status")
    fields = _FINAL_FIELDS if status == "final" else _PREPARED_FIELDS
    row = _mapping(value, fields)
    if row["schema"] != JOURNAL_SCHEMA or status not in {"prepared", "final"}:
        _fail()
    intent_ref = _parse_reference(row["intent"])
    intent = _intent_from_payload(
        row["intent_payload"],
        event_id=intent_ref.event_id,
    )
    if intent_ref.payload_digest != intent.payload_digest:
        _fail()
    payload = intent.payload
    run_id = _uuid4(row["run_id"])
    operation_id = _uuid4(row["operation_id"])
    ordinal = _integer(row["effect_ordinal"])
    kind = row["kind"]
    if (
        not isinstance(kind, str)
        or payload["run_id"] != run_id
        or payload["operation_id"] != operation_id
        or payload["effect_ordinal"] != ordinal
        or payload["kind"] != kind
    ):
        _fail()
    binding_payload = {
        "schema": _BINDING_SCHEMA,
        "run_id": run_id,
        "operation_id": operation_id,
        "kind": kind,
        "effect_ordinal": ordinal,
        "intent_event_id": intent.event_id,
        "intent_payload_digest": intent.payload_digest,
    }
    binding_digest = _binding_digest(binding_payload)
    if row["binding_digest"] != binding_digest:
        _fail()
    terminal: ReceiptReference | None = None
    observed_state: str | None = None
    observed_digest: str | None = None
    revision = _integer(row["revision"], minimum=1)
    if status == "prepared":
        if revision != 1:
            _fail()
    else:
        terminal = _parse_reference(row["terminal"])
        observed_state = row["observed_state"]
        if not isinstance(observed_state, str) or observed_state not in _STATES:
            _fail()
        observed_digest = _digest(row["observed_digest"])
        terminal_role = terminal.event_id.rpartition(":")[2]
        if (
            terminal_role not in _TERMINAL_ROLES
            or terminal.event_id != f"{intent.event_id}:{terminal_role}"
            or (terminal_role == "committed" and observed_state != "target")
            or (terminal_role == "aborted" and observed_state != "prior")
            or (terminal_role == "committed" and revision != 2)
            or (terminal_role == "aborted" and revision not in {1, 2})
        ):
            _fail()
        expected = consolidation_receipts.build_terminal(
            intent,
            role=terminal_role,
            observed_digest=observed_digest,
        )
        if terminal.payload_digest != expected.payload_digest:
            _fail()
    return ConsolidationEffectJournalState(
        schema=JOURNAL_SCHEMA,
        run_id=run_id,
        operation_id=operation_id,
        kind=kind,
        effect_ordinal=ordinal,
        binding_digest=binding_digest,
        revision=revision,
        status=status,
        intent=intent_ref,
        intent_payload=intent.payload,
        terminal=terminal,
        observed_state=observed_state,
        observed_digest=observed_digest,
        state_digest=hashlib.sha256(raw).hexdigest(),
    )


def _reference_from_record(
    record: Mapping[str, object],
    *,
    expected_event_id: str,
    expected_payload_digest: str,
    expected_phase: str,
) -> ReceiptReference:
    try:
        nested = consolidation_receipts.validate_nested(
            record.get("consolidation_event"),
            outer_phase=expected_phase,
        )
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    if (
        record.get("schema") != "receipt/v1"
        or record.get("event_type") != "consolidation"
        or record.get("phase") != expected_phase
        or record.get("event_id") != expected_event_id
        or record.get("durable") is not True
        or nested["payload_digest"] != expected_payload_digest
    ):
        _fail()
    instance_id = record.get("instance_id")
    if not isinstance(instance_id, str) or _INSTANCE_ID.fullmatch(instance_id) is None:
        _fail()
    record_hash = _digest(record.get("hash"))
    return ReceiptReference(
        event_id=expected_event_id,
        instance_id=instance_id,
        payload_digest=expected_payload_digest,
        receipt_head_digest=record_hash,
        record_hash=record_hash,
        sequence=_integer(record.get("seq"), minimum=1),
    )


def _verify_reference(
    vault_root: Path,
    reference: ReceiptReference,
    *,
    expected_phase: str,
) -> Mapping[str, object]:
    try:
        candidates = [
            record
            for record in receipts.event_records(vault_root)
            if record.get("instance_id") == reference.instance_id
            and record.get("seq") == reference.sequence
        ]
    except (OSError, RuntimeError, ValueError):
        _fail()
    if len(candidates) != 1:
        _fail()
    record = candidates[0]
    checked = _reference_from_record(
        record,
        expected_event_id=reference.event_id,
        expected_payload_digest=reference.payload_digest,
        expected_phase=expected_phase,
    )
    if checked != reference:
        _fail()
    return record


def _observation(value: object, *, event: consolidation_receipts.ConsolidationEvent) -> EffectObservation:
    if not isinstance(value, EffectObservation) or value.state not in _STATES:
        _fail()
    digest = _digest(value.digest)
    expected_field = {
        "prior": "prior_digest",
        "prepared": "prepared_digest",
        "target": "target_digest",
    }.get(value.state)
    if expected_field is not None:
        expected = event.payload.get(expected_field)
        if expected is None or digest != expected:
            _fail()
    return EffectObservation(state=value.state, digest=digest)


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationEffectUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail()


@contextmanager
def _execution_lock(
    vault_root: Path,
    *,
    effect_ordinal: int,
) -> Iterator[None]:
    """Serialize one effect across threads and processes without holding receipts."""

    try:
        manager = writer_lease.active_manager()
        coordinator = mutation_lock.VaultMutationCoordinator(
            manager.config.state_dir,
            Path(vault_root),
        )
        with coordinator.hold(
            request_id=f"effect-{effect_ordinal}",
            operation="consolidation-effect",
            holder_kind="consolidation-control",
        ):
            yield
    except ConsolidationEffectUnavailable:
        raise
    except (OSError, OpError, RuntimeError, TypeError, ValueError):
        _fail()


class ConsolidationEffectJournalStore:
    """Persist one effect's exact prepared and final cross-store references."""

    def __init__(
        self,
        vault_root: Path | str,
        *,
        run_id: str,
        effect_ordinal: int,
    ) -> None:
        self.vault_root = Path(vault_root).absolute()
        self.run_id = _uuid4(run_id)
        self.effect_ordinal = _integer(effect_ordinal)
        self.path = (
            self.vault_root
            / kb_dirname()
            / "_Consolidation"
            / "runs"
            / self.run_id
            / "effects"
            / f"effect-{self.effect_ordinal:010d}.json"
        )

    def _read(self) -> bytes:
        return reserved_paths._read_owner_bytes(  # noqa: SLF001
            self.vault_root,
            self.path,
            _DESCRIPTOR_ID,
            limit=_MAX_JOURNAL_BYTES,
        )

    def _read_optional(self) -> bytes | None:
        try:
            return self._read()
        except FileNotFoundError:
            return None

    def _publish(
        self,
        raw: bytes,
        *,
        expected_sha256: str | None = None,
        require_missing: bool = False,
    ) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            self.path,
            _DESCRIPTOR_ID,
            raw,
            expected_sha256=expected_sha256,
            require_missing=require_missing,
        )

    def load(self) -> ConsolidationEffectJournalState:
        with _authority(self.vault_root, mutation=False):
            try:
                state = _parse_state(self._read())
            except FileNotFoundError:
                _fail()
            self._validate_identity(state)
            self._verify_receipts(state)
            return state

    def load_optional(self) -> ConsolidationEffectJournalState | None:
        with _authority(self.vault_root, mutation=False):
            raw = self._read_optional()
            if raw is None:
                return None
            state = _parse_state(raw)
            self._validate_identity(state)
            self._verify_receipts(state)
            return state

    def _validate_identity(self, state: ConsolidationEffectJournalState) -> None:
        if (
            state.run_id != self.run_id
            or state.effect_ordinal != self.effect_ordinal
        ):
            _fail()

    def _verify_receipts(self, state: ConsolidationEffectJournalState) -> None:
        _verify_reference(self.vault_root, state.intent, expected_phase="intent")
        if state.status == "final":
            if state.terminal is None:
                _fail()
            role = state.terminal.event_id.rpartition(":")[2]
            _verify_reference(
                self.vault_root,
                state.terminal,
                expected_phase=role,
            )

    def prepare(
        self,
        event: consolidation_receipts.ConsolidationEvent,
        intent_record: Mapping[str, object],
    ) -> ConsolidationEffectJournalState:
        intent = _intent_from_payload(event.payload, event_id=event.event_id)
        if event != intent or intent.payload["run_id"] != self.run_id:
            _fail()
        if intent.payload["effect_ordinal"] != self.effect_ordinal:
            _fail()
        reference = _reference_from_record(
            intent_record,
            expected_event_id=intent.event_id,
            expected_payload_digest=intent.payload_digest,
            expected_phase="intent",
        )
        _verify_reference(self.vault_root, reference, expected_phase="intent")
        binding_payload = {
            "schema": _BINDING_SCHEMA,
            "run_id": self.run_id,
            "operation_id": intent.payload["operation_id"],
            "kind": intent.payload["kind"],
            "effect_ordinal": self.effect_ordinal,
            "intent_event_id": intent.event_id,
            "intent_payload_digest": intent.payload_digest,
        }
        candidate = ConsolidationEffectJournalState(
            schema=JOURNAL_SCHEMA,
            run_id=self.run_id,
            operation_id=str(intent.payload["operation_id"]),
            kind=str(intent.payload["kind"]),
            effect_ordinal=self.effect_ordinal,
            binding_digest=_binding_digest(binding_payload),
            revision=1,
            status="prepared",
            intent=reference,
            intent_payload=intent.payload,
            terminal=None,
            observed_state=None,
            observed_digest=None,
            state_digest="0" * 64,
        )
        raw = _state_bytes(candidate)
        target = replace(candidate, state_digest=hashlib.sha256(raw).hexdigest())
        with _authority(self.vault_root, mutation=True):
            existing_raw = self._read_optional()
            if existing_raw is None:
                self._publish(raw, require_missing=True)
                return target
            existing = _parse_state(existing_raw)
            self._validate_identity(existing)
            if existing.status == "prepared" and existing == target:
                return existing
            if (
                existing.status == "final"
                and existing.binding_digest == target.binding_digest
                and existing.intent == target.intent
                and dict(existing.intent_payload) == dict(target.intent_payload)
            ):
                self._verify_receipts(existing)
                return existing
            _fail()

    def finalize(
        self,
        event: consolidation_receipts.ConsolidationEvent,
        terminal_record: Mapping[str, object],
        *,
        observed_state: str,
        observed_digest: str,
    ) -> ConsolidationEffectJournalState:
        intent = _intent_from_payload(event.payload, event_id=event.event_id)
        if observed_state not in _STATES:
            _fail()
        observed = _observation(
            EffectObservation(
                state=observed_state,
                digest=observed_digest,
            ),
            event=intent,
        )
        role = terminal_record.get("phase")
        if not isinstance(role, str) or role not in _TERMINAL_ROLES:
            _fail()
        if (role == "committed" and observed.state != "target") or (
            role == "aborted" and observed.state != "prior"
        ):
            _fail()
        expected_terminal = consolidation_receipts.build_terminal(
            intent,
            role=role,
            observed_digest=observed.digest,
        )
        reference = _reference_from_record(
            terminal_record,
            expected_event_id=expected_terminal.event_id,
            expected_payload_digest=expected_terminal.payload_digest,
            expected_phase=role,
        )
        _verify_reference(self.vault_root, reference, expected_phase=role)
        with _authority(self.vault_root, mutation=True):
            try:
                raw = self._read()
            except FileNotFoundError:
                _fail()
            current = _parse_state(raw)
            self._validate_identity(current)
            if current.status == "final":
                if (
                    current.terminal == reference
                    and current.observed_state == observed.state
                    and current.observed_digest == observed.digest
                ):
                    self._verify_receipts(current)
                    return current
                _fail()
            if (
                current.intent.event_id != intent.event_id
                or current.intent.payload_digest != intent.payload_digest
                or dict(current.intent_payload) != dict(intent.payload)
            ):
                _fail()
            candidate = replace(
                current,
                revision=2,
                status="final",
                terminal=reference,
                observed_state=observed.state,
                observed_digest=observed.digest,
                state_digest="0" * 64,
            )
            target_raw = _state_bytes(candidate)
            target = replace(
                candidate,
                state_digest=hashlib.sha256(target_raw).hexdigest(),
            )
            self._publish(
                target_raw,
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
            return target

    def finalize_unprepared_abort(
        self,
        event: consolidation_receipts.ConsolidationEvent,
        intent_record: Mapping[str, object],
        terminal_record: Mapping[str, object],
    ) -> ConsolidationEffectJournalState:
        """Record the only valid intent-without-prepared recovery: abort on prior."""

        intent = _intent_from_payload(event.payload, event_id=event.event_id)
        if (
            intent.payload["run_id"] != self.run_id
            or intent.payload["effect_ordinal"] != self.effect_ordinal
            or terminal_record.get("phase") != "aborted"
        ):
            _fail()
        intent_reference = _reference_from_record(
            intent_record,
            expected_event_id=intent.event_id,
            expected_payload_digest=intent.payload_digest,
            expected_phase="intent",
        )
        terminal = consolidation_receipts.build_terminal(
            intent,
            role="aborted",
            observed_digest=str(intent.payload["prior_digest"]),
        )
        terminal_reference = _reference_from_record(
            terminal_record,
            expected_event_id=terminal.event_id,
            expected_payload_digest=terminal.payload_digest,
            expected_phase="aborted",
        )
        _verify_reference(self.vault_root, intent_reference, expected_phase="intent")
        _verify_reference(
            self.vault_root,
            terminal_reference,
            expected_phase="aborted",
        )
        binding_payload = {
            "schema": _BINDING_SCHEMA,
            "run_id": self.run_id,
            "operation_id": intent.payload["operation_id"],
            "kind": intent.payload["kind"],
            "effect_ordinal": self.effect_ordinal,
            "intent_event_id": intent.event_id,
            "intent_payload_digest": intent.payload_digest,
        }
        candidate = ConsolidationEffectJournalState(
            schema=JOURNAL_SCHEMA,
            run_id=self.run_id,
            operation_id=str(intent.payload["operation_id"]),
            kind=str(intent.payload["kind"]),
            effect_ordinal=self.effect_ordinal,
            binding_digest=_binding_digest(binding_payload),
            revision=1,
            status="final",
            intent=intent_reference,
            intent_payload=intent.payload,
            terminal=terminal_reference,
            observed_state="prior",
            observed_digest=str(intent.payload["prior_digest"]),
            state_digest="0" * 64,
        )
        raw = _state_bytes(candidate)
        target = replace(candidate, state_digest=hashlib.sha256(raw).hexdigest())
        with _authority(self.vault_root, mutation=True):
            existing_raw = self._read_optional()
            if existing_raw is None:
                self._publish(raw, require_missing=True)
                return target
            existing = _parse_state(existing_raw)
            self._validate_identity(existing)
            self._verify_receipts(existing)
            if existing == target:
                return existing
            _fail()


def _result(
    state: ConsolidationEffectJournalState,
) -> EffectExecutionResult:
    if (
        state.status != "final"
        or state.terminal is None
        or state.observed_state is None
        or state.observed_digest is None
    ):
        _fail()
    role = state.terminal.event_id.rpartition(":")[2]
    if role not in _TERMINAL_ROLES:
        _fail()
    return EffectExecutionResult(
        role=role,
        observed_state=state.observed_state,
        observed_digest=state.observed_digest,
        intent=state.intent,
        terminal=state.terminal,
        journal_digest=state.state_digest,
    )


def _execute_effect(
    *,
    vault_root: Path,
    event: consolidation_receipts.ConsolidationEvent,
    journal: ConsolidationEffectJournalStore,
    classify: Callable[[], EffectObservation],
    classify_unprepared: Callable[[], EffectObservation] | None = None,
    apply_effect: Callable[[], None],
    resume_effect: Callable[[], None] | None = None,
    timestamp: str | None = None,
) -> EffectExecutionResult:
    """Execute or recover one non-successor consolidation effect exactly once."""

    if not isinstance(journal, ConsolidationEffectJournalStore):
        _fail()
    with _execution_lock(
        Path(vault_root),
        effect_ordinal=journal.effect_ordinal,
    ):
        return _execute_effect_locked(
            vault_root=Path(vault_root),
            event=event,
            journal=journal,
            classify=classify,
            classify_unprepared=classify_unprepared,
            apply_effect=apply_effect,
            resume_effect=resume_effect,
            timestamp=timestamp,
        )


def _execute_effect_locked(
    *,
    vault_root: Path,
    event: consolidation_receipts.ConsolidationEvent,
    journal: ConsolidationEffectJournalStore,
    classify: Callable[[], EffectObservation],
    classify_unprepared: Callable[[], EffectObservation] | None,
    apply_effect: Callable[[], None],
    resume_effect: Callable[[], None] | None,
    timestamp: str | None,
) -> EffectExecutionResult:
    try:
        intent = _intent_from_payload(event.payload, event_id=event.event_id)
        if (
            event != intent
            or Path(vault_root).absolute() != journal.vault_root
            or intent.payload["run_id"] != journal.run_id
            or intent.payload["effect_ordinal"] != journal.effect_ordinal
            or "successor_context_seed_digest" in intent.payload
        ):
            _fail()
        intent_record, adopted_intent = (
            consolidation_receipts.append_intent_with_status(
            vault_root,
            intent,
            timestamp=timestamp,
            )
        )
        _crash_point("after-intent")
        existing = journal.load_optional()
        if existing is None:
            unprepared = _observation(
                (classify_unprepared or classify)(),
                event=intent,
            )
            if unprepared.state != "prior":
                _fail()
        if existing is None and adopted_intent:
            terminal_record = consolidation_receipts.append_terminal(
                vault_root,
                intent_event_id=intent.event_id,
                role="aborted",
                observed_digest=unprepared.digest,
                timestamp=timestamp,
            )
            _crash_point("after-terminal")
            journal.finalize_unprepared_abort(
                intent,
                intent_record,
                terminal_record,
            )
            _crash_point("after-final")
            _fail()
        prepared = journal.prepare(intent, intent_record)
        if prepared.status == "final":
            observation = _observation(classify(), event=intent)
            if (
                prepared.terminal is None
                or prepared.terminal.event_id.rpartition(":")[2] != "committed"
                or observation.state != "target"
                or observation.digest != prepared.observed_digest
            ):
                _fail()
            return _result(prepared)
        _crash_point("after-prepared")
        before = _observation(classify(), event=intent)
        if before.state == "mixed":
            _fail()
        if before.state == "target":
            after = before
        else:
            if before.state == "prepared":
                if resume_effect is None:
                    _fail()
                resume_effect()
            else:
                apply_effect()
            _crash_point("after-effect")
            after = _observation(classify(), event=intent)
            if after.state != "target":
                _fail()
        _crash_point("after-classification")
        terminal_record = consolidation_receipts.append_terminal(
            vault_root,
            intent_event_id=intent.event_id,
            role="committed",
            observed_digest=after.digest,
            timestamp=timestamp,
        )
        _crash_point("after-terminal")
        final = journal.finalize(
            intent,
            terminal_record,
            observed_state=after.state,
            observed_digest=after.digest,
        )
        _crash_point("after-final")
        return _result(final)
    except ConsolidationEffectUnavailable:
        raise
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    except (OSError, RuntimeError, TypeError, ValueError):
        _fail()
