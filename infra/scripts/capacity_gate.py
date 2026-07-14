#!/usr/bin/env python3
"""Gate one paid cell on attributed live economics and cluster capacity evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REFERENCE = re.compile(r"(?:invoice|statement|k8s-observation):[A-Za-z0-9._-]{8,128}\Z")


@dataclass(frozen=True)
class CapacityDecision:
    allowed: bool
    reason: str


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == UTC else None


def _economics_verified(contract: dict[str, Any], observed_at: datetime) -> bool:
    costs = contract.get("monthly_costs_eur_ex_vat")
    paddle = contract.get("paddle")
    evidence = contract.get("evidence")
    if (
        contract.get("live_costs_verified") is not True
        or not isinstance(costs, dict)
        or not costs
        or any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0
            for value in costs.values()
        )
        or not isinstance(paddle, dict)
        or paddle.get("actual_fee_tax_verified") is not True
        or not isinstance(paddle.get("fee_model"), str)
        or not paddle["fee_model"]
        or not isinstance(paddle.get("tax_treatment"), str)
        or not paddle["tax_treatment"]
        or not isinstance(paddle.get("net_receipt_eur_for_friend_price"), (int, float))
        or isinstance(paddle.get("net_receipt_eur_for_friend_price"), bool)
        or not isinstance(evidence, dict)
        or set(evidence) != {
            "provider_invoice_reference",
            "paddle_statement_reference",
            "recorded_at",
        }
    ):
        return False
    provider_reference = evidence.get("provider_invoice_reference")
    paddle_reference = evidence.get("paddle_statement_reference")
    recorded_at = _timestamp(evidence.get("recorded_at"))
    return bool(
        isinstance(provider_reference, str)
        and _REFERENCE.fullmatch(provider_reference)
        and provider_reference.startswith("invoice:")
        and isinstance(paddle_reference, str)
        and _REFERENCE.fullmatch(paddle_reference)
        and paddle_reference.startswith("statement:")
        and recorded_at is not None
        and recorded_at <= observed_at
        and (observed_at - recorded_at).total_seconds() <= 31 * 86400
        and paddle.get("evidence_recorded_at") == evidence.get("recorded_at")
    )


def evaluate(contract: dict[str, Any], observation: dict[str, Any]) -> CapacityDecision:
    observed_at = _timestamp(observation.get("observed_at"))
    active_user_cells = observation.get("active_user_cells")
    attached_volumes = observation.get("attached_volumes")
    reference = observation.get("reference")
    if (
        observation.get("schema_version") != 1
        or observation.get("source") != "kubernetes-api"
        or not isinstance(observation.get("cluster_uid"), str)
        or len(observation["cluster_uid"]) < 8
        or observed_at is None
        or not isinstance(reference, str)
        or not _REFERENCE.fullmatch(reference)
        or not reference.startswith("k8s-observation:")
        or not isinstance(active_user_cells, int)
        or isinstance(active_user_cells, bool)
        or active_user_cells < 0
        or not isinstance(attached_volumes, int)
        or isinstance(attached_volumes, bool)
        or attached_volumes < 0
    ):
        return CapacityDecision(False, "invalid-capacity-observation")
    limits = contract.get("limits")
    paddle = contract.get("paddle")
    if (
        contract.get("schema_version") != 1
        or not isinstance(limits, dict)
        or not isinstance(paddle, dict)
    ):
        return CapacityDecision(False, "invalid-capacity-contract")
    if not _economics_verified(contract, observed_at):
        return CapacityDecision(False, "live-economics-unverified")
    active_limit = limits.get("active_user_cells")
    reserved = limits.get("reserved_volume_attachments")
    provider_limit = limits.get("provider_volume_attachment_limit")
    unused_headroom = limits.get("minimum_unused_provider_headroom")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (active_limit, reserved, provider_limit, unused_headroom)
    ):
        return CapacityDecision(False, "invalid-capacity-contract")
    assert isinstance(active_limit, int)
    assert isinstance(reserved, int)
    assert isinstance(provider_limit, int)
    assert isinstance(unused_headroom, int)
    if active_user_cells >= active_limit:
        return CapacityDecision(False, "active-user-cell-capacity-exhausted")
    projected = attached_volumes + 1 + reserved
    if projected > provider_limit - unused_headroom:
        return CapacityDecision(False, "safe-volume-attachment-headroom-exhausted")
    return CapacityDecision(True, "capacity-available")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        state = json.loads(args.state.read_text(encoding="utf-8"))
        decision = evaluate(contract, state)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        print("capacity observation is invalid", file=sys.stderr)
        return 2
    print(json.dumps({"allowed": decision.allowed, "reason": decision.reason}, sort_keys=True))
    return 0 if decision.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
