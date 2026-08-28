from __future__ import annotations

import base64
import hashlib
import importlib
import inspect
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from exomem import hosted_portability
from exomem.governance import consolidation_fingerprints

_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
_PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
_D1 = "1" * 64
_D2 = "2" * 64
_D3 = "3" * 64
_D4 = "4" * 64
_D5 = "5" * 64
_EXOMEM_VECTOR_SIGNATURE = (
    "8KW3d2q2F5x_j8iJdSiTkym-JfMBg12uHrx_9ajssZGreqVHmUa10GTLXslUYMijOSQYv1A2vTT_Te_wmfm5Cg"
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
        "source_identity_binding_digest": _D2,
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
        source_identity_binding_digest=_D2,
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
        ("source_identity_binding_digest", "f" * 64),
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


def test_oversized_claims_refuse_before_json_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    attestation = _attestation_module()
    limit = getattr(attestation, "MAX_ATTESTATION_CLAIM_BYTES", None)
    assert limit == 8192
    oversized = b" " * (limit + 1)

    monkeypatch.setattr(
        attestation.json,
        "loads",
        lambda *_args, **_kwargs: pytest.fail("oversized claims reached the JSON parser"),
    )
    with pytest.raises(attestation.SourceExportAttestationUnavailable):
        attestation.canonical_source_export_claims(oversized)


def _intake_module():
    try:
        return importlib.import_module("exomem.governance.consolidation_intake")
    except ModuleNotFoundError:
        pytest.fail("the private consolidation intake boundary is missing")


def _portable_vault(root: Path) -> Path:
    vault = root / "source-vault"
    (vault / "Knowledge Base" / "_Schema").mkdir(parents=True)
    (vault / "Knowledge Base" / "_Schema" / "SKILL.md").write_text(
        "# schema\n", encoding="utf-8"
    )
    (vault / "Knowledge Base" / "Notes").mkdir()
    (vault / "Knowledge Base" / "Notes" / "private.md").write_text(
        "# Private\n\nsource-only sentinel\n", encoding="utf-8"
    )
    (vault / "Knowledge Base" / "asset.bin").write_bytes(b"\x00\x01\x02")
    return vault


def _export(root: Path):
    vault = _portable_vault(root)
    exported = hosted_portability.export_quiesced_vault(
        vault,
        root / "exports",
        context=hosted_portability.PortabilityContext(
            cell_id="cell-source-01",
            vault_id="vault-source-01",
            operation_id="export-operation-01",
            created_at="2026-08-28T09:44:00.000+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    return vault, exported


def _resolved_proof(intake, exported):
    attestation = _attestation_module()
    consolidation_census = consolidation_fingerprints.source_content_census_from_manifest(
        exported.manifest
    ).digest
    claims = _claims(source_cell_id="cell-source-01")
    claims.update(
        archive_sha256=exported.archive_sha256,
        manifest_sha256=exported.manifest_sha256,
        source_census_sha256=consolidation_census,
    )
    claim_bytes, signature = attestation.sign_source_export_attestation(
        claims,
        Ed25519PrivateKey.from_private_bytes(_SEED),
    )
    expectation = replace(
        _expectation(attestation, source_cell_id="cell-source-01"),
        archive_sha256=exported.archive_sha256,
        manifest_sha256=exported.manifest_sha256,
        source_census_sha256=consolidation_census,
    )
    proof = intake.ResolvedSourceExportProof(
        claim_bytes=claim_bytes,
        signature=signature,
        expectation=expectation,
        verifier_records=(
            _record(attestation, source_cell_id="cell-source-01"),
        ),
    )
    proof_ref = (
        "exomem-source-attestation://sha256/"
        + intake.detached_source_proof_digest(claim_bytes, signature)
    )
    return proof_ref, proof


class _Resolver:
    def __init__(self, *, archive_ref: str, archive_path: Path, proof_ref: str, proof: Any):
        self.archive_ref = archive_ref
        self.archive_path = archive_path
        self.proof_ref = proof_ref
        self.proof = proof
        self.calls: list[tuple[str, str]] = []

    def resolve_archive(self, reference: str) -> Path:
        self.calls.append(("archive", reference))
        if reference != self.archive_ref:
            raise LookupError
        return self.archive_path

    def resolve_source_proof(self, reference: str):
        self.calls.append(("proof", reference))
        if reference != self.proof_ref:
            raise LookupError
        return self.proof


def _intake_fixture(tmp_path: Path):
    intake = _intake_module()
    vault, exported = _export(tmp_path)
    proof_ref, proof = _resolved_proof(intake, exported)
    request = intake.ConsolidationIntakeRequest(
        source_artifact_ref=exported.artifact_reference,
        source_attestation_ref=proof_ref,
    )
    resolver = _Resolver(
        archive_ref=exported.artifact_reference,
        archive_path=exported.archive_path,
        proof_ref=proof_ref,
        proof=proof,
    )
    store = intake.PrivateConsolidationArtifactStore(
        tmp_path / "private-intake",
        active_vault_roots=(vault, tmp_path / "destination-vault"),
    )
    return intake, vault, exported, request, resolver, store


def test_portable_export_carries_the_exact_current_source_census(tmp_path: Path) -> None:
    vault, exported = _export(tmp_path)

    assert exported.source_census_sha256 == hosted_portability.canonical_source_census_sha256(
        exported.manifest
    )
    assert exported.source_census_sha256 == hosted_portability.canonical_vault_fingerprint(vault)

    (vault / "Knowledge Base" / "Notes" / "private.md").write_text(
        "# Private\n\nchanged after export\n", encoding="utf-8"
    )
    assert hosted_portability.canonical_vault_fingerprint(vault) != exported.source_census_sha256


def test_intake_request_has_only_opaque_archive_and_proof_references() -> None:
    intake = _intake_module()

    assert tuple(inspect.signature(intake.ConsolidationIntakeRequest).parameters) == (
        "source_artifact_ref",
        "source_attestation_ref",
    )
    for forbidden in (
        "archive",
        "archive_bytes",
        "archive_path",
        "source_root",
        "staging_root",
        "crawler",
        "mcp",
        "public_key",
    ):
        assert forbidden not in inspect.signature(intake.intake_source_export).parameters


@pytest.mark.parametrize(
    ("artifact_ref", "proof_ref"),
    (
        ("/tmp/source.zip", "exomem-source-attestation://sha256/" + "a" * 64),
        ("exomem-export://sha256/" + "a" * 64, "/tmp/proof.json"),
        ("data:application/zip;base64,AAAA", "exomem-source-attestation://sha256/" + "a" * 64),
        ("exomem-export://sha256/" + "a" * 64, "inline:{\"signature\":\"x\"}"),
    ),
)
def test_intake_rejects_inline_or_path_inputs_before_resolution(
    tmp_path: Path,
    artifact_ref: str,
    proof_ref: str,
) -> None:
    intake = _intake_module()

    class NeverResolve:
        def resolve_archive(self, _reference: str) -> Path:
            pytest.fail("invalid archive reference reached resolution")

        def resolve_source_proof(self, _reference: str):
            pytest.fail("invalid proof reference reached resolution")

    store = intake.PrivateConsolidationArtifactStore(tmp_path / "private", active_vault_roots=())
    with pytest.raises(
        intake.ConsolidationIntakeUnavailable,
        match="^consolidation intake is unavailable$",
    ):
        intake.intake_source_export(
            intake.ConsolidationIntakeRequest(artifact_ref, proof_ref),
            resolver=NeverResolve(),
            artifact_store=store,
            verified_at="2026-08-28T10:00:00.000Z",
        )


@pytest.mark.parametrize("relative", ("private-intake", "Knowledge Base/private-intake"))
def test_intake_refuses_private_extraction_beneath_an_active_vault(
    tmp_path: Path, relative: str
) -> None:
    intake, vault, _exported, request, resolver, _store = _intake_fixture(tmp_path)

    with pytest.raises(intake.ConsolidationIntakeUnavailable):
        intake.PrivateConsolidationArtifactStore(
            vault / relative,
            active_vault_roots=(vault,),
        )
    assert resolver.calls == []

    # The valid request still has no caller-controlled extraction target.
    assert not hasattr(request, "staging_root")


def test_intake_refuses_any_knowledge_base_extraction_root(tmp_path: Path) -> None:
    intake = _intake_module()

    with pytest.raises(intake.ConsolidationIntakeUnavailable):
        intake.PrivateConsolidationArtifactStore(
            tmp_path / "detached" / "Knowledge Base" / "private-intake",
            active_vault_roots=(),
        )


def test_intake_refuses_a_private_root_beneath_a_nested_symlink(
    tmp_path: Path,
) -> None:
    intake = _intake_module()
    actual = tmp_path / "actual"
    existing = actual / "existing"
    existing.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(intake.ConsolidationIntakeUnavailable):
        intake.PrivateConsolidationArtifactStore(
            alias / "existing" / "private-intake",
            active_vault_roots=(),
        )


def test_intake_extracts_only_private_content_addressed_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake, _vault, exported, request, resolver, store = _intake_fixture(tmp_path)
    archive_before = exported.archive_path.read_bytes()
    census_before = consolidation_fingerprints.source_content_census_from_manifest(
        exported.manifest
    ).digest

    monkeypatch.setattr(
        hosted_portability,
        "prepare_restore",
        lambda *_args, **_kwargs: pytest.fail("consolidation called restore staging"),
    )
    result = intake.intake_source_export(
        request,
        resolver=resolver,
        artifact_store=store,
        verified_at="2026-08-28T10:00:00.000Z",
    )

    assert resolver.calls == [
        ("archive", request.source_artifact_ref),
        ("proof", request.source_attestation_ref),
    ]
    assert result.archive_sha256 == exported.archive_sha256
    assert result.manifest_sha256 == exported.manifest_sha256
    assert result.source_census_sha256 == census_before
    assert result.source_fingerprint == consolidation_fingerprints.source_fingerprint(
        json.loads(resolver.proof.claim_bytes),
        authentication_proof_digest=result.source_proof_digest,
    ).digest
    assert result.object_count == len(exported.manifest["files"])
    assert result.total_bytes == sum(item["size"] for item in exported.manifest["files"])
    assert result.archive_artifact_ref.startswith("exomem-consolidation-archive://sha256/")
    assert result.source_proof_artifact_ref.startswith(
        "exomem-consolidation-proof://sha256/"
    )
    assert tuple(item.path for item in result.inventory) == tuple(
        item["path"] for item in exported.manifest["files"]
    )
    assert all(
        item.artifact_ref == f"exomem-consolidation-object://sha256/{item.sha256}"
        for item in result.inventory
    )
    assert all(store.resolve_object(item.artifact_ref).read_bytes() for item in result.inventory)
    assert store.resolve_archive(result.archive_artifact_ref).read_bytes() == archive_before
    proof_bytes = store.resolve_source_proof(result.source_proof_artifact_ref).read_bytes()
    assert hashlib.sha256(proof_bytes).hexdigest() == result.source_proof_digest

    rendered = json.dumps(result.to_bounded_dict(), sort_keys=True)
    assert "source-only sentinel" not in rendered
    assert str(tmp_path) not in rendered
    assert "archive_path" not in rendered
    assert exported.archive_path.read_bytes() == archive_before
    assert (
        consolidation_fingerprints.source_content_census_from_manifest(
            exported.manifest
        ).digest
        == census_before
    )


def test_intake_is_idempotent_and_deduplicates_equal_content(tmp_path: Path) -> None:
    intake, _vault, _exported, request, resolver, store = _intake_fixture(tmp_path)

    first = intake.intake_source_export(
        request,
        resolver=resolver,
        artifact_store=store,
        verified_at="2026-08-28T10:00:00.000Z",
    )
    second = intake.intake_source_export(
        request,
        resolver=resolver,
        artifact_store=store,
        verified_at="2026-08-28T10:00:00.000Z",
    )

    assert second == first
    assert len(list((store.root / "archives").glob("*.zip"))) == 1
    assert len(list((store.root / "proofs").glob("*.json"))) == 1
    assert not list(store.root.rglob("*.partial"))
    assert not list(store.root.rglob(".intake-*"))


def test_intake_refuses_changed_proof_or_archive_without_publishing(
    tmp_path: Path,
) -> None:
    intake, _vault, exported, request, resolver, store = _intake_fixture(tmp_path)
    resolver.proof = replace(
        resolver.proof,
        expectation=replace(resolver.proof.expectation, source_census_sha256="f" * 64),
    )
    archive_before = exported.archive_path.read_bytes()

    with pytest.raises(
        intake.ConsolidationIntakeUnavailable,
        match="^consolidation intake is unavailable$",
    ):
        intake.intake_source_export(
            request,
            resolver=resolver,
            artifact_store=store,
            verified_at="2026-08-28T10:00:00.000Z",
        )

    assert exported.archive_path.read_bytes() == archive_before
    assert not store.root.exists() or not [path for path in store.root.rglob("*") if path.is_file()]
