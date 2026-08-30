"""Closed plaintext-free receipt payloads for consolidation effects.

The common receipt writer owns physical append order and durability.  This
module owns the nested consolidation schema, its semantic parent, and the
deterministic intent/terminal identities.  It deliberately cannot accept
paths, content, principals, policy documents, or caller-selected payload
extensions.

Each event also carries a closed digest-only evidence object plus its
``evidence_digest``.  The object contains no paths, principals, content, or
credentials; its named digests make every kind-specific retirement, rendering,
and verification claim explicit and independently hashable.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn

from . import consolidation_plan

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}(?::(?:committed|aborted))?\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_PHASE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_MAX_ORDINAL = (1 << 31) - 1
_ROLES = frozenset({"intent", "committed", "aborted"})

_KINDS = frozenset(
    {
        "start",
        "intake",
        "snapshot-source",
        "snapshot-destination",
        "reconcile",
        "plan-cutover",
        "plan-rollback",
        "plan-retirement",
        "render-begin",
        "render-page",
        "render-ack",
        "render-complete",
        "approval",
        "token-reservation",
        "seal-intent",
        "seal-drained",
        "preimage",
        "policy-prepare",
        "policy-active",
        "content-batch",
        "rebuild-kind",
        "in-process-probe",
        "in-process-verified",
        "transport-stop",
        "transport-probe",
        "transport-verified",
        "routing-open",
        "complete",
        "abort-begin",
        "abort-policy-restore",
        "abort-candidate-cleanup",
        "abort-rebuild-kind",
        "abort-probe",
        "abort-complete",
        "rollback-nonterminal-contingency-begin",
        "rollback-terminal-plan-begin",
        "rollback-seal",
        "rollback-revalidate",
        "rollback-restore-batch",
        "rollback-rebuild-kind",
        "rollback-probe",
        "rollback-complete",
        "recover-classification",
        "repair-terminal",
        "forward-snapshot-verified",
        "surviving-copy-ledger",
        "retirement-pending-forward-only",
        "retirement-clearance",
        "retirement-pending-fence-release",
        "retirement-consume",
        "retirement-completion",
        "retirement-finalize",
    }
)
_ORDINAL_FIELD = {
    "content-batch": "batch_ordinal",
    "rollback-restore-batch": "batch_ordinal",
    "rebuild-kind": "rebuild_ordinal",
    "abort-rebuild-kind": "rebuild_ordinal",
    "rollback-rebuild-kind": "rebuild_ordinal",
    "in-process-probe": "probe_ordinal",
    "transport-probe": "probe_ordinal",
    "abort-probe": "probe_ordinal",
    "rollback-probe": "probe_ordinal",
    "render-page": "page_ordinal",
    "render-ack": "page_ordinal",
}
_PREPARED_KINDS = frozenset({"content-batch", "rollback-restore-batch"})
_CONDITIONAL_SUCCESSOR_KINDS = frozenset({"reconcile", "repair-terminal"})
_REQUIRED_SUCCESSOR_KINDS = frozenset(
    {
        "complete",
        "rollback-complete",
        "retirement-pending-forward-only",
        "retirement-finalize",
    }
)
_SUCCESSOR_KINDS = _CONDITIONAL_SUCCESSOR_KINDS | _REQUIRED_SUCCESSOR_KINDS
_EXTERNAL_PARENT_KINDS = frozenset(
    {"retirement-consume", "retirement-completion"}
)
_LOCAL_PARENT_KINDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "intake": frozenset({"start"}),
        "snapshot-source": frozenset({"intake"}),
        "snapshot-destination": frozenset({"snapshot-source"}),
        "reconcile": frozenset({"snapshot-destination"}),
        "plan-cutover": frozenset({"reconcile", "repair-terminal"}),
        "plan-rollback": frozenset(
            {
                "complete",
                "rollback-complete",
                "retirement-pending-forward-only",
                "retirement-finalize",
                "repair-terminal",
            }
        ),
        "plan-retirement": frozenset({"complete", "repair-terminal"}),
        "render-begin": frozenset(
            {"plan-cutover", "plan-rollback", "plan-retirement"}
        ),
        "render-page": frozenset({"render-begin", "render-ack"}),
        "render-ack": frozenset({"render-page"}),
        "render-complete": frozenset({"render-ack"}),
        "approval": frozenset({"render-complete"}),
        "token-reservation": frozenset({"approval"}),
        "seal-intent": frozenset({"token-reservation"}),
        "seal-drained": frozenset({"seal-intent"}),
        "preimage": frozenset({"seal-drained"}),
        "policy-prepare": frozenset({"preimage"}),
        "policy-active": frozenset({"policy-prepare"}),
        "content-batch": frozenset({"policy-active", "content-batch"}),
        "rebuild-kind": frozenset({"content-batch", "rebuild-kind"}),
        "in-process-probe": frozenset(
            {"rebuild-kind", "in-process-probe"}
        ),
        "in-process-verified": frozenset({"in-process-probe"}),
        "transport-stop": frozenset({"in-process-verified"}),
        "transport-probe": frozenset({"transport-stop", "transport-probe"}),
        "transport-verified": frozenset({"transport-probe"}),
        "routing-open": frozenset({"transport-verified"}),
        "complete": frozenset({"routing-open"}),
        "abort-begin": frozenset(
            {
                "token-reservation",
                "seal-intent",
                "seal-drained",
                "preimage",
                "policy-prepare",
                "policy-active",
            }
        ),
        "abort-policy-restore": frozenset({"abort-begin"}),
        "abort-candidate-cleanup": frozenset(
            {"abort-begin", "abort-policy-restore"}
        ),
        "abort-rebuild-kind": frozenset(
            {"abort-candidate-cleanup", "abort-rebuild-kind"}
        ),
        "abort-probe": frozenset(
            {"abort-candidate-cleanup", "abort-rebuild-kind", "abort-probe"}
        ),
        "abort-complete": frozenset(
            {"abort-candidate-cleanup", "abort-rebuild-kind", "abort-probe"}
        ),
        "rollback-nonterminal-contingency-begin": frozenset(
            {
                "content-batch",
                "rebuild-kind",
                "in-process-probe",
                "in-process-verified",
                "transport-stop",
                "transport-probe",
                "transport-verified",
                "routing-open",
            }
        ),
        "rollback-terminal-plan-begin": frozenset({"token-reservation"}),
        "rollback-seal": frozenset(
            {
                "rollback-nonterminal-contingency-begin",
                "rollback-terminal-plan-begin",
            }
        ),
        "rollback-revalidate": frozenset({"rollback-seal"}),
        "rollback-restore-batch": frozenset(
            {"rollback-revalidate", "rollback-restore-batch"}
        ),
        "rollback-rebuild-kind": frozenset(
            {"rollback-restore-batch", "rollback-rebuild-kind"}
        ),
        "rollback-probe": frozenset(
            {"rollback-rebuild-kind", "rollback-probe"}
        ),
        "rollback-complete": frozenset({"rollback-probe"}),
        "repair-terminal": frozenset({"recover-classification"}),
        "forward-snapshot-verified": frozenset({"token-reservation"}),
        "surviving-copy-ledger": frozenset(
            {
                "token-reservation",
                "forward-snapshot-verified",
                "surviving-copy-ledger",
            }
        ),
        "retirement-pending-forward-only": frozenset(
            {"surviving-copy-ledger"}
        ),
        "retirement-clearance": frozenset(
            {"surviving-copy-ledger", "retirement-pending-forward-only"}
        ),
        "retirement-pending-fence-release": frozenset(
            {"recover-classification"}
        ),
        "retirement-finalize": frozenset({"retirement-completion"}),
    }
)
_EVIDENCE_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "start": frozenset({"identity_binding_digest", "run_request_digest"}),
        "intake": frozenset(
            {"archive_attestation_digest", "intake_manifest_digest"}
        ),
        "snapshot-source": frozenset({"source_snapshot_digest"}),
        "snapshot-destination": frozenset({"destination_snapshot_digest"}),
        "reconcile": frozenset(
            {"mapping_set_digest", "reconciliation_digest"}
        ),
        "plan-cutover": frozenset(
            {"plan_digest", "plan_input_basis_digest"}
        ),
        "plan-rollback": frozenset(
            {"plan_digest", "plan_input_basis_digest"}
        ),
        "plan-retirement": frozenset(
            {"plan_digest", "plan_input_basis_digest"}
        ),
        "render-begin": frozenset(
            {"impact_summary_digest", "plan_digest", "render_session_digest"}
        ),
        "render-page": frozenset(
            {
                "impact_summary_digest",
                "page_digest",
                "plan_digest",
                "render_session_digest",
            }
        ),
        "render-ack": frozenset(
            {
                "acknowledgement_digest",
                "impact_summary_digest",
                "page_digest",
                "plan_digest",
                "render_session_digest",
            }
        ),
        "render-complete": frozenset(
            {
                "coverage_digest",
                "impact_summary_digest",
                "plan_digest",
                "render_session_digest",
            }
        ),
        "approval": frozenset(
            {"confirmation_digest", "plan_digest", "rendering_completeness_digest"}
        ),
        "token-reservation": frozenset(
            {"plan_digest", "token_jti_digest", "token_reservation_digest"}
        ),
        "seal-intent": frozenset({"seal_basis_digest"}),
        "seal-drained": frozenset({"drain_digest", "seal_basis_digest"}),
        "preimage": frozenset({"preimage_manifest_digest"}),
        "policy-prepare": frozenset(
            {"policy_bundle_digest", "policy_prepared_digest"}
        ),
        "policy-active": frozenset(
            {"policy_active_digest", "policy_bundle_digest"}
        ),
        "content-batch": frozenset(
            {"batch_manifest_digest", "classification_digest"}
        ),
        "rebuild-kind": frozenset(
            {"rebuild_basis_digest", "rebuild_result_digest"}
        ),
        "in-process-probe": frozenset(
            {"probe_digest", "probe_result_digest", "verification_basis_digest"}
        ),
        "in-process-verified": frozenset(
            {"verification_basis_digest", "verification_result_digest"}
        ),
        "transport-stop": frozenset(
            {"routing_stop_digest", "verification_basis_digest"}
        ),
        "transport-probe": frozenset(
            {"probe_digest", "probe_result_digest", "verification_basis_digest"}
        ),
        "transport-verified": frozenset(
            {"verification_basis_digest", "verification_result_digest"}
        ),
        "routing-open": frozenset(
            {"routing_basis_digest", "routing_result_digest"}
        ),
        "complete": frozenset(
            {"completion_digest", "verification_basis_digest"}
        ),
        "abort-begin": frozenset({"abort_basis_digest"}),
        "abort-policy-restore": frozenset(
            {"abort_basis_digest", "policy_restore_digest"}
        ),
        "abort-candidate-cleanup": frozenset(
            {"abort_basis_digest", "cleanup_digest"}
        ),
        "abort-rebuild-kind": frozenset(
            {"abort_basis_digest", "rebuild_result_digest"}
        ),
        "abort-probe": frozenset(
            {"abort_basis_digest", "probe_digest", "probe_result_digest"}
        ),
        "abort-complete": frozenset(
            {"abort_basis_digest", "abort_result_digest"}
        ),
        "rollback-nonterminal-contingency-begin": frozenset(
            {"rollback_authority_digest", "rollback_basis_digest"}
        ),
        "rollback-terminal-plan-begin": frozenset(
            {"rollback_plan_digest", "token_jti_digest"}
        ),
        "rollback-seal": frozenset(
            {"rollback_basis_digest", "seal_basis_digest"}
        ),
        "rollback-revalidate": frozenset(
            {"revalidation_digest", "rollback_basis_digest"}
        ),
        "rollback-restore-batch": frozenset(
            {"batch_manifest_digest", "classification_digest"}
        ),
        "rollback-rebuild-kind": frozenset(
            {"rebuild_basis_digest", "rebuild_result_digest"}
        ),
        "rollback-probe": frozenset(
            {"probe_digest", "probe_result_digest", "verification_basis_digest"}
        ),
        "rollback-complete": frozenset(
            {"rollback_result_digest", "verification_basis_digest"}
        ),
        "recover-classification": frozenset(
            {"classification_digest", "recovery_basis_digest"}
        ),
        "repair-terminal": frozenset(
            {"recovery_basis_digest", "repair_result_digest"}
        ),
        "forward-snapshot-verified": frozenset(
            {"forward_snapshot_digest", "verification_result_digest"}
        ),
        "surviving-copy-ledger": frozenset(
            {"surviving_copy_ledger_digest"}
        ),
        "retirement-pending-forward-only": frozenset(
            {
                "forward_snapshot_digest",
                "pending_fence_digest",
                "surviving_copy_ledger_digest",
            }
        ),
        "retirement-clearance": frozenset(
            {
                "clearance_outcome_digest",
                "destination_proof_digest",
                "disposition_digest",
                "retirement_plan_digest",
                "source_checkpoint_digest",
                "token_jti_digest",
            }
        ),
        "retirement-pending-fence-release": frozenset(
            {
                "clearance_jti_digest",
                "nonconsumption_digest",
                "pending_fence_digest",
                "source_survival_digest",
            }
        ),
        "retirement-consume": frozenset(
            {
                "clearance_jti_digest",
                "destination_proof_digest",
                "disposition_digest",
                "source_checkpoint_digest",
                "source_fence_digest",
                "verifier_decision_digest",
            }
        ),
        "retirement-completion": frozenset(
            {
                "authentication_proof_digest",
                "completion_attestation_digest",
                "disposition_digest",
                "source_consume_event_digest",
                "source_receipt_head_digest",
            }
        ),
        "retirement-finalize": frozenset(
            {
                "completion_attestation_digest",
                "disposition_digest",
                "permanent_fence_digest",
            }
        ),
    }
)
_SPECIALIZED_ORDINALS = frozenset(
    {"batch_ordinal", "rebuild_ordinal", "probe_ordinal", "page_ordinal"}
)
_BASE_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "run_id",
        "operation_id",
        "phase",
        "record_role",
        "effect_ordinal",
        "request_digest",
        "prior_digest",
        "target_digest",
        "evidence",
        "evidence_digest",
        "semantic_parent_event_id",
        "semantic_parent_payload_digest",
        "payload_digest",
    }
)

__all__ = [
    "CONSOLIDATION_EVENT_KINDS",
    "ConsolidationEvent",
    "ConsolidationReceiptUnavailable",
    "append_intent",
    "append_terminal",
    "build_evidence",
    "build_intent",
    "build_terminal",
    "semantic_root",
    "validate_nested",
]

CONSOLIDATION_EVENT_KINDS = _KINDS


class ConsolidationReceiptUnavailable(RuntimeError):
    """Stable content-free refusal for malformed or contradictory evidence."""

    code = "CONSOLIDATION_RECEIPT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ConsolidationEvent:
    event_id: str
    phase: str
    payload: Mapping[str, object]
    payload_digest: str


def _fail() -> NoReturn:
    raise ConsolidationReceiptUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _event_id(value: object, *, intent_only: bool = False) -> str:
    if not isinstance(value, str) or _EVENT_ID.fullmatch(value) is None:
        _fail()
    if intent_only and ":" in value:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _phase(value: object) -> str:
    if not isinstance(value, str) or _PHASE.fullmatch(value) is None:
        _fail()
    return value


def _ordinal(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_ORDINAL:
        _fail()
    return value


def _frame(domain: bytes, payload: bytes) -> bytes:
    return (
        len(domain).to_bytes(4, "big")
        + domain
        + len(payload).to_bytes(8, "big")
        + payload
    )


def _framed_digest(domain: bytes, value: Mapping[str, object]) -> str:
    try:
        canonical = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    return hashlib.sha256(_frame(domain, canonical)).hexdigest()


def semantic_root() -> tuple[str, str]:
    """Return the fixed start-only semantic root id and payload digest."""

    digest = _framed_digest(b"exomem.consolidation-semantic-root/v1", {})
    return digest, digest


def _schema(kind: str) -> str:
    if kind not in _KINDS:
        _fail()
    return f"exomem.consolidation-event/{kind}/v1"


def _evidence_schema(kind: str) -> str:
    if kind not in _KINDS:
        _fail()
    return f"exomem.consolidation-event-evidence/{kind}/v1"


def build_evidence(
    *,
    kind: str,
    digests: Mapping[str, object],
) -> Mapping[str, object]:
    """Build one closed digest-only evidence object for an effect kind."""

    expected = _EVIDENCE_FIELDS.get(kind)
    if expected is None or not isinstance(digests, Mapping):
        _fail()
    try:
        supplied = dict(digests)
    except (TypeError, ValueError):
        _fail()
    if frozenset(supplied) != expected:
        _fail()
    evidence: dict[str, object] = {
        "schema": _evidence_schema(kind),
        "kind": kind,
    }
    for field in sorted(expected):
        evidence[field] = _digest(supplied[field])
    try:
        consolidation_plan.canonical_closed_jcs(evidence)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    return MappingProxyType(evidence)


def _validated_evidence(value: object, *, kind: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail()
    try:
        snapshot = dict(value)
    except (TypeError, ValueError):
        _fail()
    if snapshot.get("schema") != _evidence_schema(kind) or snapshot.get("kind") != kind:
        _fail()
    digests = dict(snapshot)
    digests.pop("schema")
    digests.pop("kind")
    return build_evidence(kind=kind, digests=digests)


def _evidence_digest(evidence: Mapping[str, object], *, kind: str) -> str:
    return _framed_digest(
        f"exomem.consolidation-event-evidence/{kind}/v1".encode("ascii"),
        evidence,
    )


def _expected_fields(
    kind: str,
    role: str,
    *,
    has_successor_seed: bool,
) -> frozenset[str]:
    if kind not in _KINDS or role not in _ROLES:
        _fail()
    if kind in _REQUIRED_SUCCESSOR_KINDS and not has_successor_seed:
        _fail()
    if has_successor_seed and kind not in _SUCCESSOR_KINDS:
        _fail()
    fields = set(_BASE_FIELDS)
    ordinal_field = _ORDINAL_FIELD.get(kind)
    if ordinal_field is not None:
        fields.add(ordinal_field)
    if kind in _PREPARED_KINDS:
        fields.add("prepared_digest")
    if role != "intent":
        fields.add("observed_digest")
    if has_successor_seed:
        fields.add("successor_context_seed_digest")
    return frozenset(fields)


def validate_nested(
    value: object,
    *,
    outer_phase: str | None = None,
) -> Mapping[str, object]:
    """Validate one exact nested event and recompute its role payload digest."""

    if not isinstance(value, Mapping):
        _fail()
    try:
        snapshot = dict(value)
    except (TypeError, ValueError):
        _fail()
    kind = snapshot.get("kind")
    role = snapshot.get("record_role")
    if not isinstance(kind, str) or not isinstance(role, str):
        _fail()
    has_seed = "successor_context_seed_digest" in snapshot
    if frozenset(snapshot) != _expected_fields(
        kind,
        role,
        has_successor_seed=has_seed,
    ):
        _fail()
    if snapshot["schema"] != _schema(kind):
        _fail()
    if outer_phase is not None and role != outer_phase:
        _fail()
    _uuid4(snapshot["run_id"])
    _uuid4(snapshot["operation_id"])
    _phase(snapshot["phase"])
    _ordinal(snapshot["effect_ordinal"])
    _digest(snapshot["request_digest"])
    _digest(snapshot["prior_digest"])
    _digest(snapshot["target_digest"])
    evidence = _validated_evidence(snapshot["evidence"], kind=kind)
    snapshot["evidence"] = dict(evidence)
    if _digest(snapshot["evidence_digest"]) != _evidence_digest(
        evidence,
        kind=kind,
    ):
        _fail()
    semantic_parent_id = _event_id(snapshot["semantic_parent_event_id"])
    semantic_parent_digest = _digest(snapshot["semantic_parent_payload_digest"])
    root_id, root_digest = semantic_root()
    if role == "intent":
        if kind == "start":
            if (
                semantic_parent_id != root_id
                or semantic_parent_digest != root_digest
            ):
                _fail()
        elif kind == "recover-classification":
            if semantic_parent_id == root_id:
                _fail()
        elif not semantic_parent_id.endswith(":committed"):
            _fail()
    elif ":" in semantic_parent_id:
        _fail()
    ordinal_field = _ORDINAL_FIELD.get(kind)
    for field in _SPECIALIZED_ORDINALS:
        if field == ordinal_field:
            _ordinal(snapshot[field])
        elif field in snapshot:
            _fail()
    if kind in _PREPARED_KINDS:
        _digest(snapshot["prepared_digest"])
    elif "prepared_digest" in snapshot:
        _fail()
    if role == "intent":
        if "observed_digest" in snapshot:
            _fail()
    else:
        _digest(snapshot["observed_digest"])
    if has_seed:
        _digest(snapshot["successor_context_seed_digest"])
    supplied_digest = _digest(snapshot["payload_digest"])
    preimage = dict(snapshot)
    preimage.pop("payload_digest")
    expected_digest = _framed_digest(
        f"exomem.consolidation-event-payload/{kind}/{role}/v1".encode("ascii"),
        preimage,
    )
    if supplied_digest != expected_digest:
        _fail()
    try:
        canonical = consolidation_plan.canonical_closed_jcs(snapshot)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    # JCS rejects unsupported values after the one immutable snapshot above.
    if not canonical:
        _fail()
    return MappingProxyType(snapshot)


def _intent_identity(payload: Mapping[str, object]) -> str:
    checked = validate_nested(payload, outer_phase="intent")
    identity = dict(checked)
    identity.pop("payload_digest")
    identity.pop("evidence")
    kind = str(checked["kind"])
    return _framed_digest(
        f"exomem.consolidation-event-id/{kind}/v1".encode("ascii"),
        identity,
    )


def build_intent(
    *,
    kind: str,
    run_id: str,
    operation_id: str,
    phase: str,
    effect_ordinal: int,
    request_digest: str,
    prior_digest: str,
    target_digest: str,
    evidence: Mapping[str, object],
    semantic_parent_event_id: str,
    semantic_parent_payload_digest: str,
    prepared_digest: str | None = None,
    batch_ordinal: int | None = None,
    rebuild_ordinal: int | None = None,
    probe_ordinal: int | None = None,
    page_ordinal: int | None = None,
    successor_context_seed_digest: str | None = None,
) -> ConsolidationEvent:
    """Build one deterministic intent without accepting arbitrary payload data."""

    payload: dict[str, object] = {
        "schema": _schema(kind),
        "kind": kind,
        "run_id": _uuid4(run_id),
        "operation_id": _uuid4(operation_id),
        "phase": _phase(phase),
        "record_role": "intent",
        "effect_ordinal": _ordinal(effect_ordinal),
        "request_digest": _digest(request_digest),
        "prior_digest": _digest(prior_digest),
        "target_digest": _digest(target_digest),
        "semantic_parent_event_id": _event_id(semantic_parent_event_id),
        "semantic_parent_payload_digest": _digest(
            semantic_parent_payload_digest
        ),
    }
    checked_evidence = _validated_evidence(evidence, kind=kind)
    payload["evidence"] = dict(checked_evidence)
    payload["evidence_digest"] = _evidence_digest(checked_evidence, kind=kind)
    optional = {
        "prepared_digest": prepared_digest,
        "batch_ordinal": batch_ordinal,
        "rebuild_ordinal": rebuild_ordinal,
        "probe_ordinal": probe_ordinal,
        "page_ordinal": page_ordinal,
        "successor_context_seed_digest": successor_context_seed_digest,
    }
    payload.update({key: item for key, item in optional.items() if item is not None})
    payload["payload_digest"] = _framed_digest(
        f"exomem.consolidation-event-payload/{kind}/intent/v1".encode("ascii"),
        payload,
    )
    checked = validate_nested(payload, outer_phase="intent")
    event_id = _intent_identity(checked)
    return ConsolidationEvent(
        event_id=event_id,
        phase="intent",
        payload=checked,
        payload_digest=str(checked["payload_digest"]),
    )


def build_terminal(
    intent: ConsolidationEvent,
    *,
    role: str,
    observed_digest: str,
) -> ConsolidationEvent:
    """Derive the one suffix terminal from an already validated intent."""

    if not isinstance(intent, ConsolidationEvent) or intent.phase != "intent":
        _fail()
    _event_id(intent.event_id, intent_only=True)
    if role not in {"committed", "aborted"}:
        _fail()
    checked_intent = validate_nested(intent.payload, outer_phase="intent")
    if intent.event_id != _intent_identity(checked_intent):
        _fail()
    payload = dict(checked_intent)
    payload["record_role"] = role
    payload["semantic_parent_event_id"] = intent.event_id
    payload["semantic_parent_payload_digest"] = intent.payload_digest
    payload["observed_digest"] = _digest(observed_digest)
    payload.pop("payload_digest")
    kind = str(payload["kind"])
    payload["payload_digest"] = _framed_digest(
        f"exomem.consolidation-event-payload/{kind}/{role}/v1".encode("ascii"),
        payload,
    )
    checked = validate_nested(payload, outer_phase=role)
    return ConsolidationEvent(
        event_id=f"{intent.event_id}:{role}",
        phase=role,
        payload=checked,
        payload_digest=str(checked["payload_digest"]),
    )


def append_intent(
    vault_root: Path,
    event: ConsolidationEvent,
    *,
    timestamp: str | None = None,
) -> dict[str, object]:
    """Append or adopt one exact durable consolidation intent."""

    from . import receipts

    try:
        if not isinstance(event, ConsolidationEvent) or event.phase != "intent":
            _fail()
        checked = validate_nested(event.payload, outer_phase="intent")
        if (
            event.payload_digest != checked["payload_digest"]
            or event.event_id != _intent_identity(checked)
        ):
            _fail()
        record = receipts.append_event(
            Path(vault_root),
            event_type="consolidation",
            phase="intent",
            event_id=_event_id(event.event_id, intent_only=True),
            payload={"consolidation_event": dict(checked)},
            timestamp=timestamp,
            critical=True,
        )
        return {key: value for key, value in record.items() if not key.startswith("_")}
    except ConsolidationReceiptUnavailable:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _fail()


def _active_records(vault_root: Path) -> list[dict[str, object]]:
    from . import receipts

    with receipts._receipt_connection(Path(vault_root)) as connection:  # noqa: SLF001
        instance_id = receipts._instance_id(connection)  # noqa: SLF001
        records, issues = receipts._chain_state(  # noqa: SLF001
            receipts._instance_dir(Path(vault_root), instance_id)  # noqa: SLF001
        )
    if issues:
        _fail()
    return records


def _consolidation_nested(
    record: Mapping[str, object],
) -> Mapping[str, object]:
    phase = record.get("phase")
    if not isinstance(phase, str) or phase not in _ROLES:
        _fail()
    return validate_nested(
        record.get("consolidation_event"),
        outer_phase=phase,
    )


def _validate_unique_effect_ordinal(
    records: list[dict[str, object]],
    *,
    event_id: str,
    checked: Mapping[str, object],
) -> None:
    for record in records:
        if (
            record.get("event_type") != "consolidation"
            or record.get("phase") != "intent"
        ):
            continue
        existing = _consolidation_nested(record)
        if (
            existing["run_id"] == checked["run_id"]
            and existing["effect_ordinal"] == checked["effect_ordinal"]
            and record.get("event_id") != event_id
        ):
            _fail()


def _validate_specialized_parent(
    checked: Mapping[str, object],
    parent: Mapping[str, object],
) -> None:
    kind = str(checked["kind"])
    field = _ORDINAL_FIELD.get(kind)
    if field is None:
        return
    ordinal = int(checked[field])
    parent_kind = str(parent["kind"])
    if kind == "render-ack":
        if parent_kind != "render-page" or ordinal != parent["page_ordinal"]:
            _fail()
        return
    if kind == "render-page" and parent_kind == "render-ack":
        if ordinal != int(parent["page_ordinal"]) + 1:
            _fail()
        return
    if parent_kind == kind:
        if ordinal != int(parent[field]) + 1:
            _fail()
        return
    if ordinal != 0:
        _fail()


def _validate_local_intent_parent(
    records: list[dict[str, object]],
    checked: Mapping[str, object],
) -> None:
    kind = str(checked["kind"])
    parent_id = str(checked["semantic_parent_event_id"])
    parents = [
        record
        for record in records
        if record.get("event_type") == "consolidation"
        and record.get("event_id") == parent_id
    ]
    if len(parents) != 1:
        _fail()
    parent_record = parents[0]
    parent_phase = str(parent_record.get("phase"))
    if kind != "recover-classification" and parent_phase != "committed":
        _fail()
    parent = _consolidation_nested(parent_record)
    if (
        parent["run_id"] != checked["run_id"]
        or parent["payload_digest"]
        != checked["semantic_parent_payload_digest"]
        or int(checked["effect_ordinal"])
        != int(parent["effect_ordinal"]) + 1
    ):
        _fail()
    if kind != "recover-classification":
        allowed = _LOCAL_PARENT_KINDS.get(kind)
        if allowed is None or parent["kind"] not in allowed:
            _fail()
        _validate_specialized_parent(checked, parent)


def validate_outer_append(
    records: list[dict[str, object]],
    *,
    event_id: object,
    outer_phase: str,
    nested: object,
) -> None:
    """Validate identity and local intent/terminal causality before append."""

    checked = validate_nested(nested, outer_phase=outer_phase)
    if outer_phase == "intent":
        checked_event_id = _event_id(event_id, intent_only=True)
        if checked_event_id != _intent_identity(checked):
            _fail()
        kind = str(checked["kind"])
        _validate_unique_effect_ordinal(
            records,
            event_id=checked_event_id,
            checked=checked,
        )
        if kind == "start":
            if checked["effect_ordinal"] != 0:
                _fail()
            return
        # Cross-chain retirement causality must enter through the later trusted
        # source-lifecycle/control adapter.  The generic local writer cannot
        # authenticate an external receipt head, so it refuses these events.
        if kind in _EXTERNAL_PARENT_KINDS:
            _fail()
        _validate_local_intent_parent(records, checked)
        return
    if outer_phase not in {"committed", "aborted"}:
        _fail()
    supplied_id = _event_id(event_id)
    intent_event_id = supplied_id.removesuffix(f":{outer_phase}")
    if intent_event_id == supplied_id or not _DIGEST.fullmatch(intent_event_id):
        _fail()
    intents = [
        record
        for record in records
        if record.get("event_type") == "consolidation"
        and record.get("phase") == "intent"
        and record.get("event_id") == intent_event_id
    ]
    if len(intents) != 1:
        _fail()
    intent_nested = validate_nested(
        intents[0].get("consolidation_event"),
        outer_phase="intent",
    )
    intent = ConsolidationEvent(
        event_id=intent_event_id,
        phase="intent",
        payload=intent_nested,
        payload_digest=str(intent_nested["payload_digest"]),
    )
    expected = build_terminal(
        intent,
        role=outer_phase,
        observed_digest=str(checked["observed_digest"]),
    )
    if dict(expected.payload) != dict(checked) or expected.event_id != supplied_id:
        _fail()
    competing_ids = {
        f"{intent_event_id}:committed",
        f"{intent_event_id}:aborted",
    }
    if any(
        record.get("event_id") in competing_ids
        and record.get("event_id") != supplied_id
        for record in records
    ):
        _fail()


def append_terminal(
    vault_root: Path,
    *,
    intent_event_id: str,
    role: str,
    observed_digest: str,
    timestamp: str | None = None,
) -> dict[str, object]:
    """Append/adopt exactly one terminal for an intent on the active chain."""

    from . import receipts

    intent_event_id = _event_id(intent_event_id, intent_only=True)
    if role not in {"committed", "aborted"}:
        _fail()
    try:
        with receipts._receipt_lock(Path(vault_root)):  # noqa: SLF001
            records = _active_records(Path(vault_root))
            matching = [
                record
                for record in records
                if record.get("event_type") == "consolidation"
                and record.get("phase") == "intent"
                and record.get("event_id") == intent_event_id
            ]
            if len(matching) != 1:
                _fail()
            intent_record = matching[0]
            checked = validate_nested(
                intent_record.get("consolidation_event"),
                outer_phase="intent",
            )
            intent = ConsolidationEvent(
                event_id=intent_event_id,
                phase="intent",
                payload=checked,
                payload_digest=str(checked["payload_digest"]),
            )
            terminal = build_terminal(
                intent,
                role=role,
                observed_digest=observed_digest,
            )
            competing = [
                record
                for record in records
                if record.get("event_id")
                in {
                    f"{intent_event_id}:committed",
                    f"{intent_event_id}:aborted",
                }
            ]
            if competing and competing[-1].get("event_id") != terminal.event_id:
                _fail()
            record = receipts.append_event(
                Path(vault_root),
                event_type="consolidation",
                phase=role,
                event_id=terminal.event_id,
                payload={"consolidation_event": dict(terminal.payload)},
                timestamp=timestamp,
                critical=True,
            )
            return {
                key: value
                for key, value in record.items()
                if not key.startswith("_")
            }
    except ConsolidationReceiptUnavailable:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _fail()
