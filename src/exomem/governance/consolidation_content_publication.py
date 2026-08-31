"""Publish one stored consolidation plan's canonical content batches.

The coordinator joins the already committed restrictive policy terminal to the
receipt-first batch executor. Planned bytes are resolved only from the private
content-addressed artifact store by their approved final digest; request data
never supplies publication content.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import NoReturn

from .. import reserved_paths, vault
from . import (
    consolidation_admission,
    consolidation_authority,
    consolidation_batch_journal,
    consolidation_effect_coordinator,
    consolidation_intake,
    consolidation_plan,
    consolidation_plan_store,
    consolidation_receipts,
    consolidation_saga,
    consolidation_seal,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_ALLOWED_ENTRY_PHASES = frozenset({"policy-active", "publishing", "rebuilding"})

__all__ = [
    "ConsolidationContentPublicationResult",
    "ConsolidationContentPublicationUnavailable",
    "publish_stored_content_batches",
]


class ConsolidationContentPublicationUnavailable(RuntimeError):
    """Content-free refusal for changed, corrupt, or ambiguous publication state."""

    code = "CONSOLIDATION_CONTENT_PUBLICATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ConsolidationContentPublicationResult:
    partition_digest: str
    publication_boundary_ordinal: int
    committed_batch_ordinals: tuple[int, ...]
    batch_effects: tuple[consolidation_effect_coordinator.EffectExecutionResult, ...]
    batch_journal: consolidation_batch_journal.ConsolidationBatchJournalState
    seal_state: consolidation_seal.ConsolidationSealState


def _fail() -> NoReturn:
    raise ConsolidationContentPublicationUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _timestamp(value: object) -> str:
    try:
        checked, _parsed = consolidation_plan._timestamp(value)  # noqa: SLF001
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    return checked


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _require_seal(
    state: consolidation_seal.ConsolidationSealState,
    *,
    phases: frozenset[str],
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
) -> consolidation_seal.ConsolidationSealState:
    if (
        not isinstance(state, consolidation_seal.ConsolidationSealState)
        or state.kind != "consolidation-sealed"
        or state.phase not in phases
        or state.vault_binding_digest != vault_binding_digest
        or state.run_id != run_id
        or state.operation_id != operation_id
        or state.journal_digest != journal_digest
    ):
        _fail()
    return state


def _advance(
    *,
    admission: consolidation_admission.ConsolidationAdmission,
    vault_root: Path,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    source_phase: str,
    target_phase: str,
    recorded_at: str,
) -> consolidation_seal.ConsolidationSealState:
    current = _require_seal(
        admission.reload().state,
        phases=frozenset({source_phase, target_phase}),
        vault_binding_digest=vault_binding_digest,
        run_id=run_id,
        operation_id=operation_id,
        journal_digest=journal_digest,
    )
    if current.phase == target_phase:
        if current.recorded_at != recorded_at:
            _fail()
        return current
    authority = consolidation_authority.issue_authority(
        vault_binding_digest=vault_binding_digest,
        run_id=run_id,
        operation_id=operation_id,
        journal_digest=journal_digest,
        phase=source_phase,
        action="apply",
    )
    return consolidation_seal.ConsolidationSealStore(vault_root).advance_consolidation(
        authority,
        vault_binding_digest=vault_binding_digest,
        action="apply",
        target_phase=target_phase,
        recorded_at=recorded_at,
        expected_revision=current.revision,
    )


def _policy_parent(
    vault_root: Path,
    terminal: consolidation_saga.PolicyActivationTerminal,
) -> tuple[int, str, str]:
    try:
        records = consolidation_receipts._active_records(vault_root)  # noqa: SLF001
        nested = consolidation_saga._consolidation_receipt_event(  # noqa: SLF001
            records,
            event_id=terminal.terminal_event_id,
            phase="committed",
        )
    except (
        consolidation_receipts.ConsolidationReceiptUnavailable,
        consolidation_saga.PolicyFirstPublicationUnavailable,
    ):
        _fail()
    if (
        nested["kind"] != "policy-active"
        or nested["observed_digest"] != terminal.active_fingerprint
    ):
        _fail()
    ordinal = nested["effect_ordinal"]
    if type(ordinal) is not int or ordinal < 0:
        _fail()
    return ordinal, terminal.terminal_event_id, _digest(nested["payload_digest"])


def _artifact_writes(
    *,
    vault_root: Path,
    artifact_store: consolidation_intake.PrivateConsolidationArtifactStore,
    actions: tuple[Mapping[str, object], ...],
    stack: ExitStack,
) -> tuple[vault.PlannedWrite, ...]:
    writes: list[vault.PlannedWrite] = []
    for action in actions:
        before = action["expected_before_state"]
        after = action["planned_after_state"]
        before_digest = _digest(action["expected_before_sha256"])
        after_digest = _digest(action["planned_after_sha256"])
        if after != "present" or (before == after and before_digest == after_digest):
            continue
        reference = f"exomem-consolidation-object://sha256/{after_digest}"
        source = artifact_store.resolve_object(reference)
        relative = PurePosixPath(str(action["destination_path"]))
        try:
            if relative.suffix.casefold() == ".md":
                content: str | vault.PreparedBinaryContent = source.read_bytes().decode("utf-8")
            else:
                size = source.stat().st_size
                stream = stack.enter_context(source.open("rb"))
                content = vault.PreparedBinaryContent(
                    stream=stream,
                    size=size,
                    sha256=after_digest,
                )
        except (OSError, UnicodeDecodeError):
            _fail()
        writes.append(
            vault.PlannedWrite(
                path=vault_root.joinpath(*relative.parts),
                content=content,
                create_only=before == "absent",
                expected_hash=(vault.MISSING_CONTENT_HASH if before == "absent" else before_digest),
            )
        )
    return tuple(writes)


def _batch_event(
    *,
    batch: consolidation_saga.ContentBatch,
    run_id: str,
    operation_id: str,
    request_digest: str,
    effect_ordinal: int,
    parent_event_id: str,
    parent_payload_digest: str,
) -> consolidation_receipts.ConsolidationEvent:
    return consolidation_receipts.build_intent(
        kind="content-batch",
        run_id=run_id,
        operation_id=operation_id,
        phase="publishing",
        effect_ordinal=effect_ordinal,
        batch_ordinal=batch.ordinal,
        request_digest=request_digest,
        prior_digest=batch.prior_fingerprint,
        prepared_digest=batch.prepared_fingerprint,
        target_digest=batch.final_fingerprint,
        evidence=consolidation_receipts.build_evidence(
            kind="content-batch",
            digests={
                "batch_manifest_digest": batch.action_set_digest,
                "classification_digest": (
                    consolidation_saga._content_batch_classification_digest(  # noqa: SLF001
                        batch
                    )
                ),
            },
        ),
        semantic_parent_event_id=parent_event_id,
        semantic_parent_payload_digest=parent_payload_digest,
    )


def _materialize_approved_batch(
    selected: consolidation_saga.ContentBatch,
    *,
    batch: consolidation_saga.ContentBatch,
    batch_store: consolidation_batch_journal.ConsolidationBatchJournalStore,
    actions: tuple[Mapping[str, object], ...],
    vault_root: Path,
    artifact_store: consolidation_intake.PrivateConsolidationArtifactStore,
    stack: ExitStack,
) -> tuple[vault.PlannedWrite, ...]:
    if selected != batch:
        _fail()
    status = batch_store.batch_status(batch)
    if status == "prior":
        batch_store.prepare_batch(batch)
    elif status != "prepared":
        _fail()
    selected_actions = tuple(
        action for action in actions if action["batch_ordinal"] == batch.ordinal
    )
    return _artifact_writes(
        vault_root=vault_root,
        artifact_store=artifact_store,
        actions=selected_actions,
        stack=stack,
    )


def _rehydrate_completed_batches(
    *,
    vault_root: Path,
    actions: tuple[Mapping[str, object], ...],
    batches: tuple[consolidation_saga.ContentBatch, ...],
    batch_state: consolidation_batch_journal.ConsolidationBatchJournalState,
    run_id: str,
    operation_id: str,
    request_digest: str,
    parent_ordinal: int,
    parent_id: str,
    parent_digest: str,
) -> tuple[consolidation_effect_coordinator.EffectExecutionResult, ...]:
    if not batch_state.publication_boundary_committed or any(
        item.status != "final" for item in batch_state.batches
    ):
        _fail()
    effects: list[consolidation_effect_coordinator.EffectExecutionResult] = []
    for batch in batches:
        effect_ordinal = parent_ordinal + batch.ordinal + 1
        event = _batch_event(
            batch=batch,
            run_id=run_id,
            operation_id=operation_id,
            request_digest=request_digest,
            effect_ordinal=effect_ordinal,
            parent_event_id=parent_id,
            parent_payload_digest=parent_digest,
        )
        state = consolidation_effect_coordinator.ConsolidationEffectJournalStore(
            vault_root,
            run_id=run_id,
            effect_ordinal=effect_ordinal,
        ).load()
        if (
            state.status != "final"
            or state.kind != "content-batch"
            or state.operation_id != operation_id
            or state.intent.event_id != event.event_id
            or state.intent.payload_digest != event.payload_digest
            or dict(state.intent_payload) != dict(event.payload)
            or state.terminal is None
            or state.terminal.event_id != f"{event.event_id}:committed"
            or state.observed_state != "target"
            or state.observed_digest != batch.final_fingerprint
        ):
            _fail()
        observation = consolidation_saga.classify_content_batch_state(
            vault_root=vault_root,
            content_actions=actions,
            batch=batch,
        )
        if observation.state not in {"final", "equivalent"}:
            _fail()
        effect = consolidation_effect_coordinator._result(state)  # noqa: SLF001
        effects.append(effect)
        parent_id = effect.terminal.event_id
        parent_digest = effect.terminal.payload_digest
    return tuple(effects)


def publish_stored_content_batches(
    *,
    vault_root: Path | str,
    admission: consolidation_admission.ConsolidationAdmission,
    artifact_store: consolidation_intake.PrivateConsolidationArtifactStore,
    policy_terminal: consolidation_saga.PolicyActivationTerminal,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    request_digest: str,
    plan_digest: str,
    publishing_at: str,
    rebuilding_at: str,
) -> ConsolidationContentPublicationResult:
    """Publish exact private artifacts after policy activation, then enter rebuild."""

    root = Path(vault_root).absolute()
    checked_vault = _digest(vault_binding_digest)
    checked_run = _uuid4(run_id)
    checked_operation = _uuid4(operation_id)
    checked_journal = _digest(journal_digest)
    checked_request = _digest(request_digest)
    checked_plan = _digest(plan_digest)
    publishing_time = _timestamp(publishing_at)
    rebuilding_time = _timestamp(rebuilding_at)
    if (
        not isinstance(admission, consolidation_admission.ConsolidationAdmission)
        or admission.vault_root != root
        or admission.vault_binding_digest != checked_vault
        or not isinstance(
            artifact_store,
            consolidation_intake.PrivateConsolidationArtifactStore,
        )
        or publishing_time >= rebuilding_time
    ):
        _fail()
    try:
        resolved_root = root.resolve(strict=False)
        resolved_artifact_root = artifact_store.root.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        _fail()
    if _paths_overlap(resolved_root, resolved_artifact_root):
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
        expected_policy = _digest(preimage.get("prospective_policy_fingerprint"))
        terminal = consolidation_saga._verify_policy_terminal_receipt(  # noqa: SLF001
            vault_root=root,
            vault_binding_digest=checked_vault,
            terminal=policy_terminal,
            expected_policy_fingerprint=expected_policy,
            allowed_seal_phases=_ALLOWED_ENTRY_PHASES,
        )
        parent_ordinal, parent_id, parent_digest = _policy_parent(root, terminal)
        current = _require_seal(
            admission.reload().state,
            phases=_ALLOWED_ENTRY_PHASES,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
        )
        batch_store = consolidation_batch_journal.ConsolidationBatchJournalStore(
            root,
            run_id=checked_run,
        )
        if current.phase != "policy-active":
            batch_store.load()
        batch_store.create(
            operation_id=checked_operation,
            request_digest=checked_request,
            partition=partition,
        )
        batches = consolidation_saga._content_batches(partition)  # noqa: SLF001
        if current.phase == "rebuilding":
            if current.recorded_at != rebuilding_time:
                _fail()
            batch_state = batch_store.load()
            effects = _rehydrate_completed_batches(
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
            return ConsolidationContentPublicationResult(
                partition_digest=partition.digest,
                publication_boundary_ordinal=batch_state.publication_boundary_ordinal,
                committed_batch_ordinals=tuple(batch.ordinal for batch in batches),
                batch_effects=effects,
                batch_journal=batch_state,
                seal_state=current,
            )
        if current.phase == "policy-active":
            _advance(
                admission=admission,
                vault_root=root,
                vault_binding_digest=checked_vault,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                source_phase="policy-active",
                target_phase="publishing",
                recorded_at=publishing_time,
            )
        elif current.phase == "publishing":
            if current.recorded_at != publishing_time:
                _fail()
        else:
            _fail()

        effects: list[consolidation_effect_coordinator.EffectExecutionResult] = []
        committed: list[int] = []
        for batch in batches:
            effect_ordinal = parent_ordinal + batch.ordinal + 1
            event = _batch_event(
                batch=batch,
                run_id=checked_run,
                operation_id=checked_operation,
                request_digest=checked_request,
                effect_ordinal=effect_ordinal,
                parent_event_id=parent_id,
                parent_payload_digest=parent_digest,
            )
            effect_store = consolidation_effect_coordinator.ConsolidationEffectJournalStore(
                root,
                run_id=checked_run,
                effect_ordinal=effect_ordinal,
            )
            coarse_status = batch_store.batch_status(batch)
            if coarse_status in {"prepared", "final"}:
                existing_effect = effect_store.load()
                if existing_effect.kind != "content-batch" or existing_effect.status not in (
                    {"prepared", "final"} if coarse_status == "prepared" else {"final"}
                ):
                    _fail()
            with ExitStack() as stack:
                effect = consolidation_saga.publish_content_batch_receipt_first(
                    content_actions=actions,
                    batch=batch,
                    event=event,
                    journal=effect_store,
                    vault_root=root,
                    materialize_batch=partial(
                        _materialize_approved_batch,
                        batch=batch,
                        batch_store=batch_store,
                        actions=actions,
                        vault_root=root,
                        artifact_store=artifact_store,
                        stack=stack,
                    ),
                    timestamp=publishing_time,
                )
            if (
                effect.role != "committed"
                or effect.observed_state != "target"
                or effect.observed_digest != batch.final_fingerprint
                or effect.intent.event_id != event.event_id
            ):
                _fail()
            status = batch_store.batch_status(batch)
            if status == "prior":
                batch_store.prepare_batch(batch)
                status = "prepared"
            if status == "prepared":
                batch_store.commit_batch(batch)
            elif status != "final":
                _fail()
            effects.append(effect)
            committed.append(batch.ordinal)
            parent_id = effect.terminal.event_id
            parent_digest = effect.terminal.payload_digest

        batch_state = batch_store.load()
        if not batch_state.publication_boundary_committed or any(
            item.status != "final" for item in batch_state.batches
        ):
            _fail()
        seal_state = _advance(
            admission=admission,
            vault_root=root,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            source_phase="publishing",
            target_phase="rebuilding",
            recorded_at=rebuilding_time,
        )
        return ConsolidationContentPublicationResult(
            partition_digest=partition.digest,
            publication_boundary_ordinal=batch_state.publication_boundary_ordinal,
            committed_batch_ordinals=tuple(committed),
            batch_effects=tuple(effects),
            batch_journal=batch_state,
            seal_state=seal_state,
        )
    except ConsolidationContentPublicationUnavailable:
        raise
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_authority.ConsolidationAuthorityUnavailable,
        consolidation_batch_journal.ConsolidationBatchJournalUnavailable,
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        consolidation_intake.ConsolidationIntakeUnavailable,
        consolidation_plan.ConsolidationPlanUnavailable,
        consolidation_plan_store.ConsolidationPlanStoreUnavailable,
        consolidation_receipts.ConsolidationReceiptUnavailable,
        consolidation_saga.PolicyFirstPublicationUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        reserved_paths.ReservedPathLeafError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()
