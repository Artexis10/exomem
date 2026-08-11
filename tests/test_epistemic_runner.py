"""Scenario observations bind to deterministic assertion contexts."""

from __future__ import annotations

import pytest

from epistemic.assertions import AssertionResult
from epistemic.registry import RegistryError
from epistemic.runner import PhaseObservation, RunnerBindingError, run_scenario
from epistemic.schema import (
    Expectation,
    FairnessMechanism,
    FairnessPacket,
    PrivilegedEndpointCheck,
    Scenario,
    ScenarioOp,
    ScenarioPhase,
)
from epistemic.snapshot import EpistemicStateSnapshot, FieldDeclaration, ProjectorMeta, StateItem


PROJECTOR = ProjectorMeta(
    name="fixture-projector",
    version="0.1.0",
    author="benchmark-harness",
    endpoints_used=("fixture:in-memory",),
    loc=1,
)
DECLARATIONS = tuple(
    FieldDeclaration(
        field=field,
        status="declared",
        evidence=f"benchmarks/epistemic/PREREGISTRATION.md:39 ({field})",
    )
    for field in ("kind", "current", "external_edit", "locator", "export", "review_state")
)
FAIRNESS = FairnessPacket(
    why_neutral="Observable state distinguishes settled from tentative knowledge.",
    public_coverage_subtraction="state-only",
    mechanisms=(
        FairnessMechanism(
            provider_role="provider", mechanism="documented metadata", verdict="possible", evidence="docs",
        ),
    ),
    privileged_endpoint_check=(
        PrivilegedEndpointCheck(driver_tool="project", competitor_equivalent="documented read surface"),
    ),
    acceptance_predicate="A documented kind field distinguishes the two records.",
)


def snapshot(ref: str, *, text: str = "original", decision: bool = False) -> EpistemicStateSnapshot:
    items = (
        StateItem(id="decision", kind="decision", title="decision", text=text, current="yes"),
        StateItem(id="hypothesis", kind="hypothesis", title="hypothesis", text="tentative", current="yes"),
    ) if decision else (StateItem(id="file", kind="claim", title="file", text=text, current="yes"),)
    return EpistemicStateSnapshot(
        provider="fixture",
        phase=ref,
        taken_at=f"2026-01-01T00:00:{int(ref[-1]) if ref[-1].isdigit() else 0:02d}Z",
        items=items,
        declarations=DECLARATIONS,
        projector=PROJECTOR,
    )


def scenario(*phases: ScenarioPhase, family_id: str = "f07") -> Scenario:
    return Scenario(
        scenario_id="runner-fixture",
        family_id=family_id,
        kind="operational",
        public_coverage="none",
        phases=phases,
        fairness=FAIRNESS,
    )


def test_phase_observations_and_expectation_fields_bind_exactly() -> None:
    subject = "decision"
    case = scenario(
        ScenarioPhase(
            phase_id="p1",
            ops=(ScenarioOp(op="snapshot", ref="s1"),),
            expect=(
                Expectation(
                    **{
                        "assert": "decision_distinguishable_from_hypothesis",
                        "subject": subject,
                        "counterpart": "hypothesis",
                        "freshness_bound_s": 12.5,
                        "tolerance": 0.25,
                    }
                ),
            ),
        ),
    )

    result = run_scenario(
        case,
        snapshots={"s1": snapshot("s1", decision=True)},
        phase_observations={
            "p1": PhaseObservation(served_items=("decision",), foreign_case_hits=("foreign",)),
        },
    )

    context = result.assertions[0].context
    assert context.subject == subject
    assert context.counterpart == "hypothesis"
    assert context.freshness_bound_s == 12.5
    assert context.tolerance == 0.25
    assert context.served_items == ("decision",)
    assert context.foreign_case_hits == ("foreign",)


def test_current_and_prior_snapshots_bind_in_trajectory_order() -> None:
    case = scenario(
        ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),)),
        ScenarioPhase(
            phase_id="p2",
            ops=(ScenarioOp(op="snapshot", ref="s2"),),
            expect=(Expectation(**{"assert": "export_reconstructs_state", "tolerance": 0.0}),),
        ),
    )
    first, second = snapshot("s1"), snapshot("s2")

    result = run_scenario(case, snapshots={"s1": first, "s2": second})

    assert result.assertions[0].context.snapshot == second
    assert result.assertions[0].context.snapshot is not second
    assert result.assertions[0].context.prior == first
    assert result.assertions[0].context.prior is not first


