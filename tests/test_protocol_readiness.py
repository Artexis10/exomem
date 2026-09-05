from __future__ import annotations

import pytest


def test_doctor_readiness_names_failed_runtime_checks_without_private_messages() -> None:
    from protocol.readiness import SEMANTIC_DOCTOR_CHECKS, semantic_doctor_readiness

    report = {"success": False, "profile": "hybrid", "checks": [
        *({"id": name, "status": "pass"} for name in SEMANTIC_DOCTOR_CHECKS),
        {"id": "idempotency_store", "status": "fail", "message": "/private/runtime/path"},
    ]}
    readiness = semantic_doctor_readiness(report)
    assert not readiness.verified
    assert readiness.fallback_detected
    assert readiness.evidence == "current-corpus hybrid doctor failed: idempotency_store"


def test_readiness_fails_closed_and_rejects_exit_code_evidence() -> None:
    from protocol.readiness import LaneReadiness, ReadinessError, validate

    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        LaneReadiness(lane="semantic", requested=True, verified=True, method="exit-code", evidence="0")
    report = validate([LaneReadiness(lane="semantic", requested=True, verified=False, method="index-count", evidence="0")])
    assert report.status == "INVALID"
    report = validate([LaneReadiness(lane="semantic", requested=True, verified=False, method="readiness-unverifiable", evidence="provider has no completion signal")])
    assert report.status == "READINESS_UNVERIFIABLE"
    with pytest.raises(ReadinessError):
        validate([LaneReadiness(lane="semantic", requested=True, verified=True, method="semantic-probe", evidence="hit", fallback_detected=True)], strict=True)
