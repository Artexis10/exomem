"""Protocol validity vocabulary used by manifests and reports."""

from __future__ import annotations


VALID = "VALID"
READINESS_UNVERIFIABLE = "READINESS_UNVERIFIABLE"
ABORTED_BUDGET = "ABORTED_BUDGET"


def INVALID(reason: str) -> str:
    if not reason:
        raise ValueError("INVALID requires a reason")
    return "INVALID"


def BLOCKED(reason: str) -> str:
    if not reason:
        raise ValueError("BLOCKED requires a reason")
    return "BLOCKED"


def is_terminal(status: str) -> bool:
    return status in {VALID, "INVALID", READINESS_UNVERIFIABLE, ABORTED_BUDGET, "BLOCKED"}
