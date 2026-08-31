"""Durably coordinate the post-publication derivative rebuild.

The coordinator proves the exact content publication chain is final before it
samples the destination's post-publication canonical census.  It then rebuilds
the closed derivative set through one learned-result receipt per component.
Actual artifact fingerprints are persisted before their terminal receipts, so
a retry can finish receipt evidence without rerunning a completed component.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from . import (
    consolidation_admission,
    consolidation_authority,
    consolidation_batch_journal,
    consolidation_content_publication,
    consolidation_effect_coordinator,
    consolidation_fingerprints,
    consolidation_plan,
    consolidation_plan_store,
    consolidation_rebuild,
    consolidation_rebuild_adapters,
    consolidation_rebuild_journal,
    consolidation_receipts,
    consolidation_saga,
    consolidation_seal,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_ALLOWED_ENTRY_PHASES = frozenset({"rebuilding", "verifying"})
_CONTENT_EFFECTS_SCHEMA = "exomem.consolidation-content-effects/v1"
_CONTENT_EFFECTS_DOMAIN = _CONTENT_EFFECTS_SCHEMA.encode("ascii")
_REBUILD_BASIS_SCHEMA = "exomem.consolidation-rebuild-basis/v1"
_REBUILD_BASIS_DOMAIN = _REBUILD_BASIS_SCHEMA.encode("ascii")
_REBUILD_PRIOR_SCHEMA = "exomem.consolidation-rebuild-prior/v1"
_REBUILD_PRIOR_DOMAIN = _REBUILD_PRIOR_SCHEMA.encode("ascii")
_REBUILD_TARGET_SCHEMA = "exomem.consolidation-rebuild-target/v1"
_REBUILD_TARGET_DOMAIN = _REBUILD_TARGET_SCHEMA.encode("ascii")

__all__ = [
    "ConsolidationRebuildCoordinatorResult",
    "ConsolidationRebuildCoordinatorUnavailable",
    "rebuild_published_destination",
]


class ConsolidationRebuildCoordinatorUnavailable(RuntimeError):
    """Content-free refusal for an incomplete or contradictory rebuild."""

    code = "CONSOLIDATION_REBUILD_COORDINATOR_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ConsolidationRebuildCoordinatorResult:
    canonical_census_digest: str
    completed_components: tuple[str, ...]
    rebuild_effects: tuple[consolidation_effect_coordinator.EffectExecutionResult, ...]
    rebuild_journal: consolidation_rebuild_journal.ConsolidationRebuildJournalState
    seal_state: consolidation_seal.ConsolidationSealState


def _fail() -> NoReturn:
    raise ConsolidationRebuildCoordinatorUnavailable from None


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if type(value) is not str or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _timestamp(value: object) -> str:
    try:
        checked, _parsed = consolidation_plan._timestamp(value)  # noqa: SLF001
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    return checked


def _framed_digest(domain: bytes, value: object) -> str:
    try:
        payload = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = len(domain).to_bytes(4, "big") + domain + len(payload).to_bytes(8, "big") + payload
    return hashlib.sha256(framed).hexdigest()


def _snapshot_census(vault_root: Path) -> str:
    now = int(time.time())
    if now < 0:
        _fail()
    return _digest(
        consolidation_fingerprints.load_local_destination_snapshot(
            vault_root,
            now=now,
        ).canonical_census_digest
    )


def _component_rebuilder():
    return consolidation_rebuild_adapters.destination_component_rebuilder()


def _crash_point(_point: str) -> None:
    """Narrow test seam after durable rebuild completion and before phase advance."""


def _require_seal(
    state: consolidation_seal.ConsolidationSealState,
    *,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
) -> consolidation_seal.ConsolidationSealState:
    if (
        not isinstance(state, consolidation_seal.ConsolidationSealState)
        or state.kind != "consolidation-sealed"
        or state.phase not in _ALLOWED_ENTRY_PHASES
        or state.vault_binding_digest != vault_binding_digest
        or state.run_id != run_id
        or state.operation_id != operation_id
        or state.journal_digest != journal_digest
    ):
        _fail()
    return state


def _advance_to_verifying(
    *,
    admission: consolidation_admission.ConsolidationAdmission,
    vault_root: Path,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    recorded_at: str,
) -> consolidation_seal.ConsolidationSealState:
    current = _require_seal(
        admission.reload().state,
        vault_binding_digest=vault_binding_digest,
        run_id=run_id,
        operation_id=operation_id,
        journal_digest=journal_digest,
    )
    if current.phase == "verifying":
        if current.recorded_at != recorded_at:
            _fail()
        return current
    authority = consolidation_authority.issue_authority(
        vault_binding_digest=vault_binding_digest,
        run_id=run_id,
        operation_id=operation_id,
        journal_digest=journal_digest,
        phase="rebuilding",
        action="apply",
    )
    return consolidation_seal.ConsolidationSealStore(vault_root).advance_consolidation(
        authority,
        vault_binding_digest=vault_binding_digest,
        action="apply",
        target_phase="verifying",
        recorded_at=recorded_at,
        expected_revision=current.revision,
    )


def _content_effects_digest(
    effects: tuple[consolidation_effect_coordinator.EffectExecutionResult, ...],
) -> str:
    if not effects:
        _fail()
    rows: list[dict[str, object]] = []
    for effect in effects:
        if (
            not isinstance(
                effect,
                consolidation_effect_coordinator.EffectExecutionResult,
            )
            or effect.role != "committed"
            or effect.observed_state != "target"
        ):
            _fail()
        rows.append(
            {
                "intent_event_id": effect.intent.event_id,
                "intent_payload_digest": _digest(effect.intent.payload_digest),
                "terminal_event_id": effect.terminal.event_id,
                "terminal_payload_digest": _digest(effect.terminal.payload_digest),
                "observed_digest": _digest(effect.observed_digest),
                "effect_journal_digest": _digest(effect.journal_digest),
            }
        )
    return _framed_digest(
        _CONTENT_EFFECTS_DOMAIN,
        {"schema": _CONTENT_EFFECTS_SCHEMA, "effects": rows},
    )


def _rebuild_digests(
    *,
    state: consolidation_rebuild_journal.ConsolidationRebuildJournalState,
    component: str,
    rebuild_ordinal: int,
) -> tuple[str, str, str]:
    basis = _framed_digest(
        _REBUILD_BASIS_DOMAIN,
        {
            "schema": _REBUILD_BASIS_SCHEMA,
            "rebuild_journal_binding_digest": state.binding_digest,
            "canonical_census_digest": state.canonical_census_digest,
            "component": component,
            "rebuild_ordinal": rebuild_ordinal,
        },
    )
    prior = _framed_digest(
        _REBUILD_PRIOR_DOMAIN,
        {"schema": _REBUILD_PRIOR_SCHEMA, "rebuild_basis_digest": basis},
    )
    target = _framed_digest(
        _REBUILD_TARGET_DOMAIN,
        {"schema": _REBUILD_TARGET_SCHEMA, "rebuild_basis_digest": basis},
    )
    return basis, prior, target


def _rebuild_event(
    *,
    state: consolidation_rebuild_journal.ConsolidationRebuildJournalState,
    component: str,
    rebuild_ordinal: int,
    effect_ordinal: int,
    parent_event_id: str,
    parent_payload_digest: str,
) -> consolidation_receipts.ConsolidationEvent:
    basis, prior, target = _rebuild_digests(
        state=state,
        component=component,
        rebuild_ordinal=rebuild_ordinal,
    )
    return consolidation_receipts.build_intent(
        kind="rebuild-kind",
        run_id=state.run_id,
        operation_id=state.operation_id,
        phase="rebuilding",
        effect_ordinal=effect_ordinal,
        rebuild_ordinal=rebuild_ordinal,
        request_digest=state.request_digest,
        prior_digest=prior,
        target_digest=target,
        evidence=consolidation_receipts.build_evidence(
            kind="rebuild-kind",
            digests={"rebuild_basis_digest": basis},
        ),
        semantic_parent_event_id=parent_event_id,
        semantic_parent_payload_digest=parent_payload_digest,
    )


def _validate_existing_effect(
    *,
    entry: consolidation_rebuild_journal.ConsolidationRebuildJournalEntry,
    event: consolidation_receipts.ConsolidationEvent,
    store: consolidation_effect_coordinator.ConsolidationEffectJournalStore,
) -> None:
    current = store.load_optional()
    if entry.status == "prior":
        if current is not None:
            _fail()
        return
    if current is None or (
        current.kind != "rebuild-kind"
        or current.intent.event_id != event.event_id
        or current.intent.payload_digest != event.payload_digest
        or dict(current.intent_payload) != dict(event.payload)
    ):
        _fail()
    if entry.status == "prepared":
        if current.status not in {"prepared", "final"}:
            _fail()
        if current.status == "final" and (
            current.terminal is None
            or current.terminal.event_id.rpartition(":")[2] != "committed"
            or current.observed_state != "target"
            or current.observed_digest != entry.artifact_fingerprint
        ):
            _fail()
        return
    if (
        entry.status != "final"
        or current.status != "final"
        or current.terminal is None
        or current.observed_state != "target"
        or current.observed_digest != entry.artifact_fingerprint
        or current.terminal.event_id != entry.terminal_event_id
        or current.terminal.payload_digest != entry.terminal_payload_digest
        or current.state_digest != entry.effect_journal_digest
    ):
        _fail()


def _execute_rebuilds(
    *,
    vault_root: Path,
    store: consolidation_rebuild_journal.ConsolidationRebuildJournalStore,
    last_content_effect_ordinal: int,
    timestamp: str,
    expected_batch_count: int,
    committed_batch_ordinals: tuple[int, ...],
) -> tuple[
    consolidation_rebuild.DerivativeRebuildResult,
    tuple[consolidation_effect_coordinator.EffectExecutionResult, ...],
]:
    rebuild_component = _component_rebuilder()
    effects: list[consolidation_effect_coordinator.EffectExecutionResult] = []

    def execute(
        component: str,
        context: consolidation_rebuild.DerivativeRebuildContext,
    ) -> consolidation_rebuild.DerivativeRebuildTerminal:
        try:
            ordinal = consolidation_rebuild.DERIVATIVE_COMPONENTS.index(component)
        except ValueError:
            _fail()
        state = store.load()
        if ordinal == 0:
            parent_event_id = state.last_content_terminal_event_id
            parent_payload_digest = state.last_content_terminal_payload_digest
        else:
            parent = state.components[ordinal - 1]
            if (
                parent.status != "final"
                or parent.terminal_event_id is None
                or parent.terminal_payload_digest is None
            ):
                _fail()
            parent_event_id = parent.terminal_event_id
            parent_payload_digest = parent.terminal_payload_digest
        event = _rebuild_event(
            state=state,
            component=component,
            rebuild_ordinal=ordinal,
            effect_ordinal=last_content_effect_ordinal + ordinal + 1,
            parent_event_id=parent_event_id,
            parent_payload_digest=parent_payload_digest,
        )
        effect_store = consolidation_effect_coordinator.ConsolidationEffectJournalStore(
            vault_root,
            run_id=state.run_id,
            effect_ordinal=last_content_effect_ordinal + ordinal + 1,
        )
        entry = state.components[ordinal]
        _validate_existing_effect(entry=entry, event=event, store=effect_store)
        _basis, prior_digest, _target_digest = _rebuild_digests(
            state=state,
            component=component,
            rebuild_ordinal=ordinal,
        )

        def classify() -> consolidation_effect_coordinator.EffectObservation:
            current = store.load().components[ordinal]
            if current.status == "prior":
                return consolidation_effect_coordinator.EffectObservation(
                    state="prior",
                    digest=prior_digest,
                )
            if current.status not in {"prepared", "final"}:
                _fail()
            return consolidation_effect_coordinator.EffectObservation(
                state="target",
                digest=_digest(current.artifact_fingerprint),
            )

        def apply() -> None:
            terminal = consolidation_rebuild._terminal(  # noqa: SLF001
                rebuild_component(component, context),
                component=component,
                canonical_census_digest=context.canonical_census_digest,
            )
            store.record_component_result(component, terminal.artifact_fingerprint)

        effect = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault_root,
            event=event,
            journal=effect_store,
            classify=classify,
            apply_effect=apply,
            timestamp=timestamp,
        )
        if (
            effect.role != "committed"
            or effect.observed_state != "target"
            or effect.intent.event_id != event.event_id
        ):
            _fail()
        current = store.load().components[ordinal]
        if (
            current.status not in {"prepared", "final"}
            or current.artifact_fingerprint != effect.observed_digest
        ):
            _fail()
        store.finalize_component(
            component,
            effect.observed_digest,
            terminal_event_id=effect.terminal.event_id,
            terminal_payload_digest=effect.terminal.payload_digest,
            effect_journal_digest=effect.journal_digest,
        )
        effects.append(effect)
        return consolidation_rebuild.DerivativeRebuildTerminal(
            schema=consolidation_rebuild.DERIVATIVE_REBUILD_TERMINAL_SCHEMA,
            component=component,
            canonical_census_digest=context.canonical_census_digest,
            artifact_fingerprint=effect.observed_digest,
        )

    state = store.load()
    result = consolidation_rebuild.rebuild_destination_derivatives(
        vault_root=vault_root,
        expected_canonical_census_digest=state.canonical_census_digest,
        expected_batch_count=expected_batch_count,
        committed_batch_ordinals=committed_batch_ordinals,
        snapshot_census=_snapshot_census,
        rebuild_component=execute,
    )
    return result, tuple(effects)


def rebuild_published_destination(
    *,
    vault_root: Path | str,
    admission: consolidation_admission.ConsolidationAdmission,
    policy_terminal: consolidation_saga.PolicyActivationTerminal,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    request_digest: str,
    plan_digest: str,
    verifying_at: str,
) -> ConsolidationRebuildCoordinatorResult:
    """Rebuild every destination derivative and enter the verifying phase."""

    root = Path(vault_root).absolute()
    checked_vault = _digest(vault_binding_digest)
    checked_run = _uuid4(run_id)
    checked_operation = _uuid4(operation_id)
    checked_journal = _digest(journal_digest)
    checked_request = _digest(request_digest)
    checked_plan = _digest(plan_digest)
    verifying_time = _timestamp(verifying_at)
    if (
        not isinstance(admission, consolidation_admission.ConsolidationAdmission)
        or admission.vault_root != root
        or admission.vault_binding_digest != checked_vault
    ):
        _fail()
    try:
        stored = consolidation_plan_store.ConsolidationPlanStore(root).load(
            checked_run,
            plan_kind="cutover",
            plan_digest=checked_plan,
        )
        preimage = stored.preimage
        if (
            stored.digest != checked_plan
            or not isinstance(preimage, Mapping)
            or preimage.get("run_id") != checked_run
            or preimage.get("plan_kind") != "cutover"
        ):
            _fail()
        actions = consolidation_plan.validate_content_actions(preimage.get("content_actions"))
        partition = consolidation_plan.derive_journal_batch_partition(actions)
        if partition.digest != _digest(preimage.get("journal_batch_partition_digest")):
            _fail()
        terminal = consolidation_saga._verify_policy_terminal_receipt(  # noqa: SLF001
            vault_root=root,
            vault_binding_digest=checked_vault,
            terminal=policy_terminal,
            expected_policy_fingerprint=_digest(preimage.get("prospective_policy_fingerprint")),
            allowed_seal_phases=_ALLOWED_ENTRY_PHASES,
        )
        parent_ordinal, parent_id, parent_digest = consolidation_content_publication._policy_parent(  # noqa: SLF001
            root,
            terminal,
        )
        current = _require_seal(
            admission.reload().state,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
        )
        if current.phase == "verifying":
            if current.recorded_at != verifying_time:
                _fail()
        elif current.recorded_at >= verifying_time:
            _fail()
        batch_state = consolidation_batch_journal.ConsolidationBatchJournalStore(
            root,
            run_id=checked_run,
        ).load()
        if (
            batch_state.operation_id != checked_operation
            or batch_state.request_digest != checked_request
            or batch_state.partition_digest != partition.digest
        ):
            _fail()
        batches = consolidation_saga._content_batches(partition)  # noqa: SLF001
        content_effects = consolidation_content_publication._rehydrate_completed_batches(  # noqa: SLF001
            vault_root=root,
            actions=actions,
            batches=batches,
            batch_state=batch_state,
            run_id=checked_run,
            operation_id=checked_operation,
            request_digest=checked_request,
            parent_ordinal=parent_ordinal,
            parent_id=parent_id,
            parent_digest=parent_digest,
        )
        if len(content_effects) != len(batches) or not content_effects:
            _fail()
        content_effects_digest = _content_effects_digest(content_effects)
        last_content = content_effects[-1]
        rebuild_store = consolidation_rebuild_journal.ConsolidationRebuildJournalStore(
            root,
            run_id=checked_run,
        )
        if current.phase == "rebuilding":
            canonical_census_digest = _snapshot_census(root)
            rebuild_state = rebuild_store.create(
                operation_id=checked_operation,
                request_digest=checked_request,
                plan_digest=checked_plan,
                partition_digest=partition.digest,
                content_batch_journal_digest=batch_state.state_digest,
                content_effects_digest=content_effects_digest,
                last_content_terminal_event_id=last_content.terminal.event_id,
                last_content_terminal_payload_digest=(last_content.terminal.payload_digest),
                canonical_census_digest=canonical_census_digest,
            )
        else:
            rebuild_state = rebuild_store.load()
            if (
                rebuild_state.operation_id != checked_operation
                or rebuild_state.request_digest != checked_request
                or rebuild_state.plan_digest != checked_plan
                or rebuild_state.partition_digest != partition.digest
                or rebuild_state.content_batch_journal_digest != batch_state.state_digest
                or rebuild_state.content_effects_digest != content_effects_digest
                or rebuild_state.last_content_terminal_event_id != last_content.terminal.event_id
                or rebuild_state.last_content_terminal_payload_digest
                != last_content.terminal.payload_digest
            ):
                _fail()
        rebuilt, effects = _execute_rebuilds(
            vault_root=root,
            store=rebuild_store,
            last_content_effect_ordinal=parent_ordinal + len(batches),
            timestamp=verifying_time,
            expected_batch_count=len(batches),
            committed_batch_ordinals=tuple(batch.ordinal for batch in batches),
        )
        final_journal = rebuild_store.load()
        if (
            rebuilt.canonical_census_digest != final_journal.canonical_census_digest
            or rebuilt.completed_components != consolidation_rebuild.DERIVATIVE_COMPONENTS
            or len(effects) != len(consolidation_rebuild.DERIVATIVE_COMPONENTS)
            or any(entry.status != "final" for entry in final_journal.components)
        ):
            _fail()
        _crash_point("before-verifying")
        seal_state = _advance_to_verifying(
            admission=admission,
            vault_root=root,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            recorded_at=verifying_time,
        )
        return ConsolidationRebuildCoordinatorResult(
            canonical_census_digest=rebuilt.canonical_census_digest,
            completed_components=rebuilt.completed_components,
            rebuild_effects=effects,
            rebuild_journal=final_journal,
            seal_state=seal_state,
        )
    except ConsolidationRebuildCoordinatorUnavailable:
        raise
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_authority.ConsolidationAuthorityUnavailable,
        consolidation_batch_journal.ConsolidationBatchJournalUnavailable,
        consolidation_content_publication.ConsolidationContentPublicationUnavailable,
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        consolidation_fingerprints.ConsolidationFingerprintUnavailable,
        consolidation_plan.ConsolidationPlanUnavailable,
        consolidation_plan_store.ConsolidationPlanStoreUnavailable,
        consolidation_rebuild.DerivativeRebuildUnavailable,
        consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable,
        consolidation_receipts.ConsolidationReceiptUnavailable,
        consolidation_saga.PolicyFirstPublicationUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()
