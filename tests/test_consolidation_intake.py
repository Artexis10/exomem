from __future__ import annotations

import base64
import hashlib
import importlib
import inspect
import json
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
_PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
_D1 = "1" * 64
_D2 = "2" * 64
_D3 = "3" * 64
_D4 = "4" * 64
_D5 = "5" * 64
_EXOMEM_VECTOR_SIGNATURE = (
    "1aMkuhdSdAvQJ7ev9a_7iEbyBij-xirVgiRjuq5hub2U-Bth8zjuoPic-ktDqjrb00h0z7wS7TuNSC-DZD--Dw"
)


def _attestation_module():
    try:
        return importlib.import_module("exomem.governance.consolidation_attestation")
    except ModuleNotFoundError:
        pytest.fail("the detached source-export attestation boundary is missing")


def _claims(*, source_cell_id: str | None = None) -> dict[str, object]:
    claims: dict[str, object] = {
        "schema": "source-export-attestation/v1",
        "source_vault_id": "vault-source-01",
        "source_installation_id": "installation-source-01",
        "source_installation_generation": 7,
        "source_active_fence_digest": _D1,
        "export_operation_id": "export-operation-01",
        "quiescence_checkpoint_digest": _D2,
        "archive_sha256": _D3,
        "manifest_sha256": _D4,
        "source_census_sha256": _D5,
        "issued_at": "2026-08-28T09:45:00.000Z",
        "expires_at": "2026-08-28T10:45:00.000Z",
        "signer_key_id": f"ed25519-sha256:{hashlib.sha256(_PUBLIC).hexdigest()}",
    }
    if source_cell_id is not None:
        claims["source_cell_id"] = source_cell_id
    return claims


def _expectation(attestation, *, source_cell_id: str | None = None):
    return attestation.SourceExportExpectation(
        source_vault_id="vault-source-01",
        source_installation_id="installation-source-01",
        source_installation_generation=7,
        source_active_fence_digest=_D1,
        export_operation_id="export-operation-01",
        quiescence_checkpoint_digest=_D2,
        archive_sha256=_D3,
        manifest_sha256=_D4,
        source_census_sha256=_D5,
        audience="destination-trust-domain-01",
        source_cell_id=source_cell_id,
    )


def _record(attestation, **changes):
    values: dict[str, object] = {
        "schema": "SourceExportVerifierRecord/v1",
        "algorithm": "ed25519",
        "key_id": f"ed25519-sha256:{hashlib.sha256(_PUBLIC).hexdigest()}",
        "public_key": base64.urlsafe_b64encode(_PUBLIC).decode().rstrip("="),
        "purpose": "vault-consolidation-export",
        "audience": "destination-trust-domain-01",
        "source_vault_id": "vault-source-01",
        "source_installation_id": "installation-source-01",
        "source_installation_generation": 7,
        "status": "active",
        "not_before": "2026-08-28T00:00:00.000Z",
        "not_after": "2026-08-28T23:59:59.999Z",
        "registry_generation": 12,
    }
    values.update(changes)
    return attestation.SourceExportVerifierRecord(**values)


def test_source_export_attestation_contract_is_explicit() -> None:
    attestation = _attestation_module()

    assert attestation.ATTESTATION_SCHEMA == "source-export-attestation/v1"
    assert attestation.VERIFIER_RECORD_SCHEMA == "SourceExportVerifierRecord/v1"
    assert attestation.SIGNING_DOMAIN == b"exomem.source-export-attestation/v1"
    assert attestation.VERIFIER_PURPOSE == "vault-consolidation-export"


def test_source_export_attestation_exposes_only_detached_trust_primitives() -> None:
    attestation = _attestation_module()

    assert hasattr(attestation, "SourceExportExpectation")
    assert hasattr(attestation, "SourceExportVerifierRecord")
    assert hasattr(attestation, "VerifiedSourceExportAttestation")
    assert hasattr(attestation, "canonical_source_export_claims")
    assert hasattr(attestation, "source_export_signing_bytes")
    assert hasattr(attestation, "sign_source_export_attestation")
    assert hasattr(attestation, "verify_source_export_attestation")


