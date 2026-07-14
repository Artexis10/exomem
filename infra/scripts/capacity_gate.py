#!/usr/bin/env python3
"""Gate one paid cell from authenticated live-capacity and economics receipts."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CapacityGateError(RuntimeError):
    """Capacity receipts cannot authorize another cell."""


@dataclass(frozen=True)
class CapacityDecision:
    allowed: bool
    reason: str


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()


def contract_digest(contract: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(contract)).hexdigest()


def sign_receipt(receipt: dict[str, Any], key: bytes) -> dict[str, Any]:
    if len(key) < 32 or "authentication" in receipt:
        raise CapacityGateError("capacity receipt signing input is invalid")
    signed = dict(receipt)
    signed["authentication"] = {
        "algorithm": "hmac-sha256",
        "key_id": hashlib.sha256(key).hexdigest(),
        "mac": hmac.new(key, _canonical(receipt), hashlib.sha256).hexdigest(),
    }
    return signed


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == UTC else None


def _load_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise CapacityGateError("capacity receipt key must be a mode-0600 regular file")
    key = path.read_bytes()
    if len(key) < 32 or len(key) > 256:
        raise CapacityGateError("capacity receipt key is invalid")
    return key


def _authenticated_receipt(path: Path, key: bytes) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise CapacityGateError("capacity receipt must be a mode-0600 regular file")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapacityGateError("capacity receipt is invalid") from exc
    if not isinstance(receipt, dict):
        raise CapacityGateError("capacity receipt is invalid")
    authentication = receipt.get("authentication")
    unsigned = {name: value for name, value in receipt.items() if name != "authentication"}
    if not isinstance(authentication, dict) or set(authentication) != {
        "algorithm",
        "key_id",
        "mac",
    }:
        raise CapacityGateError("capacity receipt is unauthenticated")
    expected = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
    if (
        authentication.get("algorithm") != "hmac-sha256"
        or authentication.get("key_id") != hashlib.sha256(key).hexdigest()
        or not isinstance(authentication.get("mac"), str)
        or not hmac.compare_digest(authentication["mac"], expected)
    ):
        raise CapacityGateError("capacity receipt is unauthenticated")
    return unsigned


def _valid_economics(
    contract: dict[str, Any], economics: dict[str, Any], capacity_time: datetime
) -> bool:
    observed_at = _timestamp(economics.get("observed_at"))
    costs = economics.get("monthly_costs_eur_ex_vat")
    paddle = economics.get("paddle")
    contract_costs = contract.get("monthly_costs_eur_ex_vat")
    return bool(
        set(economics)
        == {
            "schema_version",
            "issuer",
            "contract_sha256",
            "observed_at",
            "monthly_costs_eur_ex_vat",
            "paddle",
            "provider_invoice_sha256",
            "paddle_statement_sha256",
        }
        and economics.get("schema_version") == 1
        and economics.get("issuer") == "exomem-live-provider-paddle-v1"
        and economics.get("contract_sha256") == contract_digest(contract)
        and observed_at is not None
        and observed_at <= capacity_time
        and (capacity_time - observed_at).total_seconds() <= 31 * 86400
        and isinstance(costs, dict)
        and isinstance(contract_costs, dict)
        and set(costs) == set(contract_costs)
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            for value in costs.values()
        )
        and isinstance(paddle, dict)
        and set(paddle)
        == {
            "actual_fee_tax_verified",
            "fee_model",
            "tax_treatment",
            "net_receipt_eur_for_friend_price",
        }
        and paddle.get("actual_fee_tax_verified") is True
        and isinstance(paddle.get("fee_model"), str)
        and bool(paddle["fee_model"])
        and isinstance(paddle.get("tax_treatment"), str)
        and bool(paddle["tax_treatment"])
        and isinstance(paddle.get("net_receipt_eur_for_friend_price"), (int, float))
        and not isinstance(paddle.get("net_receipt_eur_for_friend_price"), bool)
        and paddle["net_receipt_eur_for_friend_price"] >= 0
        and all(
            isinstance(economics.get(name), str)
            and len(economics[name]) == 64
            and all(character in "0123456789abcdef" for character in economics[name])
            for name in ("provider_invoice_sha256", "paddle_statement_sha256")
        )
    )


def evaluate_authenticated(
    contract: dict[str, Any], capacity: dict[str, Any], economics: dict[str, Any]
) -> CapacityDecision:
    observed_at = _timestamp(capacity.get("observed_at"))
    active_user_cells = capacity.get("active_user_cells")
    attached_volumes = capacity.get("attached_volumes")
    if (
        set(capacity)
        != {
            "schema_version",
            "issuer",
            "cluster_uid",
            "observed_at",
            "active_user_cells",
            "attached_volumes",
        }
        or capacity.get("schema_version") != 1
        or capacity.get("issuer") != "exomem-live-kubernetes-hcloud-v1"
        or not isinstance(capacity.get("cluster_uid"), str)
        or len(capacity["cluster_uid"]) < 8
        or observed_at is None
        or not isinstance(active_user_cells, int)
        or isinstance(active_user_cells, bool)
        or active_user_cells < 0
        or not isinstance(attached_volumes, int)
        or isinstance(attached_volumes, bool)
        or attached_volumes < 0
    ):
        return CapacityDecision(False, "invalid-live-capacity-receipt")
    limits = contract.get("limits")
    if contract.get("schema_version") != 1 or not isinstance(limits, dict):
        return CapacityDecision(False, "invalid-capacity-contract")
    if not _valid_economics(contract, economics, observed_at):
        return CapacityDecision(False, "live-economics-unverified")
    values = (
        limits.get("active_user_cells"),
        limits.get("reserved_volume_attachments"),
        limits.get("provider_volume_attachment_limit"),
        limits.get("minimum_unused_provider_headroom"),
    )
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values
    ):
        return CapacityDecision(False, "invalid-capacity-contract")
    active_limit, reserved, provider_limit, unused_headroom = values
    assert isinstance(active_limit, int)
    assert isinstance(reserved, int)
    assert isinstance(provider_limit, int)
    assert isinstance(unused_headroom, int)
    if active_user_cells >= active_limit:
        return CapacityDecision(False, "active-user-cell-capacity-exhausted")
    if attached_volumes + 1 + reserved > provider_limit - unused_headroom:
        return CapacityDecision(False, "safe-volume-attachment-headroom-exhausted")
    return CapacityDecision(True, "capacity-available")


def evaluate_files(
    contract: dict[str, Any],
    *,
    capacity_receipt: Path,
    economics_receipt: Path,
    capacity_key: bytes,
    economics_key: bytes,
) -> CapacityDecision:
    return evaluate_authenticated(
        contract,
        _authenticated_receipt(capacity_receipt, capacity_key),
        _authenticated_receipt(economics_receipt, economics_key),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--capacity-receipt", type=Path, required=True)
    parser.add_argument("--economics-receipt", type=Path, required=True)
    parser.add_argument("--capacity-key-file", type=Path, required=True)
    parser.add_argument("--economics-key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        decision = evaluate_files(
            contract,
            capacity_receipt=args.capacity_receipt,
            economics_receipt=args.economics_receipt,
            capacity_key=_load_key(args.capacity_key_file),
            economics_key=_load_key(args.economics_key_file),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CapacityGateError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"allowed": decision.allowed, "reason": decision.reason}, sort_keys=True))
    return 0 if decision.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
