"""Receipt-first coordinator for exact-cell transport verification.

The durable consolidation seal remains closed while this module stops normal
routing, exercises each public surface through a single-use process-local read
route, and prepares the routing-open effect.  Completion and unsealing are a
separate successor-producing step.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from . import (
    consolidation_admission,
    consolidation_authority,
    consolidation_effect_coordinator,
    consolidation_plan,
    consolidation_receipts,
    consolidation_seal,
    consolidation_transport_journal,
    consolidation_transport_verification,
    consolidation_verification_journal,
)

TRANSPORT_PROBE_TERMINAL_SCHEMA = "exomem.consolidation-transport-probe-terminal/v1"

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_PHASES = (
    "verified",
    "transport-stopping",
    "transport-verifying",
    "transport-verified",
    "routing-opening",
)
_STOP_PRIOR_SCHEMA = "exomem.consolidation-transport-stop-prior/v1"
_PROBE_PRIOR_SCHEMA = "exomem.consolidation-transport-probe-prior/v1"
_VERIFIED_PRIOR_SCHEMA = "exomem.consolidation-transport-verified-prior/v1"
_VERIFIED_RESULT_SCHEMA = "exomem.consolidation-transport-verified-result/v1"
_ROUTING_PRIOR_SCHEMA = "exomem.consolidation-routing-open-prior/v1"
_ROUTING_RESULT_SCHEMA = "exomem.consolidation-routing-open-result/v1"

__all__ = [
    "TRANSPORT_PROBE_TERMINAL_SCHEMA",
    "ConsolidationTransportCoordinatorResult",
    "ConsolidationTransportCoordinatorUnavailable",
    "TransportProbeContext",
    "TransportProbeTerminal",
    "TransportRoutingEffectContext",
    "TransportRoutingObservation",
    "TransportSupervisor",
    "verify_exact_cell_transport",
]


class ConsolidationTransportCoordinatorUnavailable(RuntimeError):
    """Content-free refusal for incomplete or contradictory transport state."""

    code = "CONSOLIDATION_TRANSPORT_COORDINATOR_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class TransportRoutingObservation:
    state: Literal["prior", "prepared", "target", "mixed"]
    digest: str


@dataclass(frozen=True, slots=True)
class TransportRoutingEffectContext:
    plan_digest: str
    verification_basis_digest: str
    prior_digest: str
    target_digest: str


@dataclass(frozen=True, slots=True)
class TransportProbeContext:
    admission: consolidation_admission.ConsolidationAdmission
    plan_digest: str
    verification_basis_digest: str


@dataclass(frozen=True, slots=True)
class TransportProbeTerminal:
    schema: str
    probe_id: str
    probe_digest: str
    result_digest: str
    outcome: Literal["passed"]


class TransportSupervisor:
    """Trusted adapter for routing state and real public-surface probes."""

    def revalidate_pre_stop_basis(self, basis: Mapping[str, object]) -> str:
        raise NotImplementedError

    def classify_stop(
        self,
        context: TransportRoutingEffectContext,
    ) -> TransportRoutingObservation:
        raise NotImplementedError

    def stop_and_drain(self, context: TransportRoutingEffectContext) -> None:
        raise NotImplementedError

    def revalidate_basis(
        self,
        plan: consolidation_transport_verification.TransportVerificationPlan,
    ) -> str:
        raise NotImplementedError

    def run_probe(
        self,
        probe: consolidation_transport_verification.TransportProbe,
        context: TransportProbeContext,
    ) -> TransportProbeTerminal:
        raise NotImplementedError

    def classify_open(
        self,
        context: TransportRoutingEffectContext,
    ) -> TransportRoutingObservation:
        raise NotImplementedError

    def open_routing(self, context: TransportRoutingEffectContext) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ConsolidationTransportCoordinatorResult:
    completed_probe_ids: tuple[str, ...]
    stop_effect: consolidation_effect_coordinator.EffectExecutionResult
    probe_effects: tuple[consolidation_effect_coordinator.EffectExecutionResult, ...]
    verification_effect: consolidation_effect_coordinator.EffectExecutionResult
    routing_effect: consolidation_effect_coordinator.EffectExecutionResult
    transport_journal: consolidation_transport_journal.ConsolidationTransportJournalState
    seal_state: consolidation_seal.ConsolidationSealState


def _fail() -> NoReturn:
    raise ConsolidationTransportCoordinatorUnavailable from None


def _crash_point(_point: str) -> None:
    """Narrow test seam at coordinator-owned cross-store boundaries."""


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _timestamp(value: object) -> str:
    try:
        checked, _parsed = consolidation_plan._timestamp(value)  # noqa: SLF001
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    return checked


def _framed_digest(schema: str, value: Mapping[str, object]) -> str:
    try:
        raw = consolidation_plan.canonical_closed_jcs({"schema": schema, **value})
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    domain = schema.encode("ascii")
    framed = len(domain).to_bytes(4, "big") + domain + len(raw).to_bytes(8, "big") + raw
    return hashlib.sha256(framed).hexdigest()


def _require_parent(
    state: consolidation_verification_journal.ConsolidationVerificationJournalState,
    *,
    run_id: str,
    operation_id: str,
    plan_digest: str,
) -> consolidation_verification_journal.ConsolidationVerificationJournalState:
    terminal = state.terminal
    if (
        state.run_id != run_id
        or state.operation_id != operation_id
        or state.plan_digest != plan_digest
        or any(entry.status != "final" for entry in state.probes)
        or terminal.status != "final"
        or terminal.result_digest is None
        or terminal.terminal_event_id is None
        or terminal.terminal_payload_digest is None
        or terminal.effect_journal_digest is None
    ):
        _fail()
    return state


def _require_seal(
    state: consolidation_seal.ConsolidationSealState,
    *,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
) -> consolidation_seal.ConsolidationSealState:
    if (
        state.kind != "consolidation-sealed"
        or state.phase not in _ENTRY_PHASES
        or state.vault_binding_digest != vault_binding_digest
        or state.run_id != run_id
        or state.operation_id != operation_id
        or state.journal_digest != journal_digest
    ):
        _fail()
    return state


def _phase_index(phase: str | None) -> int:
    if phase not in _ENTRY_PHASES:
        _fail()
    return _ENTRY_PHASES.index(phase)


def _ensure_phase(
    *,
    admission: consolidation_admission.ConsolidationAdmission,
    vault_root: Path,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    target_phase: str,
    recorded_at: str,
) -> consolidation_seal.ConsolidationSealState:
    current = _require_seal(
        admission.reload().state,
        vault_binding_digest=vault_binding_digest,
        run_id=run_id,
        operation_id=operation_id,
        journal_digest=journal_digest,
    )
    current_index = _phase_index(current.phase)
    target_index = _phase_index(target_phase)
    if current_index >= target_index:
        return current
    if current_index + 1 != target_index:
        _fail()
    authority = consolidation_authority.issue_authority(
        vault_binding_digest=vault_binding_digest,
        run_id=run_id,
        operation_id=operation_id,
        journal_digest=journal_digest,
        phase=str(current.phase),
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


def _effect_ordinal(
    parent: consolidation_verification_journal.ConsolidationVerificationJournalState,
    offset: int,
) -> int:
    return parent.last_rebuild_effect_ordinal + len(parent.probes) + 1 + offset


def _effect_store(
    vault_root: Path,
    *,
    run_id: str,
    ordinal: int,
) -> consolidation_effect_coordinator.ConsolidationEffectJournalStore:
    return consolidation_effect_coordinator.ConsolidationEffectJournalStore(
        vault_root,
        run_id=run_id,
        effect_ordinal=ordinal,
    )


def _routing_observation(
    value: object,
) -> consolidation_effect_coordinator.EffectObservation:
    if type(value) is not TransportRoutingObservation:
        _fail()
    return consolidation_effect_coordinator.EffectObservation(
        state=value.state,
        digest=_digest(value.digest),
    )


def _stop_event(
    parent: consolidation_verification_journal.ConsolidationVerificationJournalState,
    *,
    routing_stop_digest: str,
) -> tuple[consolidation_receipts.ConsolidationEvent, str]:
    terminal = parent.terminal
    if terminal.terminal_event_id is None or terminal.terminal_payload_digest is None:
        _fail()
    prior = _framed_digest(
        _STOP_PRIOR_SCHEMA,
        {
            "verification_basis_digest": parent.binding_digest,
            "verification_result_digest": _digest(terminal.result_digest),
        },
    )
    return (
        consolidation_receipts.build_intent(
            kind="transport-stop",
            run_id=parent.run_id,
            operation_id=parent.operation_id,
            phase="transport-stopping",
            effect_ordinal=_effect_ordinal(parent, 1),
            request_digest=parent.request_digest,
            prior_digest=prior,
            target_digest=routing_stop_digest,
            evidence=consolidation_receipts.build_evidence(
                kind="transport-stop",
                digests={
                    "routing_stop_digest": routing_stop_digest,
                    "verification_basis_digest": parent.binding_digest,
                },
            ),
            semantic_parent_event_id=terminal.terminal_event_id,
            semantic_parent_payload_digest=terminal.terminal_payload_digest,
        ),
        prior,
    )


def _execute_stop(
    *,
    vault_root: Path,
    parent: consolidation_verification_journal.ConsolidationVerificationJournalState,
    plan_digest: str,
    routing_stop_digest: str,
    supervisor: TransportSupervisor,
    timestamp: str,
) -> consolidation_effect_coordinator.EffectExecutionResult:
    event, prior = _stop_event(parent, routing_stop_digest=routing_stop_digest)
    context = TransportRoutingEffectContext(
        plan_digest=plan_digest,
        verification_basis_digest=parent.binding_digest,
        prior_digest=prior,
        target_digest=routing_stop_digest,
    )
    return consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
        vault_root=vault_root,
        event=event,
        journal=_effect_store(
            vault_root,
            run_id=parent.run_id,
            ordinal=_effect_ordinal(parent, 1),
        ),
        classify=lambda: _routing_observation(supervisor.classify_stop(context)),
        apply_effect=lambda: supervisor.stop_and_drain(context),
        resume_effect=lambda: supervisor.stop_and_drain(context),
        timestamp=timestamp,
    )


def _aggregate_effect(
    effect: consolidation_effect_coordinator.EffectExecutionResult,
    *,
    kind: Literal["transport-stop", "transport-probe", "transport-verified", "routing-open"],
    probe_ordinal: int | None = None,
) -> consolidation_transport_journal.ConsolidationTransportJournalEffect:
    if (
        effect.role != "committed"
        or effect.observed_state != "target"
        or not effect.terminal.event_id.endswith(":committed")
    ):
        _fail()
    return consolidation_transport_journal.ConsolidationTransportJournalEffect(
        kind=kind,
        probe_ordinal=probe_ordinal,
        status="final",
        result_digest=_digest(effect.observed_digest),
        terminal_event_id=effect.terminal.event_id,
        terminal_payload_digest=effect.terminal.payload_digest,
        effect_journal_digest=effect.journal_digest,
    )


def _probe_prior_digest(
    state: consolidation_transport_journal.ConsolidationTransportJournalState,
    probe: consolidation_transport_verification.TransportProbe,
) -> str:
    return _framed_digest(
        _PROBE_PRIOR_SCHEMA,
        {
            "verification_basis_digest": state.binding_digest,
            "probe_ordinal": probe.ordinal,
            "probe_digest": probe.probe_digest,
        },
    )


def _parent_for_probe(
    state: consolidation_transport_journal.ConsolidationTransportJournalState,
    probe: consolidation_transport_verification.TransportProbe,
) -> tuple[str, str]:
    parent = state.effects[probe.ordinal]
    if (
        parent.status != "final"
        or parent.terminal_event_id is None
        or parent.terminal_payload_digest is None
    ):
        _fail()
    return parent.terminal_event_id, parent.terminal_payload_digest


def _probe_event(
    state: consolidation_transport_journal.ConsolidationTransportJournalState,
    probe: consolidation_transport_verification.TransportProbe,
    *,
    effect_ordinal: int,
) -> consolidation_receipts.ConsolidationEvent:
    parent_event, parent_payload = _parent_for_probe(state, probe)
    return consolidation_receipts.build_intent(
        kind="transport-probe",
        run_id=state.run_id,
        operation_id=state.operation_id,
        phase="transport-verifying",
        effect_ordinal=effect_ordinal,
        probe_ordinal=probe.ordinal,
        request_digest=state.request_digest,
        prior_digest=_probe_prior_digest(state, probe),
        target_digest=probe.expected_result_digest,
        evidence=consolidation_receipts.build_evidence(
            kind="transport-probe",
            digests={
                "probe_digest": probe.probe_digest,
                "probe_result_digest": probe.expected_result_digest,
                "verification_basis_digest": state.binding_digest,
            },
        ),
        semantic_parent_event_id=parent_event,
        semantic_parent_payload_digest=parent_payload,
    )


def _validate_probe_terminal(
    value: object,
    *,
    probe: consolidation_transport_verification.TransportProbe,
) -> TransportProbeTerminal:
    if (
        type(value) is not TransportProbeTerminal
        or value.schema != TRANSPORT_PROBE_TERMINAL_SCHEMA
        or value.probe_id != probe.probe_id
        or value.probe_digest != probe.probe_digest
        or value.result_digest != probe.expected_result_digest
        or value.outcome != "passed"
    ):
        _fail()
    return value


def _finalize_aggregate(
    store: consolidation_transport_journal.ConsolidationTransportJournalStore,
    *,
    kind: str,
    result: consolidation_effect_coordinator.EffectExecutionResult,
    probe_ordinal: int | None = None,
) -> consolidation_transport_journal.ConsolidationTransportJournalState:
    store.record_transport_effect_result(
        kind=kind,
        probe_ordinal=probe_ordinal,
        result_digest=result.observed_digest,
    )
    return store.finalize_transport_effect(
        kind=kind,
        probe_ordinal=probe_ordinal,
        result_digest=result.observed_digest,
        terminal_event_id=result.terminal.event_id,
        terminal_payload_digest=result.terminal.payload_digest,
        effect_journal_digest=result.journal_digest,
    )


def _execute_probes(
    *,
    vault_root: Path,
    admission: consolidation_admission.ConsolidationAdmission,
    parent: consolidation_verification_journal.ConsolidationVerificationJournalState,
    plan: consolidation_transport_verification.TransportVerificationPlan,
    store: consolidation_transport_journal.ConsolidationTransportJournalStore,
    supervisor: TransportSupervisor,
    timestamp: str,
) -> tuple[consolidation_effect_coordinator.EffectExecutionResult, ...]:
    effects: list[consolidation_effect_coordinator.EffectExecutionResult] = []
    authority = consolidation_authority.issue_authority(
        vault_binding_digest=plan.basis.vault_binding_digest,
        run_id=plan.basis.run_id,
        operation_id=plan.basis.operation_id,
        journal_digest=plan.basis.journal_digest,
        phase="transport-verifying",
        action="probe",
    )
    for probe in plan.probes:
        state = store.load()
        ordinal = _effect_ordinal(parent, 2 + probe.ordinal)
        event = _probe_event(state, probe, effect_ordinal=ordinal)
        prior = _probe_prior_digest(state, probe)

        def classify(
            *,
            expected_probe: consolidation_transport_verification.TransportProbe = probe,
            expected_prior: str = prior,
        ) -> consolidation_effect_coordinator.EffectObservation:
            current = store.load().effects[1 + expected_probe.ordinal]
            if current.status == "prior":
                return consolidation_effect_coordinator.EffectObservation(
                    state="prior",
                    digest=expected_prior,
                )
            if current.status not in {"result", "final"}:
                _fail()
            return consolidation_effect_coordinator.EffectObservation(
                state="target",
                digest=_digest(current.result_digest),
            )

        def apply(
            *,
            expected_probe: consolidation_transport_verification.TransportProbe = probe,
        ) -> None:
            route = consolidation_transport_verification.issue_transport_probe_route(
                authority,
                plan=plan,
                probe=expected_probe,
            )
            context = TransportProbeContext(
                admission=admission,
                plan_digest=plan.digest,
                verification_basis_digest=store.load().binding_digest,
            )
            with consolidation_transport_verification.transport_probe_route_scope(
                route,
                plan=plan,
                probe=expected_probe,
            ):
                terminal = _validate_probe_terminal(
                    supervisor.run_probe(expected_probe, context),
                    probe=expected_probe,
                )
            _crash_point(f"after-probe-route:{expected_probe.ordinal}")
            store.record_transport_effect_result(
                kind="transport-probe",
                probe_ordinal=expected_probe.ordinal,
                result_digest=terminal.result_digest,
            )
            _crash_point(f"after-probe-result:{expected_probe.ordinal}")

        effect = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault_root,
            event=event,
            journal=_effect_store(
                vault_root,
                run_id=parent.run_id,
                ordinal=ordinal,
            ),
            classify=classify,
            apply_effect=apply,
            timestamp=timestamp,
        )
        _crash_point(f"after-probe-effect-final:{probe.ordinal}")
        _finalize_aggregate(
            store,
            kind="transport-probe",
            probe_ordinal=probe.ordinal,
            result=effect,
        )
        _crash_point(f"after-probe-aggregate-final:{probe.ordinal}")
        effects.append(effect)
    return tuple(effects)


def _transport_verified_result(
    state: consolidation_transport_journal.ConsolidationTransportJournalState,
) -> str:
    probe_effects = state.effects[1 : 1 + len(state.probes)]
    if any(
        effect.status != "final"
        or effect.result_digest is None
        or effect.terminal_event_id is None
        or effect.terminal_payload_digest is None
        or effect.effect_journal_digest is None
        for effect in probe_effects
    ):
        _fail()
    return _framed_digest(
        _VERIFIED_RESULT_SCHEMA,
        {
            "verification_basis_digest": state.binding_digest,
            "probe_effects": tuple(
                {
                    "probe_ordinal": effect.probe_ordinal,
                    "result_digest": effect.result_digest,
                    "terminal_event_id": effect.terminal_event_id,
                    "terminal_payload_digest": effect.terminal_payload_digest,
                    "effect_journal_digest": effect.effect_journal_digest,
                }
                for effect in probe_effects
            ),
        },
    )


def _execute_transport_verified(
    *,
    vault_root: Path,
    parent: consolidation_verification_journal.ConsolidationVerificationJournalState,
    store: consolidation_transport_journal.ConsolidationTransportJournalStore,
    timestamp: str,
) -> consolidation_effect_coordinator.EffectExecutionResult:
    state = store.load()
    result_digest = _transport_verified_result(state)
    prior_digest = _framed_digest(
        _VERIFIED_PRIOR_SCHEMA,
        {"verification_basis_digest": state.binding_digest},
    )
    parent_effect = state.effects[len(state.probes)]
    if (
        parent_effect.status != "final"
        or parent_effect.terminal_event_id is None
        or parent_effect.terminal_payload_digest is None
    ):
        _fail()
    ordinal = _effect_ordinal(parent, 2 + len(state.probes))
    event = consolidation_receipts.build_intent(
        kind="transport-verified",
        run_id=state.run_id,
        operation_id=state.operation_id,
        phase="transport-verifying",
        effect_ordinal=ordinal,
        request_digest=state.request_digest,
        prior_digest=prior_digest,
        target_digest=result_digest,
        evidence=consolidation_receipts.build_evidence(
            kind="transport-verified",
            digests={
                "verification_basis_digest": state.binding_digest,
                "verification_result_digest": result_digest,
            },
        ),
        semantic_parent_event_id=parent_effect.terminal_event_id,
        semantic_parent_payload_digest=parent_effect.terminal_payload_digest,
    )

    def classify() -> consolidation_effect_coordinator.EffectObservation:
        current = store.load().effects[1 + len(state.probes)]
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
        journal=_effect_store(vault_root, run_id=state.run_id, ordinal=ordinal),
        classify=classify,
        apply_effect=lambda: store.record_transport_effect_result(
            kind="transport-verified",
            result_digest=result_digest,
        ),
        timestamp=timestamp,
    )
    _crash_point("after-transport-verified-effect-final")
    _finalize_aggregate(store, kind="transport-verified", result=effect)
    _crash_point("after-transport-verified-aggregate-final")
    return effect


def _execute_routing_open(
    *,
    vault_root: Path,
    admission: consolidation_admission.ConsolidationAdmission,
    vault_binding_digest: str,
    parent: consolidation_verification_journal.ConsolidationVerificationJournalState,
    store: consolidation_transport_journal.ConsolidationTransportJournalStore,
    supervisor: TransportSupervisor,
    recorded_at: str,
) -> consolidation_effect_coordinator.EffectExecutionResult:
    state = store.load()
    verified = state.effects[1 + len(state.probes)]
    if (
        verified.status != "final"
        or verified.result_digest is None
        or verified.terminal_event_id is None
        or verified.terminal_payload_digest is None
    ):
        _fail()
    prior_digest = _framed_digest(
        _ROUTING_PRIOR_SCHEMA,
        {
            "verification_basis_digest": state.binding_digest,
            "verification_result_digest": verified.result_digest,
        },
    )
    target_digest = _framed_digest(
        _ROUTING_RESULT_SCHEMA,
        {
            "routing_basis_digest": state.basis_digest,
            "verification_result_digest": verified.result_digest,
        },
    )
    context = TransportRoutingEffectContext(
        plan_digest=state.plan_digest,
        verification_basis_digest=state.binding_digest,
        prior_digest=prior_digest,
        target_digest=target_digest,
    )
    ordinal = _effect_ordinal(parent, 3 + len(state.probes))
    event = consolidation_receipts.build_intent(
        kind="routing-open",
        run_id=state.run_id,
        operation_id=state.operation_id,
        phase="routing-opening",
        effect_ordinal=ordinal,
        request_digest=state.request_digest,
        prior_digest=prior_digest,
        target_digest=target_digest,
        evidence=consolidation_receipts.build_evidence(
            kind="routing-open",
            digests={
                "routing_basis_digest": state.basis_digest,
                "routing_result_digest": target_digest,
            },
        ),
        semantic_parent_event_id=verified.terminal_event_id,
        semantic_parent_payload_digest=verified.terminal_payload_digest,
    )

    def apply() -> None:
        _ensure_phase(
            admission=admission,
            vault_root=vault_root,
            vault_binding_digest=vault_binding_digest,
            run_id=state.run_id,
            operation_id=state.operation_id,
            journal_digest=state.apply_journal_digest,
            target_phase="routing-opening",
            recorded_at=recorded_at,
        )
        _crash_point("after-routing-opening-phase")
        supervisor.open_routing(context)
        _crash_point("after-routing-open-side-effect")

    effect = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
        vault_root=vault_root,
        event=event,
        journal=_effect_store(vault_root, run_id=state.run_id, ordinal=ordinal),
        classify=lambda: _routing_observation(supervisor.classify_open(context)),
        apply_effect=apply,
        resume_effect=apply,
        timestamp=recorded_at,
    )
    _crash_point("after-routing-open-effect-final")
    _finalize_aggregate(store, kind="routing-open", result=effect)
    _crash_point("after-routing-open-aggregate-final")
    return effect

def verify_exact_cell_transport(
    *,
    vault_root: Path | str,
    admission: consolidation_admission.ConsolidationAdmission,
    journal_digest: str,
    basis: Mapping[str, object],
    contracts: Sequence[Mapping[str, object]],
    supervisor: TransportSupervisor,
    recorded_at: str,
) -> ConsolidationTransportCoordinatorResult:
    """Stop, probe, and prepare routing for one exact sealed destination cell."""

    try:
        root = Path(vault_root).absolute()
        checked_journal = _digest(journal_digest)
        checked_time = _timestamp(recorded_at)
        if (
            not isinstance(admission, consolidation_admission.ConsolidationAdmission)
            or admission.vault_root != root
            or not isinstance(supervisor, TransportSupervisor)
            or not isinstance(basis, Mapping)
            or isinstance(contracts, (str, bytes))
            or not isinstance(contracts, Sequence)
        ):
            _fail()
        vault_binding = _digest(basis.get("vault_binding_digest"))
        run_id = str(basis.get("run_id"))
        operation_id = str(basis.get("operation_id"))
        request_plan = _digest(basis.get("plan_digest"))
        if admission.vault_binding_digest != vault_binding:
            _fail()
        parent = _require_parent(
            consolidation_verification_journal.ConsolidationVerificationJournalStore(
                root, run_id=run_id
            ).load(),
            run_id=run_id,
            operation_id=operation_id,
            plan_digest=request_plan,
        )
        _require_seal(
            admission.reload().state,
            vault_binding_digest=vault_binding,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=checked_journal,
        )
        expected_pre_stop_basis = (
            consolidation_transport_verification.transport_verification_basis_fingerprint(
                basis
            )
        )
        if (
            _digest(supervisor.revalidate_pre_stop_basis(basis))
            != expected_pre_stop_basis
        ):
            _fail()
        _ensure_phase(
            admission=admission,
            vault_root=root,
            vault_binding_digest=vault_binding,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=checked_journal,
            target_phase="transport-stopping",
            recorded_at=checked_time,
        )
        routing_stop_digest = _digest(basis.get("routing_stop_digest"))
        stop_effect = _execute_stop(
            vault_root=root,
            parent=parent,
            plan_digest=request_plan,
            routing_stop_digest=routing_stop_digest,
            supervisor=supervisor,
            timestamp=checked_time,
        )
        _crash_point("after-stop-effect-final")
        stop_aggregate = _aggregate_effect(stop_effect, kind="transport-stop")

        authority = consolidation_authority.issue_authority(
            vault_binding_digest=vault_binding,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=checked_journal,
            phase="transport-stopping",
            action="apply",
        )
        binding = consolidation_transport_verification.issue_exact_destination_binding(
            authority,
            journal_digest=checked_journal,
            basis=basis,
        )
        plan = consolidation_transport_verification.build_transport_verification_plan(
            basis=basis,
            contracts=contracts,
            exact_destination_binding=binding,
        )
        if _digest(supervisor.revalidate_basis(plan)) != plan.basis.digest:
            _fail()
        store = consolidation_transport_journal.ConsolidationTransportJournalStore(
            root,
            run_id=run_id,
        )
        store.create(
            verification_journal=parent,
            transport_plan=plan,
            transport_stop_effect=stop_aggregate,
        )
        _crash_point("after-transport-journal")
        _ensure_phase(
            admission=admission,
            vault_root=root,
            vault_binding_digest=vault_binding,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=checked_journal,
            target_phase="transport-verifying",
            recorded_at=checked_time,
        )
        _crash_point("after-transport-verifying")
        probe_effects = _execute_probes(
            vault_root=root,
            admission=admission,
            parent=parent,
            plan=plan,
            store=store,
            supervisor=supervisor,
            timestamp=checked_time,
        )
        if _digest(supervisor.revalidate_basis(plan)) != plan.basis.digest:
            _fail()
        verification_effect = _execute_transport_verified(
            vault_root=root,
            parent=parent,
            store=store,
            timestamp=checked_time,
        )
        _ensure_phase(
            admission=admission,
            vault_root=root,
            vault_binding_digest=vault_binding,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=checked_journal,
            target_phase="transport-verified",
            recorded_at=checked_time,
        )
        _crash_point("after-transport-verified-phase")
        if _digest(supervisor.revalidate_basis(plan)) != plan.basis.digest:
            _fail()
        routing_effect = _execute_routing_open(
            vault_root=root,
            admission=admission,
            vault_binding_digest=vault_binding,
            parent=parent,
            store=store,
            supervisor=supervisor,
            recorded_at=checked_time,
        )
        seal_state = _require_seal(
            admission.reload().state,
            vault_binding_digest=vault_binding,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=checked_journal,
        )
        if seal_state.phase != "routing-opening":
            _fail()
        final = store.load()
        return ConsolidationTransportCoordinatorResult(
            completed_probe_ids=tuple(probe.probe_id for probe in plan.probes),
            stop_effect=stop_effect,
            probe_effects=probe_effects,
            verification_effect=verification_effect,
            routing_effect=routing_effect,
            transport_journal=final,
            seal_state=seal_state,
        )
    except ConsolidationTransportCoordinatorUnavailable:
        raise
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_authority.ConsolidationAuthorityUnavailable,
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        consolidation_plan.ConsolidationPlanUnavailable,
        consolidation_receipts.ConsolidationReceiptUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        consolidation_transport_journal.ConsolidationTransportJournalUnavailable,
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable,
        consolidation_verification_journal.ConsolidationVerificationJournalUnavailable,
        NotImplementedError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()
