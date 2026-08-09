"""Protocol validity vocabulary used by manifests and reports."""

from __future__ import annotations

from dataclasses import dataclass

VALID = "VALID"
READINESS_UNVERIFIABLE = "READINESS_UNVERIFIABLE"
ABORTED_BUDGET = "ABORTED_BUDGET"


def INVALID(reason: str) -> str:
    return f"INVALID:{reason}"


def BLOCKED(reason: str) -> str:
    return f"BLOCKED:{reason}"


def is_terminal(status: str) -> bool:
    return status in {VALID, READINESS_UNVERIFIABLE, ABORTED_BUDGET} or status.startswith(("INVALID:", "BLOCKED:"))
