#!/usr/bin/env python3
"""Sign a directory evidence record for marketplace submission.

The verifier for these records lives in ``hosted_plugins._load_signed_directory_evidence``
but nothing shipped could produce one, so every submission attempt failed on
``<name> is missing``. This closes that half.

It deliberately does **not** gather the facts it signs. An evidence record
asserts that probes were run against a real deployment; a tool that invented
those results would turn the whole signed-evidence chain into decoration. You
supply the payload, this binds it to a deployment and signs it.

    export EXOMEM_HOSTED_PROMOTION_SECRET=...
    python scripts/sign-directory-evidence.py production-evidence \
        --payload probes.json --deployment-sha256 <64hex> --key-id <id>

`--payload` holds only the evidence-type-specific fields (for
production-evidence: surfaces, the four digests, origin_rejection,
response_minimization, sampled_output_sale_free). The envelope --
schema_version, evidence_type, deployment_sha256, checked_at, expires_at,
operator_key_id -- is added here, and the signature covers all of it.

Records expire in at most 24 hours by contract, so this is run immediately
before a submission, not once.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirrors the verifier's payload_fields table. Kept here rather than imported so
# a mismatch shows up as a signing error rather than a silent agreement between
# two copies of the same mistake.
EVIDENCE_TYPES = {
    "production-evidence": (
        "directory-production-probes",
        {
            "surfaces",
            "compatibility_sha256",
            "command_surface_sha256",
            "schema_contract_sha256",
            "full_tool_contract_sha256",
            "origin_rejection",
            "response_minimization",
            "sampled_output_sale_free",
        },
    ),
    "prerequisite-evidence": ("directory-prerequisites", {"channels"}),
    "public-admission-evidence": ("directory-public-admission", {"admission"}),
    "reviewer-access-evidence": ("directory-reviewer-access", {"channels"}),
}

MAX_TTL = timedelta(hours=24)
HEX64 = re.compile(r"[0-9a-f]{64}")


def canonical_json(value: object) -> bytes:
    """Byte-for-byte identical to the verifier's canonicalisation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(
    *,
    name: str,
    payload: dict,
    deployment_sha256: str,
    key_id: str,
    secret: str,
    ttl: timedelta,
) -> dict:
    evidence_type, expected_fields = EVIDENCE_TYPES[name]
    if set(payload) != expected_fields:
        missing = sorted(expected_fields - set(payload))
        extra = sorted(set(payload) - expected_fields)
        raise SystemExit(
            f"{name}: payload fields do not match the contract"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected {extra}" if extra else "")
        )
    if not HEX64.fullmatch(deployment_sha256):
        raise SystemExit("--deployment-sha256 must be 64 lowercase hex characters")
    if ttl <= timedelta(0) or ttl > MAX_TTL:
        raise SystemExit("--ttl-hours must be greater than 0 and at most 24")

    checked_at = datetime.now(UTC)
    record = {
        "schema_version": 1,
        "evidence_type": evidence_type,
        "deployment_sha256": deployment_sha256,
        "checked_at": stamp(checked_at),
        "expires_at": stamp(checked_at + ttl),
        "operator_key_id": key_id,
        **payload,
    }
    # The signature covers the whole record except itself, exactly as the
    # verifier recomputes it.
    record["operator_signature"] = hmac.new(
        secret.encode("utf-8"), canonical_json(record), hashlib.sha256
    ).hexdigest()
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=sorted(EVIDENCE_TYPES))
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--deployment-sha256", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--ttl-hours", type=float, default=24.0)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="defaults to plugins/hosted/directory/<name>.json",
    )
    args = parser.parse_args(argv)

    secret = os.environ.get("EXOMEM_HOSTED_PROMOTION_SECRET", "").strip()
    if not secret:
        raise SystemExit("EXOMEM_HOSTED_PROMOTION_SECRET is not set")

    try:
        payload = json.loads(args.payload.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read payload: {error}") from error
    if not isinstance(payload, dict):
        raise SystemExit("payload must be a JSON object")

    record = build(
        name=args.name,
        payload=payload,
        deployment_sha256=args.deployment_sha256,
        key_id=args.key_id,
        secret=secret,
        ttl=timedelta(hours=args.ttl_hours),
    )
    out = args.out or REPO_ROOT / "plugins/hosted/directory" / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"signed {args.name} -> {out} (expires {record['expires_at']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