def test_missing_snapshot_observation_refuses_before_assertion_execution(monkeypatch) -> None:
    case = scenario(
        ScenarioPhase(
            phase_id="p1",
            ops=(ScenarioOp(op="snapshot", ref="missing"),),
            expect=(Expectation(**{"assert": "open_question_queryable"}),),
        ),
        family_id="f08",
    )
    called = False

    def fail_if_called(_context):
        nonlocal called
        called = True
        return AssertionResult(name="open_question_queryable", outcome="pass", evidence="unexpected")

    monkeypatch.setattr("epistemic.runner.resolve", lambda _name: fail_if_called)
    with pytest.raises(RunnerBindingError, match="missing observed snapshot"):
        run_scenario(case, snapshots={})
    assert not called


def test_pair_required_expectation_refuses_when_only_one_snapshot_is_observed() -> None:
    case = scenario(
        ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),)),
        ScenarioPhase(
            phase_id="p2",
            ops=(ScenarioOp(op="snapshot", ref="s2"),),
            expect=(Expectation(**{"assert": "export_reconstructs_state", "tolerance": 0.0}),),
        ),
    )

    with pytest.raises(RunnerBindingError):
        run_scenario(case, snapshots={"s1": snapshot("s1")})


def test_unknown_phase_observation_refuses() -> None:
    case = scenario(ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),)))

    with pytest.raises(RunnerBindingError, match="unknown phase"):
        run_scenario(
            case,
            snapshots={"s1": snapshot("s1")},
            phase_observations={"wrong": PhaseObservation()},
        )


def test_duplicate_snapshot_refs_refuse_as_ambiguous() -> None:
    case = scenario(
        ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="same"),)),
        ScenarioPhase(phase_id="p2", ops=(ScenarioOp(op="snapshot", ref="same"),)),
    )

    with pytest.raises(RunnerBindingError, match="duplicate snapshot ref"):
        run_scenario(case, snapshots={"same": snapshot("same")})


def test_explicit_empty_probe_observations_are_not_collapsed_to_none() -> None:
    case = scenario(
        ScenarioPhase(
            phase_id="p1",
            ops=(ScenarioOp(op="snapshot", ref="s1"),),
            expect=(Expectation(**{"assert": "no_cross_case_residue"}),),
        ),
        family_id="f14",
    )

    result = run_scenario(
        case,
        snapshots={"s1": snapshot("s1")},
        phase_observations={"p1": PhaseObservation(served_items=(), foreign_case_hits=())},
    )

    context = result.assertions[0].context
    assert context.served_items == ()
    assert context.foreign_case_hits == ()
    assert result.assertions[0].result.outcome == "pass"


def test_latest_preceding_external_edit_timestamp_binds_and_missing_stamp_refuses() -> None:
    phase = ScenarioPhase(
        phase_id="p2",
        ops=(
            ScenarioOp(op="external_edit", ref="old", at="2026-01-01T00:00:01Z"),
            ScenarioOp(op="external_edit", ref="new", at="2026-01-01T00:00:02Z"),
            ScenarioOp(op="snapshot", ref="s2"),
        ),
        expect=(
            Expectation(
                **{
                    "assert": "external_edit_authoritative_within",
                    "subject": "file",
                    "freshness_bound_s": 60.0,
                }
            ),
        ),
    )
    with_prior = scenario(ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),)), phase, family_id="f12")

    result = run_scenario(with_prior, snapshots={"s1": snapshot("s1"), "s2": snapshot("s2", text="edited")})
    assert result.assertions[0].context.external_edit_at == "2026-01-01T00:00:02Z"

    unstamped = scenario(
        ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),)),
        ScenarioPhase(
            phase_id="p2",
            ops=(ScenarioOp(op="external_edit", ref="edit"), ScenarioOp(op="snapshot", ref="s2")),
            expect=phase.expect,
        ),
        family_id="f12",
    )
    with pytest.raises(RunnerBindingError, match="stamped preceding external edit"):
        run_scenario(unstamped, snapshots={"s1": snapshot("s1"), "s2": snapshot("s2", text="edited")})


