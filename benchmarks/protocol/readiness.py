"""Fail-closed readiness reporting with typed positive evidence."""

from __future__ import annotations

from .models import LaneReadiness, ReadinessReport


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
