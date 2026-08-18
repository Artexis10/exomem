from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest
from epistemic.assertions import AssertionContext
from epistemic.snapshot import EpistemicStateSnapshot, FieldDeclaration, ProjectorMeta, StateItem


def _snapshot() -> EpistemicStateSnapshot:
    return EpistemicStateSnapshot(
        provider="prod|uct", variant="native", phase="p1", taken_at="2026-08-11T00:00:00Z",
        items=(
            StateItem(
                id="retired", kind="claim", title="retired", text="stale",
                current="yes", retired_reason="superseded",
            ),
        ),
        declarations=(
            FieldDeclaration(field="current", status="declared", evidence="https://example.invalid/current"),
        ),
        projector=ProjectorMeta(
            name="fixture", version="1", author="test", endpoints_used=("broker:state.read",), loc=1,
        ),
    )


def _stored_cohort(tmp_path: Path, *, escaped_ref: bool = False) -> Path:
    from epistemic.assertions import no_retired_state_served_as_current
    from epistemic.cohort import (
        CohortAssertionResult,
        CohortExpectationIdentity,
        EpistemicCohortRow,
        persist_validated_cohort,
        validate_epistemic_cohort,
    )
    from epistemic.evidence import AssertionEvidenceRef, persist_assertion_evidence

    context = AssertionContext(snapshot=_snapshot(), subject="retired")
    failed = no_retired_state_served_as_current(context)
    assert failed.outcome == "fail"
    evidence_ref = persist_assertion_evidence(
        run_root=tmp_path,
        scenario_id="scenario|one",
        scenario_sha256="1" * 64,
        family_id="f01",
        phase_id="p1",
        expectation_ordinal=1,
        assertion=failed.name,
        context=context,
        result=failed,
    )
    if escaped_ref:
        target = Path("assertion-evidence/fail|proof.json")
        (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / target).write_bytes((tmp_path / evidence_ref.path).read_bytes())
        evidence_ref = AssertionEvidenceRef(
            path=target.as_posix(), sha256=hashlib.sha256((tmp_path / target).read_bytes()).hexdigest()
        )
    identity = CohortExpectationIdentity(
        scenario_id="scenario|one", scenario_sha256="1" * 64,
        phase_id="p1", expectation_ordinal=1, assertion=failed.name,
        subject="retired", counterpart=None, tolerance=0.0, freshness_bound_s=None,
    )
    product = CohortAssertionResult(identity=identity, result=failed, evidence_ref=evidence_ref)
    def control_cell(provider: str) -> CohortAssertionResult:
        control_snapshot = _snapshot().model_copy(
            update={
                "provider": provider,
                "variant": "control",
                "items": tuple(
                    item.model_copy(update={"current": "no"})
                    for item in _snapshot().items
                ),
            }
        )
        control_context = AssertionContext(snapshot=control_snapshot, subject="retired")
        control_result = no_retired_state_served_as_current(control_context)
        assert control_result.outcome == "pass"
        control_ref = persist_assertion_evidence(
            run_root=tmp_path,
            scenario_id="scenario|one",
            scenario_sha256="1" * 64,
            family_id="f01",
            phase_id="p1",
            expectation_ordinal=1,
            assertion=control_result.name,
            context=control_context,
            result=control_result,
        )
        return CohortAssertionResult(
            identity=identity,
            result=control_result,
            evidence_ref=control_ref,
        )
    rows = (
        EpistemicCohortRow(provider="prod|uct", variant="native", assertions=(product,)),
        EpistemicCohortRow(
            provider="grep-markdown",
            variant="control",
            assertions=(control_cell("grep-markdown"),),
        ),
        EpistemicCohortRow(
            provider="no-memory",
            variant="control",
            assertions=(control_cell("no-memory"),),
        ),
    )
    cohort = validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=tmp_path)
    return persist_validated_cohort(
        tmp_path / "validated-cohort.v1.json", cohort, run_root=tmp_path
    )


