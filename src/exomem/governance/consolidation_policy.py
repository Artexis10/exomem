"""Fresh destination authority and prospective policy for consolidation.

Source-side policy is evidence, never authority.  This module accepts only
digest-only source review records, resolves destination principals from the
already-verified request context, and compiles newly authored destination
documents against the exact live authoring snapshot.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

from ..held_fs import StableIdentity
from . import policy
from .principal import MOST_RESTRICTIVE_AUDIENCE, RequestPrincipal
from .projections import ProjectionCanonicalizationError, canonical_jcs

DESTINATION_PRINCIPAL_ATTESTATION_SCHEMA = "destination-principal-attestation/v1"
DESTINATION_POLICY_PLAN_SCHEMA = "exomem.consolidation-destination-policy/v1"

_AUTHENTICATION_BINDING_SCHEMA = "exomem.destination-authentication-binding/v1"
_ATTESTATION_DOMAIN = b"exomem.destination-principal-attestation/v1"
_AUTHENTICATION_BINDING_DOMAIN = b"exomem.destination-authentication-binding/v1"
_SOURCE_AUTHORITY_REVIEW_DOMAIN = b"exomem.source-authority-review/v1"
_ATTESTATION_SET_DOMAIN = b"exomem.destination-principal-attestation-set/v1"
_DOCUMENT_SET_DOMAIN = b"exomem.consolidation-destination-documents/v1"
_AUTHORING_SNAPSHOT_DOMAIN = b"exomem.consolidation-authoring-snapshot/v1"
_PLAN_DOMAIN = b"exomem.consolidation-destination-policy-plan/v1"

_TRUSTED_SURFACES = frozenset({"cli", "hosted", "mcp", "rest"})
_SOURCE_AUTHORITY_KINDS = frozenset(
    {
        "access_control",
        "audience",
        "authorization_session",
        "credential",
        "grant",
        "policy",
        "receipt",
        "release_approval",
        "review_authority",
        "runtime_binding",
        "token",
    }
)
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z")
_RFC3339_MILLISECONDS = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_DOCUMENTS = 1024
_MAX_DOCUMENT_BYTES = 1 << 20

__all__ = [
    "DESTINATION_POLICY_PLAN_SCHEMA",
    "DESTINATION_PRINCIPAL_ATTESTATION_SCHEMA",
    "DestinationPolicyPlan",
    "DestinationPolicyUnavailable",
    "DestinationPrincipalAttestation",
    "SourceAuthorityReviewArtifact",
    "VerifiedDestinationPrincipalAttestation",
    "compile_destination_policy",
    "issue_destination_principal_attestation",
    "principal_attestation_set_digest",
    "revalidate_destination_policy",
    "source_authority_review_digest",
    "verify_destination_principal_attestation",
]


class DestinationPolicyUnavailable(RuntimeError):
    """Content-free refusal for an unavailable destination policy plan."""

    code = "DESTINATION_POLICY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("destination policy is unavailable")


@dataclass(frozen=True, slots=True)
class DestinationPrincipalAttestation:
    schema: str
    destination_vault_id: str
    issuer_family: str
    surface: str
    principal_id: str
    purposes: tuple[str, ...]
    issued_at: str
    expires_at: str
    authentication_binding_digest: str
    nonce: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class VerifiedDestinationPrincipalAttestation:
    attestation: DestinationPrincipalAttestation
    verified_at: str


@dataclass(frozen=True, slots=True)
class SourceAuthorityReviewArtifact:
    """Authenticated source provenance with no executable bytes or credentials."""

    object_ref: str
    object_kind: str
    object_sha256: str
    bundle_sha256: str
    provenance_ref: str


@dataclass(frozen=True, slots=True)
class DestinationPolicyPlan:
    schema: str
    destination_vault_id: str
    prospective: policy.ProspectiveCompile
    document_edits: tuple[tuple[str, str | None], ...]
    document_set_digest: str
    authoring_snapshot_digest: str
    source_authority: tuple[SourceAuthorityReviewArtifact, ...]
    source_authority_review_digest: str
    attestations: tuple[DestinationPrincipalAttestation, ...]
    principal_attestation_set_digest: str
    principal_requirements: tuple[tuple[str, tuple[str, ...]], ...]
    named_principals: tuple[str, ...]
    digest: str


def _fail() -> NoReturn:
    raise DestinationPolicyUnavailable from None


def _text(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    if normalized != value:
        _fail()
    return normalized


def _identifier(value: object) -> str:
    normalized = _text(value)
    if _IDENTIFIER.fullmatch(normalized) is None:
        _fail()
    return normalized


def _reference(value: object) -> str:
    normalized = _text(value)
    if _REFERENCE.fullmatch(normalized) is None:
        _fail()
    return normalized


def _digest(value: object) -> str:
    normalized = _text(value)
    if _HEX_DIGEST.fullmatch(normalized) is None:
        _fail()
    return normalized


def _timestamp(value: object) -> tuple[str, datetime]:
    normalized = _text(value)
    if _RFC3339_MILLISECONDS.fullmatch(normalized) is None:
        _fail()
    try:
        parsed = datetime.fromisoformat(normalized.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail()
    if parsed.tzinfo != UTC:
        _fail()
    return normalized, parsed


def _canonical(value: object) -> bytes:
    try:
        return canonical_jcs(value)
    except ProjectionCanonicalizationError:
        _fail()


def _framed_digest(domain: bytes, value: object) -> str:
    raw = _canonical(value)
    framed = len(domain).to_bytes(4, "big") + domain + len(raw).to_bytes(8, "big") + raw
    return hashlib.sha256(framed).hexdigest()


def _purposes(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or len(values) > 64:
        _fail()
    normalized = tuple(_identifier(item) for item in values)
    if tuple(sorted(set(normalized))) != normalized:
        _fail()
    return normalized


def _authentication_binding(
    principal: RequestPrincipal, destination_vault_id: str
) -> dict[str, object]:
    if (
        not isinstance(principal, RequestPrincipal)
        or not principal.resolved
        or principal.audience_id == MOST_RESTRICTIVE_AUDIENCE
        or principal.surface not in _TRUSTED_SURFACES
        or principal.verified_authorization_session is None
    ):
        _fail()
    context = principal.verified_authorization_session
    principal_id = _identifier(principal.audience_id)
    issuer_family = _identifier(principal.issuer_family)
    if (
        principal_id != _identifier(context.principal_id)
        or issuer_family != _identifier(context.issuer_family)
        or destination_vault_id != _identifier(context.logical_vault_id)
        or principal.authorization_session_id != context.session_id
    ):
        _fail()
    if type(context.credential_generation) is not int or not (
        1 <= context.credential_generation <= _MAX_SAFE_INTEGER
    ):
        _fail()
    if type(context.expires_at) is not int or not 1 <= context.expires_at <= _MAX_SAFE_INTEGER:
        _fail()
    binding: dict[str, object] = {
        "schema": _AUTHENTICATION_BINDING_SCHEMA,
        "destination_vault_id": _identifier(destination_vault_id),
        "surface": principal.surface,
        "principal_id": principal_id,
        "issuer_family": issuer_family,
        "authorization_session_id": _identifier(context.session_id),
        "cell_id": _identifier(context.cell_id),
        "logical_vault_id": _identifier(context.logical_vault_id),
        "keyring_id": _identifier(context.keyring_id),
        "credential_generation": context.credential_generation,
        "session_expires_at": context.expires_at,
    }
    if principal.session_id is not None:
        binding["transport_session_id"] = _identifier(principal.session_id)
    return binding


def _authentication_binding_digest(principal: RequestPrincipal, destination_vault_id: str) -> str:
    return _framed_digest(
        _AUTHENTICATION_BINDING_DOMAIN,
        _authentication_binding(principal, destination_vault_id),
    )


def _attestation_claims(
    attestation: DestinationPrincipalAttestation,
) -> dict[str, object]:
    return {
        "schema": attestation.schema,
        "destination_vault_id": attestation.destination_vault_id,
        "issuer_family": attestation.issuer_family,
        "surface": attestation.surface,
        "principal_id": attestation.principal_id,
        "purposes": list(attestation.purposes),
        "issued_at": attestation.issued_at,
        "expires_at": attestation.expires_at,
        "authentication_binding_digest": attestation.authentication_binding_digest,
        "nonce": attestation.nonce,
    }


def _validate_attestation_shape(
    attestation: DestinationPrincipalAttestation,
) -> tuple[datetime, datetime]:
    if not isinstance(attestation, DestinationPrincipalAttestation):
        _fail()
    if attestation.schema != DESTINATION_PRINCIPAL_ATTESTATION_SCHEMA:
        _fail()
    _identifier(attestation.destination_vault_id)
    _identifier(attestation.issuer_family)
    if attestation.surface not in _TRUSTED_SURFACES:
        _fail()
    _identifier(attestation.principal_id)
    _purposes(attestation.purposes)
    _digest(attestation.authentication_binding_digest)
    _identifier(attestation.nonce)
    _digest(attestation.fingerprint)
    _issued_text, issued = _timestamp(attestation.issued_at)
    _expires_text, expires = _timestamp(attestation.expires_at)
    if issued >= expires:
        _fail()
    expected = _framed_digest(_ATTESTATION_DOMAIN, _attestation_claims(attestation))
    if not secrets.compare_digest(expected, attestation.fingerprint):
        _fail()
    return issued, expires


def issue_destination_principal_attestation(
    principal: RequestPrincipal,
    *,
    destination_vault_id: str,
    purposes: Sequence[str],
    issued_at: str,
    expires_at: str,
    nonce: str,
) -> DestinationPrincipalAttestation:
    """Issue from a trusted, already-resolved destination request context."""

    vault_id = _identifier(destination_vault_id)
    normalized_purposes = _purposes(purposes)
    issued_text, issued = _timestamp(issued_at)
    expires_text, expires = _timestamp(expires_at)
    normalized_nonce = _identifier(nonce)
    binding = _authentication_binding(principal, vault_id)
    if issued >= expires or expires.timestamp() > cast(int, binding["session_expires_at"]):
        _fail()
    unsigned = DestinationPrincipalAttestation(
        schema=DESTINATION_PRINCIPAL_ATTESTATION_SCHEMA,
        destination_vault_id=vault_id,
        issuer_family=_identifier(principal.issuer_family),
        surface=principal.surface,
        principal_id=_identifier(principal.audience_id),
        purposes=normalized_purposes,
        issued_at=issued_text,
        expires_at=expires_text,
        authentication_binding_digest=_framed_digest(_AUTHENTICATION_BINDING_DOMAIN, binding),
        nonce=normalized_nonce,
        fingerprint="0" * 64,
    )
    return DestinationPrincipalAttestation(
        schema=unsigned.schema,
        destination_vault_id=unsigned.destination_vault_id,
        issuer_family=unsigned.issuer_family,
        surface=unsigned.surface,
        principal_id=unsigned.principal_id,
        purposes=unsigned.purposes,
        issued_at=unsigned.issued_at,
        expires_at=unsigned.expires_at,
        authentication_binding_digest=unsigned.authentication_binding_digest,
        nonce=unsigned.nonce,
        fingerprint=_framed_digest(_ATTESTATION_DOMAIN, _attestation_claims(unsigned)),
    )


def verify_destination_principal_attestation(
    attestation: DestinationPrincipalAttestation,
    *,
    principal: RequestPrincipal,
    destination_vault_id: str,
    required_purpose: str | None,
    expected_nonce: str,
    verified_at: str,
) -> VerifiedDestinationPrincipalAttestation:
    """Bind one attestation to the current trusted destination session."""

    issued, expires = _validate_attestation_shape(attestation)
    vault_id = _identifier(destination_vault_id)
    nonce = _identifier(expected_nonce)
    verified_text, verified = _timestamp(verified_at)
    binding_digest = _authentication_binding_digest(principal, vault_id)
    if (
        attestation.destination_vault_id != vault_id
        or attestation.issuer_family != principal.issuer_family
        or attestation.surface != principal.surface
        or attestation.principal_id != principal.audience_id
        or attestation.authentication_binding_digest != binding_digest
        or attestation.nonce != nonce
        or verified < issued
        or verified >= expires
    ):
        _fail()
    if required_purpose is not None and _identifier(required_purpose) not in attestation.purposes:
        _fail()
    return VerifiedDestinationPrincipalAttestation(
        attestation=attestation,
        verified_at=verified_text,
    )


def _source_authority_value(
    artifacts: Sequence[SourceAuthorityReviewArtifact],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, SourceAuthorityReviewArtifact):
            _fail()
        row = {
            "object_ref": _reference(artifact.object_ref),
            "object_kind": _identifier(artifact.object_kind),
            "object_sha256": _digest(artifact.object_sha256),
            "bundle_sha256": _digest(artifact.bundle_sha256),
            "provenance_ref": _reference(artifact.provenance_ref),
        }
        if row["object_kind"] not in _SOURCE_AUTHORITY_KINDS or row["object_ref"] in seen:
            _fail()
        seen.add(row["object_ref"])
        rows.append(row)
    if len(rows) > 4096:
        _fail()
    rows.sort(key=lambda item: item["object_ref"])
    return rows


def source_authority_review_digest(
    artifacts: Sequence[SourceAuthorityReviewArtifact],
) -> str:
    return _framed_digest(_SOURCE_AUTHORITY_REVIEW_DOMAIN, _source_authority_value(artifacts))


def _attestation_value(
    attestations: Sequence[DestinationPrincipalAttestation],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for attestation in attestations:
        _validate_attestation_shape(attestation)
        if attestation.fingerprint in seen:
            _fail()
        seen.add(attestation.fingerprint)
        rows.append({**_attestation_claims(attestation), "fingerprint": attestation.fingerprint})
    rows.sort(key=lambda item: str(item["fingerprint"]))
    return rows


def principal_attestation_set_digest(
    attestations: Sequence[DestinationPrincipalAttestation],
) -> str:
    return _framed_digest(_ATTESTATION_SET_DOMAIN, _attestation_value(attestations))


def _document_edits(documents: Mapping[str, str | None]) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(documents, Mapping) or len(documents) > _MAX_DOCUMENTS:
        _fail()
    normalized: dict[str, str | None] = {}
    for raw_path, raw_content in documents.items():
        path = _text(raw_path)
        if "\\" in path:
            _fail()
        parsed = PurePosixPath(path)
        if (
            path != parsed.as_posix()
            or parsed.is_absolute()
            or any(part in {"", ".", ".."} for part in parsed.parts)
            or parsed.parts[0] not in {"grants", "rules", "scopes"}
            or parsed.suffix not in {".yaml", ".yml"}
            or path in normalized
        ):
            _fail()
        if raw_content is None:
            content = None
        else:
            content = _text(raw_content)
            if len(content.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
                _fail()
        normalized[path] = content
    return tuple(sorted(normalized.items()))


def _document_set_digest(documents: Sequence[tuple[str, bytes]]) -> str:
    rows = [
        {"path": _text(path), "byte_size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for path, raw in documents
    ]
    return _framed_digest(_DOCUMENT_SET_DOMAIN, rows)


def _stable_identity_value(identity: StableIdentity) -> dict[str, object]:
    device = identity.device
    inode = identity.inode
    kind = identity.kind
    link_count = identity.link_count
    if any(type(value) is not int or value < 0 for value in (device, inode, link_count)):
        _fail()
    return {
        "device": device,
        "inode": inode,
        "kind": _identifier(kind),
        "link_count": link_count,
    }


def _authoring_snapshot_digest(snapshot: policy.AuthoringSnapshot) -> str:
    root: dict[str, object]
    if snapshot.governance_root_identity is None:
        root = {"state": "absent"}
    else:
        root = {
            "state": "present",
            "identity": _stable_identity_value(snapshot.governance_root_identity),
        }
    value = {
        "source_fingerprint": _digest(snapshot.source_fingerprint),
        "conflict_set_digest": _digest(snapshot.conflict_set_digest),
        "guard_generation": _text(snapshot.guard_generation),
        "documents": [
            {
                "path": _text(path),
                "byte_size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for path, raw in snapshot.documents
        ],
        "file_identities": [
            {
                "path": _text(item.path),
                "sha256": _digest(item.sha256),
                "identity": _stable_identity_value(item.identity),
            }
            for item in snapshot.file_identities
        ],
        "directory_identities": [
            {"path": _text(path), "identity": _stable_identity_value(identity)}
            for path, identity in snapshot.directory_identities
        ],
        "governance_root": root,
    }
    return _framed_digest(_AUTHORING_SNAPSHOT_DOMAIN, value)


def _requirements(compiled: policy.Policy) -> dict[str, set[str]]:
    required: dict[str, set[str]] = {}
    for rule in compiled.rules:
        required.setdefault(rule.audience, set())
        if rule.purpose is not None:
            required[rule.audience].add(rule.purpose)
    for grant in compiled.grants:
        required.setdefault(grant.audience, set())
    for release in compiled.release_grants:
        required.setdefault(release.to_audience, set())
    for principal_id in required:
        _identifier(principal_id)
    return required


def _complete_requirements(
    compiled: policy.Policy,
    representative: Mapping[str, Sequence[str]] | None,
) -> dict[str, set[str]]:
    required = _requirements(compiled)
    if representative is None:
        return required
    if not isinstance(representative, Mapping) or len(representative) > 1024:
        _fail()
    normalized: dict[str, tuple[str, ...]] = {}
    for raw_principal, raw_purposes in representative.items():
        principal_id = _identifier(raw_principal)
        if principal_id in normalized:
            _fail()
        normalized[principal_id] = _purposes(raw_purposes)
    for principal_id, purposes in normalized.items():
        required.setdefault(principal_id, set()).update(purposes)
    return required


def _requirements_tuple(
    requirements: Mapping[str, set[str]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (principal_id, tuple(sorted(purposes)))
        for principal_id, purposes in sorted(requirements.items())
    )


def _principal_map(principals: Sequence[RequestPrincipal]) -> dict[str, RequestPrincipal]:
    result: dict[str, RequestPrincipal] = {}
    for principal in principals:
        if not isinstance(principal, RequestPrincipal):
            _fail()
        principal_id = _identifier(principal.audience_id)
        if principal_id in result:
            _fail()
        result[principal_id] = principal
    return result


def _verify_attestation_set(
    attestations: Sequence[DestinationPrincipalAttestation],
    principals: Sequence[RequestPrincipal],
    requirements: Mapping[str, set[str]],
    *,
    destination_vault_id: str,
    expected_nonce: str,
    verified_at: str,
) -> tuple[DestinationPrincipalAttestation, ...]:
    principal_by_id = _principal_map(principals)
    attestation_by_principal: dict[str, DestinationPrincipalAttestation] = {}
    for attestation in attestations:
        if attestation.principal_id in attestation_by_principal:
            _fail()
        principal = principal_by_id.get(attestation.principal_id)
        if principal is None:
            _fail()
        verify_destination_principal_attestation(
            attestation,
            principal=principal,
            destination_vault_id=destination_vault_id,
            required_purpose=None,
            expected_nonce=expected_nonce,
            verified_at=verified_at,
        )
        attestation_by_principal[attestation.principal_id] = attestation
    if set(attestation_by_principal) != set(requirements):
        _fail()
    for principal_id, purposes in requirements.items():
        if not purposes <= set(attestation_by_principal[principal_id].purposes):
            _fail()
    return tuple(sorted(attestation_by_principal.values(), key=lambda item: item.principal_id))


def _plan_digest(plan: DestinationPolicyPlan) -> str:
    snapshot = plan.prospective.snapshot
    value = {
        "schema": plan.schema,
        "destination_vault_id": plan.destination_vault_id,
        "source_fingerprint": snapshot.source_fingerprint,
        "conflict_set_digest": snapshot.conflict_set_digest,
        "guard_generation": snapshot.guard_generation,
        "document_set_digest": plan.document_set_digest,
        "authoring_snapshot_digest": plan.authoring_snapshot_digest,
        "policy_fingerprint": plan.prospective.policy.fingerprint,
        "source_authority_review_digest": plan.source_authority_review_digest,
        "principal_attestation_set_digest": plan.principal_attestation_set_digest,
        "principal_requirements": [
            {"principal_id": principal_id, "purposes": list(purposes)}
            for principal_id, purposes in plan.principal_requirements
        ],
        "named_principals": list(plan.named_principals),
    }
    return _framed_digest(_PLAN_DOMAIN, value)


def compile_destination_policy(
    vault_root: Path,
    *,
    documents: Mapping[str, str | None],
    source_authority: Sequence[SourceAuthorityReviewArtifact],
    attestations: Sequence[DestinationPrincipalAttestation],
    principal_contexts: Sequence[RequestPrincipal],
    representative_principal_purposes: Mapping[str, Sequence[str]] | None = None,
    destination_vault_id: str,
    expected_nonce: str,
    verified_at: str,
) -> DestinationPolicyPlan:
    """Compile newly authored destination documents without activating them."""

    vault_id = _identifier(destination_vault_id)
    edits = _document_edits(documents)
    prospective = policy.compile_prospective(Path(vault_root), dict(edits))
    if prospective is None or prospective.policy.blocked or prospective.policy.conflicted:
        _fail()
    requirements = _complete_requirements(prospective.policy, representative_principal_purposes)
    verified_attestations = _verify_attestation_set(
        attestations,
        principal_contexts,
        requirements,
        destination_vault_id=vault_id,
        expected_nonce=expected_nonce,
        verified_at=verified_at,
    )
    source_rows = _source_authority_value(source_authority)
    ordered_source = tuple(sorted(source_authority, key=lambda item: item.object_ref))
    source_digest = _framed_digest(_SOURCE_AUTHORITY_REVIEW_DOMAIN, source_rows)
    attestation_digest = principal_attestation_set_digest(verified_attestations)
    plan = DestinationPolicyPlan(
        schema=DESTINATION_POLICY_PLAN_SCHEMA,
        destination_vault_id=vault_id,
        prospective=prospective,
        document_edits=edits,
        document_set_digest=_document_set_digest(prospective.target_documents),
        authoring_snapshot_digest=_authoring_snapshot_digest(prospective.snapshot),
        source_authority=ordered_source,
        source_authority_review_digest=source_digest,
        attestations=verified_attestations,
        principal_attestation_set_digest=attestation_digest,
        principal_requirements=_requirements_tuple(requirements),
        named_principals=tuple(sorted(requirements)),
        digest="0" * 64,
    )
    return DestinationPolicyPlan(
        schema=plan.schema,
        destination_vault_id=plan.destination_vault_id,
        prospective=plan.prospective,
        document_edits=plan.document_edits,
        document_set_digest=plan.document_set_digest,
        authoring_snapshot_digest=plan.authoring_snapshot_digest,
        source_authority=plan.source_authority,
        source_authority_review_digest=plan.source_authority_review_digest,
        attestations=plan.attestations,
        principal_attestation_set_digest=plan.principal_attestation_set_digest,
        principal_requirements=plan.principal_requirements,
        named_principals=plan.named_principals,
        digest=_plan_digest(plan),
    )


def revalidate_destination_policy(
    vault_root: Path,
    plan: DestinationPolicyPlan,
    *,
    principal_contexts: Sequence[RequestPrincipal],
    destination_vault_id: str,
    expected_nonce: str,
    verified_at: str,
) -> DestinationPolicyPlan:
    """Recheck exact policy and live destination sessions immediately before apply."""

    if (
        not isinstance(plan, DestinationPolicyPlan)
        or plan.schema != DESTINATION_POLICY_PLAN_SCHEMA
        or plan.destination_vault_id != _identifier(destination_vault_id)
        or plan.digest != _plan_digest(plan)
        or plan.source_authority_review_digest
        != source_authority_review_digest(plan.source_authority)
        or plan.principal_attestation_set_digest
        != principal_attestation_set_digest(plan.attestations)
        or plan.authoring_snapshot_digest != _authoring_snapshot_digest(plan.prospective.snapshot)
    ):
        _fail()
    prospective = policy.compile_prospective(Path(vault_root), dict(plan.document_edits))
    if (
        prospective is None
        or prospective.policy.blocked
        or prospective.policy.conflicted
        or prospective != plan.prospective
        or _document_set_digest(prospective.target_documents) != plan.document_set_digest
        or _authoring_snapshot_digest(prospective.snapshot) != plan.authoring_snapshot_digest
    ):
        _fail()
    requirements = {
        principal_id: set(purposes) for principal_id, purposes in plan.principal_requirements
    }
    if _requirements_tuple(requirements) != plan.principal_requirements or any(
        not purposes <= requirements.get(principal_id, set())
        for principal_id, purposes in _requirements(prospective.policy).items()
    ):
        _fail()
    verified = _verify_attestation_set(
        plan.attestations,
        principal_contexts,
        requirements,
        destination_vault_id=plan.destination_vault_id,
        expected_nonce=expected_nonce,
        verified_at=verified_at,
    )
    if tuple(sorted(requirements)) != plan.named_principals or verified != plan.attestations:
        _fail()
    return plan
