from __future__ import annotations

import json
from pathlib import Path

import pytest

from epistemic.assertions import AssertionResult


def _ref(path: str = "evidence/failure.json"):
    from epistemic.evidence import AssertionEvidenceRef

    return AssertionEvidenceRef(path=path, sha256="a" * 64)


def _key(**changes):
    from epistemic.cohort import CohortExpectationIdentity

    payload = {
        "scenario_id": "scenario-1",
        "scenario_sha256": "1" * 64,
        "phase_id": "p1",
        "expectation_ordinal": 1,
        "assertion": "exactly_one_current_revision",
        "subject": "chain",
        "counterpart": None,
        "tolerance": 0.0,
        "freshness_bound_s": None,
    }
    payload.update(changes)
    return CohortExpectationIdentity(**payload)


def _cell(outcome: str = "pass", *, key=None, evidence_ref=None):
    from epistemic.cohort import CohortAssertionResult

    identity = key or _key()
    return CohortAssertionResult(
        identity=identity,
        result=AssertionResult(
            name=identity.assertion, outcome=outcome, evidence=f"{identity.assertion}: {outcome}",
            subject=identity.subject,
        ),
        evidence_ref=evidence_ref if evidence_ref is not None else _ref(),
    )


def _row(provider: str, *cells, variant: str | None = None):
    from epistemic.cohort import EpistemicCohortRow

    return EpistemicCohortRow(
        provider=provider,
        variant=variant or ("control" if provider in {"grep-markdown", "no-memory"} else "native"),
        assertions=tuple(cells or (_cell(),)),
    )


def _valid_rows(run_root: Path, *, product_outcome: str = "pass"):
    return (
        _row(
            "product",
            _recheck3_replayable_cell(
                run_root, provider="product", variant="native", outcome=product_outcome
            ),
        ),
        _row(
            "grep-markdown",
            _recheck3_replayable_cell(
                run_root, provider="grep-markdown", variant="control", outcome="fail"
            ),
        ),
        _row(
            "no-memory",
            _recheck3_replayable_cell(
                run_root, provider="no-memory", variant="control", outcome="fail"
            ),
        ),
    )


def test_validated_cohort_has_exact_ordered_identity_and_schema_v1(tmp_path: Path) -> None:
    from epistemic.cohort import validate_epistemic_cohort

    cohort = validate_epistemic_cohort(
        run_id="run-1", rows=_valid_rows(tmp_path), run_root=tmp_path
    )
    assert cohort.artifact_type == "validated-epistemic-cohort.v1"
    assert cohort.schema_version == 1
    assert [row.provider for row in cohort.rows] == ["product", "grep-markdown", "no-memory"]
    assert cohort.rows[0].assertions[0].identity == cohort.rows[1].assertions[0].identity


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scenario_id", "scenario-other"),
        ("scenario_sha256", "2" * 64),
        ("phase_id", "p2"),
        ("expectation_ordinal", 2),
        ("assertion", "prior_revision_retained"),
        ("subject", "other"),
        ("counterpart", "other"),
        ("tolerance", 0.1),
        ("freshness_bound_s", 10.0),
    ],
)
def test_any_exact_cohort_identity_difference_refuses(
    tmp_path: Path, field: str, value
) -> None:
    from epistemic.cohort import CohortValidationError, validate_epistemic_cohort

    changed = _key(**{field: value})
    rows = (
        _row("product", _cell()),
        _row("grep-markdown", _cell("fail", key=changed)),
        _row("no-memory", _cell("fail")),
    )
    with pytest.raises(CohortValidationError, match="ordered cohort"):
        validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=tmp_path)


def test_order_not_set_equality_defines_the_cohort(tmp_path: Path) -> None:
    from epistemic.cohort import CohortValidationError, validate_epistemic_cohort

    one = _cell(key=_key(expectation_ordinal=1))
    two = _cell(key=_key(expectation_ordinal=2, assertion="prior_revision_retained"))
    rows = (
        _row("product", one, two),
        _row("grep-markdown", _cell("fail", key=two.identity), _cell("fail", key=one.identity)),
        _row("no-memory", _cell("fail", key=one.identity), _cell("fail", key=two.identity)),
    )
    with pytest.raises(CohortValidationError, match="ordered cohort"):
        validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=tmp_path)


