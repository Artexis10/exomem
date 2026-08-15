"""Suppression semantics: catastrophic integrity, N/A poisoning, no aggregates."""

from __future__ import annotations

from epistemic.assertions import AssertionResult
from epistemic.catastrophic import CATASTROPHIC_ASSERTIONS, catastrophic_failures
from epistemic.scoring import FamilyResult, RunResult, assemble_family, assemble_run
from pydantic import BaseModel

BANNED_FIELD_TOKENS = ("score", "total", "overall", "aggregate", "points", "mean", "average")


def result(name: str, outcome: str, subject: str | None = None) -> AssertionResult:
    return AssertionResult(
        name=name, outcome=outcome, evidence=f"{name} -> {outcome}", subject=subject
    )


def family(
    family_id: str,
    provider: str,
    results: tuple[AssertionResult, ...],
    variant: str = "native",
) -> FamilyResult:
    return assemble_family(
        family_id=family_id, provider=provider, variant=variant, assertion_results=results
    )


def test_catastrophic_failures_are_detected_by_the_frozen_set() -> None:
    results = (
        result("evidence_path_resolves", "fail"),
        result("uncertainty_declared", "fail"),
    )
    failures = catastrophic_failures(results)
    assert tuple(item.name for item in failures) == ("evidence_path_resolves",)
    assert "uncertainty_declared" not in CATASTROPHIC_ASSERTIONS


def test_non_fail_outcomes_of_catastrophic_assertions_are_not_failures() -> None:
    for outcome in ("pass", "not_applicable", "unsupported", "blocked"):
        assert catastrophic_failures((result("no_cross_case_residue", outcome),)) == ()


def test_catastrophic_failure_marks_the_family_integrity_fail() -> None:
    scored = family("f05", "alpha", (result("prior_revision_retained", "fail"),))
    assert scored.status == "INTEGRITY_FAIL"
    assert scored.catastrophic_failures == ("prior_revision_retained",)


def test_refuted_retrieval_failure_is_now_an_integrity_failure() -> None:
    """The founder's `accept` decision, in the one place it has to show up.

    f18's assertion joined the §3 set at the 2026-08-15 acknowledgment, so
    silently losing or demoting a refuted result now suppresses every aggregate
    for that provider — the same treatment as losing a prior revision, which is
    the parity the amendment argued for. Before acknowledgment this same failure
    merely cost a row.
    """

    assert "refuted_retrievable_at_full_standing" in CATASTROPHIC_ASSERTIONS

    scored = family(
        "f18", "alpha", (result("refuted_retrievable_at_full_standing", "fail"),)
    )
    assert scored.status == "INTEGRITY_FAIL"
    assert scored.catastrophic_failures == ("refuted_retrievable_at_full_standing",)


def test_refuted_retrieval_observability_outcomes_stay_out_of_integrity() -> None:
    """The other direction, which the module's docstring insists on.

    ``not_applicable``, ``unsupported`` and ``blocked`` are statements about what
    could be observed, not about harm done. Promoting the new member must not
    turn an honest declaration into an integrity failure — that would make
    declaring a limitation more expensive than hiding one.
    """

    for outcome in ("pass", "not_applicable", "unsupported", "blocked"):
        results = (result("refuted_retrievable_at_full_standing", outcome),)
        assert catastrophic_failures(results) == ()
        assert family("f18", "alpha", results).status != "INTEGRITY_FAIL"


def test_integrity_failure_suppresses_every_aggregate_for_that_provider() -> None:
    run = assemble_run(
        run_id="run-1",
        families=(
            family("f01", "alpha", (result("no_retired_state_served_as_current", "fail"),)),
            family("f07", "alpha", (result("decision_distinguishable_from_hypothesis", "pass"),)),
            family("f07", "beta", (result("decision_distinguishable_from_hypothesis", "pass"),)),
        ),
    )
    assert run.provider_status["alpha"] == "INTEGRITY_FAIL"
    assert run.provider_status["beta"] == "OK"
    assert "alpha" in run.suppressed_providers
    assert "beta" not in run.suppressed_providers
    alpha_rows = [row for row in run.families if row.provider == "alpha"]
    assert alpha_rows
    assert all(row.status == "INTEGRITY_FAIL" for row in alpha_rows)
    beta_rows = [row for row in run.families if row.provider == "beta"]
    assert all(row.status == "scored" for row in beta_rows)
    assert any("no_retired_state_served_as_current" in note for note in run.exclusions)


def test_not_applicable_poisons_the_family_for_every_provider() -> None:
    run = assemble_run(
        run_id="run-2",
        families=(
            family("f08", "alpha", (result("open_question_queryable", "not_applicable"),)),
            family("f08", "beta", (result("open_question_queryable", "pass"),)),
            family("f07", "alpha", (result("decision_distinguishable_from_hypothesis", "pass"),)),
            family("f07", "beta", (result("decision_distinguishable_from_hypothesis", "pass"),)),
        ),
    )
    assert run.family_comparability["f08"] is False
    assert run.family_comparability["f07"] is True
    f08_rows = [row for row in run.families if row.family_id == "f08"]
    assert len(f08_rows) == 2
    assert all(row.comparable is False for row in f08_rows)
    f07_rows = [row for row in run.families if row.family_id == "f07"]
    assert all(row.comparable is True for row in f07_rows)
    assert any("f08" in note and "not_applicable" in note for note in run.exclusions)


def test_unsupported_does_not_poison_the_family() -> None:
    run = assemble_run(
        run_id="run-3",
        families=(family("f06", "alpha", (result("evidence_path_exists", "unsupported"),)),),
    )
    assert run.family_comparability["f06"] is True


def test_blocked_marks_the_family_blocked_without_integrity_failure() -> None:
    scored = family("f12", "alpha", (result("external_edit_authoritative_within", "blocked"),))
    assert scored.status == "blocked"
    assert scored.catastrophic_failures == ()


def _field_names(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for name, info in model.model_fields.items():
        names.add(name)
        annotation = info.annotation
        for candidate in (annotation, *getattr(annotation, "__args__", ())):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                names |= _field_names(candidate, seen)
    return names


def test_result_models_carry_no_aggregate_score_field() -> None:
    names = _field_names(RunResult) | _field_names(FamilyResult)
    assert names
    offenders = sorted(
        name for name in names if any(token in name.lower() for token in BANNED_FIELD_TOKENS)
    )
    assert offenders == []


def test_run_result_round_trips_as_json() -> None:
    run = assemble_run(
        run_id="run-4",
        families=(family("f01", "alpha", (result("exactly_one_current_revision", "pass"),)),),
    )
    assert RunResult.model_validate_json(run.model_dump_json()) == run
