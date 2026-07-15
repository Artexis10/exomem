#!/usr/bin/env python3
"""Gate one paid cell from authenticated live-capacity and economics receipts."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_CAPACITY_DOMAIN = b"exomem.capacity-live-receipt.v1\0"
_ECONOMICS_DOMAIN = b"exomem.capacity-economics-receipt.v1\0"
_RECEIPT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


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


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == UTC else None


def _load_public_key(path: Path) -> Ed25519PublicKey:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise CapacityGateError("capacity receipt public key must be a non-writable regular file")
    try:
        raw = path.read_bytes()
        try:
            key = serialization.load_pem_public_key(raw)
        except ValueError:
            encoded = raw.decode("ascii").strip()
            if "=" in encoded or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
                raise ValueError("invalid raw public key") from None
            padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(padded, altchars=b"-_", validate=True)
            )
    except (OSError, UnicodeDecodeError, ValueError, binascii.Error) as exc:
        raise CapacityGateError("capacity receipt public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise CapacityGateError("capacity receipt public key is invalid")
    return key


def _public_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


def _require_trusted_public_keys(
    contract: dict[str, Any],
    capacity_public_key: Ed25519PublicKey,
    economics_public_key: Ed25519PublicKey,
) -> None:
    authentication = contract.get("receipt_authentication")
    if (
        not isinstance(authentication, dict)
        or authentication.get("algorithm") != "ed25519"
        or authentication.get("capacity_public_key_id") != _public_key_id(capacity_public_key)
        or authentication.get("economics_public_key_id") != _public_key_id(economics_public_key)
    ):
        raise CapacityGateError("capacity receipt public key is not trusted by the contract")


def _authenticated_receipt(
    path: Path, public_key: Ed25519PublicKey, *, domain: bytes
) -> dict[str, Any]:
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
        "signature",
    }:
        raise CapacityGateError("capacity receipt is unauthenticated")
    signature_hex = authentication.get("signature")
    if (
        authentication.get("algorithm") != "ed25519"
        or authentication.get("key_id") != _public_key_id(public_key)
        or not isinstance(signature_hex, str)
    ):
        raise CapacityGateError("capacity receipt is unauthenticated")
    try:
        signature = bytes.fromhex(signature_hex)
        public_key.verify(signature, domain + _canonical(unsigned))
    except (ValueError, InvalidSignature) as exc:
        raise CapacityGateError("capacity receipt is unauthenticated") from exc
    return unsigned


def _valid_economics(
    contract: dict[str, Any], economics: dict[str, Any], capacity_time: datetime
) -> bool:
    observed_at = _timestamp(economics.get("observed_at"))
    expires_at = _timestamp(economics.get("expires_at"))
    costs = economics.get("monthly_costs_eur_ex_vat")
    paddle = economics.get("paddle")
    contract_costs = contract.get("monthly_costs_eur_ex_vat")
    contract_paddle = contract.get("paddle")
    contract_evidence = contract.get("evidence")
    evidence_recorded_at = (
        _timestamp(contract_evidence.get("recorded_at"))
        if isinstance(contract_evidence, dict)
        else None
    )
    return bool(
        set(economics)
        == {
            "schema_version",
            "issuer",
            "contract_sha256",
            "receipt_id",
            "sequence",
            "observed_at",
            "expires_at",
            "monthly_costs_eur_ex_vat",
            "paddle",
            "provider_invoice_sha256",
            "paddle_statement_sha256",
        }
        and economics.get("schema_version") == 1
        and economics.get("issuer") == "exomem-live-provider-paddle-v1"
        and economics.get("contract_sha256") == contract_digest(contract)
        and contract.get("live_costs_verified") is True
        and isinstance(economics.get("receipt_id"), str)
        and _RECEIPT_ID.fullmatch(economics["receipt_id"]) is not None
        and isinstance(economics.get("sequence"), int)
        and not isinstance(economics.get("sequence"), bool)
        and economics["sequence"] > 0
        and observed_at is not None
        and expires_at is not None
        and observed_at <= capacity_time
        and capacity_time <= expires_at
        and 0 < (expires_at - observed_at).total_seconds() <= 31 * 86400
        and (capacity_time - observed_at).total_seconds() <= 31 * 86400
        and isinstance(costs, dict)
        and isinstance(contract_costs, dict)
        and set(costs) == set(contract_costs)
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            for value in costs.values()
        )
        and costs == contract_costs
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
        and isinstance(contract_paddle, dict)
        and set(contract_paddle)
        == {
            "actual_fee_tax_verified",
            "fee_model",
            "tax_treatment",
            "net_receipt_eur_for_friend_price",
            "evidence_recorded_at",
        }
        and contract_paddle.get("actual_fee_tax_verified") is True
        and all(
            paddle.get(field) == contract_paddle.get(field)
            for field in (
                "actual_fee_tax_verified",
                "fee_model",
                "tax_treatment",
                "net_receipt_eur_for_friend_price",
            )
        )
        and isinstance(contract_evidence, dict)
        and set(contract_evidence)
        == {
            "provider_invoice_reference",
            "paddle_statement_reference",
            "recorded_at",
        }
        and evidence_recorded_at is not None
        and evidence_recorded_at <= observed_at
        and contract_paddle.get("evidence_recorded_at") == contract_evidence.get("recorded_at")
        and economics.get("provider_invoice_sha256")
        == contract_evidence.get("provider_invoice_reference")
        and economics.get("paddle_statement_sha256")
        == contract_evidence.get("paddle_statement_reference")
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
    active_recovery_cells = capacity.get("active_recovery_cells")
    attached_volumes = capacity.get("attached_volumes")
    if (
        set(capacity)
        != {
            "schema_version",
            "issuer",
            "contract_sha256",
            "receipt_id",
            "sequence",
            "cluster_uid",
            "hcloud_server_id",
            "hcloud_location",
            "observed_at",
            "expires_at",
            "active_user_cells",
            "active_recovery_cells",
            "attached_volumes",
        }
        or capacity.get("schema_version") != 1
        or capacity.get("issuer") != "exomem-live-kubernetes-hcloud-v1"
        or capacity.get("contract_sha256") != contract_digest(contract)
        or not isinstance(capacity.get("receipt_id"), str)
        or _RECEIPT_ID.fullmatch(capacity["receipt_id"]) is None
        or not isinstance(capacity.get("sequence"), int)
        or isinstance(capacity.get("sequence"), bool)
        or capacity["sequence"] <= 0
        or not isinstance(capacity.get("cluster_uid"), str)
        or len(capacity["cluster_uid"]) < 8
        or not isinstance(capacity.get("hcloud_server_id"), int)
        or isinstance(capacity.get("hcloud_server_id"), bool)
        or capacity["hcloud_server_id"] < 1
        or not isinstance(capacity.get("hcloud_location"), str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", capacity["hcloud_location"])
        is None
        or observed_at is None
        or not isinstance(active_user_cells, int)
        or isinstance(active_user_cells, bool)
        or active_user_cells < 0
        or not isinstance(active_recovery_cells, int)
        or isinstance(active_recovery_cells, bool)
        or active_recovery_cells < 0
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
        limits.get("active_recovery_cells"),
        limits.get("maximum_potential_attachments"),
        limits.get("provider_volume_attachment_limit"),
        limits.get("minimum_unused_provider_headroom"),
    )
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values
    ):
        return CapacityDecision(False, "invalid-capacity-contract")
    active_limit, recovery_limit, maximum_potential, provider_limit, unused_headroom = values
    assert isinstance(active_limit, int)
    assert isinstance(recovery_limit, int)
    assert isinstance(maximum_potential, int)
    assert isinstance(provider_limit, int)
    assert isinstance(unused_headroom, int)
    if maximum_potential > provider_limit - unused_headroom:
        return CapacityDecision(False, "invalid-capacity-contract")
    if active_user_cells >= active_limit:
        return CapacityDecision(False, "active-user-cell-capacity-exhausted")
    if active_recovery_cells > recovery_limit:
        return CapacityDecision(False, "active-recovery-cell-capacity-exhausted")
    if attached_volumes + 1 > maximum_potential:
        return CapacityDecision(False, "safe-volume-attachment-headroom-exhausted")
    return CapacityDecision(True, "capacity-available")


def evaluate_files(
    contract: dict[str, Any],
    *,
    capacity_receipt: Path,
    economics_receipt: Path,
    capacity_public_key: Ed25519PublicKey,
    economics_public_key: Ed25519PublicKey,
    now: datetime | None = None,
) -> CapacityDecision:
    _require_trusted_public_keys(contract, capacity_public_key, economics_public_key)
    authentication = contract.get("receipt_authentication")
    if (
        not isinstance(authentication, dict)
        or authentication.get("capacity_domain") != _CAPACITY_DOMAIN[:-1].decode()
        or authentication.get("economics_domain") != _ECONOMICS_DOMAIN[:-1].decode()
        or authentication.get("capacity_ttl_seconds") != 300
        or authentication.get("economics_ttl_seconds") != 31 * 86400
    ):
        raise CapacityGateError("capacity receipt authentication contract is invalid")
    observed_now = now or datetime.now(UTC)
    if observed_now.tzinfo != UTC:
        raise CapacityGateError("capacity gate clock is invalid")
    capacity = _authenticated_receipt(
        capacity_receipt,
        capacity_public_key,
        domain=_CAPACITY_DOMAIN,
    )
    economics = _authenticated_receipt(
        economics_receipt,
        economics_public_key,
        domain=_ECONOMICS_DOMAIN,
    )
    capacity_observed_at = _timestamp(capacity.get("observed_at"))
    capacity_expires_at = _timestamp(capacity.get("expires_at"))
    economics_observed_at = _timestamp(economics.get("observed_at"))
    economics_expires_at = _timestamp(economics.get("expires_at"))
    if (
        capacity_observed_at is None
        or capacity_expires_at is None
        or capacity_observed_at > observed_now
        or observed_now > capacity_expires_at
        or (capacity_expires_at - capacity_observed_at).total_seconds() != 300
        or economics_observed_at is None
        or economics_expires_at is None
        or economics_observed_at > observed_now
        or observed_now > economics_expires_at
        or not 0 < (economics_expires_at - economics_observed_at).total_seconds() <= 31 * 86400
    ):
        raise CapacityGateError("capacity or economics receipt is expired or not yet valid")
    return evaluate_authenticated(
        contract,
        capacity,
        economics,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--capacity-receipt", type=Path, required=True)
    parser.add_argument("--economics-receipt", type=Path, required=True)
    parser.add_argument("--capacity-public-key-file", type=Path, required=True)
    parser.add_argument("--economics-public-key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        decision = evaluate_files(
            contract,
            capacity_receipt=args.capacity_receipt,
            economics_receipt=args.economics_receipt,
            capacity_public_key=_load_public_key(args.capacity_public_key_file),
            economics_public_key=_load_public_key(args.economics_public_key_file),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CapacityGateError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"allowed": decision.allowed, "reason": decision.reason}, sort_keys=True))
    return 0 if decision.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