def test_sole_public_renderer_accepts_only_a_stored_validated_cohort(tmp_path: Path) -> None:
    import epistemic.report as report

    assert report.__all__ == ["render_epistemic_report"]
    with pytest.raises(TypeError):
        report.render_epistemic_report({}, run_root=tmp_path)
    invalid = tmp_path / "rows.json"
    invalid.write_text(json.dumps({"rows": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="validated.*cohort"):
        report.render_epistemic_report(invalid, run_root=tmp_path)


def test_renderer_includes_exact_controls_and_catastrophic_headline_artifact_path(tmp_path: Path) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    rendered = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert "grep-markdown" in rendered
    assert "no-memory" in rendered
    assert "INTEGRITY FAIL" in rendered
    assert "no_retired_state_served_as_current" in rendered
    cohort = json.loads(path.read_text())
    evidence_path = cohort["rows"][0]["assertions"][0]["evidence_ref"]["path"]
    assert evidence_path in rendered
    assert "Signal disposition" in rendered
    assert "product_signal" in rendered


def test_unreplayable_failure_suppresses_headline_and_product_row(tmp_path: Path) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    cohort = json.loads(path.read_text())
    evidence_path = tmp_path / cohort["rows"][0]["assertions"][0]["evidence_ref"]["path"]
    evidence_path.write_bytes(evidence_path.read_bytes() + b"tampered")
    rendered = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert "WITHHELD" in rendered
    assert "unreplayable assertion evidence" in rendered
    assert "INTEGRITY FAIL" not in rendered
    assert "| prod" not in rendered


def test_failure_evidence_must_bind_the_same_cohort_identity(tmp_path: Path) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    cohort = json.loads(path.read_text())
    for row in cohort["rows"]:
        row["assertions"][0]["identity"]["scenario_id"] = "different-scenario"
    path.write_text(json.dumps(cohort), encoding="utf-8")
    rendered = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert "WITHHELD" in rendered
    assert "evidence identity" in rendered
    assert "INTEGRITY FAIL" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider", "other-product"), ("variant", "other-variant")],
)
def test_failure_evidence_cannot_be_substituted_across_provider_or_variant(
    tmp_path: Path, field: str, value: str
) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    cohort = json.loads(path.read_text())
    cohort["rows"][0][field] = value
    path.write_text(json.dumps(cohort), encoding="utf-8")

    rendered = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert "WITHHELD" in rendered
    assert "provider" in rendered.lower() or "variant" in rendered.lower()
    assert "INTEGRITY FAIL" not in rendered


@pytest.mark.skipif(
    os.name == "nt",
    reason="the fixture needs a filename Windows reserves",
)
def test_markdown_cells_and_catastrophic_artifact_paths_are_escaped(tmp_path: Path) -> None:
    from epistemic.report import render_epistemic_report

    rendered = render_epistemic_report(
        _stored_cohort(tmp_path, escaped_ref=True), run_root=tmp_path, offline=True
    )
    assert "prod\\|uct" in rendered
    assert "scenario\\|one" in rendered
    assert "fail\\|proof.json" in rendered
    assert "| prod|uct |" not in rendered


def test_offline_regeneration_uses_the_shared_network_guard(tmp_path: Path, monkeypatch) -> None:
    import epistemic.report as report

    path = _stored_cohort(tmp_path)
    original = report._render_validated_cohort

    def attempt_network(*args, **kwargs):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.connect(("127.0.0.1", 9))
        return original(*args, **kwargs)

    monkeypatch.setattr(report, "_render_validated_cohort", attempt_network)
    with pytest.raises(OSError, match="offline report generation forbids socket.connect"):
        report.render_epistemic_report(path, run_root=tmp_path, offline=True)


def test_offline_regeneration_is_byte_identical(tmp_path: Path) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    first = render_epistemic_report(path, run_root=tmp_path, offline=True)
    second = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert first == second


# ---------------------------------------------------------------------------
# Independent final recheck: every rendered claim is evidence-reconstructed.
# ---------------------------------------------------------------------------


def test_recheck3_renderer_withholds_fabricated_product_pass_without_evidence(
    tmp_path: Path,
) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    cohort = json.loads(path.read_text(encoding="utf-8"))
    product = cohort["rows"][0]["assertions"][0]
    product["result"] = {
        "name": product["identity"]["assertion"],
        "outcome": "pass",
        "evidence": "fabricated product pass",
        "subject": product["identity"]["subject"],
    }
    product["evidence_ref"] = None
    path.write_text(json.dumps(cohort), encoding="utf-8")

    rendered = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert "WITHHELD" in rendered
    assert "fabricated product pass" not in rendered
    assert "| prod\\|uct |" not in rendered


def test_recheck3_renderer_replays_control_pass_before_signal_masking(
    tmp_path: Path,
) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    cohort = json.loads(path.read_text(encoding="utf-8"))
    failure_ref = cohort["rows"][0]["assertions"][0]["evidence_ref"]
    for row in cohort["rows"][1:]:
        row["assertions"][0]["evidence_ref"] = failure_ref
    path.write_text(json.dumps(cohort), encoding="utf-8")

    rendered = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert "WITHHELD" in rendered
    assert "control" in rendered.lower() or "evidence" in rendered.lower()
    assert "no_product_signal" not in rendered


@pytest.mark.parametrize("tampered_control", ["grep-markdown", "no-memory"])
def test_finalreview_any_control_replay_failure_suppresses_the_entire_cohort_table(
    tmp_path: Path, tampered_control: str
) -> None:
    from epistemic.report import render_epistemic_report

    path = _stored_cohort(tmp_path)
    cohort = json.loads(path.read_text(encoding="utf-8"))
    row = next(item for item in cohort["rows"] if item["provider"] == tampered_control)
    reference = row["assertions"][0]["evidence_ref"]
    evidence_path = tmp_path / reference["path"]
    evidence_path.write_bytes(evidence_path.read_bytes() + b"tampered")

    rendered = render_epistemic_report(path, run_root=tmp_path, offline=True)
    assert "WITHHELD" in rendered
    assert "## Validated cohort" not in rendered
    assert "| Provider |" not in rendered
    assert "prod\\|uct" not in rendered
    assert not (
        "| grep-markdown |" in rendered and "| no-memory |" not in rendered
    )
    assert not (
        "| no-memory |" in rendered and "| grep-markdown |" not in rendered
    )
