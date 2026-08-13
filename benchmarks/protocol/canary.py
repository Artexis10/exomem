"""Deterministic canaries used to prove per-case namespace isolation."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Literal

CanaryKind = Literal["presence", "cross_case", "never_ingested"]


def canary_for(run_secret: str, case_id: str, kind: CanaryKind) -> str:
    digest = hmac.new(run_secret.encode("utf-8"), f"{case_id}\0{kind}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"canary-{kind.replace('_', '-')}-{digest[:32]}"


def evaluate_probes(hits_by_kind: Mapping[str, object]) -> Literal["isolated", "contaminated", "unverifiable"]:
    required = {"presence", "cross_case", "never_ingested"}
    if not required <= set(hits_by_kind):
        return "unverifiable"
    hits = {kind: bool(hits_by_kind[kind]) for kind in required}
    if hits["cross_case"] or hits["never_ingested"]:
        return "contaminated"
    return "isolated" if hits["presence"] else "unverifiable"