@pytest.mark.parametrize("missing", ["grep-markdown", "no-memory"])
def test_exact_controls_are_required(tmp_path: Path, missing: str) -> None:
    from epistemic.cohort import CohortValidationError, validate_epistemic_cohort

    rows = tuple(row for row in _valid_rows(tmp_path) if row.provider != missing)
    with pytest.raises(CohortValidationError, match=missing):
        validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=tmp_path)


@pytest.mark.parametrize("control", ["grep-markdown", "no-memory"])
def test_each_control_scenario_requires_at_least_one_pass_or_fail(
    tmp_path: Path, control: str
) -> None:
    from epistemic.cohort import CohortValidationError, validate_epistemic_cohort

    rows = list(_valid_rows(tmp_path))
    index = next(i for i, row in enumerate(rows) if row.provider == control)
    rows[index] = _row(
        control,
        _recheck3_replayable_cell(
            tmp_path, provider=control, variant="control", outcome="unsupported"
        ),
    )
    with pytest.raises(CohortValidationError, match=f"{control}.*scenario-1"):
        validate_epistemic_cohort(
            run_id="run-1", rows=tuple(rows), run_root=tmp_path
        )


@pytest.mark.parametrize(
    "product_outcome",
    ["pass", "fail", "not_applicable", "unsupported", "blocked"],
)
def test_control_pass_preserves_every_product_outcome_but_masks_product_signal(
    tmp_path: Path, product_outcome: str,
) -> None:
    from epistemic.cohort import iter_strategy_gate_assertions, validate_epistemic_cohort

    cohort = validate_epistemic_cohort(
        run_id="run-1",
        rows=(
            _row(
                "product",
                _five_value_replayable_cell(
                    tmp_path, provider="product", variant="native", outcome=product_outcome
                ),
            ),
            _row(
                "grep-markdown",
                _five_value_replayable_cell(
                    tmp_path, provider="grep-markdown", variant="control", outcome="pass"
                ),
            ),
            _row(
                "no-memory",
                _five_value_replayable_cell(
                    tmp_path, provider="no-memory", variant="control", outcome="fail"
                ),
            ),
        ),
        run_root=tmp_path,
    )
    product = cohort.rows[0].assertions[0]
    assert product.result.outcome == product_outcome
    assert product.signal_disposition == "no_product_signal"
    assert list(iter_strategy_gate_assertions(cohort, run_root=tmp_path)) == []


def test_unmasked_product_results_are_included_in_gate_iterator_without_g2_threshold(
    tmp_path: Path,
) -> None:
    from epistemic.cohort import iter_strategy_gate_assertions, validate_epistemic_cohort

    cohort = validate_epistemic_cohort(
        run_id="run-1",
        rows=_valid_rows(tmp_path, product_outcome="pass"),
        run_root=tmp_path,
    )
    yielded = list(iter_strategy_gate_assertions(cohort, run_root=tmp_path))
    assert len(yielded) == 1
    assert yielded[0].result.outcome == "pass"
    assert not hasattr(cohort, "g2")
    assert not hasattr(cohort, "threshold")


def test_cohort_persists_and_reloads_only_after_validation(tmp_path: Path) -> None:
    from epistemic.cohort import load_validated_cohort, persist_validated_cohort, validate_epistemic_cohort

    cohort = validate_epistemic_cohort(
        run_id="run-1", rows=_valid_rows(tmp_path), run_root=tmp_path
    )
    path = persist_validated_cohort(
        tmp_path / "cohort.json", cohort, run_root=tmp_path
    )
    assert json.loads(path.read_text())["artifact_type"] == "validated-epistemic-cohort.v1"
    assert load_validated_cohort(path, run_root=tmp_path) == cohort


def test_persist_refuses_unvalidated_rows_object(tmp_path: Path) -> None:
    from epistemic.cohort import ValidatedEpistemicCohort, persist_validated_cohort

    with pytest.raises(TypeError, match="ValidatedEpistemicCohort"):
        persist_validated_cohort(
            tmp_path / "cohort.json", _valid_rows(tmp_path), run_root=tmp_path
        )
    mislabeled = ValidatedEpistemicCohort(
        artifact_type="validated-epistemic-cohort.v1",
        schema_version=1,
        run_id="run-1",
        rows=(_row("product"),),
    )
    with pytest.raises(ValueError, match="validated.*cohort|control"):
        persist_validated_cohort(tmp_path / "cohort.json", mislabeled, run_root=tmp_path)


