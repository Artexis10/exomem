"""Comparative reader: protocol gates, artifact digests, independent regrade."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
from protocol.contracts import AmendmentAcknowledgmentPendingError, ContractIdentityError, derive_preregistration_identity

from tests.test_utility_runner_integrity import _Backend, _CellCM, _product_root

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_run(tmp_path, variants=("helpful_history",)):
    from membench.utility.runner import run_utility

    product_root = _product_root(tmp_path)
    asyncio.run(run_utility(
        tmp_path / "run", seed=7, product_root=product_root, python=Path(sys.executable),
        tokenizer_path=product_root / "src" / "exomem" / "_scaffold" / "_Schema" / "SKILL.md",
        profile="fixture", cap_usd=2.0, paid=True, approval_token="operator-approved",
        phase_seconds=5, variants=variants,
        identity_provider=lambda root: derive_preregistration_identity(REPO_ROOT),
        family_gate=lambda identity, families: None,
        cell_factory=lambda root, **kw: _CellCM(root),
        backend_factory=lambda root, cap, token: _Backend(root, cap, token),
    ))
    return tmp_path / "run", product_root


def _load(run_dir, product_root, **kwargs):
    from membench.utility.runner import load_report

    defaults = dict(
        allow_synthetic=True,
        identity_validator=lambda identity, root: None,
        family_gate=lambda identity, families: None,
        fixture_gate=lambda family_id, root: None,
    )
    defaults.update(kwargs)
    return load_report(run_dir, product_root, **defaults)


def test_reader_recomputes_outcomes_from_observed_evidence(tmp_path):
    run_dir, product_root = _make_run(tmp_path)
    loaded = _load(run_dir, product_root)
    assert loaded["synthetic"] is True
    assert loaded["recomputed"]["scores"]["helpful_history"]["pair"]["attempted"] == 1
    assert loaded["manifest"]["scenario"]["seed"] == 7
    for arm in ("control", "memory"):
        checks = json.loads((run_dir / "pairs" / "helpful_history" / arm / "assertions.json").read_text())
        assert checks == {"utility_action_state_valid": "fail", "utility_no_prohibited_effects": "pass"}


def test_reader_refuses_a_synthetic_run_for_comparative_claims(tmp_path):
    from membench.utility.runner import UtilityReportError

    run_dir, product_root = _make_run(tmp_path)
    with pytest.raises(UtilityReportError, match="synthetic"):
        _load(run_dir, product_root, allow_synthetic=False)


def test_reader_requests_exactly_the_registered_family(tmp_path):
    run_dir, product_root = _make_run(tmp_path)
    seen = {}
    _load(run_dir, product_root,
          family_gate=lambda identity, families: seen.update(families=list(families)),
          fixture_gate=lambda family_id, root: seen.update(fixture=(family_id, root)))
    assert seen["families"] == ["f32"]
    assert seen["fixture"][0] == "f32" and Path(seen["fixture"][1]) == Path(product_root)


def test_pending_amendment_blocks_the_reader(tmp_path):
    def pending(identity, families):
        raise AmendmentAcknowledgmentPendingError("amendment sequence 6 founder acknowledgment is pending")

    run_dir, product_root = _make_run(tmp_path)
    with pytest.raises(AmendmentAcknowledgmentPendingError):
        _load(run_dir, product_root, family_gate=pending)


def test_pending_fixture_release_blocks_the_reader(tmp_path):
    def pending(family_id, root):
        raise AmendmentAcknowledgmentPendingError("f32 may not back a comparative run")

    run_dir, product_root = _make_run(tmp_path)
    with pytest.raises(AmendmentAcknowledgmentPendingError):
        _load(run_dir, product_root, fixture_gate=pending)


def test_mismatched_identity_blocks_the_reader(tmp_path):
    def mismatch(identity, root):
        raise ContractIdentityError("caller-substituted or incomplete pre-registration identity")

    run_dir, product_root = _make_run(tmp_path)
    with pytest.raises(ContractIdentityError):
        _load(run_dir, product_root, identity_validator=mismatch)


def test_unrelated_pending_family_does_not_block(tmp_path):
    """Only f32's own amendment withholds this instrument."""
    from protocol.contracts import AmendmentIdentity, PreregistrationIdentity, require_amended_families_released

    amendment = AmendmentIdentity.model_construct(
        sequence=3, acknowledgment_status="pending", introduced_family_ids=("f19",))
    identity = PreregistrationIdentity.model_construct(amendments=(amendment,))
    require_amended_families_released(identity, ["f32"])

    run_dir, product_root = _make_run(tmp_path)
    loaded = _load(run_dir, product_root,
                   family_gate=lambda _identity, families: require_amended_families_released(identity, families))
    assert loaded["recomputed"]["scores"]


def test_real_gates_refuse_while_f32_acknowledgment_is_pending(tmp_path):
    """No injection: the shipped gate functions against this repository."""
    from membench.utility.runner import load_report

    run_dir, _product_root = _make_run(tmp_path)
    with pytest.raises(AmendmentAcknowledgmentPendingError):
        load_report(run_dir, REPO_ROOT, allow_synthetic=True,
                    identity_validator=lambda identity, root: None)


def test_tampered_observed_evidence_is_refused(tmp_path):
    from membench.utility.runner import UtilityReportError

    run_dir, product_root = _make_run(tmp_path)
    world = run_dir / "pairs" / "helpful_history" / "control" / "action-world.json"
    payload = json.loads(world.read_text(encoding="utf-8"))
    payload["applied"] = {"anything": {"steps": [], "constraint": ""}}
    world.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UtilityReportError, match="digest"):
        _load(run_dir, product_root)


def test_self_reported_success_row_cannot_override_the_regrade(tmp_path):
    from membench.utility.runner import UtilityReportError

    run_dir, product_root = _make_run(tmp_path)
    report_path = run_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["scores"]["helpful_history"]["pair"]["wins"] = 99
    report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    digests_path = run_dir / "digests.json"
    digests = json.loads(digests_path.read_text(encoding="utf-8"))
    digests["files"]["report.json"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    digests_path.write_text(json.dumps(digests, sort_keys=True), encoding="utf-8")
    with pytest.raises(UtilityReportError, match="recomputed|disagree"):
        _load(run_dir, product_root)


def test_missing_action_evidence_is_never_a_clean_pass(tmp_path):
    from membench.utility.runner import UtilityReportError

    run_dir, product_root = _make_run(tmp_path)
    (run_dir / "pairs" / "helpful_history" / "control" / "action-world.json").unlink()
    with pytest.raises(UtilityReportError):
        _load(run_dir, product_root)
