"""Bind observed scenario state to frozen deterministic assertions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .amendments import require_family_released
from .assertions import AssertionContext, AssertionResult
from .broker import (
    InvocationReceiptRef,
    ProviderBroker,
    SandboxDriverResult,
    audit_invocation_receipts,
)
from .registry import REQUIRES_SNAPSHOT_PAIR, resolve
from .schema import Scenario
from .snapshot import EpistemicStateSnapshot


_RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class RunnerBindingError(ValueError):
    """A scenario cannot be scored from the supplied observations."""


@dataclass(frozen=True)
class PhaseObservation:
    """Explicit observations recorded for one scenario phase."""

    served_items: tuple[str, ...] | None = None
    foreign_case_hits: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BoundAssertion:
    """One deterministic result and the fully bound context that produced it."""

    phase_id: str
    assertion: str
    context: AssertionContext
    result: AssertionResult


@dataclass(frozen=True)
class ScenarioRun:
    """All deterministic assertion results in declared trajectory order."""

    assertions: tuple[BoundAssertion, ...]
    comparability_exclusions: tuple[str, ...] = ()


def _binding_error(detail: str) -> RunnerBindingError:
    return RunnerBindingError(f"runner binding: {detail}")


def _validate_inputs(
    scenario: Scenario,
    snapshots: Mapping[str, EpistemicStateSnapshot],
    phase_observations: Mapping[str, PhaseObservation],
) -> tuple[str, ...]:
    phase_ids = tuple(phase.phase_id for phase in scenario.phases)
    if len(set(phase_ids)) != len(phase_ids):
        raise _binding_error("duplicate phase id")

    unknown_phases = sorted(set(phase_observations) - set(phase_ids))
    if unknown_phases:
        raise _binding_error(f"unknown phase observation(s): {', '.join(unknown_phases)}")

    snapshot_refs = tuple(
        op.ref for phase in scenario.phases for op in phase.ops if op.op == "snapshot"
    )
    duplicate_refs = sorted({ref for ref in snapshot_refs if snapshot_refs.count(ref) > 1})
    if duplicate_refs:
        raise _binding_error(f"duplicate snapshot ref(s): {', '.join(duplicate_refs)}")

    unexpected_refs = sorted(set(snapshots) - set(snapshot_refs))
    if unexpected_refs:
        raise _binding_error(f"unknown observed snapshot ref(s): {', '.join(unexpected_refs)}")

    missing_refs = [ref for ref in snapshot_refs if ref not in snapshots]
    if missing_refs:
        raise _binding_error(f"missing observed snapshot ref(s): {', '.join(missing_refs)}")
    observed = tuple(snapshots[ref] for ref in snapshot_refs)
    if len({id(snapshot) for snapshot in observed}) != len(observed):
        raise _binding_error("two snapshot refs point at the same observation object")
    baseline = observed[0] if observed else None
    if baseline is not None:
        for candidate in observed[1:]:
            if candidate.provider != baseline.provider:
                raise _binding_error("snapshot provider differs within the scenario row")
            if candidate.variant != baseline.variant:
                raise _binding_error("snapshot variant differs within the scenario row")
            if candidate.projector != baseline.projector:
                raise _binding_error("snapshot projector differs within the scenario row")
    return phase_ids


def _timestamp(value: str, *, label: str) -> datetime:
    try:
        if _RFC3339_TIMESTAMP.fullmatch(value) is None:
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError) as error:
        raise _binding_error(f"{label} timestamp is not RFC3339: {value!r}") from error


def evaluate_scenario(
    scenario: Scenario,
    *,
    snapshots: Mapping[str, EpistemicStateSnapshot],
    phase_observations: Mapping[str, PhaseObservation] | None = None,
) -> ScenarioRun:
    """Evaluate registered expectations from explicit, already-observed state.

    This function is deliberately only binding glue: it executes neither
    provider operations nor clock reads. Snapshot bytes and probe observations
    are supplied by the caller, and each expectation resolves through the
    frozen assertion registry.
    """

    # The loader refuses a withheld family, so a Scenario for one should not
    # exist — but this function accepts a caller-built Scenario, and "should not
    # exist" is not a guarantee. Evaluating is the moment a family influences a
    # score, so the receipt is checked here too rather than trusted upstream.
    require_family_released(scenario.family_id)

    observations = {} if phase_observations is None else phase_observations
    _validate_inputs(scenario, snapshots, observations)

    reached_snapshots: list[EpistemicStateSnapshot] = []
    external_edit_at: str | None = None
    snapshots_before_external_edit: int | None = None
    contexts: list[tuple[str, str, AssertionContext]] = []
    for phase in scenario.phases:
        for op in phase.ops:
            if op.op == "snapshot":
                reached_snapshots.append(snapshots[op.ref].model_copy(deep=True))
            elif op.op == "external_edit":
                external_edit_at = op.at
                snapshots_before_external_edit = len(reached_snapshots)

        if not phase.expect:
            continue
        if not reached_snapshots:
            raise _binding_error(f"phase {phase.phase_id!r} has an expectation before a snapshot")

        observation = observations.get(phase.phase_id, PhaseObservation())
        for expectation in phase.expect:
            if expectation.assertion in REQUIRES_SNAPSHOT_PAIR and len(reached_snapshots) < 2:
                raise _binding_error(
                    f"phase {phase.phase_id!r} requires a snapshot pair but only "
                    f"{len(reached_snapshots)} observed snapshot(s) were reached"
                )
            if (
                expectation.assertion == "external_edit_authoritative_within"
                and external_edit_at is None
            ):
                raise _binding_error(
                    f"phase {phase.phase_id!r} requires a stamped preceding external edit"
                )
            if expectation.assertion == "external_edit_authoritative_within":
                assert external_edit_at is not None
                if snapshots_before_external_edit != len(reached_snapshots) - 1:
                    raise _binding_error(
                        f"phase {phase.phase_id!r} snapshot pair does not straddle the "
                        "latest preceding external edit"
                    )
                edit_time = _timestamp(external_edit_at, label="external edit")
                prior_time = _timestamp(reached_snapshots[-2].taken_at, label="prior snapshot")
                current_time = _timestamp(reached_snapshots[-1].taken_at, label="current snapshot")
                if prior_time > edit_time:
                    raise _binding_error("prior snapshot timestamp follows the external edit")
                if current_time < edit_time:
                    raise _binding_error("current snapshot timestamp precedes the external edit")

            context = AssertionContext(
                snapshot=reached_snapshots[-1],
                prior=reached_snapshots[-2] if len(reached_snapshots) > 1 else None,
                subject=expectation.subject,
                counterpart=expectation.counterpart,
                served_items=observation.served_items,
                foreign_case_hits=observation.foreign_case_hits,
                freshness_bound_s=expectation.freshness_bound_s,
                external_edit_at=external_edit_at,
                tolerance=0.0 if expectation.tolerance is None else expectation.tolerance,
            )
            contexts.append((phase.phase_id, expectation.assertion, context))

    resolved = tuple(
        (phase_id, assertion, context, resolve(assertion))
        for phase_id, assertion, context in contexts
    )
    return ScenarioRun(
        assertions=tuple(
            BoundAssertion(
                phase_id=phase_id,
                assertion=assertion,
                context=context,
                result=assertion_fn(context),
            )
            for phase_id, assertion, context, assertion_fn in resolved
        )
    )


def run_scenario(
    scenario: Scenario,
    *,
    snapshots: Mapping[str, EpistemicStateSnapshot],
    run_root: Path | str | None = None,
    provider: str | None = None,
    variant: str | None = None,
    invocation_receipt_ref: InvocationReceiptRef | None = None,
    broker: ProviderBroker | None = None,
    sandbox_result: SandboxDriverResult | None = None,
    phase_observations: Mapping[str, PhaseObservation] | None = None,
) -> ScenarioRun:
    """Audit a comparative row before evaluating any frozen assertion."""

    if run_root is None or provider is None or variant is None:
        raise _binding_error("comparative receipt audit requires run_root, provider, and variant")
    if invocation_receipt_ref is not None or broker is None or sandbox_result is None:
        raise _binding_error(
            "comparative audit requires a live broker sandbox result attestation; raw receipt refused"
        )
    audit = audit_invocation_receipts(
        broker=broker,
        run_root=run_root,
        driver_result=sandbox_result,
        matrix=scenario.fairness.privileged_endpoint_matrix,
        provider=provider,
        variant=variant,
    )
    if not audit.comparable:
        return ScenarioRun(assertions=(), comparability_exclusions=audit.exclusions)
    return evaluate_scenario(
        scenario,
        snapshots=snapshots,
        phase_observations=phase_observations,
    )