def test_load_revalidates_semantics_instead_of_trusting_the_artifact_label(tmp_path: Path) -> None:
    from epistemic.cohort import load_validated_cohort, persist_validated_cohort, validate_epistemic_cohort

    path = persist_validated_cohort(
        tmp_path / "cohort.json",
        validate_epistemic_cohort(
            run_id="run-1", rows=_valid_rows(tmp_path), run_root=tmp_path
        ),
        run_root=tmp_path,
    )
    payload = json.loads(path.read_text())
    payload["rows"] = [row for row in payload["rows"] if row["provider"] != "no-memory"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="validated.*cohort|no-memory"):
        load_validated_cohort(path, run_root=tmp_path)


def test_failed_cohort_cell_requires_evidence_ref() -> None:
    from epistemic.cohort import CohortAssertionResult

    with pytest.raises(Exception, match="reference"):
        CohortAssertionResult(
            identity=_key(),
            result=AssertionResult(name=_key().assertion, outcome="fail", evidence="failed"),
            evidence_ref=None,
        )


# ---------------------------------------------------------------------------
# Independent final recheck: every cell is replayed before claims or masking.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome", ["pass", "fail", "not_applicable", "unsupported", "blocked"]
)
def test_recheck3_every_five_valued_cohort_claim_requires_evidence(outcome: str) -> None:
    from epistemic.cohort import CohortAssertionResult

    with pytest.raises(Exception, match="evidence|reference"):
        CohortAssertionResult(
            identity=_key(),
            result=AssertionResult(
                name=_key().assertion,
                outcome=outcome,
                evidence=f"claimed {outcome}",
                subject="chain",
            ),
            evidence_ref=None,
        )


def _recheck3_replayable_cell(
    run_root: Path,
    *,
    provider: str,
    variant: str,
    outcome: str,
):
    from epistemic.assertions import AssertionContext, no_retired_state_served_as_current
    from epistemic.cohort import CohortAssertionResult
    from epistemic.evidence import persist_assertion_evidence
    from epistemic.snapshot import EpistemicStateSnapshot, FieldDeclaration, ProjectorMeta, StateItem

    current = "yes" if outcome == "fail" else "no"
    declaration_status = {
        "unsupported": "unavailable",
        "not_applicable": "absent_by_design",
    }.get(outcome, "declared")
    snapshot = EpistemicStateSnapshot(
        provider=provider,
        variant=variant,
        phase="p1",
        taken_at="2026-08-11T00:00:00Z",
        items=(
            StateItem(
                id="chain",
                kind="claim",
                title="chain",
                current=current,
                retired_reason="superseded",
            ),
        ),
        declarations=(
            FieldDeclaration(
                field="current",
                status=declaration_status,
                evidence="https://example.invalid/current",
            ),
        ),
        projector=ProjectorMeta(
            name="fixture",
            version="1",
            author="test",
            endpoints_used=("broker:state.read",),
            loc=1,
        ),
    )
    context = AssertionContext(snapshot=snapshot, subject="chain")
    result = no_retired_state_served_as_current(context)
    assert result.outcome == outcome
    reference = persist_assertion_evidence(
        run_root=run_root,
        scenario_id="scenario-1",
        scenario_sha256="1" * 64,
        family_id="f01",
        phase_id="p1",
        expectation_ordinal=1,
        assertion=result.name,
        context=context,
        result=result,
    )
    return CohortAssertionResult(
        identity=_key(assertion=result.name), result=result, evidence_ref=reference
    )


