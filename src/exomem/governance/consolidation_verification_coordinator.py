"""Receipt-first coordinator for plan-bound in-process verification."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from . import (
    consolidation_admission,
    consolidation_authority,
    consolidation_effect_coordinator,
    consolidation_fingerprints,
    consolidation_plan,
    consolidation_plan_store,
    consolidation_rebuild,
    consolidation_rebuild_journal,
    consolidation_receipts,
    consolidation_runtime,
    consolidation_seal,
    consolidation_verification,
    consolidation_verification_journal,
    consolidation_verification_manifest,
    consolidation_verification_registry,
)
from . import (
    principal as governance_principal,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_ENTRY_PHASES = frozenset({"verifying", "verified"})
_PROBE_PRIOR_SCHEMA = "exomem.consolidation-probe-prior/v1"
_PROBE_PRIOR_DOMAIN = _PROBE_PRIOR_SCHEMA.encode("ascii")
_VERIFIED_PRIOR_SCHEMA = "exomem.consolidation-in-process-verified-prior/v1"
_VERIFIED_PRIOR_DOMAIN = _VERIFIED_PRIOR_SCHEMA.encode("ascii")
_VERIFICATION_RESULT_SCHEMA = "exomem.consolidation-verification-result/v1"
_VERIFICATION_RESULT_DOMAIN = _VERIFICATION_RESULT_SCHEMA.encode("ascii")

__all__ = [
    "ConsolidationVerificationCoordinatorResult",
    "ConsolidationVerificationCoordinatorUnavailable",
    "verify_rebuilt_destination",
]


class ConsolidationVerificationCoordinatorUnavailable(RuntimeError):
    """Content-free refusal for incomplete, changed, or failed verification."""

    code = "CONSOLIDATION_VERIFICATION_COORDINATOR_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ConsolidationVerificationCoordinatorResult:
    verification_basis_digest: str
    verification_result_digest: str
    completed_probe_ids: tuple[str, ...]
    probe_effects: tuple[consolidation_effect_coordinator.EffectExecutionResult, ...]
    verification_effect: consolidation_effect_coordinator.EffectExecutionResult
    verification_journal: consolidation_verification_journal.ConsolidationVerificationJournalState
    seal_state: consolidation_seal.ConsolidationSealState


def _fail() -> NoReturn:
    raise ConsolidationVerificationCoordinatorUnavailable from None


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
        encoded = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = len(domain).to_bytes(4, "big") + domain + len(encoded).to_bytes(8, "big") + encoded
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


def _crash_point(_point: str) -> None:
    """Narrow seam after durable verification and before phase advance."""


def _canonical_surface_probe_runner(
    probe: consolidation_verification.VerificationProbe,
    context: consolidation_verification.VerificationProbeContext,
) -> consolidation_verification.VerificationProbeTerminal:
    """Run one exact contract through the fixed canonical surface registry."""

    return consolidation_verification_registry.run_probe(probe, context)


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
        or state.phase not in _ENTRY_PHASES
        or state.vault_binding_digest != vault_binding_digest
        or state.run_id != run_id
        or state.operation_id != operation_id
        or state.journal_digest != journal_digest
    ):
        _fail()
    return state


def _advance_to_verified(
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
    if current.phase == "verified":
        if current.recorded_at != recorded_at:
            _fail()
        return current
    authority = consolidation_authority.issue_authority(
        vault_binding_digest=vault_binding_digest,
        run_id=run_id,
        operation_id=operation_id,
        journal_digest=journal_digest,
        phase="verifying",
        action="apply",
    )
    return consolidation_seal.ConsolidationSealStore(vault_root).advance_consolidation(
        authority,
        vault_binding_digest=vault_binding_digest,
        action="apply",
        target_phase="verified",
        recorded_at=recorded_at,
        expected_revision=current.revision,
    )


def _plan_verification(
    value: object,
) -> tuple[str, str]:
    if not isinstance(value, Mapping) or frozenset(value) != {
        "schema",
        "positive_probe_digest",
        "negative_probe_digest",
    }:
        _fail()
    if value["schema"] != "exomem.consolidation-verification-plan/v1":
        _fail()
    return _digest(value["positive_probe_digest"]), _digest(value["negative_probe_digest"])


def _last_rebuild_receipt(
    vault_root: Path,
    state: consolidation_rebuild_journal.ConsolidationRebuildJournalState,
) -> tuple[int, str, str]:
    if len(state.components) != len(consolidation_rebuild.DERIVATIVE_COMPONENTS) or any(
        entry.status != "final" for entry in state.components
    ):
        _fail()
    last = state.components[-1]
    if last.terminal_event_id is None or last.terminal_payload_digest is None:
        _fail()
    try:
        matching = [
            record
            for record in consolidation_receipts._active_records(vault_root)  # noqa: SLF001
            if record.get("event_type") == "consolidation"
            and record.get("phase") == "committed"
            and record.get("event_id") == last.terminal_event_id
        ]
        if len(matching) != 1:
            _fail()
        nested = consolidation_receipts.validate_nested(
            matching[0].get("consolidation_event"),
            outer_phase="committed",
        )
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    effect_ordinal = nested.get("effect_ordinal")
    if (
        nested.get("kind") != "rebuild-kind"
        or nested.get("rebuild_ordinal") != len(consolidation_rebuild.DERIVATIVE_COMPONENTS) - 1
        or nested.get("payload_digest") != last.terminal_payload_digest
        or type(effect_ordinal) is not int
        or effect_ordinal < 0
    ):
        _fail()
    return effect_ordinal, last.terminal_event_id, last.terminal_payload_digest


def _probe_prior_digest(
    state: consolidation_verification_journal.ConsolidationVerificationJournalState,
    probe: consolidation_verification.VerificationProbe,
) -> str:
    return _framed_digest(
        _PROBE_PRIOR_DOMAIN,
        {
            "schema": _PROBE_PRIOR_SCHEMA,
            "verification_basis_digest": state.binding_digest,
            "probe_ordinal": probe.ordinal,
            "probe_digest": probe.probe_digest,
        },
    )


def _probe_event(
    *,
    state: consolidation_verification_journal.ConsolidationVerificationJournalState,
    probe: consolidation_verification.VerificationProbe,
    parent_event_id: str,
    parent_payload_digest: str,
) -> consolidation_receipts.ConsolidationEvent:
    return consolidation_receipts.build_intent(
        kind="in-process-probe",
        run_id=state.run_id,
        operation_id=state.operation_id,
        phase="verifying",
        effect_ordinal=state.last_rebuild_effect_ordinal + 1 + probe.ordinal,
        probe_ordinal=probe.ordinal,
        request_digest=state.request_digest,
        prior_digest=_probe_prior_digest(state, probe),
        target_digest=probe.expected_result_digest,
        evidence=consolidation_receipts.build_evidence(
            kind="in-process-probe",
            digests={
                "probe_digest": probe.probe_digest,
                "probe_result_digest": probe.expected_result_digest,
                "verification_basis_digest": state.binding_digest,
            },
        ),
        semantic_parent_event_id=parent_event_id,
        semantic_parent_payload_digest=parent_payload_digest,
    )


def _validate_existing_probe_effect(
    *,
    entry: consolidation_verification_journal.ConsolidationVerificationJournalEntry,
    event: consolidation_receipts.ConsolidationEvent,
    store: consolidation_effect_coordinator.ConsolidationEffectJournalStore,
) -> None:
    current = store.load_optional()
    if entry.status == "prior":
        if current is None:
            return
        if (
            current.status != "prepared"
            or current.kind != "in-process-probe"
            or current.intent.event_id != event.event_id
            or current.intent.payload_digest != event.payload_digest
            or dict(current.intent_payload) != dict(event.payload)
        ):
            _fail()
        return
    if current is None or (
        current.kind != "in-process-probe"
        or current.intent.event_id != event.event_id
        or current.intent.payload_digest != event.payload_digest
        or dict(current.intent_payload) != dict(event.payload)
    ):
        _fail()
    if entry.status == "result":
        if current.status not in {"prepared", "final"}:
            _fail()
        if current.status == "final" and (
            current.terminal is None
            or current.terminal.event_id.rpartition(":")[2] != "committed"
            or current.observed_state != "target"
            or current.observed_digest != entry.result_digest
        ):
            _fail()
        return
    if (
        entry.status != "final"
        or current.status != "final"
        or current.terminal is None
        or current.observed_state != "target"
        or current.observed_digest != entry.result_digest
        or current.terminal.event_id != entry.terminal_event_id
        or current.terminal.payload_digest != entry.terminal_payload_digest
        or current.state_digest != entry.effect_journal_digest
    ):
        _fail()


def _execute_probes(
    *,
    vault_root: Path,
    admission: consolidation_admission.ConsolidationAdmission,
    store: consolidation_verification_journal.ConsolidationVerificationJournalStore,
    manifest: consolidation_verification_manifest.VerificationManifest,
    vault_binding_digest: str,
    journal_digest: str,
    verified_at: str,
    principals: tuple[governance_principal.RequestPrincipal, ...],
    authority: object,
    timestamp: str,
) -> tuple[consolidation_effect_coordinator.EffectExecutionResult, ...]:
    effects: list[consolidation_effect_coordinator.EffectExecutionResult] = []
    initial = store.load()
    if tuple(entry.probe for entry in initial.probes) != manifest.verification_plan.probes:
        _fail()
    for probe in (entry.probe for entry in initial.probes):
        contract = manifest.contracts[probe.ordinal]
        if (
            contract.probe_id != probe.probe_id
            or contract.contract_digest != probe.contract_digest
            or contract.expected_result_digest != probe.expected_result_digest
            or contract.executor_id != probe.executor_id
        ):
            _fail()
        state = store.load()
        if probe.ordinal == 0:
            parent_event_id = state.last_rebuild_terminal_event_id
            parent_payload_digest = state.last_rebuild_terminal_payload_digest
        else:
            parent = state.probes[probe.ordinal - 1]
            if (
                parent.status != "final"
                or parent.terminal_event_id is None
                or parent.terminal_payload_digest is None
            ):
                _fail()
            parent_event_id = parent.terminal_event_id
            parent_payload_digest = parent.terminal_payload_digest
        event = _probe_event(
            state=state,
            probe=probe,
            parent_event_id=parent_event_id,
            parent_payload_digest=parent_payload_digest,
        )
        effect_store = consolidation_effect_coordinator.ConsolidationEffectJournalStore(
            vault_root,
            run_id=state.run_id,
            effect_ordinal=state.last_rebuild_effect_ordinal + 1 + probe.ordinal,
        )
        entry = state.probes[probe.ordinal]
        _validate_existing_probe_effect(entry=entry, event=event, store=effect_store)
        prior_digest = _probe_prior_digest(state, probe)

        def classify(
            *,
            expected_probe: consolidation_verification.VerificationProbe = probe,
            expected_prior_digest: str = prior_digest,
        ) -> consolidation_effect_coordinator.EffectObservation:
            current = store.load().probes[expected_probe.ordinal]
            if current.probe != expected_probe:
                _fail()
            if current.status == "prior":
                return consolidation_effect_coordinator.EffectObservation(
                    state="prior",
                    digest=expected_prior_digest,
                )
            if current.status not in {"result", "final"}:
                _fail()
            return consolidation_effect_coordinator.EffectObservation(
                state="target",
                digest=_digest(current.result_digest),
            )

        def apply(
            *,
            expected_probe: consolidation_verification.VerificationProbe = probe,
            expected_contract: consolidation_verification_manifest.VerificationContract = contract,
        ) -> None:
            current = store.load()
            context = consolidation_verification.VerificationProbeContext(
                vault_root=vault_root,
                vault_binding_digest=vault_binding_digest,
                run_id=current.run_id,
                operation_id=current.operation_id,
                journal_digest=journal_digest,
                plan_digest=current.plan_digest,
                canonical_census_digest=current.canonical_census_digest,
                verification_basis_digest=current.binding_digest,
                verified_at=verified_at,
                principals=principals,
                authority=authority,
                contract=expected_contract,
            )
            with consolidation_runtime.bind_verification_admission(admission, authority):
                terminal = consolidation_verification._run_probe(  # noqa: SLF001
                    _canonical_surface_probe_runner,
                    expected_probe,
                    context,
                )
            if _snapshot_census(vault_root) != current.canonical_census_digest:
                _fail()
            store.record_probe_result(expected_probe, terminal.result_digest)

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
            or effect.observed_digest != probe.expected_result_digest
        ):
            _fail()
        store.finalize_probe(
            probe,
            effect.observed_digest,
            terminal_event_id=effect.terminal.event_id,
            terminal_payload_digest=effect.terminal.payload_digest,
            effect_journal_digest=effect.journal_digest,
        )
        effects.append(effect)
    return tuple(effects)


def _verification_result_digest(
    state: consolidation_verification_journal.ConsolidationVerificationJournalState,
) -> str:
    if any(entry.status != "final" for entry in state.probes):
        _fail()
    return _framed_digest(
        _VERIFICATION_RESULT_DOMAIN,
        {
            "schema": _VERIFICATION_RESULT_SCHEMA,
            "verification_basis_digest": state.binding_digest,
            "probes": tuple(
                {
                    "probe_ordinal": entry.probe.ordinal,
                    "probe_digest": entry.probe.probe_digest,
                    "result_digest": _digest(entry.result_digest),
                    "terminal_event_id": entry.terminal_event_id,
                    "terminal_payload_digest": entry.terminal_payload_digest,
                    "effect_journal_digest": entry.effect_journal_digest,
                }
                for entry in state.probes
            ),
        },
    )


def _verified_event(
    state: consolidation_verification_journal.ConsolidationVerificationJournalState,
    *,
    result_digest: str,
) -> consolidation_receipts.ConsolidationEvent:
    parent = state.probes[-1]
    if (
        parent.status != "final"
        or parent.terminal_event_id is None
        or parent.terminal_payload_digest is None
    ):
        _fail()
    prior = _framed_digest(
        _VERIFIED_PRIOR_DOMAIN,
        {
            "schema": _VERIFIED_PRIOR_SCHEMA,
            "verification_basis_digest": state.binding_digest,
        },
    )
    return consolidation_receipts.build_intent(
        kind="in-process-verified",
        run_id=state.run_id,
        operation_id=state.operation_id,
        phase="verifying",
        effect_ordinal=state.last_rebuild_effect_ordinal + len(state.probes) + 1,
        request_digest=state.request_digest,
        prior_digest=prior,
        target_digest=result_digest,
        evidence=consolidation_receipts.build_evidence(
            kind="in-process-verified",
            digests={
                "verification_basis_digest": state.binding_digest,
                "verification_result_digest": result_digest,
            },
        ),
        semantic_parent_event_id=parent.terminal_event_id,
        semantic_parent_payload_digest=parent.terminal_payload_digest,
    )


def _execute_verified_terminal(
    *,
    vault_root: Path,
    store: consolidation_verification_journal.ConsolidationVerificationJournalStore,
    timestamp: str,
) -> consolidation_effect_coordinator.EffectExecutionResult:
    state = store.load()
    result_digest = _verification_result_digest(state)
    event = _verified_event(state, result_digest=result_digest)
    effect_store = consolidation_effect_coordinator.ConsolidationEffectJournalStore(
        vault_root,
        run_id=state.run_id,
        effect_ordinal=state.last_rebuild_effect_ordinal + len(state.probes) + 1,
    )
    current_effect = effect_store.load_optional()
    terminal = state.terminal
    if terminal.status == "prior":
        if current_effect is None:
            pass
        elif (
            current_effect.status != "prepared"
            or current_effect.kind != "in-process-verified"
            or current_effect.intent.event_id != event.event_id
            or current_effect.intent.payload_digest != event.payload_digest
            or dict(current_effect.intent_payload) != dict(event.payload)
        ):
            _fail()
    elif current_effect is None or (
        current_effect.kind != "in-process-verified"
        or current_effect.intent.event_id != event.event_id
        or current_effect.intent.payload_digest != event.payload_digest
        or dict(current_effect.intent_payload) != dict(event.payload)
    ):
        _fail()
    elif terminal.status == "result":
        if current_effect.status not in {"prepared", "final"}:
            _fail()
    elif (
        terminal.status != "final"
        or current_effect.status != "final"
        or current_effect.terminal is None
        or current_effect.observed_digest != terminal.result_digest
        or current_effect.terminal.event_id != terminal.terminal_event_id
        or current_effect.terminal.payload_digest != terminal.terminal_payload_digest
        or current_effect.state_digest != terminal.effect_journal_digest
    ):
        _fail()
    prior_digest = _framed_digest(
        _VERIFIED_PRIOR_DOMAIN,
        {
            "schema": _VERIFIED_PRIOR_SCHEMA,
            "verification_basis_digest": state.binding_digest,
        },
    )

    def classify() -> consolidation_effect_coordinator.EffectObservation:
        current = store.load().terminal
        if current.status == "prior":
            return consolidation_effect_coordinator.EffectObservation(
                state="prior",
                digest=prior_digest,
            )
        if current.status not in {"result", "final"}:
            _fail()
        return consolidation_effect_coordinator.EffectObservation(
            state="target",
            digest=_digest(current.result_digest),
        )

    effect = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
        vault_root=vault_root,
        event=event,
        journal=effect_store,
        classify=classify,
        apply_effect=lambda: store.record_terminal_result(result_digest),
        timestamp=timestamp,
    )
    if (
        effect.role != "committed"
        or effect.observed_state != "target"
        or effect.observed_digest != result_digest
    ):
        _fail()
    store.finalize_terminal(
        result_digest,
        terminal_event_id=effect.terminal.event_id,
        terminal_payload_digest=effect.terminal.payload_digest,
        effect_journal_digest=effect.journal_digest,
    )
    return effect


def verify_rebuilt_destination(
    *,
    vault_root: Path | str,
    admission: consolidation_admission.ConsolidationAdmission,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    request_digest: str,
    plan_digest: str,
    verified_at: str,
    attested_principals: Sequence[governance_principal.RequestPrincipal] = (),
) -> ConsolidationVerificationCoordinatorResult:
    """Run exact in-process probes and advance only a proven result to verified."""

    root = Path(vault_root).absolute()
    checked_vault = _digest(vault_binding_digest)
    checked_run = _uuid4(run_id)
    checked_operation = _uuid4(operation_id)
    checked_journal = _digest(journal_digest)
    checked_request = _digest(request_digest)
    checked_plan_digest = _digest(plan_digest)
    checked_time = _timestamp(verified_at)
    if isinstance(attested_principals, (str, bytes)) or not isinstance(
        attested_principals, Sequence
    ):
        _fail()
    principals = tuple(attested_principals)
    if len(principals) > 1024 or any(
        type(item) is not governance_principal.RequestPrincipal
        or not item.resolved
        or item.audience_id == governance_principal.OWNER_AUDIENCE
        for item in principals
    ):
        _fail()
    principal_keys = tuple(
        (item.audience_id, item.surface, item.authorization_session_id) for item in principals
    )
    if len(set(principal_keys)) != len(principal_keys):
        _fail()
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
            plan_digest=checked_plan_digest,
        )
        preimage = stored.preimage
        if (
            stored.digest != checked_plan_digest
            or not isinstance(preimage, Mapping)
            or preimage.get("run_id") != checked_run
            or preimage.get("plan_kind") != "cutover"
        ):
            _fail()
        positive_digest, negative_digest = _plan_verification(preimage.get("verification_plan"))
        manifest = consolidation_verification_manifest.ConsolidationVerificationManifestStore(
            root
        ).load(checked_run, checked_plan_digest)
        checked_plan = consolidation_verification._checked_plan(  # noqa: SLF001
            manifest.verification_plan
        )
        if (
            positive_digest != checked_plan.positive_probe_digest
            or negative_digest != checked_plan.negative_probe_digest
        ):
            _fail()
        current_seal = _require_seal(
            admission.reload().state,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
        )
        if current_seal.phase == "verified":
            if current_seal.recorded_at != checked_time:
                _fail()
        elif current_seal.recorded_at >= checked_time:
            _fail()
        rebuild = consolidation_rebuild_journal.ConsolidationRebuildJournalStore(
            root,
            run_id=checked_run,
        ).load()
        if (
            rebuild.operation_id != checked_operation
            or rebuild.request_digest != checked_request
            or rebuild.plan_digest != checked_plan_digest
            or _snapshot_census(root) != rebuild.canonical_census_digest
        ):
            _fail()
        last_ordinal, last_event_id, last_payload_digest = _last_rebuild_receipt(
            root,
            rebuild,
        )
        journal_store = consolidation_verification_journal.ConsolidationVerificationJournalStore(
            root,
            run_id=checked_run,
        )
        if current_seal.phase == "verifying":
            state = journal_store.create(
                operation_id=checked_operation,
                request_digest=checked_request,
                plan_digest=checked_plan_digest,
                rebuild_journal_digest=rebuild.state_digest,
                canonical_census_digest=rebuild.canonical_census_digest,
                verification_plan=checked_plan,
                last_rebuild_terminal_event_id=last_event_id,
                last_rebuild_terminal_payload_digest=last_payload_digest,
                last_rebuild_effect_ordinal=last_ordinal,
            )
        else:
            state = journal_store.load()
        if (
            state.operation_id != checked_operation
            or state.request_digest != checked_request
            or state.plan_digest != checked_plan_digest
            or state.rebuild_journal_digest != rebuild.state_digest
            or state.canonical_census_digest != rebuild.canonical_census_digest
            or state.positive_probe_digest != checked_plan.positive_probe_digest
            or state.negative_probe_digest != checked_plan.negative_probe_digest
            or tuple(entry.probe for entry in state.probes) != checked_plan.probes
            or state.last_rebuild_terminal_event_id != last_event_id
            or state.last_rebuild_terminal_payload_digest != last_payload_digest
            or state.last_rebuild_effect_ordinal != last_ordinal
        ):
            _fail()
        authority = consolidation_authority.issue_authority(
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            phase="verifying",
            action="verify",
        )
        probe_effects = _execute_probes(
            vault_root=root,
            admission=admission,
            store=journal_store,
            manifest=manifest,
            vault_binding_digest=checked_vault,
            journal_digest=checked_journal,
            verified_at=checked_time,
            principals=principals,
            authority=authority,
            timestamp=checked_time,
        )
        verification_effect = _execute_verified_terminal(
            vault_root=root,
            store=journal_store,
            timestamp=checked_time,
        )
        final_state = journal_store.load()
        result_digest = _verification_result_digest(final_state)
        if (
            len(probe_effects) != len(checked_plan.probes)
            or final_state.terminal.status != "final"
            or final_state.terminal.result_digest != result_digest
            or _snapshot_census(root) != final_state.canonical_census_digest
        ):
            _fail()
        _crash_point("before-verified")
        seal_state = _advance_to_verified(
            admission=admission,
            vault_root=root,
            vault_binding_digest=checked_vault,
            run_id=checked_run,
            operation_id=checked_operation,
            journal_digest=checked_journal,
            recorded_at=checked_time,
        )
        return ConsolidationVerificationCoordinatorResult(
            verification_basis_digest=final_state.binding_digest,
            verification_result_digest=result_digest,
            completed_probe_ids=tuple(entry.probe.probe_id for entry in final_state.probes),
            probe_effects=probe_effects,
            verification_effect=verification_effect,
            verification_journal=final_state,
            seal_state=seal_state,
        )
    except ConsolidationVerificationCoordinatorUnavailable:
        raise
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_authority.ConsolidationAuthorityUnavailable,
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        consolidation_fingerprints.ConsolidationFingerprintUnavailable,
        consolidation_plan.ConsolidationPlanUnavailable,
        consolidation_plan_store.ConsolidationPlanStoreUnavailable,
        consolidation_rebuild_journal.ConsolidationRebuildJournalUnavailable,
        consolidation_receipts.ConsolidationReceiptUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        consolidation_verification.ConsolidationVerificationUnavailable,
        consolidation_verification_journal.ConsolidationVerificationJournalUnavailable,
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        consolidation_verification_registry.ConsolidationVerificationRegistryUnavailable,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()
