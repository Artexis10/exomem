"""Fail-closed readiness reporting with typed positive evidence."""

from __future__ import annotations

from .models import LaneReadiness, ReadinessReport

SEMANTIC_DOCTOR_CHECKS = frozenset({
    "embeddings.enabled", "dep.sentence-transformers", "dep.torch",
    "dep.pillow", "models.cache", "embeddings.sidecar",
})


def semantic_doctor_readiness(report: dict) -> LaneReadiness:
    """Current-corpus live doctor evidence, shared by direct and guest lanes."""
    checks: dict[str, str] = {}
    malformed = not isinstance(report, dict)
    rows = report.get("checks") if not malformed else None
    if not isinstance(rows, list):
        malformed = True
        rows = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row.get("status") not in {"pass", "warn", "fail"} or row["id"] in checks:
            malformed = True
            continue
        checks[row["id"]] = row["status"]
    failed = sorted(
        {name for name in SEMANTIC_DOCTOR_CHECKS if checks.get(name) != "pass"}
        | {name for name, status in checks.items() if status == "fail"}
    )
    verified = not malformed and report.get("success") is True and report.get("profile") == "hybrid" and not failed
    return LaneReadiness(
        lane="semantic", requested=True, verified=verified,
        method="doctor-check", fallback_detected=not verified,
        evidence=("current-corpus hybrid doctor checks pass: " + ", ".join(sorted(SEMANTIC_DOCTOR_CHECKS))) if verified else "current-corpus hybrid doctor failed: " + ", ".join(failed or ["report-invalid"]),
    )


class ReadinessError(ValueError):
    pass


def validate(readiness_list: list[LaneReadiness], *, strict: bool = False) -> ReadinessReport:
    invalid: list[str] = []
    unverifiable: list[str] = []
    for readiness in readiness_list:
        if not readiness.requested:
            continue
        if readiness.fallback_detected:
            invalid.append(f"{readiness.lane}: fallback detected")
        elif readiness.verified:
            continue
        elif readiness.method == "readiness-unverifiable":
            unverifiable.append(f"{readiness.lane}: {readiness.evidence}")
        else:
            invalid.append(f"{readiness.lane}: requested but not verified")
    status = "INVALID" if invalid else "READINESS_UNVERIFIABLE" if unverifiable else "VALID"
    report = ReadinessReport(status=status, lanes=readiness_list, reasons=[*invalid, *unverifiable])
    if strict and status == "INVALID":
        raise ReadinessError("; ".join(invalid))
    return report