def test_later_binding_error_refuses_before_any_assertion_execution(monkeypatch) -> None:
    case = scenario(
        ScenarioPhase(
            phase_id="p1",
            ops=(ScenarioOp(op="snapshot", ref="s1"),),
            expect=(
                Expectation(
                    **{
                        "assert": "decision_distinguishable_from_hypothesis",
                        "subject": "decision",
                        "counterpart": "hypothesis",
                    }
                ),
            ),
        ),
        ScenarioPhase(
            phase_id="p2",
            ops=(ScenarioOp(op="external_edit", ref="edit"), ScenarioOp(op="snapshot", ref="s2")),
            expect=(
                Expectation(
                    **{
                        "assert": "external_edit_authoritative_within",
                        "subject": "file",
                        "freshness_bound_s": 60.0,
                    }
                ),
            ),
        ),
        family_id="f12",
    )
    resolved: list[str] = []

    def record(name: str):
        resolved.append(name)
        return lambda _context: AssertionResult(name=name, outcome="pass", evidence="unexpected")

    monkeypatch.setattr("epistemic.runner.resolve", record)
    with pytest.raises(RunnerBindingError, match="stamped preceding external edit"):
        run_scenario(
            case,
            snapshots={"s1": snapshot("s1", decision=True), "s2": snapshot("s2", text="edited")},
        )
    assert resolved == []


def test_frozen_registry_evaluation_returns_its_deterministic_result() -> None:
    case = scenario(
        ScenarioPhase(
            phase_id="p1",
            ops=(ScenarioOp(op="snapshot", ref="s1"),),
            expect=(
                Expectation(
                    **{
                        "assert": "decision_distinguishable_from_hypothesis",
                        "subject": "decision",
                        "counterpart": "hypothesis",
                    }
                ),
            ),
        ),
    )

    result = run_scenario(case, snapshots={"s1": snapshot("s1", decision=True)})

    assert result.assertions[0].assertion == "decision_distinguishable_from_hypothesis"
    assert result.assertions[0].result.outcome == "pass"


# --------------------------------------------------------------------------
# Review correction: feedback1.
# --------------------------------------------------------------------------


def feedback1_external_edit_case(*ops: ScenarioOp) -> Scenario:
    return scenario(
        ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),)),
        ScenarioPhase(
            phase_id="p2",
            ops=ops,
            expect=(
                Expectation(
                    **{
                        "assert": "external_edit_authoritative_within",
                        "subject": "file",
                        "freshness_bound_s": 60.0,
                    }
                ),
            ),
        ),
        family_id="f12",
    )


def test_feedback1_external_edit_pair_must_straddle_the_edit_in_op_order() -> None:
    case = feedback1_external_edit_case(
        ScenarioOp(op="snapshot", ref="s2"),
        ScenarioOp(op="external_edit", ref="edit", at="2026-01-01T00:00:01Z"),
    )

    with pytest.raises(RunnerBindingError, match="straddle"):
        run_scenario(
            case,
            snapshots={"s1": snapshot("s1"), "s2": snapshot("s2", text="edited")},
        )


def test_feedback1_external_edit_current_timestamp_cannot_precede_the_edit() -> None:
    case = feedback1_external_edit_case(
        ScenarioOp(op="external_edit", ref="edit", at="2026-01-01T00:00:09Z"),
        ScenarioOp(op="snapshot", ref="s2"),
    )

    with pytest.raises(RunnerBindingError, match="precedes"):
        run_scenario(
            case,
            snapshots={"s1": snapshot("s1"), "s2": snapshot("s2", text="edited")},
        )


@pytest.mark.parametrize(
    ("edit_at", "current_taken_at"),
    (
        ("not-a-timestamp", "2026-01-01T00:00:10Z"),
        ("2026-01-01T00:00:09Z", "2026-01-01T00:00:10"),
    ),
)
def test_feedback1_external_edit_timestamps_must_be_offset_aware_rfc3339(
    edit_at: str, current_taken_at: str
) -> None:
    case = feedback1_external_edit_case(
        ScenarioOp(op="external_edit", ref="edit", at=edit_at),
        ScenarioOp(op="snapshot", ref="s2"),
    )

    with pytest.raises(RunnerBindingError, match="timestamp"):
        run_scenario(
            case,
            snapshots={
                "s1": snapshot("s1"),
                "s2": snapshot("s2", text="edited").model_copy(
                    update={"taken_at": current_taken_at}
                ),
            },
        )


