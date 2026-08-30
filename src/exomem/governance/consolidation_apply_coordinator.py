"""Receipt-first apply preparation through a verified destination preimage.

This coordinator begins at the already committed cutover token reservation and
stops before policy or content publication.  Each durable transition has its
own receipt and effect journal so restart can distinguish prior, prepared,
target, and mixed state without repeating an effect.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from . import (
    consolidation_admission,
    consolidation_authority,
    consolidation_effect_coordinator,
    consolidation_intake,
    consolidation_plan,
    consolidation_plan_store,
    consolidation_policy,
    consolidation_preimage,
    consolidation_receipts,
    consolidation_seal,
)
from .principal import RequestPrincipal

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMITTED_EVENT = re.compile(r"[0-9a-f]{64}:committed\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_EFFECT_SCHEMA = "exomem.consolidation-apply-preparation-effect/v1"

__all__ = [
    "ApplyPreparationResult",
    "ConsolidationApplyPreparationUnavailable",
    "prepare_apply_through_preimage",
]


class ConsolidationApplyPreparationUnavailable(RuntimeError):
    """Content-free refusal for invalid or ambiguous apply preparation state."""

    code = "CONSOLIDATION_APPLY_PREPARATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ApplyPreparationResult:
    seal_intent: consolidation_effect_coordinator.EffectExecutionResult
    seal_drained: consolidation_effect_coordinator.EffectExecutionResult
    preimage_effect: consolidation_effect_coordinator.EffectExecutionResult
    preimage_plan: consolidation_preimage.DestinationPreimagePlan
    preimage: consolidation_preimage.DestinationPreimage
    seal_state: consolidation_seal.ConsolidationSealState


def _fail() -> NoReturn:
    raise ConsolidationApplyPreparationUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _policy_verification_timestamp() -> str:
    """Return a server-current millisecond timestamp for fresh session checks."""

    current = datetime.now(UTC)
    current = current.replace(microsecond=(current.microsecond // 1000) * 1000)
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _effect_digest(kind: str, state: str, facts: dict[str, object]) -> str:
    try:
        raw = consolidation_plan.canonical_closed_jcs(
            {
                "schema": _EFFECT_SCHEMA,
                "kind": kind,
                "state": state,
                "facts": facts,
            }
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    domain = _EFFECT_SCHEMA.encode("ascii")
    framed = len(domain).to_bytes(4, "big") + domain + len(raw).to_bytes(8, "big") + raw
    return hashlib.sha256(framed).hexdigest()


def _observation(
    state: str,
    digest: str,
) -> consolidation_effect_coordinator.EffectObservation:
    return consolidation_effect_coordinator.EffectObservation(  # type: ignore[arg-type]
        state=state,
        digest=digest,
    )


def _committed_token_parent(
    vault_root: Path,
    *,
    event_id: str,
    payload_digest: str,
    run_id: str,
    operation_id: str,
    request_digest: str,
    plan_digest: str,
) -> tuple[int, str, str]:
    if not isinstance(event_id, str) or _COMMITTED_EVENT.fullmatch(event_id) is None:
        _fail()
    expected_payload = _digest(payload_digest)
    matches = [
        record
        for record in consolidation_receipts._active_records(vault_root)  # noqa: SLF001
        if record.get("event_type") == "consolidation"
        and record.get("phase") == "committed"
        and record.get("event_id") == event_id
    ]
    if len(matches) != 1:
        _fail()
    try:
        nested = consolidation_receipts.validate_nested(
            matches[0].get("consolidation_event"),
            outer_phase="committed",
        )
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    if (
        nested["kind"] != "token-reservation"
        or nested["run_id"] != run_id
        or nested["operation_id"] != operation_id
        or nested["request_digest"] != request_digest
        or nested["payload_digest"] != expected_payload
        or nested["evidence"]["plan_digest"] != plan_digest
    ):
        _fail()
    return int(nested["effect_ordinal"]), event_id, expected_payload


def _seal_matches(
    state: consolidation_seal.ConsolidationSealState,
    *,
    phase: str,
    revision: int,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    sealed_at: str,
    recorded_at: str,
) -> bool:
    return (
        state.kind == "consolidation-sealed"
        and state.phase == phase
        and state.revision == revision
        and state.vault_binding_digest == vault_binding_digest
        and state.run_id == run_id
        and state.operation_id == operation_id
        and state.journal_digest == journal_digest
        and state.sealed_at == sealed_at
        and state.recorded_at == recorded_at
    )


def _apply_lineage_matches(
    state: consolidation_seal.ConsolidationSealState,
    *,
    minimum_phase: str,
    base_revision: int,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    sealed_at: str,
) -> bool:
    revisions: dict[str, int] = {}
    phase = "sealing"
    revision = 1
    while phase not in revisions:
        revisions[phase] = revision
        successor = consolidation_seal._PHASE_SUCCESSORS.get(phase)  # noqa: SLF001
        if successor is None:
            break
        phase = successor
        revision += 1
    current_revision = revisions.get(str(state.phase))
    minimum_revision = revisions.get(minimum_phase)
    if current_revision is None or minimum_revision is None:
        return False
    return (
        current_revision >= minimum_revision
        and state.kind == "consolidation-sealed"
        and state.revision == base_revision + current_revision
        and state.vault_binding_digest == vault_binding_digest
        and state.run_id == run_id
        and state.operation_id == operation_id
        and state.journal_digest == journal_digest
        and state.sealed_at == sealed_at
    )


def _apply_base_revision(
    state: consolidation_seal.ConsolidationSealState,
    *,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    sealed_at: str,
) -> int:
    if state.kind == "open" and state.vault_binding_digest == vault_binding_digest:
        return state.revision
    if (
        state.kind != "consolidation-sealed"
        or state.vault_binding_digest != vault_binding_digest
        or state.run_id != run_id
        or state.operation_id != operation_id
        or state.journal_digest != journal_digest
        or state.sealed_at != sealed_at
    ):
        _fail()
    phase = "sealing"
    increment = 1
    while True:
        if state.phase == phase:
            base = state.revision - increment
            if base < 0:
                _fail()
            return base
        successor = consolidation_seal._PHASE_SUCCESSORS.get(phase)  # noqa: SLF001
        if successor is None:
            _fail()
        phase = successor
        increment += 1


def prepare_apply_through_preimage(
    *,
    vault_root: Path | str,
    admission: consolidation_admission.ConsolidationAdmission,
    control: consolidation_admission.ConsolidationControlAdmission,
    artifact_store: consolidation_intake.PrivateConsolidationArtifactStore,
    token_reservation_event_id: str,
    token_reservation_payload_digest: str,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    request_digest: str,
    plan_digest: str,
    principal_contexts: Sequence[RequestPrincipal],
    sealed_at: str,
    drained_at: str,
    preimage_ready_at: str,
    now: int,
    timeout: float,
) -> ApplyPreparationResult:
    """Seal, drain, and prove the exact preimage; publish no policy or content."""

    root = Path(vault_root).absolute()
    if (
        not isinstance(admission, consolidation_admission.ConsolidationAdmission)
        or admission.vault_root != root
        or not isinstance(
            control,
            consolidation_admission.ConsolidationControlAdmission,
        )
        or not isinstance(
            artifact_store,
            consolidation_intake.PrivateConsolidationArtifactStore,
        )
    ):
        _fail()
    checked_run = _uuid4(run_id)
    checked_operation = _uuid4(operation_id)
    checked_vault = _digest(vault_binding_digest)
    checked_journal = _digest(journal_digest)
    checked_request = _digest(request_digest)
    checked_plan = _digest(plan_digest)
    if (
        admission.vault_binding_digest != checked_vault
        or type(now) is not int
        or now < 0
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or timeout < 0
    ):
        _fail()
    try:
        _sealed_text, sealed_time = consolidation_plan._timestamp(  # noqa: SLF001
            sealed_at
        )
        _drained_text, drained_time = consolidation_plan._timestamp(  # noqa: SLF001
            drained_at
        )
        _ready_text, ready_time = consolidation_plan._timestamp(  # noqa: SLF001
            preimage_ready_at
        )
        if not sealed_time < drained_time < ready_time:
            _fail()
        plan_store = consolidation_plan_store.ConsolidationPlanStore(root)
        stored_plan = plan_store.load(
            checked_run,
            plan_kind="cutover",
            plan_digest=checked_plan,
        )
        if (
            stored_plan.digest != checked_plan
            or stored_plan.preimage["run_id"] != checked_run
            or stored_plan.preimage["plan_kind"] != "cutover"
        ):
            _fail()
        checked_basis = _digest(stored_plan.control_basis.digest)
        checked_snapshot = _digest(stored_plan.preimage["destination_snapshot_fingerprint"])
        checked_census = _digest(
            stored_plan.preimage["expected_destination_preimage_census_digest"]
        )
        plan_nonce = stored_plan.preimage["nonce"]
        if not isinstance(plan_nonce, str):
            _fail()
        stored_policy_bundle = plan_store.load_policy_bundle(
            checked_run,
            plan_kind="cutover",
            plan_digest=checked_plan,
        )
        consolidation_policy.revalidate_destination_policy(
            root,
            stored_policy_bundle,
            principal_contexts=principal_contexts,
            destination_vault_id=stored_policy_bundle.destination_vault_id,
            expected_nonce=plan_nonce,
            verified_at=_policy_verification_timestamp(),
        )
        parent_ordinal, parent_id, parent_digest = _committed_token_parent(
            root,
            event_id=token_reservation_event_id,
            payload_digest=token_reservation_payload_digest,
            run_id=checked_run,
            operation_id=checked_operation,
            request_digest=checked_request,
            plan_digest=checked_plan,
        )
        sealing_authority = consolidation_authority.issue_authority(
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            phase="sealing",
            action="apply",
        )
        base_revision = _apply_base_revision(
            admission.reload().state,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            sealed_at=sealed_at,
        )
        seal_facts = {
            "vault_binding_digest": checked_vault,
            "run_id": checked_run,
            "operation_id": checked_operation,
            "journal_digest": checked_journal,
            "sealed_at": sealed_at,
            "expected_revision": base_revision,
        }
        seal_prior = _effect_digest("seal-intent", "prior", seal_facts)
        seal_target = _effect_digest("seal-intent", "target", seal_facts)
        seal_basis = _effect_digest("seal-intent", "basis", seal_facts)

        def classify_seal_intent() -> consolidation_effect_coordinator.EffectObservation:
            state = admission.reload().state
            if (
                state.kind == "open"
                and state.revision == base_revision
                and state.vault_binding_digest == checked_vault
            ):
                return _observation("prior", seal_prior)
            if _apply_lineage_matches(
                state,
                minimum_phase="sealing",
                base_revision=base_revision,
                vault_binding_digest=checked_vault,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                sealed_at=sealed_at,
            ):
                return _observation("target", seal_target)
            return _observation("mixed", state.state_digest)

        seal_intent_event = consolidation_receipts.build_intent(
            kind="seal-intent",
            run_id=checked_run,
            operation_id=checked_operation,
            phase="sealing",
            effect_ordinal=parent_ordinal + 1,
            request_digest=checked_request,
            prior_digest=seal_prior,
            target_digest=seal_target,
            evidence=consolidation_receipts.build_evidence(
                kind="seal-intent",
                digests={"seal_basis_digest": seal_basis},
            ),
            semantic_parent_event_id=parent_id,
            semantic_parent_payload_digest=parent_digest,
        )
        seal_intent = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=root,
            event=seal_intent_event,
            journal=consolidation_effect_coordinator.ConsolidationEffectJournalStore(
                root,
                run_id=checked_run,
                effect_ordinal=parent_ordinal + 1,
            ),
            classify=classify_seal_intent,
            apply_effect=lambda: admission.begin_seal(
                control=control,
                authority=sealing_authority,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                sealed_at=sealed_at,
                expected_revision=base_revision,
                timeout=timeout,
            ),
            timestamp=sealed_at,
        )

        drain_facts = {**seal_facts, "drained_at": drained_at}
        drain_prior = _effect_digest("seal-drained", "prior", drain_facts)
        drain_target = _effect_digest("seal-drained", "target", drain_facts)
        drain_digest = _effect_digest("seal-drained", "drain", drain_facts)

        def classify_drained() -> consolidation_effect_coordinator.EffectObservation:
            state = admission.reload().state
            if _seal_matches(
                state,
                phase="sealing",
                revision=base_revision + 1,
                vault_binding_digest=checked_vault,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                sealed_at=sealed_at,
                recorded_at=sealed_at,
            ):
                return _observation("prior", drain_prior)
            if _apply_lineage_matches(
                state,
                minimum_phase="sealed",
                base_revision=base_revision,
                vault_binding_digest=checked_vault,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                sealed_at=sealed_at,
            ):
                return _observation("target", drain_target)
            return _observation("mixed", state.state_digest)

        drain_event = consolidation_receipts.build_intent(
            kind="seal-drained",
            run_id=checked_run,
            operation_id=checked_operation,
            phase="sealed",
            effect_ordinal=parent_ordinal + 2,
            request_digest=checked_request,
            prior_digest=drain_prior,
            target_digest=drain_target,
            evidence=consolidation_receipts.build_evidence(
                kind="seal-drained",
                digests={
                    "drain_digest": drain_digest,
                    "seal_basis_digest": seal_basis,
                },
            ),
            semantic_parent_event_id=seal_intent.terminal.event_id,
            semantic_parent_payload_digest=seal_intent.terminal.payload_digest,
        )
        seal_drained = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=root,
            event=drain_event,
            journal=consolidation_effect_coordinator.ConsolidationEffectJournalStore(
                root,
                run_id=checked_run,
                effect_ordinal=parent_ordinal + 2,
            ),
            classify=classify_drained,
            apply_effect=lambda: admission.drain_and_seal(
                control=control,
                authority=sealing_authority,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                sealed_at=sealed_at,
                completed_at=drained_at,
                expected_revision=base_revision + 1,
                timeout=timeout,
            ),
            timestamp=drained_at,
        )

        binding = consolidation_preimage.DestinationPreimageBinding(
            run_id=checked_run,
            operation_id=checked_operation,
            plan_digest=checked_plan,
            control_basis_digest=checked_basis,
            semantic_predecessor_event_id=seal_drained.terminal.event_id,
            semantic_predecessor_digest=seal_drained.terminal.payload_digest,
            destination_snapshot_fingerprint=checked_snapshot,
            destination_census_digest=checked_census,
        )
        preimage_plan = consolidation_preimage.plan_local_destination_preimage(
            root,
            binding=binding,
            artifact_store=artifact_store,
            now=now,
        )
        preimage_facts = {
            "manifest_digest": preimage_plan.manifest_digest,
            "seal_drained_effect_digest": drain_target,
            "preimage_ready_at": preimage_ready_at,
        }
        preimage_prior = _effect_digest("preimage", "prior", preimage_facts)
        preimage_prepared = _effect_digest("preimage", "prepared", preimage_facts)
        preimage_target = _effect_digest("preimage", "target", preimage_facts)
        manifest_ref = f"exomem-consolidation-preimage://sha256/{preimage_plan.manifest_digest}"
        manifest_path = artifact_store.root / "preimages" / f"{preimage_plan.manifest_digest}.json"

        def manifest_is_absent() -> bool:
            try:
                manifest_path.lstat()
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return False

        def verified_preimage() -> consolidation_preimage.DestinationPreimage | None:
            try:
                return consolidation_preimage.verify_destination_preimage(
                    manifest_ref,
                    binding=binding,
                    artifact_store=artifact_store,
                )
            except consolidation_preimage.ConsolidationPreimageUnavailable:
                return None

        def classify_preimage() -> consolidation_effect_coordinator.EffectObservation:
            state = admission.reload().state
            verified = verified_preimage()
            if _seal_matches(
                state,
                phase="sealed",
                revision=base_revision + 2,
                vault_binding_digest=checked_vault,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                sealed_at=sealed_at,
                recorded_at=drained_at,
            ):
                if verified is not None:
                    return _observation("prepared", preimage_prepared)
                if manifest_is_absent():
                    return _observation("prior", preimage_prior)
            if (
                _seal_matches(
                    state,
                    phase="preimage-ready",
                    revision=base_revision + 3,
                    vault_binding_digest=checked_vault,
                    run_id=checked_run,
                    operation_id=checked_operation,
                    journal_digest=checked_journal,
                    sealed_at=sealed_at,
                    recorded_at=preimage_ready_at,
                )
                and verified is not None
            ):
                return _observation("target", preimage_target)
            return _observation("mixed", state.state_digest)

        sealed_authority = consolidation_authority.issue_authority(
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            phase="sealed",
            action="apply",
        )

        def advance_preimage_ready() -> None:
            consolidation_seal.ConsolidationSealStore(root).advance_consolidation(
                sealed_authority,
                vault_binding_digest=checked_vault,
                action="apply",
                target_phase="preimage-ready",
                recorded_at=preimage_ready_at,
                expected_revision=base_revision + 2,
            )

        def materialize_and_advance() -> None:
            consolidation_preimage.materialize_planned_destination_preimage(
                root,
                plan=preimage_plan,
                artifact_store=artifact_store,
                now=now,
            )
            advance_preimage_ready()

        preimage_event = consolidation_receipts.build_intent(
            kind="preimage",
            run_id=checked_run,
            operation_id=checked_operation,
            phase="preimage",
            effect_ordinal=parent_ordinal + 3,
            request_digest=checked_request,
            prior_digest=preimage_prior,
            prepared_digest=preimage_prepared,
            target_digest=preimage_target,
            evidence=consolidation_receipts.build_evidence(
                kind="preimage",
                digests={"preimage_manifest_digest": preimage_plan.manifest_digest},
            ),
            semantic_parent_event_id=seal_drained.terminal.event_id,
            semantic_parent_payload_digest=seal_drained.terminal.payload_digest,
        )
        preimage_effect = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=root,
            event=preimage_event,
            journal=consolidation_effect_coordinator.ConsolidationEffectJournalStore(
                root,
                run_id=checked_run,
                effect_ordinal=parent_ordinal + 3,
            ),
            classify=classify_preimage,
            apply_effect=materialize_and_advance,
            resume_effect=advance_preimage_ready,
            timestamp=preimage_ready_at,
        )
        preimage = consolidation_preimage.verify_destination_preimage(
            manifest_ref,
            binding=binding,
            artifact_store=artifact_store,
        )
        seal_state = admission.reload().state
        if seal_state.phase != "preimage-ready":
            _fail()
        return ApplyPreparationResult(
            seal_intent=seal_intent,
            seal_drained=seal_drained,
            preimage_effect=preimage_effect,
            preimage_plan=preimage_plan,
            preimage=preimage,
            seal_state=seal_state,
        )
    except ConsolidationApplyPreparationUnavailable:
        raise
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_authority.ConsolidationAuthorityUnavailable,
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        consolidation_intake.ConsolidationIntakeUnavailable,
        consolidation_plan_store.ConsolidationPlanStoreUnavailable,
        consolidation_policy.DestinationPolicyUnavailable,
        consolidation_preimage.ConsolidationPreimageUnavailable,
        consolidation_receipts.ConsolidationReceiptUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()
