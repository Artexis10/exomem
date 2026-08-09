"""Fail-closed readiness reporting with positive, non-exit-code evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .models import ReadinessReport


class ReadinessError(ValueError):
    pass


@dataclass(frozen=True)
class LaneReadiness:
    lane: str
    requested: bool
    verified: bool
    method: str
    evidence: str
    fallback_detected: bool = False

    def __post_init__(self) -> None:
        if self.method == "exit-code":
            raise ValueError("exit-code is not readiness evidence")
        if self.requested and not self.evidence:
            raise ValueError("requested readiness needs positive evidence or an explicit unverifiable reason")


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
    report = ReadinessReport(status=status, lanes=[asdict(item) for item in readiness_list], reasons=[*invalid, *unverifiable])
    if strict and status == "INVALID":
        raise ReadinessError("; ".join(invalid))
    return report