def _five_value_replayable_cell(
    run_root: Path,
    *,
    provider: str,
    variant: str,
    outcome: str,
):
    from epistemic.assertions import AssertionContext, external_edit_authoritative_within
    from epistemic.cohort import CohortAssertionResult
    from epistemic.evidence import persist_assertion_evidence
    from epistemic.snapshot import EpistemicStateSnapshot, FieldDeclaration, ProjectorMeta, StateItem

    declaration_status = {
        "unsupported": "unavailable",
        "not_applicable": "absent_by_design",
    }.get(outcome, "declared")

    def snapshot(*, title: str, taken_at: str, phase: str) -> EpistemicStateSnapshot:
        return EpistemicStateSnapshot(
            provider=provider,
            variant=variant,
            phase=phase,
            taken_at=taken_at,
            items=(
                StateItem(
                    id="chain",
                    kind="claim",
                    title=title,
                    current="yes",
                ),
            ),
            declarations=(
                FieldDeclaration(
                    field="external_edit",
                    status=declaration_status,
                    evidence="https://example.invalid/external-edit",
                ),
            ),
            projector=ProjectorMeta(
                name="fixture",
                version="1",
                author="test",
                endpoints_used=("broker:state.read",),
                loc=1,
            ),
        )

    prior = snapshot(
        title="before", taken_at="2026-08-11T00:00:00+00:00", phase="p0"
    )
    changed_title = "before" if outcome == "fail" else "after"
    current_time = (
        "not-a-timestamp"
        if outcome == "blocked"
        else "2026-08-11T00:00:05+00:00"
    )
    current = snapshot(title=changed_title, taken_at=current_time, phase="p1")
    context = AssertionContext(
        snapshot=current,
        prior=prior,
        subject="chain",
        freshness_bound_s=10.0,
        external_edit_at="2026-08-11T00:00:01+00:00",
    )
    result = external_edit_authoritative_within(context)
    assert result.outcome == outcome
    reference = persist_assertion_evidence(
        run_root=run_root,
        scenario_id="scenario-1",
        scenario_sha256="1" * 64,
        family_id="f01",
        phase_id="p1",
        expectation_ordinal=1,
        assertion=result.name,
        context=context,
        result=result,
    )
    return CohortAssertionResult(
        identity=_key(
            assertion=result.name,
            freshness_bound_s=10.0,
        ),
        result=result,
        evidence_ref=reference,
    )


def test_recheck3_control_pass_is_not_masked_until_its_evidence_replays(
    tmp_path: Path,
) -> None:
    from epistemic.cohort import CohortValidationError, EpistemicCohortRow, validate_epistemic_cohort

    product = _recheck3_replayable_cell(
        tmp_path, provider="product", variant="native", outcome="pass"
    )
    forged_control_pass = _recheck3_replayable_cell(
        tmp_path, provider="grep-markdown", variant="control", outcome="fail"
    ).model_copy(
        update={
            "result": AssertionResult(
                name=product.identity.assertion,
                outcome="pass",
                evidence="fabricated control pass",
                subject="chain",
            )
        }
    )
    no_memory = _recheck3_replayable_cell(
        tmp_path, provider="no-memory", variant="control", outcome="fail"
    )
    rows = (
        EpistemicCohortRow(provider="product", variant="native", assertions=(product,)),
        EpistemicCohortRow(
            provider="grep-markdown", variant="control", assertions=(forged_control_pass,)
        ),
        EpistemicCohortRow(
            provider="no-memory", variant="control", assertions=(no_memory,)
        ),
    )

    with pytest.raises(CohortValidationError, match="replay|result|evidence"):
        validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=tmp_path)


def test_recheck3_gate_iterator_rereads_every_cell_evidence(
    tmp_path: Path,
) -> None:
    from epistemic.cohort import (
        CohortValidationError,
        EpistemicCohortRow,
        iter_strategy_gate_assertions,
        validate_epistemic_cohort,
    )

    cells = {
        ("product", "native"): _recheck3_replayable_cell(
            tmp_path, provider="product", variant="native", outcome="pass"
        ),
        ("grep-markdown", "control"): _recheck3_replayable_cell(
            tmp_path, provider="grep-markdown", variant="control", outcome="fail"
        ),
        ("no-memory", "control"): _recheck3_replayable_cell(
            tmp_path, provider="no-memory", variant="control", outcome="fail"
        ),
    }
    rows = tuple(
        EpistemicCohortRow(provider=provider, variant=variant, assertions=(cell,))
        for (provider, variant), cell in cells.items()
    )
    cohort = validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=tmp_path)
    product_ref = cells[("product", "native")].evidence_ref
    assert product_ref is not None
    path = tmp_path / product_ref.path
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(CohortValidationError, match="replay|digest|evidence"):
        list(iter_strategy_gate_assertions(cohort, run_root=tmp_path))
