"""Detached source authority for governed vault consolidation exports.

The archive manifest proves byte consistency, not provenance.  This module
owns the separate Ed25519 proof which binds one quiesced export to a
destination-configured source identity.  It deliberately contains no archive
extraction, request parsing, or trust-root provisioning.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .projections import ProjectionCanonicalizationError, canonical_jcs

ATTESTATION_SCHEMA = "source-export-attestation/v1"
VERIFIER_RECORD_SCHEMA = "SourceExportVerifierRecord/v1"
SIGNING_DOMAIN = b"exomem.source-export-attestation/v1"
VERIFIER_PURPOSE = "vault-consolidation-export"
MAX_ATTESTATION_CLAIM_BYTES = 8192

__all__ = [
    "ATTESTATION_SCHEMA",
    "MAX_ATTESTATION_CLAIM_BYTES",
    "SIGNING_DOMAIN",
    "VERIFIER_PURPOSE",
    "VERIFIER_RECORD_SCHEMA",
    "SourceExportAttestationUnavailable",
    "SourceExportExpectation",
    "SourceExportVerifierRecord",
    "VerifiedSourceExportAttestation",
    "canonical_source_export_claims",
    "sign_source_export_attestation",
    "source_export_signing_bytes",
    "verify_source_export_attestation",
]

_ALGORITHM = "ed25519"
_GATES = frozenset({"intake", "apply", "retirement-clearance", "retirement-consumption"})
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RFC3339_MILLISECONDS = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_MAX_SAFE_INTEGER = (1 << 53) - 1
_REQUIRED_CLAIMS = frozenset(
    {
        "schema",
        "source_vault_id",
        "source_installation_id",
        "source_installation_generation",
        "source_active_fence_digest",
        "export_operation_id",
        "quiescence_checkpoint_digest",
        "archive_sha256",
        "manifest_sha256",
        "source_census_sha256",
        "issued_at",
        "expires_at",
        "signer_key_id",
    }
)
_OPTIONAL_CLAIMS = frozenset({"source_cell_id"})


class SourceExportAttestationUnavailable(RuntimeError):
    """Stable, content-free refusal to authenticate a source export."""

    code = "SOURCE_EXPORT_ATTESTATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("source export attestation is unavailable")


@dataclass(frozen=True, slots=True)
class SourceExportExpectation:
    source_vault_id: str
    source_installation_id: str
    source_installation_generation: int
    source_active_fence_digest: str
    export_operation_id: str
    quiescence_checkpoint_digest: str
    archive_sha256: str
    manifest_sha256: str
    source_census_sha256: str
    audience: str
    source_cell_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourceExportVerifierRecord:
    schema: str
    algorithm: str
    key_id: str
    public_key: str
    purpose: str
    audience: str
    source_vault_id: str
    source_installation_id: str
    source_installation_generation: int
    status: str
    not_before: str
    not_after: str
    registry_generation: int
    source_cell_id: str | None = None
    revoked_at: str | None = None
    revocation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedSourceExportAttestation:
    claims: Mapping[str, str | int]
    claims_sha256: str
    key_id: str
    verification_gate: str


def _fail() -> None:
    raise SourceExportAttestationUnavailable from None


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    return normalized


def _identifier(value: object) -> str:
    normalized = _normalize_text(value)
    if _IDENTIFIER.fullmatch(normalized) is None:
        _fail()
    return normalized


def _digest(value: object) -> str:
    normalized = _normalize_text(value)
    if _HEX_DIGEST.fullmatch(normalized) is None:
        _fail()
    return normalized


def _generation(value: object) -> int:
    if type(value) is not int or value < 1 or value > _MAX_SAFE_INTEGER:
        _fail()
    return value


def _timestamp(value: object) -> tuple[str, datetime]:
    normalized = _normalize_text(value)
    if _RFC3339_MILLISECONDS.fullmatch(normalized) is None:
        _fail()
    try:
        parsed = datetime.fromisoformat(normalized.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail()
    if parsed.tzinfo != UTC:
        _fail()
    return normalized, parsed


def _reject_json_constant(_value: str) -> None:
    _fail()


def _reject_json_float(_value: str) -> None:
    _fail()


def _parse_json_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        _fail()
    if parsed < 0 or parsed > _MAX_SAFE_INTEGER:
        _fail()
    return parsed


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for raw_key, item in pairs:
        key = _normalize_text(raw_key)
        if key in value:
            _fail()
        value[key] = item
    return value


def _parse_claim_bytes(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_closed_json_object,
            parse_int=_parse_json_integer,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except (SourceExportAttestationUnavailable, UnicodeDecodeError, json.JSONDecodeError):
        _fail()
    if not isinstance(parsed, dict):
        _fail()
    return parsed


def _normalize_claims(claims: Mapping[str, object]) -> dict[str, str | int]:
    normalized: dict[str, object] = {}
    for raw_key, item in claims.items():
        key = _normalize_text(raw_key)
        if key in normalized:
            _fail()
        normalized[key] = item
    keys = frozenset(normalized)
    if keys - (_REQUIRED_CLAIMS | _OPTIONAL_CLAIMS) or not _REQUIRED_CLAIMS <= keys:
        _fail()

    if normalized["schema"] != ATTESTATION_SCHEMA:
        _fail()
    result: dict[str, str | int] = {
        "schema": ATTESTATION_SCHEMA,
        "source_vault_id": _identifier(normalized["source_vault_id"]),
        "source_installation_id": _identifier(normalized["source_installation_id"]),
        "source_installation_generation": _generation(normalized["source_installation_generation"]),
        "source_active_fence_digest": _digest(normalized["source_active_fence_digest"]),
        "export_operation_id": _identifier(normalized["export_operation_id"]),
        "quiescence_checkpoint_digest": _digest(normalized["quiescence_checkpoint_digest"]),
        "archive_sha256": _digest(normalized["archive_sha256"]),
        "manifest_sha256": _digest(normalized["manifest_sha256"]),
        "source_census_sha256": _digest(normalized["source_census_sha256"]),
        "issued_at": _timestamp(normalized["issued_at"])[0],
        "expires_at": _timestamp(normalized["expires_at"])[0],
        "signer_key_id": _normalize_text(normalized["signer_key_id"]),
    }
    if "source_cell_id" in normalized:
        result["source_cell_id"] = _identifier(normalized["source_cell_id"])
        if result["source_cell_id"] == result["source_vault_id"]:
            _fail()
    if _timestamp(result["issued_at"])[1] >= _timestamp(result["expires_at"])[1]:
        _fail()
    if not result["signer_key_id"].startswith("ed25519-sha256:") or not _HEX_DIGEST.fullmatch(
        result["signer_key_id"].removeprefix("ed25519-sha256:")
    ):
        _fail()
    return result


def canonical_source_export_claims(claims: Mapping[str, object] | bytes) -> bytes:
    """Return the one canonical JCS encoding accepted by the signature contract."""

    raw = bytes(claims) if isinstance(claims, (bytes, bytearray)) else None
    if raw is not None and len(raw) > MAX_ATTESTATION_CLAIM_BYTES:
        _fail()
    parsed = _parse_claim_bytes(raw) if raw is not None else claims
    if not isinstance(parsed, Mapping):
        _fail()
    normalized = _normalize_claims(parsed)
    try:
        encoded = canonical_jcs(normalized)
    except ProjectionCanonicalizationError:
        _fail()
    if len(encoded) > MAX_ATTESTATION_CLAIM_BYTES:
        _fail()
    if raw is not None and raw != encoded:
        _fail()
    return encoded


def source_export_signing_bytes(claim_bytes: bytes) -> bytes:
    """Frame canonical claims exactly, without admitting an ambiguous prefix."""

    canonical = canonical_source_export_claims(claim_bytes)
    return (
        len(SIGNING_DOMAIN).to_bytes(4, "big")
        + SIGNING_DOMAIN
        + len(canonical).to_bytes(8, "big")
        + canonical
    )


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_base64url(value: object, *, size: int) -> bytes:
    text = _normalize_text(value)
    if not text or "=" in text or re.fullmatch(r"[A-Za-z0-9_-]+", text) is None:
        _fail()
    try:
        decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error):
        _fail()
    if len(decoded) != size or _base64url(decoded) != text:
        _fail()
    return decoded


def _public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _key_id(public_key: bytes) -> str:
    return f"ed25519-sha256:{hashlib.sha256(public_key).hexdigest()}"


def sign_source_export_attestation(
    claims: Mapping[str, object], private_key: Ed25519PrivateKey
) -> tuple[bytes, str]:
    """Sign exact claims with a source-custodied key and return a detached signature."""

    if not isinstance(private_key, Ed25519PrivateKey):
        _fail()
    claim_bytes = canonical_source_export_claims(claims)
    parsed = _normalize_claims(claims)
    if parsed["signer_key_id"] != _key_id(_public_key_bytes(private_key)):
        _fail()
    signature = private_key.sign(source_export_signing_bytes(claim_bytes))
    return claim_bytes, _base64url(signature)


def _validate_record(record: SourceExportVerifierRecord) -> tuple[bytes, datetime, datetime]:
    if not isinstance(record, SourceExportVerifierRecord):
        _fail()
    if record.schema != VERIFIER_RECORD_SCHEMA or record.algorithm != _ALGORITHM:
        _fail()
    public_key = _decode_base64url(record.public_key, size=32)
    if record.key_id != _key_id(public_key):
        _fail()
    if record.purpose != VERIFIER_PURPOSE:
        _fail()
    _identifier(record.audience)
    _identifier(record.source_vault_id)
    _identifier(record.source_installation_id)
    _generation(record.source_installation_generation)
    if record.source_cell_id is not None:
        _identifier(record.source_cell_id)
        if record.source_cell_id == record.source_vault_id:
            _fail()
    _generation(record.registry_generation)
    not_before = _timestamp(record.not_before)[1]
    not_after = _timestamp(record.not_after)[1]
    if not_before >= not_after or record.status not in {"active", "inactive", "revoked"}:
        _fail()
    if record.status == "revoked":
        if record.revoked_at is None or not record.revocation_reason:
            _fail()
        _timestamp(record.revoked_at)
        _normalize_text(record.revocation_reason)
    elif record.revoked_at is not None or record.revocation_reason is not None:
        _fail()
    return public_key, not_before, not_after


def _verify_expected_claims(
    claims: Mapping[str, str | int], expectation: SourceExportExpectation
) -> None:
    if not isinstance(expectation, SourceExportExpectation):
        _fail()
    expected = {
        "source_vault_id": _identifier(expectation.source_vault_id),
        "source_installation_id": _identifier(expectation.source_installation_id),
        "source_installation_generation": _generation(expectation.source_installation_generation),
        "source_active_fence_digest": _digest(expectation.source_active_fence_digest),
        "export_operation_id": _identifier(expectation.export_operation_id),
        "quiescence_checkpoint_digest": _digest(expectation.quiescence_checkpoint_digest),
        "archive_sha256": _digest(expectation.archive_sha256),
        "manifest_sha256": _digest(expectation.manifest_sha256),
        "source_census_sha256": _digest(expectation.source_census_sha256),
    }
    if any(claims[name] != value for name, value in expected.items()):
        _fail()
    if expectation.source_cell_id is None:
        if "source_cell_id" in claims:
            _fail()
    elif claims.get("source_cell_id") != _identifier(expectation.source_cell_id):
        _fail()


def verify_source_export_attestation(
    claim_bytes: bytes,
    signature: str,
    verifier_records: Sequence[SourceExportVerifierRecord],
    *,
    expectation: SourceExportExpectation,
    verified_at: str,
    verification_gate: str,
) -> VerifiedSourceExportAttestation:
    """Verify one detached proof against private destination trust and exact bytes."""

    if not isinstance(verification_gate, str) or verification_gate not in _GATES:
        _fail()
    if not isinstance(verifier_records, Sequence) or not 1 <= len(verifier_records) <= 2:
        _fail()
    if any(not isinstance(record, SourceExportVerifierRecord) for record in verifier_records):
        _fail()
    if len({record.key_id for record in verifier_records}) != len(verifier_records):
        _fail()

    canonical = canonical_source_export_claims(claim_bytes)
    claims = _normalize_claims(_parse_claim_bytes(canonical))
    _verify_expected_claims(claims, expectation)
    signature_bytes = _decode_base64url(signature, size=64)
    now = _timestamp(verified_at)[1]
    issued_at = _timestamp(claims["issued_at"])[1]
    expires_at = _timestamp(claims["expires_at"])[1]

    validated_records = [(record, *_validate_record(record)) for record in verifier_records]
    if len({record.registry_generation for record, *_rest in validated_records}) != 1:
        _fail()
    for record, _public_key, _not_before, _not_after in validated_records:
        if (
            record.status != "active"
            or record.audience != expectation.audience
            or record.source_vault_id != claims["source_vault_id"]
            or record.source_installation_id != claims["source_installation_id"]
            or record.source_installation_generation != claims["source_installation_generation"]
            or record.source_cell_id != claims.get("source_cell_id")
        ):
            _fail()
    matches = [
        validated
        for validated in validated_records
        if validated[0].key_id == claims["signer_key_id"]
    ]
    if len(matches) != 1:
        _fail()
    record, public_key, not_before, not_after = matches[0]
    if (
        issued_at < not_before
        or issued_at > not_after
        or now < not_before
        or now > not_after
        or now < issued_at
        or now > expires_at
    ):
        _fail()
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature_bytes, source_export_signing_bytes(canonical)
        )
    except (InvalidSignature, ValueError):
        _fail()

    return VerifiedSourceExportAttestation(
        claims=MappingProxyType(dict(claims)),
        claims_sha256=hashlib.sha256(canonical).hexdigest(),
        key_id=record.key_id,
        verification_gate=verification_gate,
    )
