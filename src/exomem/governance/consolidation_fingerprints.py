"""Immutable content fingerprints for governed vault consolidation.

The source fingerprint authenticates one quiesced export.  The destination
snapshot independently binds the current active installation and every
canonical byte that a consolidation plan may change.  Mutable run, receipt,
seal, journal, and rebuild state never enters either content preimage.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .. import access, hosted_portability
from ..kbdir import kb_dirname
from . import authorization_custody, schema_v4, store
from .consolidation_attestation import canonical_source_export_claims
from .consolidation_identity import ConsolidationCellIdentity, load_local_identity
from .projections import ProjectionCanonicalizationError, canonical_jcs

SOURCE_FINGERPRINT_SCHEMA = "exomem.consolidation-source-fingerprint/v1"
DESTINATION_SNAPSHOT_SCHEMA = "exomem.consolidation-destination-snapshot/v1"
CANONICAL_CENSUS_SCHEMA = "exomem.consolidation-canonical-census/v1"

_SOURCE_DOMAIN = SOURCE_FINGERPRINT_SCHEMA.encode("ascii")
_DESTINATION_DOMAIN = DESTINATION_SNAPSHOT_SCHEMA.encode("ascii")
_CENSUS_DOMAIN = CANONICAL_CENSUS_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_MAX_SAFE_INTEGER = (1 << 53) - 1

__all__ = [
    "CANONICAL_CENSUS_SCHEMA",
    "DESTINATION_SNAPSHOT_SCHEMA",
    "SOURCE_FINGERPRINT_SCHEMA",
    "CanonicalCensus",
    "CanonicalCensusEntry",
    "ConsolidationFingerprintUnavailable",
    "DestinationSnapshot",
    "SourceFingerprint",
    "canonical_content_census",
    "destination_snapshot",
    "load_local_destination_snapshot",
    "load_local_source_content_census",
    "source_fingerprint",
    "source_content_census_from_manifest",
]


class ConsolidationFingerprintUnavailable(RuntimeError):
    """Stable, content-free refusal to construct an exact snapshot."""

    code = "CONSOLIDATION_FINGERPRINT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation fingerprint is unavailable")


@dataclass(frozen=True, slots=True)
class CanonicalCensusEntry:
    path: str
    entry_type: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalCensus:
    schema: str
    entries: tuple[CanonicalCensusEntry, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    schema: str
    verified_claims: Mapping[str, str | int]
    authentication_proof_digest: str
    digest: str


@dataclass(frozen=True, slots=True)
class DestinationSnapshot:
    schema: str
    vault_id: str
    installation_id: str
    installation_generation: int
    active_fence_digest: str
    identity_root_binding_fingerprint: str
    canonical_census_digest: str
    active_policy_fingerprint: str
    access_state_fingerprint: str
    review_state_fingerprint: str
    digest: str


@dataclass(frozen=True, slots=True)
class _DestinationSample:
    identity: ConsolidationCellIdentity
    entries: tuple[CanonicalCensusEntry, ...]
    active_policy_fingerprint: str
    access_state_fingerprint: str
    review_state_fingerprint: str


def _fail() -> None:
    raise ConsolidationFingerprintUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _state_fingerprint(value: object) -> str:
    if value == access.MISSING_POLICY_FINGERPRINT:
        return access.MISSING_POLICY_FINGERPRINT
    return _digest(value)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _MAX_SAFE_INTEGER
    ):
        _fail()
    return value


def _normalized_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        _fail()
    normalized = unicodedata.normalize("NFC", value)
    candidate = PurePosixPath(normalized)
    if (
        normalized != value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _fail()
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    return candidate.as_posix()


def _access_state_path() -> str:
    return f"{kb_dirname()}/_access.yaml"


def _review_state_path() -> str:
    return f"{kb_dirname()}/.review-state.json"


def _active_policy_prefix() -> str:
    return f"{kb_dirname()}/_Governance"


def _canonical_bytes(value: object) -> bytes:
    try:
        return canonical_jcs(value)
    except ProjectionCanonicalizationError:
        _fail()


def _framed_digest(domain: bytes, value: object) -> str:
    payload = _canonical_bytes(value)
    framed = (
        len(domain).to_bytes(4, "big")
        + domain
        + len(payload).to_bytes(8, "big")
        + payload
    )
    return hashlib.sha256(framed).hexdigest()


def _entry_value(entry: CanonicalCensusEntry) -> dict[str, str | int]:
    return {
        "entry_type": entry.entry_type,
        "path": entry.path,
        "sha256": entry.sha256,
        "size": entry.size,
    }


def canonical_content_census(
    entries: tuple[CanonicalCensusEntry, ...],
) -> CanonicalCensus:
    """Canonicalize a trusted filesystem inventory without consulting live state."""

    if not isinstance(entries, tuple):
        _fail()
    normalized: list[CanonicalCensusEntry] = []
    if len(entries) > 100_000:
        _fail()
    seen: set[str] = set()
    folded: set[str] = set()
    for item in entries:
        if not isinstance(item, CanonicalCensusEntry):
            _fail()
        path = _normalized_path(item.path)
        if item.entry_type != "file":
            _fail()
        size = _integer(item.size)
        digest = _digest(item.sha256)
        collision = path.casefold()
        if path in seen or collision in folded:
            _fail()
        seen.add(path)
        folded.add(collision)
        normalized.append(
            CanonicalCensusEntry(
                path=path,
                entry_type="file",
                size=size,
                sha256=digest,
            )
        )
    ordered = tuple(sorted(normalized, key=lambda item: item.path))
    value = {
        "schema": CANONICAL_CENSUS_SCHEMA,
        "entries": [_entry_value(item) for item in ordered],
    }
    return CanonicalCensus(
        schema=CANONICAL_CENSUS_SCHEMA,
        entries=ordered,
        digest=_framed_digest(_CENSUS_DOMAIN, value),
    )


def source_content_census_from_manifest(
    manifest: Mapping[str, object],
) -> CanonicalCensus:
    """Derive the consolidation content census from an authenticated archive manifest.

    The archive digest still authenticates every admitted portable byte.  This
    separate census deliberately omits derived evidence/receipt state so its
    later append-only churn cannot stale an already-reviewed content plan.
    """

    if not isinstance(manifest, Mapping):
        _fail()
    records = manifest.get("files")
    if not isinstance(records, list):
        _fail()
    entries: list[CanonicalCensusEntry] = []
    for raw in records:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "size",
            "sha256",
            "classification",
        }:
            _fail()
        path = _normalized_path(raw["path"])
        classification = raw["classification"]
        if classification not in {
            hosted_portability.ArtifactClass.CANONICAL.value,
            hosted_portability.ArtifactClass.PORTABLE_DERIVED.value,
        }:
            _fail()
        if (
            classification != hosted_portability.ArtifactClass.CANONICAL.value
            and path != _review_state_path()
        ):
            continue
        entries.append(
            CanonicalCensusEntry(
                path=path,
                entry_type="file",
                size=_integer(raw["size"]),
                sha256=_digest(raw["sha256"]),
            )
        )
    return canonical_content_census(tuple(entries))


def source_fingerprint(
    claims: Mapping[str, object],
    *,
    authentication_proof_digest: str,
) -> SourceFingerprint:
    """Bind exact verified source claims to their detached proof bytes."""

    if not isinstance(claims, Mapping):
        _fail()
    try:
        canonical_claims = canonical_source_export_claims(claims)
        decoded = json.loads(canonical_claims)
    except Exception:  # noqa: BLE001 - one content-free fingerprint refusal
        _fail()
    if not isinstance(decoded, dict):
        _fail()
    proof_digest = _digest(authentication_proof_digest)
    value = {
        "schema": SOURCE_FINGERPRINT_SCHEMA,
        "verified_claims": decoded,
        "authentication_proof_digest": proof_digest,
    }
    return SourceFingerprint(
        schema=SOURCE_FINGERPRINT_SCHEMA,
        verified_claims=MappingProxyType(dict(decoded)),
        authentication_proof_digest=proof_digest,
        digest=_framed_digest(_SOURCE_DOMAIN, value),
    )


def destination_snapshot(
    identity: ConsolidationCellIdentity,
    *,
    census: CanonicalCensus,
    active_policy_fingerprint: str,
    access_state_fingerprint: str,
    review_state_fingerprint: str,
) -> DestinationSnapshot:
    """Construct the immutable destination preimage from already-verified facts."""

    if (
        not isinstance(identity, ConsolidationCellIdentity)
        or not isinstance(census, CanonicalCensus)
        or census.schema != CANONICAL_CENSUS_SCHEMA
    ):
        _fail()
    vault_id = _identifier(identity.vault_id)
    installation_id = _identifier(identity.installation_id)
    generation = _integer(identity.installation_generation, minimum=1)
    active_fence = _digest(identity.active_fence_digest)
    binding = _digest(identity.record_digest)
    census_digest = _digest(census.digest)
    policy_fingerprint = _digest(active_policy_fingerprint)
    access_fingerprint = _state_fingerprint(access_state_fingerprint)
    review_fingerprint = _state_fingerprint(review_state_fingerprint)
    value = {
        "schema": DESTINATION_SNAPSHOT_SCHEMA,
        "vault_id": vault_id,
        "installation_id": installation_id,
        "installation_generation": generation,
        "active_fence_digest": active_fence,
        "identity_root_binding_fingerprint": binding,
        "canonical_census_digest": census_digest,
        "active_policy_fingerprint": policy_fingerprint,
        "access_state_fingerprint": access_fingerprint,
        "review_state_fingerprint": review_fingerprint,
    }
    return DestinationSnapshot(
        schema=DESTINATION_SNAPSHOT_SCHEMA,
        vault_id=vault_id,
        installation_id=installation_id,
        installation_generation=generation,
        active_fence_digest=active_fence,
        identity_root_binding_fingerprint=binding,
        canonical_census_digest=census_digest,
        active_policy_fingerprint=policy_fingerprint,
        access_state_fingerprint=access_fingerprint,
        review_state_fingerprint=review_fingerprint,
        digest=_framed_digest(_DESTINATION_DOMAIN, value),
    )


def _load_active_policy_snapshot(vault_root: Path, *, now: int) -> schema_v4.ActivePolicySnapshot:
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(vault_root, now=now)
        control = custody.control
        if (
            not control.governance_enrolled
            or control.activation_store_id is None
            or control.activation_epoch is None
            or control.activation_state_digest is None
        ):
            _fail()
        connection = store.open_active_governance_read_connection(vault_root)
        connection.execute("BEGIN")
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=control.activation_store_id,
            expected_activation_epoch=control.activation_epoch,
            expected_activation_state_digest=control.activation_state_digest,
        )
        connection.commit()
        return snapshot
    except ConsolidationFingerprintUnavailable:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ):
        if connection is not None and connection.in_transaction:
            connection.rollback()
        _fail()
    finally:
        if connection is not None:
            connection.close()


def _entry_from_snapshot(snapshot: object) -> CanonicalCensusEntry:
    try:
        return CanonicalCensusEntry(
            path=str(snapshot.path),
            entry_type="file",
            size=int(snapshot.size),
            sha256=str(snapshot.sha256),
        )
    except (AttributeError, TypeError, ValueError):
        _fail()


def _content_entries_from_snapshots(
    snapshots: Iterable[object],
) -> tuple[CanonicalCensusEntry, ...]:
    return tuple(
        _entry_from_snapshot(item)
        for item in snapshots
        if item.classification == hosted_portability.ArtifactClass.CANONICAL.value
        or item.path == _review_state_path()
    )


def load_local_source_content_census(
    vault_root: Path,
    *,
    limits: hosted_portability.PortabilityLimits | None = None,
) -> CanonicalCensus:
    """Re-read one live source census using the consolidation exclusions."""

    root = Path(vault_root)
    effective_limits = limits or hosted_portability.PortabilityLimits()
    try:
        hosted_portability._validate_limits(effective_limits)  # noqa: SLF001
        first = canonical_content_census(
            _content_entries_from_snapshots(
                hosted_portability._enumerate_source(root, effective_limits)  # noqa: SLF001
            )
        )
        repeated = canonical_content_census(
            _content_entries_from_snapshots(
                hosted_portability._enumerate_source(root, effective_limits)  # noqa: SLF001
            )
        )
        if repeated != first:
            _fail()
        return first
    except ConsolidationFingerprintUnavailable:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, hosted_portability.PortabilityError):
        _fail()


def _policy_entries(
    documents: tuple[tuple[str, bytes], ...],
) -> tuple[CanonicalCensusEntry, ...]:
    entries: list[CanonicalCensusEntry] = []
    for relative, content in documents:
        if not isinstance(relative, str) or not isinstance(content, bytes):
            _fail()
        path = _normalized_path(f"{_active_policy_prefix()}/{relative}")
        entries.append(
            CanonicalCensusEntry(
                path=path,
                entry_type="file",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(entries)


def _state_digest(
    entries: tuple[CanonicalCensusEntry, ...], path: str
) -> str:
    matches = [entry.sha256 for entry in entries if entry.path == path]
    if not matches:
        return access.MISSING_POLICY_FINGERPRINT
    if len(matches) != 1:
        _fail()
    return matches[0]


def _sample_local_destination(
    vault_root: Path,
    *,
    now: int,
    limits: hosted_portability.PortabilityLimits | None,
) -> _DestinationSample:
    root = Path(vault_root)
    effective_limits = limits or hosted_portability.PortabilityLimits()
    try:
        hosted_portability._validate_limits(effective_limits)  # noqa: SLF001
        identity = load_local_identity(root, now=now)
        active = _load_active_policy_snapshot(root, now=now)
        if active.active.logical_vault_id != identity.vault_id:
            _fail()
        snapshots = hosted_portability._enumerate_source(  # noqa: SLF001
            root, effective_limits
        )
        content_entries = _content_entries_from_snapshots(snapshots)
        entries = content_entries + _policy_entries(active.source_documents)
        canonical = canonical_content_census(entries)
        return _DestinationSample(
            identity=identity,
            entries=canonical.entries,
            active_policy_fingerprint=active.active.policy_fingerprint,
            access_state_fingerprint=_state_digest(
                canonical.entries, _access_state_path()
            ),
            review_state_fingerprint=_state_digest(
                canonical.entries, _review_state_path()
            ),
        )
    except ConsolidationFingerprintUnavailable:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, hosted_portability.PortabilityError):
        _fail()


def load_local_destination_snapshot(
    vault_root: Path,
    *,
    now: int,
    limits: hosted_portability.PortabilityLimits | None = None,
) -> DestinationSnapshot:
    """Re-read every trusted component and return one stable local snapshot."""

    root = Path(vault_root)
    first = _sample_local_destination(root, now=now, limits=limits)
    repeated = _sample_local_destination(root, now=now, limits=limits)
    if repeated != first:
        _fail()
    census = canonical_content_census(first.entries)
    return destination_snapshot(
        first.identity,
        census=census,
        active_policy_fingerprint=first.active_policy_fingerprint,
        access_state_fingerprint=first.access_state_fingerprint,
        review_state_fingerprint=first.review_state_fingerprint,
    )
