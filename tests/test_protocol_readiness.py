from __future__ import annotations

import pytest


def test_readiness_fails_closed_and_rejects_exit_code_evidence() -> None:
    from protocol.readiness import LaneReadiness, ReadinessError, validate

    with pytest.raises(ValueError, match="exit-code"):
        LaneReadiness(lane="semantic", requested=True, verified=True, method="exit-code", evidence="0")
    report = validate([LaneReadiness(lane="semantic", requested=True, verified=False, method="index-count", evidence="0")])
    assert report.status == "INVALID"
    report = validate([LaneReadiness(lane="semantic", requested=True, verified=False, method="readiness-unverifiable", evidence="provider has no completion signal")])
    assert report.status == "READINESS_UNVERIFIABLE"
    with pytest.raises(ReadinessError):
        validate([LaneReadiness(lane="semantic", requested=True, verified=True, method="probe", evidence="hit", fallback_detected=True)], strict=True)
