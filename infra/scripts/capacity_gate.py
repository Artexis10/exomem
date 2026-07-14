#!/usr/bin/env python3
"""Gate one additional paid cell on live economics and attachment headroom."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CapacityDecision:
    allowed: bool
    reason: str


def evaluate(
    contract: dict[str, Any], *, active_user_cells: int, attached_volumes: int
) -> CapacityDecision:
    if active_user_cells < 0 or attached_volumes < 0:
        return CapacityDecision(False, "invalid-capacity-observation")
    limits = contract.get("limits")
    paddle = contract.get("paddle")
    if (
        contract.get("schema_version") != 1
        or not isinstance(limits, dict)
        or not isinstance(paddle, dict)
    ):
        return CapacityDecision(False, "invalid-capacity-contract")
    if contract.get("live_costs_verified") is not True or paddle.get("actual_fee_tax_verified") is not True:
        return CapacityDecision(False, "live-economics-unverified")
    active_limit = limits.get("active_user_cells")
    reserved = limits.get("reserved_volume_attachments")
    provider_limit = limits.get("provider_volume_attachment_limit")
    unused_headroom = limits.get("minimum_unused_provider_headroom")
    if not (
        isinstance(active_limit, int)
        and not isinstance(active_limit, bool)
        and active_limit >= 0
        and isinstance(reserved, int)
        and not isinstance(reserved, bool)
        and reserved >= 0
        and isinstance(provider_limit, int)
        and not isinstance(provider_limit, bool)
        and provider_limit >= 0
        and isinstance(unused_headroom, int)
        and not isinstance(unused_headroom, bool)
        and unused_headroom >= 0
    ):
        return CapacityDecision(False, "invalid-capacity-contract")
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
        decision = evaluate(
            contract,
            active_user_cells=state["active_user_cells"],
            attached_volumes=state["attached_volumes"],
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        print("capacity observation is invalid", file=sys.stderr)
        return 2
    print(json.dumps({"allowed": decision.allowed, "reason": decision.reason}, sort_keys=True))
    return 0 if decision.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