def test_source_export_attestation_uses_exact_jcs_frame_and_rfc8032_key() -> None:
    attestation = _attestation_module()
    private_key = Ed25519PrivateKey.from_private_bytes(_SEED)
    assert (
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        == _PUBLIC
    )

    try:
        claims_bytes, signature = attestation.sign_source_export_attestation(_claims(), private_key)
    except attestation.SourceExportAttestationUnavailable:
        pytest.fail("valid detached source-export claims were refused")

    assert (
        claims_bytes
        == json.dumps(_claims(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    assert b"signature" not in claims_bytes
    expected_frame = (
        len(attestation.SIGNING_DOMAIN).to_bytes(4, "big")
        + attestation.SIGNING_DOMAIN
        + len(claims_bytes).to_bytes(8, "big")
        + claims_bytes
    )
    assert attestation.source_export_signing_bytes(claims_bytes) == expected_frame
    assert "=" not in signature
    assert len(base64.urlsafe_b64decode(signature + "==")) == 64
    assert signature == _EXOMEM_VECTOR_SIGNATURE


@pytest.mark.parametrize(
    "gate",
    ("intake", "apply", "retirement-clearance", "retirement-consumption"),
)
def test_source_export_attestation_verifies_independently_at_every_gate(gate: str) -> None:
    attestation = _attestation_module()
    claims_bytes, signature = attestation.sign_source_export_attestation(
        _claims(), Ed25519PrivateKey.from_private_bytes(_SEED)
    )

    try:
        verified = attestation.verify_source_export_attestation(
            claims_bytes,
            signature,
            (_record(attestation),),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate=gate,
        )
    except attestation.SourceExportAttestationUnavailable:
        pytest.fail(f"valid detached source-export claims were refused at {gate}")

    assert verified.claims == _claims()
    assert verified.key_id == _claims()["signer_key_id"]
    assert verified.verification_gate == gate
    assert verified.claims_sha256 == hashlib.sha256(claims_bytes).hexdigest()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("source_vault_id", "vault-other-01"),
        ("source_installation_id", "installation-other-01"),
        ("source_installation_generation", 8),
        ("source_active_fence_digest", "a" * 64),
        ("export_operation_id", "export-operation-other"),
        ("quiescence_checkpoint_digest", "b" * 64),
        ("archive_sha256", "c" * 64),
        ("manifest_sha256", "d" * 64),
        ("source_census_sha256", "e" * 64),
    ),
)
def test_source_export_attestation_refuses_every_changed_binding(
    field: str, replacement: str | int
) -> None:
    attestation = _attestation_module()
    changed = _claims()
    changed[field] = replacement
    claim_bytes, signature = attestation.sign_source_export_attestation(
        changed, Ed25519PrivateKey.from_private_bytes(_SEED)
    )

    with pytest.raises(
        attestation.SourceExportAttestationUnavailable,
        match="^source export attestation is unavailable$",
    ):
        attestation.verify_source_export_attestation(
            claim_bytes,
            signature,
            (_record(attestation),),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="intake",
        )


@pytest.mark.parametrize(
    "record_changes",
    (
        {"schema": "SourceExportVerifierRecord/v2"},
        {"algorithm": "hmac-sha256"},
        {"purpose": "export-consolidation"},
        {"audience": "other-destination"},
        {"source_vault_id": "vault-other-01"},
        {"source_installation_id": "installation-other-01"},
        {"source_installation_generation": 8},
        {"status": "inactive"},
        {
            "status": "revoked",
            "revoked_at": "2026-08-28T09:50:00.000Z",
            "revocation_reason": "retired",
        },
        {"not_before": "2026-08-28T09:46:00.000Z"},
        {"not_after": "2026-08-28T09:59:59.999Z"},
    ),
)
def test_source_export_attestation_refuses_unusable_private_trust_records(
    record_changes: dict[str, object],
) -> None:
    attestation = _attestation_module()
    claim_bytes, signature = attestation.sign_source_export_attestation(
        _claims(), Ed25519PrivateKey.from_private_bytes(_SEED)
    )

    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.verify_source_export_attestation(
            claim_bytes,
            signature,
            (_record(attestation, **record_changes),),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="intake",
        )


def test_source_export_attestation_rotation_is_explicitly_bounded_to_two_keys() -> None:
    attestation = _attestation_module()
    claim_bytes, signature = attestation.sign_source_export_attestation(
        _claims(), Ed25519PrivateKey.from_private_bytes(_SEED)
    )
    other_public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    other = _record(
        attestation,
        key_id=f"ed25519-sha256:{hashlib.sha256(other_public).hexdigest()}",
        public_key=base64.urlsafe_b64encode(other_public).decode().rstrip("="),
        registry_generation=13,
    )

    current_record = _record(attestation, registry_generation=13)
    assert (
        attestation.verify_source_export_attestation(
            claim_bytes,
            signature,
            (current_record, other),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="apply",
        ).key_id
        == _claims()["signer_key_id"]
    )
    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.verify_source_export_attestation(
            claim_bytes,
            signature,
            (current_record, other, replace(other, registry_generation=14)),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="apply",
        )

    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.verify_source_export_attestation(
            claim_bytes,
            signature,
            (_record(attestation, registry_generation=12), other),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="apply",
        )


def test_source_export_attestation_refuses_invalid_signature_and_unknown_key() -> None:
    attestation = _attestation_module()
    claim_bytes, signature = attestation.sign_source_export_attestation(
        _claims(), Ed25519PrivateKey.from_private_bytes(_SEED)
    )
    invalid_signature = ("A" if signature[0] != "A" else "B") + signature[1:]

    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.verify_source_export_attestation(
            claim_bytes,
            invalid_signature,
            (_record(attestation),),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="intake",
        )
    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.verify_source_export_attestation(
            claim_bytes,
            signature,
            (),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="intake",
        )


def test_source_export_attestation_keeps_local_and_hosted_ids_typed() -> None:
    attestation = _attestation_module()
    private_key = Ed25519PrivateKey.from_private_bytes(_SEED)

    hosted_claims, hosted_signature = attestation.sign_source_export_attestation(
        _claims(source_cell_id="cell-source-01"), private_key
    )
    verified = attestation.verify_source_export_attestation(
        hosted_claims,
        hosted_signature,
        (_record(attestation, source_cell_id="cell-source-01"),),
        expectation=_expectation(attestation, source_cell_id="cell-source-01"),
        verified_at="2026-08-28T10:00:00.000Z",
        verification_gate="intake",
    )
    assert verified.claims["source_cell_id"] == "cell-source-01"

    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.sign_source_export_attestation(
            _claims(source_cell_id="vault-source-01"), private_key
        )
    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.verify_source_export_attestation(
            hosted_claims,
            hosted_signature,
            (_record(attestation, source_cell_id="cell-source-01"),),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="intake",
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda claims: {**claims, "unknown": "value"},
        lambda claims: {key: value for key, value in claims.items() if key != "manifest_sha256"},
        lambda claims: {**claims, "schema": "source-export-attestation/v2"},
        lambda claims: {**claims, "source_vault_id": None},
        lambda claims: {**claims, "source_installation_generation": 1.5},
        lambda claims: {**claims, "issued_at": "2026-08-28T09:45:00Z"},
        lambda claims: {**claims, "expires_at": "2026-08-28T09:45:00.000Z"},
        lambda claims: {**claims, "signer_key_id": "sha256:" + "1" * 64},
    ),
)
def test_source_export_claims_reject_noncanonical_or_open_shapes(mutation) -> None:
    attestation = _attestation_module()

    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.canonical_source_export_claims(mutation(_claims()))