def feedback1_pair_case() -> Scenario:
    return scenario(
        ScenarioPhase(phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),)),
        ScenarioPhase(
            phase_id="p2",
            ops=(ScenarioOp(op="snapshot", ref="s2"),),
            expect=(Expectation(**{"assert": "export_reconstructs_state", "tolerance": 0.0}),),
        ),
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("provider", "other-provider", "provider"),
        ("variant", "other-variant", "variant"),
        (
            "projector",
            ProjectorMeta(
                name="other-projector",
                version="0.1.0",
                author="benchmark-harness",
                endpoints_used=("fixture:in-memory",),
                loc=1,
            ),
            "projector",
        ),
    ),
)
def test_feedback1_snapshot_identity_must_match_across_the_row(
    field: str, value: object, error: str
) -> None:
    first, second = snapshot("s1"), snapshot("s2")

    with pytest.raises(RunnerBindingError, match=error):
        run_scenario(feedback1_pair_case(), snapshots={"s1": first, "s2": second.model_copy(update={field: value})})


def test_feedback1_snapshot_refs_cannot_alias_one_observation_object() -> None:
    observed = snapshot("s1")

    with pytest.raises(RunnerBindingError, match="same observation object"):
        run_scenario(feedback1_pair_case(), snapshots={"s1": observed, "s2": observed})


def test_feedback1_later_unknown_registry_name_resolves_before_earlier_evaluation(monkeypatch) -> None:
    case = scenario(
        ScenarioPhase(
            phase_id="p1",
            ops=(ScenarioOp(op="snapshot", ref="s1"),),
            expect=(
                Expectation(
                    **{
                        "assert": "decision_distinguishable_from_hypothesis",
                        "subject": "decision",
                        "counterpart": "hypothesis",
                    }
                ),
            ),
        ),
        ScenarioPhase(
            phase_id="p2",
            ops=(ScenarioOp(op="snapshot", ref="s2"),),
            expect=(Expectation(**{"assert": "later-unknown"}),),
        ),
    )
    evaluated: list[str] = []

    def resolve_with_late_failure(name: str):
        if name == "later-unknown":
            raise RegistryError("late unknown")
        return lambda _context: evaluated.append(name) or AssertionResult(
            name=name, outcome="pass", evidence="unexpected"
        )

    monkeypatch.setattr("epistemic.runner.resolve", resolve_with_late_failure)
    with pytest.raises(RegistryError, match="late unknown"):
        run_scenario(
            case,
            snapshots={"s1": snapshot("s1", decision=True), "s2": snapshot("s2", decision=True)},
        )
    assert evaluated == []


def test_feedback1_bound_snapshots_are_deeply_isolated_from_caller_mutation() -> None:
    observed = snapshot("s1", decision=True)
    expected = observed.model_copy(deep=True)
    case = scenario(
        ScenarioPhase(
            phase_id="p1",
            ops=(ScenarioOp(op="snapshot", ref="s1"),),
            expect=(
                Expectation(
                    **{
                        "assert": "decision_distinguishable_from_hypothesis",
                        "subject": "decision",
                        "counterpart": "hypothesis",
                    }
                ),
            ),
        ),
    )

    result = run_scenario(case, snapshots={"s1": observed})
    observed.items = (StateItem(id="changed", kind="claim"),)

    assert result.assertions[0].context.snapshot == expected
    assert result.assertions[0].context.snapshot is not observed
    assert result.assertions[0].result.outcome == "pass"


# --------------------------------------------------------------------------
# Independent recheck: feedback2.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "edit_at",
    (
        "2026-W01-4T00:00:01Z",
        "2026-01-01T00:00:01+0000",
        "2026-01-01T00:00:01,5Z",
    ),
)
def test_feedback2_rejects_non_rfc3339_datetime_lexemes(edit_at: str) -> None:
    case = feedback1_external_edit_case(
        ScenarioOp(op="external_edit", ref="edit", at=edit_at),
        ScenarioOp(op="snapshot", ref="s2"),
    )

    with pytest.raises(RunnerBindingError, match="timestamp"):
        run_scenario(
            case,
            snapshots={"s1": snapshot("s1"), "s2": snapshot("s2", text="edited")},
        )


@pytest.mark.parametrize(
    "edit_at",
    (
        "2026-01-01T00:00:01Z",
        "2026-01-01T00:00:01+00:00",
        "2026-01-01T00:00:01.5Z",
    ),
)
def test_feedback2_accepts_rfc3339_datetime_lexemes(edit_at: str) -> None:
    case = feedback1_external_edit_case(
        ScenarioOp(op="external_edit", ref="edit", at=edit_at),
        ScenarioOp(op="snapshot", ref="s2"),
    )

    result = run_scenario(
        case,
        snapshots={"s1": snapshot("s1"), "s2": snapshot("s2", text="edited")},
    )

    assert result.assertions[0].result.outcome == "pass"
