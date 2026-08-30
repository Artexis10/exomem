"""Fresh destination authority and prospective policy for consolidation.

Source-side policy is evidence, never authority.  This module accepts only
digest-only source review records, resolves destination principals from the
already-verified request context, and compiles newly authored destination
documents against the exact live authoring snapshot.
"""

from __future__ import annotations

import hashlib
import json
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
_PLAN_DOMAIN = DESTINATION_POLICY_PLAN_SCHEMA.encode("ascii")

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
_MAX_POLICY_BUNDLE_BYTES = 16 * 1024 * 1024

__all__ = [
    "DESTINATION_POLICY_PLAN_SCHEMA",
    "DESTINATION_PRINCIPAL_ATTESTATION_SCHEMA",
    "DestinationPolicyPlan",
    "DestinationPolicyUnavailable",
    "DestinationPrincipalAttestation",
    "SourceAuthorityReviewArtifact",
    "VerifiedDestinationPrincipalAttestation",
    "compile_destination_policy",
    "canonical_destination_policy_bundle",
    "destination_policy_bundle_digest",
    "issue_destination_principal_attestation",
    "parse_destination_policy_bundle",
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
    nonce: str
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


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        _fail()
    return value


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _policy_path(value: object) -> str:
    path = _text(value)
    parsed = PurePosixPath(path)
    if (
        not path
        or len(path.encode("utf-8")) > 4096
        or "\\" in path
        or parsed.is_absolute()
        or parsed.as_posix() != path
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        _fail()
    return path


def _document_rows(documents: Sequence[tuple[str, bytes]]) -> list[dict[str, object]]:
    if len(documents) > _MAX_DOCUMENTS:
        _fail()
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_path, raw in documents:
        path = _policy_path(raw_path)
        if path in seen or not isinstance(raw, bytes) or len(raw) > _MAX_DOCUMENT_BYTES:
            _fail()
        try:
            content = _text(raw.decode("utf-8"))
        except UnicodeDecodeError:
            _fail()
        if content.encode("utf-8") != raw:
            _fail()
        seen.add(path)
        rows.append(
            {
                "path": path,
                "content": content,
                "byte_size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    if tuple(row["path"] for row in rows) != tuple(sorted(seen)):
        _fail()
    return rows


def _parse_document_rows(value: object) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(value, list) or len(value) > _MAX_DOCUMENTS:
        _fail()
    documents: list[tuple[str, bytes]] = []
    for raw in value:
        item = _mapping(
            raw,
            frozenset({"path", "content", "byte_size", "sha256"}),
        )
        path = _policy_path(item["path"])
        content = _text(item["content"])
        encoded = content.encode("utf-8")
        if (
            len(encoded) > _MAX_DOCUMENT_BYTES
            or _integer(item["byte_size"]) != len(encoded)
            or _digest(item["sha256"]) != hashlib.sha256(encoded).hexdigest()
        ):
            _fail()
        documents.append((path, encoded))
    if tuple(path for path, _raw in documents) != tuple(sorted({path for path, _raw in documents})):
        _fail()
    return tuple(documents)


def _stable_identity_from_value(value: object) -> StableIdentity:
    item = _mapping(
        value,
        frozenset({"device", "inode", "kind", "link_count"}),
    )
    return StableIdentity(
        device=_integer(item["device"]),
        inode=_integer(item["inode"]),
        kind=_identifier(item["kind"]),
        link_count=_integer(item["link_count"]),
    )


def _authoring_snapshot_value(snapshot: policy.AuthoringSnapshot) -> dict[str, object]:
    root: dict[str, object]
    if snapshot.governance_root_identity is None:
        root = {"state": "absent"}
    else:
        root = {
            "state": "present",
            "identity": _stable_identity_value(snapshot.governance_root_identity),
        }
    file_paths = tuple(_policy_path(item.path) for item in snapshot.file_identities)
    directory_paths = tuple(_policy_path(path) for path, _identity in snapshot.directory_identities)
    file_identities = [
        {
            "path": path,
            "sha256": _digest(item.sha256),
            "identity": _stable_identity_value(item.identity),
        }
        for path, item in zip(file_paths, snapshot.file_identities, strict=True)
    ]
    directory_identities = [
        {
            "path": checked_path,
            "identity": _stable_identity_value(identity),
        }
        for checked_path, (_path, identity) in zip(
            directory_paths,
            snapshot.directory_identities,
            strict=True,
        )
    ]
    if file_paths != tuple(sorted(set(file_paths))) or directory_paths != tuple(
        sorted(set(directory_paths))
    ):
        _fail()
    return {
        "documents": _document_rows(snapshot.documents),
        "source_fingerprint": _digest(snapshot.source_fingerprint),
        "conflict_set_digest": _digest(snapshot.conflict_set_digest),
        "guard_generation": _text(snapshot.guard_generation),
        "file_identities": file_identities,
        "directory_identities": directory_identities,
        "governance_root": root,
    }


def _parse_authoring_snapshot(value: object) -> policy.AuthoringSnapshot:
    item = _mapping(
        value,
        frozenset(
            {
                "documents",
                "source_fingerprint",
                "conflict_set_digest",
                "guard_generation",
                "file_identities",
                "directory_identities",
                "governance_root",
            }
        ),
    )
    documents = _parse_document_rows(item["documents"])
    raw_files = item["file_identities"]
    raw_directories = item["directory_identities"]
    if not isinstance(raw_files, list) or not isinstance(raw_directories, list):
        _fail()
    files: list[policy.AuthoringFileIdentity] = []
    for raw in raw_files:
        row = _mapping(raw, frozenset({"path", "sha256", "identity"}))
        files.append(
            policy.AuthoringFileIdentity(
                path=_policy_path(row["path"]),
                sha256=_digest(row["sha256"]),
                identity=_stable_identity_from_value(row["identity"]),
            )
        )
    directories: list[tuple[str, StableIdentity]] = []
    for raw in raw_directories:
        row = _mapping(raw, frozenset({"path", "identity"}))
        directories.append(
            (_policy_path(row["path"]), _stable_identity_from_value(row["identity"]))
        )
    if tuple(item.path for item in files) != tuple(sorted({item.path for item in files})) or tuple(
        path for path, _identity in directories
    ) != tuple(sorted({path for path, _identity in directories})):
        _fail()
    raw_root = item["governance_root"]
    if raw_root == {"state": "absent"}:
        root = None
    else:
        root_row = _mapping(raw_root, frozenset({"state", "identity"}))
        if root_row["state"] != "present":
            _fail()
        root = _stable_identity_from_value(root_row["identity"])
    snapshot = policy.AuthoringSnapshot(
        documents=documents,
        source_fingerprint=_digest(item["source_fingerprint"]),
        conflict_set_digest=_digest(item["conflict_set_digest"]),
        guard_generation=_text(item["guard_generation"]),
        file_identities=tuple(files),
        directory_identities=tuple(directories),
        governance_root_identity=root,
    )
    if _authoring_snapshot_value(snapshot) != value:
        _fail()
    return snapshot


def _document_edit_rows(
    edits: Sequence[tuple[str, str | None]],
) -> list[dict[str, object]]:
    checked = _document_edits(dict(edits))
    if tuple(edits) != checked:
        _fail()
    rows: list[dict[str, object]] = []
    for path, content in checked:
        if content is None:
            rows.append({"path": path, "state": "absent"})
        else:
            encoded = content.encode("utf-8")
            rows.append(
                {
                    "path": path,
                    "state": "present",
                    "content": content,
                    "byte_size": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )
    return rows


def _parse_document_edits(value: object) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, list) or len(value) > _MAX_DOCUMENTS:
        _fail()
    edits: dict[str, str | None] = {}
    for raw in value:
        if not isinstance(raw, Mapping) or raw.get("state") not in {"absent", "present"}:
            _fail()
        if raw["state"] == "absent":
            row = _mapping(raw, frozenset({"path", "state"}))
            content = None
        else:
            row = _mapping(
                raw,
                frozenset({"path", "state", "content", "byte_size", "sha256"}),
            )
            content = _text(row["content"])
            encoded = content.encode("utf-8")
            if (
                _integer(row["byte_size"]) != len(encoded)
                or _digest(row["sha256"]) != hashlib.sha256(encoded).hexdigest()
            ):
                _fail()
        path = _policy_path(row["path"])
        if path in edits:
            _fail()
        edits[path] = content
    checked = _document_edits(edits)
    if _document_edit_rows(checked) != value:
        _fail()
    return checked


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


def _policy_bundle_value(plan: DestinationPolicyPlan) -> dict[str, object]:
    if (
        not isinstance(plan, DestinationPolicyPlan)
        or plan.schema != DESTINATION_POLICY_PLAN_SCHEMA
        or not isinstance(plan.prospective, policy.ProspectiveCompile)
        or not isinstance(plan.prospective.snapshot, policy.AuthoringSnapshot)
    ):
        _fail()
    vault_id = _identifier(plan.destination_vault_id)
    nonce = _identifier(plan.nonce)
    snapshot = plan.prospective.snapshot
    target_documents = plan.prospective.target_documents
    compiled = policy.compile_documents(dict(target_documents))
    requirements = {
        _identifier(principal_id): set(_purposes(purposes))
        for principal_id, purposes in plan.principal_requirements
    }
    attestations_by_principal = {
        attestation.principal_id: attestation for attestation in plan.attestations
    }
    if (
        plan.prospective.policy != compiled
        or plan.document_edits != _document_edits(dict(plan.document_edits))
        or plan.document_set_digest != _document_set_digest(target_documents)
        or plan.authoring_snapshot_digest != _authoring_snapshot_digest(snapshot)
        or plan.source_authority
        != tuple(sorted(plan.source_authority, key=lambda item: item.object_ref))
        or plan.source_authority_review_digest
        != source_authority_review_digest(plan.source_authority)
        or plan.attestations != tuple(sorted(plan.attestations, key=lambda item: item.principal_id))
        or plan.principal_attestation_set_digest
        != principal_attestation_set_digest(plan.attestations)
        or len(attestations_by_principal) != len(plan.attestations)
        or set(attestations_by_principal) != set(requirements)
        or any(
            attestation.destination_vault_id != vault_id
            or attestation.nonce != nonce
            or not requirements[principal_id] <= set(attestation.purposes)
            for principal_id, attestation in attestations_by_principal.items()
        )
        or _requirements_tuple(requirements) != plan.principal_requirements
        or any(
            not purposes <= requirements.get(principal_id, set())
            for principal_id, purposes in _requirements(compiled).items()
        )
        or tuple(sorted(requirements)) != plan.named_principals
    ):
        _fail()
    return {
        "schema": plan.schema,
        "destination_vault_id": vault_id,
        "nonce": nonce,
        "prospective": {
            "snapshot": _authoring_snapshot_value(snapshot),
            "target_documents": _document_rows(target_documents),
            "policy_fingerprint": _digest(compiled.fingerprint),
        },
        "document_edits": _document_edit_rows(plan.document_edits),
        "document_set_digest": _digest(plan.document_set_digest),
        "authoring_snapshot_digest": _digest(plan.authoring_snapshot_digest),
        "source_authority": _source_authority_value(plan.source_authority),
        "source_authority_review_digest": _digest(plan.source_authority_review_digest),
        "attestations": _attestation_value(plan.attestations),
        "principal_attestation_set_digest": _digest(plan.principal_attestation_set_digest),
        "principal_requirements": [
            {"principal_id": principal_id, "purposes": list(purposes)}
            for principal_id, purposes in plan.principal_requirements
        ],
        "named_principals": list(plan.named_principals),
    }


def _source_authority_from_value(value: object) -> tuple[SourceAuthorityReviewArtifact, ...]:
    if not isinstance(value, list) or len(value) > 4096:
        _fail()
    artifacts: list[SourceAuthorityReviewArtifact] = []
    for raw in value:
        item = _mapping(
            raw,
            frozenset(
                {
                    "object_ref",
                    "object_kind",
                    "object_sha256",
                    "bundle_sha256",
                    "provenance_ref",
                }
            ),
        )
        artifacts.append(
            SourceAuthorityReviewArtifact(
                object_ref=_reference(item["object_ref"]),
                object_kind=_identifier(item["object_kind"]),
                object_sha256=_digest(item["object_sha256"]),
                bundle_sha256=_digest(item["bundle_sha256"]),
                provenance_ref=_reference(item["provenance_ref"]),
            )
        )
    ordered = tuple(sorted(artifacts, key=lambda item: item.object_ref))
    if _source_authority_value(ordered) != value:
        _fail()
    return ordered


def _attestations_from_value(
    value: object,
) -> tuple[DestinationPrincipalAttestation, ...]:
    if not isinstance(value, list) or len(value) > 1024:
        _fail()
    attestations: list[DestinationPrincipalAttestation] = []
    fields = frozenset(
        {
            "schema",
            "destination_vault_id",
            "issuer_family",
            "surface",
            "principal_id",
            "purposes",
            "issued_at",
            "expires_at",
            "authentication_binding_digest",
            "nonce",
            "fingerprint",
        }
    )
    for raw in value:
        item = _mapping(raw, fields)
        purposes = item["purposes"]
        if not isinstance(purposes, list):
            _fail()
        attestations.append(
            DestinationPrincipalAttestation(
                schema=_text(item["schema"]),
                destination_vault_id=_identifier(item["destination_vault_id"]),
                issuer_family=_identifier(item["issuer_family"]),
                surface=_text(item["surface"]),
                principal_id=_identifier(item["principal_id"]),
                purposes=_purposes(purposes),
                issued_at=_timestamp(item["issued_at"])[0],
                expires_at=_timestamp(item["expires_at"])[0],
                authentication_binding_digest=_digest(item["authentication_binding_digest"]),
                nonce=_identifier(item["nonce"]),
                fingerprint=_digest(item["fingerprint"]),
            )
        )
    ordered = tuple(sorted(attestations, key=lambda item: item.principal_id))
    if _attestation_value(ordered) != value:
        _fail()
    return ordered


def _requirements_from_value(
    value: object,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, list) or len(value) > 1024:
        _fail()
    rows: list[tuple[str, tuple[str, ...]]] = []
    for raw in value:
        item = _mapping(raw, frozenset({"principal_id", "purposes"}))
        purposes = item["purposes"]
        if not isinstance(purposes, list):
            _fail()
        rows.append((_identifier(item["principal_id"]), _purposes(purposes)))
    requirements = tuple(rows)
    if requirements != _requirements_tuple(
        {principal_id: set(purposes) for principal_id, purposes in requirements}
    ):
        _fail()
    return requirements


def _policy_plan_from_value(value: object) -> DestinationPolicyPlan:
    item = _mapping(
        value,
        frozenset(
            {
                "schema",
                "destination_vault_id",
                "nonce",
                "prospective",
                "document_edits",
                "document_set_digest",
                "authoring_snapshot_digest",
                "source_authority",
                "source_authority_review_digest",
                "attestations",
                "principal_attestation_set_digest",
                "principal_requirements",
                "named_principals",
            }
        ),
    )
    if item["schema"] != DESTINATION_POLICY_PLAN_SCHEMA:
        _fail()
    prospective_value = _mapping(
        item["prospective"],
        frozenset({"snapshot", "target_documents", "policy_fingerprint"}),
    )
    snapshot = _parse_authoring_snapshot(prospective_value["snapshot"])
    target_documents = _parse_document_rows(prospective_value["target_documents"])
    compiled = policy.compile_documents(dict(target_documents))
    if compiled.fingerprint != _digest(prospective_value["policy_fingerprint"]):
        _fail()
    prospective = policy.ProspectiveCompile(
        snapshot=snapshot,
        target_documents=target_documents,
        policy=compiled,
    )
    source_authority = _source_authority_from_value(item["source_authority"])
    attestations = _attestations_from_value(item["attestations"])
    requirements = _requirements_from_value(item["principal_requirements"])
    named = item["named_principals"]
    if not isinstance(named, list):
        _fail()
    named_principals = tuple(_identifier(principal_id) for principal_id in named)
    preliminary = DestinationPolicyPlan(
        schema=DESTINATION_POLICY_PLAN_SCHEMA,
        destination_vault_id=_identifier(item["destination_vault_id"]),
        nonce=_identifier(item["nonce"]),
        prospective=prospective,
        document_edits=_parse_document_edits(item["document_edits"]),
        document_set_digest=_digest(item["document_set_digest"]),
        authoring_snapshot_digest=_digest(item["authoring_snapshot_digest"]),
        source_authority=source_authority,
        source_authority_review_digest=_digest(item["source_authority_review_digest"]),
        attestations=attestations,
        principal_attestation_set_digest=_digest(item["principal_attestation_set_digest"]),
        principal_requirements=requirements,
        named_principals=named_principals,
        digest="0" * 64,
    )
    if _policy_bundle_value(preliminary) != value:
        _fail()
    return DestinationPolicyPlan(
        schema=preliminary.schema,
        destination_vault_id=preliminary.destination_vault_id,
        nonce=preliminary.nonce,
        prospective=preliminary.prospective,
        document_edits=preliminary.document_edits,
        document_set_digest=preliminary.document_set_digest,
        authoring_snapshot_digest=preliminary.authoring_snapshot_digest,
        source_authority=preliminary.source_authority,
        source_authority_review_digest=preliminary.source_authority_review_digest,
        attestations=preliminary.attestations,
        principal_attestation_set_digest=preliminary.principal_attestation_set_digest,
        principal_requirements=preliminary.principal_requirements,
        named_principals=preliminary.named_principals,
        digest=_plan_digest(preliminary),
    )


def canonical_destination_policy_bundle(plan: DestinationPolicyPlan) -> bytes:
    """Return the exact canonical bytes whose framed digest authorizes apply."""

    value = _policy_bundle_value(plan)
    if plan.digest != _framed_digest(_PLAN_DOMAIN, value):
        _fail()
    return _canonical(value)


def destination_policy_bundle_digest(plan: DestinationPolicyPlan) -> str:
    """Return the outside digest of one exact canonical policy bundle."""

    canonical_destination_policy_bundle(plan)
    return plan.digest


def parse_destination_policy_bundle(raw: bytes) -> DestinationPolicyPlan:
    """Parse only exact canonical stored destination-policy bundle bytes."""

    if not isinstance(raw, bytes) or not 1 <= len(raw) <= _MAX_POLICY_BUNDLE_BYTES:
        _fail()

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            normalized = _text(key)
            if normalized in result:
                _fail()
            result[normalized] = item
        return result

    def invalid_number(_value: str) -> NoReturn:
        _fail()

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_float=invalid_number,
            parse_constant=invalid_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _fail()
    plan = _policy_plan_from_value(parsed)
    if canonical_destination_policy_bundle(plan) != raw:
        _fail()
    return plan


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
    return _framed_digest(_PLAN_DOMAIN, _policy_bundle_value(plan))


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
        nonce=_identifier(expected_nonce),
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
        nonce=plan.nonce,
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
        or plan.nonce != _identifier(expected_nonce)
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