def test_source_export_claim_parser_rejects_duplicate_and_noncanonical_json() -> None:
    attestation = _attestation_module()
    canonical = attestation.canonical_source_export_claims(_claims())
    duplicate = canonical[:-1] + b',"schema":"source-export-attestation/v1"}'

    for raw in (duplicate, b" " + canonical, canonical.replace(b'"schema":', b'"sche\\u006da":')):
        with pytest.raises(attestation.SourceExportAttestationUnavailable):
            attestation.canonical_source_export_claims(raw)


def test_attestation_api_has_no_caller_key_or_shared_hmac_seam() -> None:
    attestation = _attestation_module()
    parameters = inspect.signature(attestation.verify_source_export_attestation).parameters

    assert "public_key" not in parameters
    assert "expected_hash" not in parameters
    assert "hmac_secret" not in parameters
    assert "shared_secret" not in parameters


def test_verified_source_claims_cannot_be_mutated_after_verification() -> None:
    attestation = _attestation_module()
    claim_bytes, signature = attestation.sign_source_export_attestation(
        _claims(), Ed25519PrivateKey.from_private_bytes(_SEED)
    )
    verified = attestation.verify_source_export_attestation(
        claim_bytes,
        signature,
        (_record(attestation),),
        expectation=_expectation(attestation),
        verified_at="2026-08-28T10:00:00.000Z",
        verification_gate="intake",
    )

    with pytest.raises(TypeError):
        verified.claims["archive_sha256"] = "f" * 64


def test_malformed_trust_set_refuses_with_the_same_content_free_error() -> None:
    attestation = _attestation_module()
    claim_bytes, signature = attestation.sign_source_export_attestation(
        _claims(), Ed25519PrivateKey.from_private_bytes(_SEED)
    )

    with pytest.raises(
        attestation.SourceExportAttestationUnavailable,
        match="^source export attestation is unavailable$",
    ):
        attestation.verify_source_export_attestation(
            claim_bytes,
            signature,
            ({"public_key": "caller-selected"},),
            expectation=_expectation(attestation),
            verified_at="2026-08-28T10:00:00.000Z",
            verification_gate="intake",
        )
