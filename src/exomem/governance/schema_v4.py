"""Explicit, offline governance schema-v4 migration primitives.

Ordinary governance-store openers deliberately remain schema-v3 owners.  This
module is the narrow coordinator boundary that can replace v3's caller-chosen
session handles with bearer-free verifier rows and publish the first immutable
policy/projector/catalog tuple in one transaction.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from . import authorization_sessions

if TYPE_CHECKING:
    from .policy import Policy

SCHEMA_USER_VERSION: Final = 4
_MAX_SQLITE_INTEGER: Final = (1 << 63) - 1
_MAX_TEXT_BYTES: Final = 4096
_MAX_BLOB_BYTES: Final = 64 * 1024 * 1024
_HEX_DIGITS: Final = frozenset("0123456789abcdef")
_ACTIVATION_DIGEST_DOMAIN: Final = b"exomem.activation-state.v1\0"


class SchemaV4Error(RuntimeError):
    """The explicit v4 coordinator cannot prove a safe migration or write."""


class ActiveTupleStale(SchemaV4Error):
    """The complete active tuple no longer matches a reviewed predecessor."""


def _text(value: object, name: str, *, maximum: int = _MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaV4Error(f"{name} must be non-empty bounded text")
    if unicodedata.normalize("NFC", value) != value:
        raise SchemaV4Error(f"{name} must be NFC text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SchemaV4Error(f"{name} must be valid Unicode text") from exc
    if len(encoded) > maximum:
        raise SchemaV4Error(f"{name} must be non-empty bounded text")
    return value


def _integer(
    value: object,
    name: str,
    *,
    minimum: int = 1,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_SQLITE_INTEGER
    ):
        raise SchemaV4Error(f"{name} must be a bounded integer")
    return value


def _blob(value: object, name: str, *, allow_empty: bool = False) -> bytes:
    if not isinstance(value, bytes):
        raise SchemaV4Error(f"{name} must be bounded bytes")
    if (not value and not allow_empty) or len(value) > _MAX_BLOB_BYTES:
        raise SchemaV4Error(f"{name} must be bounded bytes")
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise SchemaV4Error(f"{name} must be one lowercase SHA-256 digest")
    return value


def _framed(domain: bytes, *fields: bytes) -> bytes:
    result = bytearray(domain)
    result.append(0)
    for field in fields:
        if len(field) > (1 << 32) - 1:
            raise SchemaV4Error("framed governance value is too large")
        result.extend(len(field).to_bytes(4, "big"))
        result.extend(field)
    return bytes(result)


def _framed_digest(domain: bytes, *fields: bytes) -> str:
    return hashlib.sha256(_framed(domain, *fields)).hexdigest()


def _ascii_integer(value: int) -> bytes:
    return str(value).encode("ascii")


def _optional_text(value: str | None, name: str) -> bytes:
    if value is None:
        return b"\x00"
    return b"\x01" + _text(value, name).encode("utf-8")


def _closed_jcs(value: dict[str, str | int]) -> bytes:
    for key, item in value.items():
        _text(key, "JCS key")
        if isinstance(item, str):
            _text(item, key)
        elif isinstance(item, int) and not isinstance(item, bool):
            _integer(item, key)
        else:  # pragma: no cover - closed callers make this defensive only
            raise SchemaV4Error("activation state contains an unsupported JCS value")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PolicyGenerationSeed:
    generation_id: str
    source_documents: tuple[tuple[str, bytes], ...]
    source_fingerprint: str
    conflict_digest: str
    compiled_policy: bytes
    policy_fingerprint: str
    compiler_schema_version: int
    projector_schema_version: int
    predecessor_generation_id: str | None
    authoring_event_id: str
    receipt_event_id: str
    created_at: int


@dataclass(frozen=True, slots=True)
class CatalogGenerationSeed:
    catalog_generation: int
    descriptor: bytes
    artifact_count: int
    created_at: int


@dataclass(frozen=True, slots=True)
class ProjectionNamespaceSeed:
    namespace_id: str
    evidence: bytes
    ready_at: int


@dataclass(frozen=True, slots=True)
class MigrationSeed:
    activation_store_id: str
    logical_vault_id: str
    activation_epoch: int
    policy: PolicyGenerationSeed
    catalog: CatalogGenerationSeed
    namespace: ProjectionNamespaceSeed
    migrated_at: int


@dataclass(frozen=True, slots=True)
class MigrationResult:
    schema_version: int
    activation_store_id: str
    activation_state_digest: str


@dataclass(frozen=True, slots=True)
class DownmigrationResult:
    schema_version: int
    activation_store_id: str
    activation_epoch: int
    activation_state_digest: str
    policy_generation_id: str
    policy_fingerprint: str
    source_documents: tuple[tuple[str, bytes], ...]
    closed_sessions: int
    expired_grants: int
    expired_purposes: int
    expired_tokens: int
    expired_proposals: int
    recovery_event_id: str
    recovery_terminal_digest: str


def _complete_schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_autoindex_%' "
            "ORDER BY type, name"
        )
    )


@functools.cache
def _expected_v3_schema_signature() -> tuple[tuple[object, ...], ...]:
    from .. import sidecar_store
    from . import store

    reference = sqlite3.connect(":memory:")
    try:
        store._migrate(reference)
        sidecar_store.ensure_meta_table(
            reference,
            store.DATA_TABLE,
            "governance-v3-reference",
        )
        return _complete_schema_signature(reference)
    finally:
        reference.close()


def require_exact_v3_connection(connection: sqlite3.Connection) -> None:
    """Refuse any database that is not the frozen current schema-v3 authority."""

    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_check = tuple(connection.execute("PRAGMA quick_check(1)"))
        signature = _complete_schema_signature(connection)
    except (AttributeError, TypeError, ValueError, sqlite3.Error) as exc:
        raise SchemaV4Error("schema v3 migration source is unavailable") from exc
    if version != 3:
        raise SchemaV4Error(
            f"schema v3 migration source requires exact schema v3, found v{version}"
        )
    if quick_check != (("ok",),) or signature != _expected_v3_schema_signature():
        raise SchemaV4Error("schema v3 migration source is not exact")


@dataclass(frozen=True, slots=True)
class VerifiedActiveGovernanceState:
    logical_vault_id: str
    activation_store_id: str
    activation_epoch: int
    activation_state_digest: str
    policy_generation_id: str
    policy_fingerprint: str
    projector_schema_version: int
    catalog_generation: int
    projection_namespace_id: str


@dataclass(frozen=True, slots=True)
class ActivePolicySnapshot:
    active: VerifiedActiveGovernanceState
    policy: Policy
    source_documents: tuple[tuple[str, bytes], ...]
    catalog_descriptor: bytes
    projection_namespace_evidence: bytes


@dataclass(frozen=True, slots=True)
class TuplePublicationResult:
    active: VerifiedActiveGovernanceState
    policy_row_digest: str
    catalog_descriptor_digest: str
    projection_namespace_digest: str


@dataclass(frozen=True, slots=True)
class ActivationRegistryAcknowledgement:
    activation_store_id: str
    activation_epoch: int
    activation_state_digest: str


RegistryAcknowledger = Callable[
    [VerifiedActiveGovernanceState], ActivationRegistryAcknowledgement
]


@dataclass(frozen=True, slots=True)
class _SeedMaterial:
    source_documents: bytes
    policy_row_digest: str
    catalog_descriptor_digest: str
    namespace_digest: str
    activation_digest: str
    migration_seed_digest: str


def _source_document_bytes(documents: tuple[tuple[str, bytes], ...]) -> bytes:
    if not isinstance(documents, tuple):
        raise SchemaV4Error("source_documents must be an ordered tuple")
    if len(documents) > 100_000:
        raise SchemaV4Error("source_documents exceeds the bounded catalog")
    encoded = bytearray(b"exomem.policy-source-map.v1\0")
    encoded.extend(len(documents).to_bytes(4, "big"))
    seen: set[str] = set()
    for ordinal, entry in enumerate(documents):
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise SchemaV4Error("source_documents contains an invalid entry")
        path = _text(entry[0], f"source_documents[{ordinal}].path")
        content = _blob(
            entry[1],
            f"source_documents[{ordinal}].content",
            allow_empty=True,
        )
        if path in seen:
            raise SchemaV4Error("source_documents contains a duplicate path")
        seen.add(path)
        encoded.extend(_framed(b"exomem.policy-source-document.v1", path.encode(), content))
    if len(encoded) > _MAX_BLOB_BYTES:
        raise SchemaV4Error("source_documents exceeds the bounded byte map")
    return bytes(encoded)


def source_documents_digest(documents: tuple[tuple[str, bytes], ...]) -> str:
    """Bind one exact ordered policy-source byte map for an offline mirror proof."""

    return hashlib.sha256(
        _framed(
            b"exomem.governance-v3-workspace-mirror.v1",
            _source_document_bytes(documents),
        )
    ).hexdigest()


def catalog_rebuild_digest(descriptor: bytes) -> str:
    """Bind the exact active catalog descriptor rebuilt by an offline rollback."""

    return hashlib.sha256(
        _framed(
            b"exomem.governance-v3-catalog-rebuild.v1",
            _blob(descriptor, "catalog descriptor", allow_empty=True),
        )
    ).hexdigest()


def _decode_source_document_bytes(value: object) -> tuple[tuple[str, bytes], ...]:
    raw = _blob(value, "policy.source_documents")
    prefix = b"exomem.policy-source-map.v1\0"
    frame_prefix = b"exomem.policy-source-document.v1\0"
    if not raw.startswith(prefix) or len(raw) < len(prefix) + 4:
        raise SchemaV4Error("policy source document map is malformed")
    offset = len(prefix)
    count = int.from_bytes(raw[offset : offset + 4], "big")
    offset += 4
    if count > 100_000:
        raise SchemaV4Error("policy source document map is malformed")
    result: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for ordinal in range(count):
        if raw[offset : offset + len(frame_prefix)] != frame_prefix:
            raise SchemaV4Error("policy source document map is malformed")
        offset += len(frame_prefix)
        fields: list[bytes] = []
        for _field in range(2):
            if offset + 4 > len(raw):
                raise SchemaV4Error("policy source document map is malformed")
            size = int.from_bytes(raw[offset : offset + 4], "big")
            offset += 4
            if offset + size > len(raw):
                raise SchemaV4Error("policy source document map is malformed")
            fields.append(raw[offset : offset + size])
            offset += size
        try:
            path = fields[0].decode("utf-8")
        except UnicodeDecodeError:
            raise SchemaV4Error("policy source document map is malformed") from None
        path = _text(path, f"source_documents[{ordinal}].path")
        if path in seen:
            raise SchemaV4Error("policy source document map contains a duplicate path")
        seen.add(path)
        result.append((path, fields[1]))
    if offset != len(raw):
        raise SchemaV4Error("policy source document map is malformed")
    documents = tuple(result)
    if _source_document_bytes(documents) != raw:
        raise SchemaV4Error("policy source document map is non-canonical")
    return documents


def _validate_policy(seed: PolicyGenerationSeed) -> tuple[bytes, str]:
    if not isinstance(seed, PolicyGenerationSeed):
        raise SchemaV4Error("policy migration seed is invalid")
    generation_id = _text(seed.generation_id, "policy.generation_id")
    source_documents = _source_document_bytes(seed.source_documents)
    source_fingerprint = _digest(seed.source_fingerprint, "policy.source_fingerprint")
    conflict_digest = _digest(seed.conflict_digest, "policy.conflict_digest")
    compiled_policy = _blob(seed.compiled_policy, "policy.compiled_policy", allow_empty=True)
    policy_fingerprint = _digest(seed.policy_fingerprint, "policy.policy_fingerprint")
    compiler_version = _integer(
        seed.compiler_schema_version,
        "policy.compiler_schema_version",
    )
    projector_version = _integer(
        seed.projector_schema_version,
        "policy.projector_schema_version",
    )
    _optional_text(seed.predecessor_generation_id, "policy.predecessor_generation_id")
    authoring_event_id = _text(seed.authoring_event_id, "policy.authoring_event_id")
    receipt_event_id = _text(seed.receipt_event_id, "policy.receipt_event_id")
    created_at = _integer(seed.created_at, "policy.created_at")
    row_digest = _framed_digest(
        b"exomem.compiled-policy-generation.v1",
        generation_id.encode(),
        source_documents,
        source_fingerprint.encode("ascii"),
        conflict_digest.encode("ascii"),
        compiled_policy,
        policy_fingerprint.encode("ascii"),
        _ascii_integer(compiler_version),
        _ascii_integer(projector_version),
        _optional_text(seed.predecessor_generation_id, "policy.predecessor_generation_id"),
        authoring_event_id.encode(),
        receipt_event_id.encode(),
        _ascii_integer(created_at),
    )
    return source_documents, row_digest


def _verify_policy_source_parity(
    seed: PolicyGenerationSeed,
    source_documents: bytes,
) -> None:
    """Require stored source bytes to reproduce the claimed compiled authority."""

    from . import policy as policy_module

    documents = _decode_source_document_bytes(source_documents)
    compiled = policy_module.compile_documents(dict(documents))
    if (
        compiled.empty
        or compiled.blocked
        or compiled.fingerprint != seed.source_fingerprint
        or compiled.fingerprint != seed.policy_fingerprint
        or not hmac.compare_digest(
            policy_module.canonical_compiled_bytes(compiled),
            seed.compiled_policy,
        )
    ):
        raise SchemaV4Error("policy source parity does not verify")


def _stored_policy_row_digest(row: tuple[object, ...]) -> str:
    (
        generation_id,
        source_documents,
        source_fingerprint,
        conflict_digest,
        compiled_policy,
        policy_fingerprint,
        compiler_schema_version,
        projector_schema_version,
        predecessor_generation_id,
        authoring_event_id,
        receipt_event_id,
        _stored_digest,
        created_at,
    ) = row
    return _framed_digest(
        b"exomem.compiled-policy-generation.v1",
        _text(generation_id, "policy.generation_id").encode(),
        _blob(source_documents, "policy.source_documents"),
        _digest(source_fingerprint, "policy.source_fingerprint").encode("ascii"),
        _digest(conflict_digest, "policy.conflict_digest").encode("ascii"),
        _blob(compiled_policy, "policy.compiled_policy", allow_empty=True),
        _digest(policy_fingerprint, "policy.policy_fingerprint").encode("ascii"),
        _ascii_integer(_integer(compiler_schema_version, "policy.compiler_schema_version")),
        _ascii_integer(_integer(projector_schema_version, "policy.projector_schema_version")),
        _optional_text(
            predecessor_generation_id,
            "policy.predecessor_generation_id",
        ),
        _text(authoring_event_id, "policy.authoring_event_id").encode(),
        _text(receipt_event_id, "policy.receipt_event_id").encode(),
        _ascii_integer(_integer(created_at, "policy.created_at")),
    )


def activation_state_digest(
    *,
    logical_vault_id: str,
    activation_store_id: str,
    activation_epoch: int,
    policy_generation_id: str,
    policy_fingerprint: str,
    policy_row_digest: str,
    projector_schema_version: int,
    catalog_generation: int,
    catalog_descriptor_digest: str,
    projection_namespace_identity: str,
) -> str:
    """Return the exact closed-JCS activation identity pinned by the registry."""

    value: dict[str, str | int] = {
        "activation_epoch": _integer(activation_epoch, "activation_epoch"),
        "activation_store_id": _text(activation_store_id, "activation_store_id"),
        "catalog_descriptor_digest": _digest(
            catalog_descriptor_digest,
            "catalog_descriptor_digest",
        ),
        "catalog_generation": _integer(catalog_generation, "catalog_generation"),
        "logical_vault_id": _text(logical_vault_id, "logical_vault_id"),
        "policy_fingerprint": _digest(policy_fingerprint, "policy_fingerprint"),
        "policy_generation_id": _text(policy_generation_id, "policy_generation_id"),
        "policy_row_digest": _digest(policy_row_digest, "policy_row_digest"),
        "projection_namespace_identity": _text(
            projection_namespace_identity,
            "projection_namespace_identity",
        ),
        "projector_schema_version": _integer(
            projector_schema_version,
            "projector_schema_version",
        ),
    }
    return hashlib.sha256(_ACTIVATION_DIGEST_DOMAIN + _closed_jcs(value)).hexdigest()


def _seed_material(seed: MigrationSeed) -> _SeedMaterial:
    if not isinstance(seed, MigrationSeed):
        raise SchemaV4Error("migration seed is invalid")
    activation_store_id = _text(seed.activation_store_id, "activation_store_id")
    logical_vault_id = _text(seed.logical_vault_id, "logical_vault_id")
    activation_epoch = _integer(seed.activation_epoch, "activation_epoch")
    _integer(seed.migrated_at, "migrated_at")
    source_documents, policy_row_digest = _validate_policy(seed.policy)
    _verify_policy_source_parity(seed.policy, source_documents)
    if not isinstance(seed.catalog, CatalogGenerationSeed):
        raise SchemaV4Error("catalog migration seed is invalid")
    catalog_generation = _integer(
        seed.catalog.catalog_generation,
        "catalog.catalog_generation",
    )
    descriptor = _blob(seed.catalog.descriptor, "catalog.descriptor", allow_empty=True)
    artifact_count = _integer(
        seed.catalog.artifact_count,
        "catalog.artifact_count",
        minimum=0,
    )
    catalog_created_at = _integer(seed.catalog.created_at, "catalog.created_at")
    catalog_descriptor_digest = _framed_digest(
        b"exomem.catalog-generation-descriptor.v1",
        _ascii_integer(catalog_generation),
        descriptor,
        _ascii_integer(artifact_count),
        _ascii_integer(catalog_created_at),
    )
    if not isinstance(seed.namespace, ProjectionNamespaceSeed):
        raise SchemaV4Error("projection namespace migration seed is invalid")
    namespace_id = _text(seed.namespace.namespace_id, "namespace.namespace_id")
    evidence = _blob(seed.namespace.evidence, "namespace.evidence", allow_empty=True)
    ready_at = _integer(seed.namespace.ready_at, "namespace.ready_at")
    namespace_digest = _framed_digest(
        b"exomem.authorization-projection-namespace.v1",
        seed.policy.policy_fingerprint.encode("ascii"),
        _ascii_integer(seed.policy.projector_schema_version),
        _ascii_integer(catalog_generation),
        namespace_id.encode(),
        evidence,
        _ascii_integer(ready_at),
    )
    activation_digest = activation_state_digest(
        logical_vault_id=logical_vault_id,
        activation_store_id=activation_store_id,
        activation_epoch=activation_epoch,
        policy_generation_id=seed.policy.generation_id,
        policy_fingerprint=seed.policy.policy_fingerprint,
        policy_row_digest=policy_row_digest,
        projector_schema_version=seed.policy.projector_schema_version,
        catalog_generation=catalog_generation,
        catalog_descriptor_digest=catalog_descriptor_digest,
        projection_namespace_identity=namespace_digest,
    )
    migration_seed_digest = _framed_digest(
        b"exomem.governance-schema-migration-seed.v1",
        activation_store_id.encode(),
        logical_vault_id.encode(),
        _ascii_integer(activation_epoch),
        policy_row_digest.encode("ascii"),
        catalog_descriptor_digest.encode("ascii"),
        namespace_digest.encode("ascii"),
        _ascii_integer(seed.migrated_at),
    )
    return _SeedMaterial(
        source_documents=source_documents,
        policy_row_digest=policy_row_digest,
        catalog_descriptor_digest=catalog_descriptor_digest,
        namespace_digest=namespace_digest,
        activation_digest=activation_digest,
        migration_seed_digest=migration_seed_digest,
    )


def migration_target(seed: MigrationSeed) -> VerifiedActiveGovernanceState:
    """Derive the exact initial active tuple before irreversible enrollment."""

    material = _seed_material(seed)
    return VerifiedActiveGovernanceState(
        logical_vault_id=seed.logical_vault_id,
        activation_store_id=seed.activation_store_id,
        activation_epoch=seed.activation_epoch,
        activation_state_digest=material.activation_digest,
        policy_generation_id=seed.policy.generation_id,
        policy_fingerprint=seed.policy.policy_fingerprint,
        projector_schema_version=seed.policy.projector_schema_version,
        catalog_generation=seed.catalog.catalog_generation,
        projection_namespace_id=seed.namespace.namespace_id,
    )


def _json_archive_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return {"base64url": base64.urlsafe_b64encode(value).decode("ascii")}
    raise SchemaV4Error("legacy authority contains an unsupported SQLite value")


def _create_legacy_archive(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE governance_legacy_authority ("
        "source_table TEXT NOT NULL, source_key TEXT NOT NULL, row_json BLOB NOT NULL, "
        "row_digest TEXT NOT NULL, reason TEXT NOT NULL, expired_at INTEGER NOT NULL, "
        "PRIMARY KEY(source_table, source_key))"
    )
    for suffix, operation in (("update", "UPDATE"), ("delete", "DELETE")):
        connection.execute(
            f"CREATE TRIGGER governance_legacy_authority_no_{suffix} "
            f"BEFORE {operation} ON governance_legacy_authority BEGIN "
            "SELECT RAISE(ABORT, 'governance legacy authority is immutable'); END"
        )


def _archive_legacy_table(
    connection: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    migrated_at: int,
) -> None:
    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY {key_column}")
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    for row in cursor.fetchall():
        values = {
            column: _json_archive_value(value) for column, value in zip(columns, row, strict=True)
        }
        encoded = json.dumps(
            values,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        key = _text(values[key_column], f"{table}.{key_column}")
        connection.execute(
            "INSERT INTO governance_legacy_authority "
            "(source_table, source_key, row_json, row_digest, reason, expired_at) "
            "VALUES (?, ?, ?, ?, 'v3-unbound-authorization', ?)",
            (table, key, encoded, hashlib.sha256(encoded).hexdigest(), migrated_at),
        )


def _create_v4_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE governance_authorization_sessions ("
        "session_id TEXT PRIMARY KEY, locator_digest BLOB NOT NULL UNIQUE "
        "CHECK(length(locator_digest)=32), verifier BLOB NOT NULL "
        "CHECK(length(verifier)=32), verifier_key_id TEXT NOT NULL, "
        "credential_generation INTEGER NOT NULL CHECK(credential_generation>0), "
        "principal_id TEXT NOT NULL, issuer_family TEXT NOT NULL, cell_id TEXT NOT NULL, "
        "logical_vault_id TEXT NOT NULL, keyring_id TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK(status IN ('active','closed')), "
        "created_at INTEGER NOT NULL, rotated_at INTEGER, expires_at INTEGER NOT NULL, "
        "closed_at INTEGER)"
    )
    connection.execute(
        "CREATE TABLE compiled_policy_generations ("
        "generation_id TEXT PRIMARY KEY, source_documents BLOB NOT NULL, "
        "source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint)=64), "
        "conflict_digest TEXT NOT NULL CHECK(length(conflict_digest)=64), "
        "compiled_policy BLOB NOT NULL, policy_fingerprint TEXT NOT NULL "
        "CHECK(length(policy_fingerprint)=64), compiler_schema_version INTEGER NOT NULL, "
        "projector_schema_version INTEGER NOT NULL, predecessor_generation_id TEXT, "
        "authoring_event_id TEXT NOT NULL, receipt_event_id TEXT NOT NULL, "
        "immutable_row_digest TEXT NOT NULL CHECK(length(immutable_row_digest)=64), "
        "created_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE catalog_generation_descriptors ("
        "catalog_generation INTEGER PRIMARY KEY, descriptor BLOB NOT NULL, "
        "descriptor_digest TEXT NOT NULL CHECK(length(descriptor_digest)=64), "
        "artifact_count INTEGER NOT NULL CHECK(artifact_count>=0), created_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE governance_projection_namespaces ("
        "policy_fingerprint TEXT NOT NULL, projector_schema_version INTEGER NOT NULL, "
        "catalog_generation INTEGER NOT NULL, namespace_id TEXT NOT NULL UNIQUE, "
        "namespace_digest TEXT NOT NULL CHECK(length(namespace_digest)=64), "
        "evidence BLOB NOT NULL, ready_at INTEGER NOT NULL, "
        "PRIMARY KEY(policy_fingerprint, projector_schema_version, catalog_generation))"
    )
    connection.execute(
        "CREATE TABLE active_governance_tuple ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), policy_generation_id TEXT NOT NULL, "
        "policy_fingerprint TEXT NOT NULL, projector_schema_version INTEGER NOT NULL, "
        "catalog_generation INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE governance_activation_store ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), activation_store_id TEXT NOT NULL, "
        "logical_vault_id TEXT NOT NULL, activation_epoch INTEGER NOT NULL "
        "CHECK(activation_epoch>0), activation_state_digest TEXT NOT NULL "
        "CHECK(length(activation_state_digest)=64))"
    )
    connection.execute(
        "CREATE TABLE governance_tuple_publications ("
        "event_id TEXT PRIMARY KEY, publication_kind TEXT NOT NULL "
        "CHECK(publication_kind IN ('migration','policy','catalog')), "
        "predecessor_activation_state_digest TEXT, "
        "target_activation_state_digest TEXT NOT NULL UNIQUE "
        "CHECK(length(target_activation_state_digest)=64), "
        "policy_generation_id TEXT NOT NULL, policy_fingerprint TEXT NOT NULL "
        "CHECK(length(policy_fingerprint)=64), projector_schema_version INTEGER NOT NULL, "
        "catalog_generation INTEGER NOT NULL, activation_epoch INTEGER NOT NULL UNIQUE "
        "CHECK(activation_epoch>0), status TEXT NOT NULL CHECK(status='committed'), "
        "activated_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE governance_schema_migrations ("
        "migration_id TEXT PRIMARY KEY, source_schema_version INTEGER NOT NULL, "
        "target_schema_version INTEGER NOT NULL, seed_digest TEXT NOT NULL "
        "CHECK(length(seed_digest)=64), migrated_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE governance_session_grants ("
        "grant_id TEXT PRIMARY KEY, authorization_session_id TEXT NOT NULL, "
        "principal_id TEXT NOT NULL, issuer_family TEXT NOT NULL, audience TEXT NOT NULL, "
        "purpose TEXT, ceiling INTEGER NOT NULL, paths TEXT NOT NULL, "
        "fingerprints TEXT NOT NULL, scope_ids TEXT NOT NULL, "
        "membership_manifest TEXT NOT NULL, policy_fingerprint TEXT NOT NULL, "
        "token_jti TEXT NOT NULL, status TEXT NOT NULL, prepared_event_id TEXT, "
        "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER)"
    )
    connection.execute(
        "CREATE TABLE governance_session_purpose ("
        "authorization_session_id TEXT NOT NULL, principal_id TEXT NOT NULL, "
        "issuer_family TEXT NOT NULL, audience TEXT NOT NULL, purpose TEXT NOT NULL, "
        "status TEXT NOT NULL, prepared_event_id TEXT, created_at INTEGER NOT NULL, "
        "expires_at INTEGER NOT NULL, PRIMARY KEY(authorization_session_id, audience))"
    )
    connection.execute(
        "CREATE TABLE governance_session_purpose_staging ("
        "event_id TEXT PRIMARY KEY, authorization_session_id TEXT NOT NULL, "
        "principal_id TEXT NOT NULL, issuer_family TEXT NOT NULL, audience TEXT NOT NULL, "
        "purpose TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE withhold_tokens ("
        "jti TEXT PRIMARY KEY, authorization_session_id TEXT NOT NULL, "
        "principal_id TEXT NOT NULL, issuer_family TEXT NOT NULL, audience TEXT NOT NULL, "
        "max_level INTEGER NOT NULL, fingerprints TEXT NOT NULL, paths TEXT NOT NULL, "
        "scope_ids TEXT NOT NULL, purpose TEXT, org_ceiling INTEGER NOT NULL, "
        "status TEXT NOT NULL, prepared_event_id TEXT, expires_at INTEGER NOT NULL, "
        "minted_at INTEGER NOT NULL, consumed_at INTEGER)"
    )
    connection.execute(
        "CREATE INDEX governance_authorization_sessions_lookup "
        "ON governance_authorization_sessions(locator_digest, status, expires_at)"
    )
    connection.execute(
        "CREATE INDEX governance_grants_session "
        "ON governance_session_grants(authorization_session_id, audience, status)"
    )
    for table in (
        "compiled_policy_generations",
        "catalog_generation_descriptors",
        "governance_projection_namespaces",
        "governance_tuple_publications",
        "governance_schema_migrations",
    ):
        for suffix, operation in (("update", "UPDATE"), ("delete", "DELETE")):
            connection.execute(
                f"CREATE TRIGGER {table}_no_{suffix} BEFORE {operation} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} rows are immutable'); END"
            )


def _insert_seed(
    connection: sqlite3.Connection,
    seed: MigrationSeed,
    material: _SeedMaterial,
) -> None:
    policy = seed.policy
    catalog = seed.catalog
    namespace = seed.namespace
    connection.execute(
        "INSERT INTO compiled_policy_generations "
        "(generation_id, source_documents, source_fingerprint, conflict_digest, "
        "compiled_policy, policy_fingerprint, compiler_schema_version, "
        "projector_schema_version, predecessor_generation_id, authoring_event_id, "
        "receipt_event_id, immutable_row_digest, created_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            policy.generation_id,
            material.source_documents,
            policy.source_fingerprint,
            policy.conflict_digest,
            policy.compiled_policy,
            policy.policy_fingerprint,
            policy.compiler_schema_version,
            policy.projector_schema_version,
            policy.predecessor_generation_id,
            policy.authoring_event_id,
            policy.receipt_event_id,
            material.policy_row_digest,
            policy.created_at,
        ),
    )
    connection.execute(
        "INSERT INTO catalog_generation_descriptors "
        "(catalog_generation, descriptor, descriptor_digest, artifact_count, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            catalog.catalog_generation,
            catalog.descriptor,
            material.catalog_descriptor_digest,
            catalog.artifact_count,
            catalog.created_at,
        ),
    )
    connection.execute(
        "INSERT INTO governance_projection_namespaces "
        "(policy_fingerprint, projector_schema_version, catalog_generation, namespace_id, "
        "namespace_digest, evidence, ready_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            policy.policy_fingerprint,
            policy.projector_schema_version,
            catalog.catalog_generation,
            namespace.namespace_id,
            material.namespace_digest,
            namespace.evidence,
            namespace.ready_at,
        ),
    )
    connection.execute(
        "INSERT INTO active_governance_tuple "
        "(singleton, policy_generation_id, policy_fingerprint, "
        "projector_schema_version, catalog_generation) VALUES (1, ?, ?, ?, ?)",
        (
            policy.generation_id,
            policy.policy_fingerprint,
            policy.projector_schema_version,
            catalog.catalog_generation,
        ),
    )
    connection.execute(
        "INSERT INTO governance_activation_store "
        "(singleton, activation_store_id, logical_vault_id, activation_epoch, "
        "activation_state_digest) VALUES (1, ?, ?, ?, ?)",
        (
            seed.activation_store_id,
            seed.logical_vault_id,
            seed.activation_epoch,
            material.activation_digest,
        ),
    )
    connection.execute(
        "INSERT INTO governance_tuple_publications "
        "(event_id, publication_kind, predecessor_activation_state_digest, "
        "target_activation_state_digest, policy_generation_id, policy_fingerprint, "
        "projector_schema_version, catalog_generation, activation_epoch, status, "
        "activated_at) VALUES (?, 'migration', NULL, ?, ?, ?, ?, ?, ?, 'committed', ?)",
        (
            policy.receipt_event_id,
            material.activation_digest,
            policy.generation_id,
            policy.policy_fingerprint,
            policy.projector_schema_version,
            catalog.catalog_generation,
            seed.activation_epoch,
            seed.migrated_at,
        ),
    )
    connection.execute(
        "INSERT INTO governance_schema_migrations "
        "(migration_id, source_schema_version, target_schema_version, seed_digest, "
        "migrated_at) VALUES ('v3-to-v4', 3, 4, ?, ?)",
        (material.migration_seed_digest, seed.migrated_at),
    )


def _crash_point(point: str) -> None:
    """Test seam for coordinator crash-injection barriers."""

    del point


def _replay_matches(
    connection: sqlite3.Connection,
    seed: MigrationSeed,
    material: _SeedMaterial,
) -> bool:
    policy = connection.execute(
        "SELECT generation_id, source_documents, source_fingerprint, conflict_digest, "
        "compiled_policy, policy_fingerprint, compiler_schema_version, "
        "projector_schema_version, predecessor_generation_id, authoring_event_id, "
        "receipt_event_id, immutable_row_digest, created_at "
        "FROM compiled_policy_generations WHERE generation_id=?",
        (seed.policy.generation_id,),
    ).fetchone()
    catalog = connection.execute(
        "SELECT catalog_generation, descriptor, descriptor_digest, artifact_count, created_at "
        "FROM catalog_generation_descriptors WHERE catalog_generation=?",
        (seed.catalog.catalog_generation,),
    ).fetchone()
    namespace = connection.execute(
        "SELECT policy_fingerprint, projector_schema_version, catalog_generation, "
        "namespace_id, namespace_digest, evidence, ready_at "
        "FROM governance_projection_namespaces WHERE policy_fingerprint=? "
        "AND projector_schema_version=? AND catalog_generation=?",
        (
            seed.policy.policy_fingerprint,
            seed.policy.projector_schema_version,
            seed.catalog.catalog_generation,
        ),
    ).fetchone()
    active = connection.execute("SELECT * FROM active_governance_tuple").fetchone()
    activation = connection.execute("SELECT * FROM governance_activation_store").fetchone()
    publication = connection.execute(
        "SELECT event_id, publication_kind, predecessor_activation_state_digest, "
        "target_activation_state_digest, policy_generation_id, policy_fingerprint, "
        "projector_schema_version, catalog_generation, activation_epoch, status, "
        "activated_at FROM governance_tuple_publications WHERE event_id=?",
        (seed.policy.receipt_event_id,),
    ).fetchone()
    migration = connection.execute(
        "SELECT * FROM governance_schema_migrations WHERE migration_id='v3-to-v4'"
    ).fetchone()
    return (
        policy
        == (
            seed.policy.generation_id,
            material.source_documents,
            seed.policy.source_fingerprint,
            seed.policy.conflict_digest,
            seed.policy.compiled_policy,
            seed.policy.policy_fingerprint,
            seed.policy.compiler_schema_version,
            seed.policy.projector_schema_version,
            seed.policy.predecessor_generation_id,
            seed.policy.authoring_event_id,
            seed.policy.receipt_event_id,
            material.policy_row_digest,
            seed.policy.created_at,
        )
        and catalog
        == (
            seed.catalog.catalog_generation,
            seed.catalog.descriptor,
            material.catalog_descriptor_digest,
            seed.catalog.artifact_count,
            seed.catalog.created_at,
        )
        and namespace
        == (
            seed.policy.policy_fingerprint,
            seed.policy.projector_schema_version,
            seed.catalog.catalog_generation,
            seed.namespace.namespace_id,
            material.namespace_digest,
            seed.namespace.evidence,
            seed.namespace.ready_at,
        )
        and active
        == (
            1,
            seed.policy.generation_id,
            seed.policy.policy_fingerprint,
            seed.policy.projector_schema_version,
            seed.catalog.catalog_generation,
        )
        and activation
        == (
            1,
            seed.activation_store_id,
            seed.logical_vault_id,
            seed.activation_epoch,
            material.activation_digest,
        )
        and publication
        == (
            seed.policy.receipt_event_id,
            "migration",
            None,
            material.activation_digest,
            seed.policy.generation_id,
            seed.policy.policy_fingerprint,
            seed.policy.projector_schema_version,
            seed.catalog.catalog_generation,
            seed.activation_epoch,
            "committed",
            seed.migrated_at,
        )
        and migration
        == (
            "v3-to-v4",
            3,
            4,
            material.migration_seed_digest,
            seed.migrated_at,
        )
    )


def migrate_v3_connection(
    connection: sqlite3.Connection,
    seed: MigrationSeed,
) -> MigrationResult:
    """Atomically replace one exact quiesced v3 authority with exact schema v4."""

    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {3, SCHEMA_USER_VERSION}:
        raise SchemaV4Error(
            f"explicit governance migration requires exact schema v3, found v{version}"
        )
    if version == 3 and connection.in_transaction:
        raise SchemaV4Error("schema v3 migration requires a clean transaction boundary")

    # Validate the content-bearing seed only after the source database has
    # proven it is an admissible migration/replay target.  Unsupported schema
    # versions must refuse without parsing attacker-controlled seed material,
    # and the clean-transaction precondition must remain effect-free.
    material = _seed_material(seed)
    if version == SCHEMA_USER_VERSION:
        try:
            matches = _replay_matches(connection, seed, material)
        except sqlite3.Error as exc:
            raise SchemaV4Error("schema v4 migration seed cannot be verified") from exc
        if not matches:
            raise SchemaV4Error("schema v4 migration seed does not match active state")
        return MigrationResult(4, seed.activation_store_id, material.activation_digest)

    connection.execute("BEGIN IMMEDIATE")
    try:
        _create_legacy_archive(connection)
        for table, key_column in (
            ("governance_session_grants", "grant_id"),
            ("governance_session_purpose", "authorization_session"),
            ("governance_session_purpose_staging", "event_id"),
            ("withhold_tokens", "jti"),
        ):
            _archive_legacy_table(
                connection,
                table=table,
                key_column=key_column,
                migrated_at=seed.migrated_at,
            )
        connection.execute(
            "UPDATE governance_operation_journals "
            "SET blocked_reason='v3-unbound-authorization' "
            "WHERE authorization_session IS NOT NULL AND authorization_session<>'' "
            "AND phase IN ('allocating','pending') AND blocked_reason IS NULL"
        )
        _crash_point("after-legacy-archive")
        connection.execute("DROP TABLE governance_session_grants")
        connection.execute("DROP TABLE governance_session_purpose")
        connection.execute("DROP TABLE governance_session_purpose_staging")
        connection.execute("DROP TABLE withhold_tokens")
        _create_v4_schema(connection)
        _crash_point("after-schema")
        _insert_seed(connection, seed, material)
        _crash_point("after-seed")
        connection.execute("PRAGMA user_version=4")
        _crash_point("before-commit")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return MigrationResult(4, seed.activation_store_id, material.activation_digest)


_V4_ONLY_TABLES: Final = (
    "governance_authorization_sessions",
    "compiled_policy_generations",
    "catalog_generation_descriptors",
    "governance_projection_namespaces",
    "active_governance_tuple",
    "governance_activation_store",
    "governance_tuple_publications",
    "governance_schema_migrations",
    "governance_legacy_authority",
)
_VERSIONED_AUTHORITY_TABLES: Final = (
    "governance_session_grants",
    "governance_session_purpose",
    "governance_session_purpose_staging",
    "withhold_tokens",
)
_V3_COMMON_TABLES: Final = (
    "compiled_policy",
    "governance_operation_components",
    "governance_operation_journals",
    "governance_policy_archives",
    "governance_proposals",
    "receipt_instance",
    "receipt_secrets",
    "receipts_head",
)


def _versioned_schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    tables = (*_VERSIONED_AUTHORITY_TABLES, *_V4_ONLY_TABLES)
    placeholders = ",".join("?" for _ in tables)
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            f"WHERE tbl_name IN ({placeholders}) "
            "AND name NOT LIKE 'sqlite_autoindex_%' "
            "ORDER BY type, name",
            tables,
        )
    )


def _expected_versioned_schema_signature() -> tuple[tuple[object, ...], ...]:
    reference = sqlite3.connect(":memory:")
    try:
        _create_legacy_archive(reference)
        _create_v4_schema(reference)
        return _versioned_schema_signature(reference)
    finally:
        reference.close()


def _require_downmigration_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    required = (
        set(_V4_ONLY_TABLES)
        | set(_VERSIONED_AUTHORITY_TABLES)
        | set(_V3_COMMON_TABLES)
    )
    if not required <= tables:
        raise SchemaV4Error("schema v4 downmigration source is incomplete")
    unexpected = tables - required - {"meta"}
    if unexpected:
        raise SchemaV4Error("schema v4 downmigration source has unknown state")
    if _versioned_schema_signature(connection) != _expected_versioned_schema_signature():
        raise SchemaV4Error("schema v4 downmigration source is not exact")


@functools.cache
def _expected_v4_schema_signature() -> tuple[tuple[object, ...], ...]:
    from .. import sidecar_store
    from . import policy as policy_module
    from . import store

    documents = (
        (
            "scopes/reference.yaml",
            b"governance_version: 1\n"
            b"id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
            b"paths:\n"
            b"  - Notes/**\n",
        ),
    )
    compiled = policy_module.compile_documents(dict(documents))
    seed = MigrationSeed(
        activation_store_id="activation-store-reference",
        logical_vault_id="logical-vault-reference",
        activation_epoch=1,
        policy=PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source_documents=documents,
            source_fingerprint=compiled.fingerprint,
            conflict_digest="0" * 64,
            compiled_policy=policy_module.canonical_compiled_bytes(compiled),
            policy_fingerprint=compiled.fingerprint,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-schema-reference",
            receipt_event_id="receipt-schema-reference",
            created_at=1,
        ),
        catalog=CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b"",
            artifact_count=0,
            created_at=1,
        ),
        namespace=ProjectionNamespaceSeed(
            namespace_id="projection-namespace-reference",
            evidence=b"",
            ready_at=1,
        ),
        migrated_at=1,
    )
    reference = sqlite3.connect(":memory:")
    try:
        store._migrate(reference)
        sidecar_store.ensure_meta_table(
            reference,
            store.DATA_TABLE,
            "governance-v4-reference",
        )
        reference.commit()
        migrate_v3_connection(reference, seed)
        return _complete_schema_signature(reference)
    finally:
        reference.close()


def require_exact_v4_connection(connection: sqlite3.Connection) -> None:
    """Refuse any incomplete, corrupt, or extended schema-v4 authority."""

    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_check = tuple(connection.execute("PRAGMA quick_check(1)"))
        signature = _complete_schema_signature(connection)
    except (AttributeError, TypeError, ValueError, sqlite3.Error) as exc:
        raise SchemaV4Error("schema v4 authority is unavailable") from exc
    if version != SCHEMA_USER_VERSION:
        raise SchemaV4Error(
            f"schema v4 authority requires exact schema v4, found v{version}"
        )
    if quick_check != (("ok",),) or signature != _expected_v4_schema_signature():
        raise SchemaV4Error("schema v4 authority is not exact")


def _close_downmigration_authority(
    connection: sqlite3.Connection,
    *,
    downmigrated_at: int,
) -> tuple[int, int, int, int, int]:
    sessions = int(
        connection.execute(
            "UPDATE governance_authorization_sessions "
            "SET status='closed', closed_at=? WHERE status='active'",
            (downmigrated_at,),
        ).rowcount
        or 0
    )
    grants = int(
        connection.execute(
            "UPDATE governance_session_grants "
            "SET status='revoked', revoked_at=? WHERE status<>'revoked'",
            (downmigrated_at,),
        ).rowcount
        or 0
    )
    purposes = int(
        connection.execute(
            "UPDATE governance_session_purpose SET status='revoked' "
            "WHERE status<>'revoked'"
        ).rowcount
        or 0
    )
    purposes += int(
        connection.execute("DELETE FROM governance_session_purpose_staging").rowcount
        or 0
    )
    tokens = int(
        connection.execute(
            "UPDATE withhold_tokens SET status='expired', "
            "consumed_at=COALESCE(consumed_at, ?) WHERE status<>'expired'",
            (downmigrated_at,),
        ).rowcount
        or 0
    )
    proposals = int(
        connection.execute(
            "UPDATE governance_proposals SET status='expired', "
            "spent_at=COALESCE(spent_at, ?) WHERE status='pending'",
            (downmigrated_at,),
        ).rowcount
        or 0
    )
    return sessions, grants, purposes, tokens, proposals


def _downmigration_terminal(
    *,
    event_id: str,
    plan_digest: str,
    expected: VerifiedActiveGovernanceState,
    workspace_digest: str,
    catalog_digest: str,
    downmigrated_at: int,
    closed: tuple[int, int, int, int, int],
) -> tuple[str, str]:
    value: dict[str, str | int] = {
        "schema": "exomem.governance-downmigration-terminal/v1",
        "recovery_event_id": event_id,
        "recovery_plan_digest": plan_digest,
        "logical_vault_id": expected.logical_vault_id,
        "activation_store_id": expected.activation_store_id,
        "activation_epoch": expected.activation_epoch,
        "activation_state_digest": expected.activation_state_digest,
        "policy_generation_id": expected.policy_generation_id,
        "policy_fingerprint": expected.policy_fingerprint,
        "projector_schema_version": expected.projector_schema_version,
        "catalog_generation": expected.catalog_generation,
        "projection_namespace_id": expected.projection_namespace_id,
        "workspace_digest": workspace_digest,
        "catalog_digest": catalog_digest,
        "downmigrated_at": downmigrated_at,
        "closed_sessions": closed[0],
        "expired_grants": closed[1],
        "expired_purposes": closed[2],
        "expired_tokens": closed[3],
        "expired_proposals": closed[4],
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = _framed_digest(
        b"exomem.governance-downmigration-terminal.v1",
        encoded,
    )
    return encoded.decode("utf-8"), digest


def downmigrate_v4_connection(
    connection: sqlite3.Connection,
    *,
    expected: VerifiedActiveGovernanceState,
    expected_source_documents: tuple[tuple[str, bytes], ...],
    expected_catalog_descriptor: bytes,
    verified_workspace_digest: str,
    verified_catalog_digest: str,
    recovery_event_id: str,
    recovery_plan_digest: str,
    downmigrated_at: int,
) -> DownmigrationResult:
    """Atomically return a fully verified, quiesced v4 store to exact schema v3.

    This is the database half of the offline rollback coordinator.  Its caller
    must already hold the whole-tree/schema/replica fence and must supply the
    digests produced after mirroring the pointed source bytes and rebuilding
    the pointed catalog.  Ordinary openers never call this function.
    """

    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_USER_VERSION:
        raise SchemaV4Error(
            f"explicit governance downmigration requires exact schema v4, found v{version}"
        )
    if connection.in_transaction:
        raise SchemaV4Error("schema v4 downmigration requires a clean transaction boundary")
    if not isinstance(expected, VerifiedActiveGovernanceState):
        raise SchemaV4Error("schema v4 downmigration expected tuple is invalid")
    source_digest = source_documents_digest(expected_source_documents)
    descriptor = _blob(
        expected_catalog_descriptor,
        "expected_catalog_descriptor",
        allow_empty=True,
    )
    workspace_digest = _digest(
        verified_workspace_digest,
        "verified_workspace_digest",
    )
    rebuild_digest = _digest(
        verified_catalog_digest,
        "verified_catalog_digest",
    )
    event_id = _digest(recovery_event_id, "recovery_event_id")
    plan_digest = _digest(recovery_plan_digest, "recovery_plan_digest")
    completed_at = _integer(downmigrated_at, "downmigrated_at")
    if not hmac.compare_digest(source_digest, workspace_digest):
        raise SchemaV4Error("downmigration workspace parity does not verify")
    if not hmac.compare_digest(catalog_rebuild_digest(descriptor), rebuild_digest):
        raise SchemaV4Error("downmigration catalog parity does not verify")

    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_downmigration_schema(connection)
        pending = connection.execute(
            "SELECT event_id FROM governance_operation_journals "
            "WHERE phase IN ('allocating','pending') ORDER BY event_id LIMIT 1"
        ).fetchone()
        if pending is not None:
            raise SchemaV4Error("downmigration cannot discard an open recovery journal")
        if connection.execute(
            "SELECT 1 FROM governance_operation_journals WHERE event_id=?",
            (event_id,),
        ).fetchone() is not None:
            raise SchemaV4Error("downmigration recovery event already exists")
        snapshot = load_active_policy(
            connection,
            expected_logical_vault_id=expected.logical_vault_id,
            expected_activation_store_id=expected.activation_store_id,
            expected_activation_epoch=expected.activation_epoch,
            expected_activation_state_digest=expected.activation_state_digest,
        )
        if snapshot.active != expected:
            raise SchemaV4Error("downmigration active tuple changed")
        if snapshot.source_documents != expected_source_documents:
            raise SchemaV4Error("downmigration workspace parity does not verify")
        if not hmac.compare_digest(snapshot.catalog_descriptor, descriptor):
            raise SchemaV4Error("downmigration catalog parity does not verify")
        closed = _close_downmigration_authority(
            connection,
            downmigrated_at=completed_at,
        )
        terminal_json, terminal_digest = _downmigration_terminal(
            event_id=event_id,
            plan_digest=plan_digest,
            expected=expected,
            workspace_digest=workspace_digest,
            catalog_digest=rebuild_digest,
            downmigrated_at=completed_at,
            closed=closed,
        )
        affected_ids = json.dumps(
            sorted((expected.activation_store_id, expected.logical_vault_id)),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO governance_operation_journals "
            "(event_id, operation, causation_id, authorization_session, principal_id, "
            "phase, direction, prior_digest, prepared_digest, final_digest, affected_ids, "
            "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
            "marker_required, created_at, updated_at, blocked_reason) "
            "VALUES (?, 'governance_schema_v4_downmigration', ?, NULL, "
            "'offline-schema-coordinator', 'closed', 'narrowing', ?, ?, ?, ?, '[]', "
            "'[]', NULL, 1, 0, ?, ?, NULL)",
            (
                event_id,
                event_id,
                expected.activation_state_digest,
                plan_digest,
                terminal_digest,
                affected_ids,
                completed_at,
                completed_at,
            ),
        )
        connection.execute(
            "INSERT INTO governance_operation_components "
            "(event_id, phase, ordinal, component_kind, component_key, value_json, "
            "value_hash, status) VALUES (?, 'final', 0, "
            "'schema-downmigration-terminal', ?, ?, ?, 'complete')",
            (event_id, event_id, terminal_json, terminal_digest),
        )
        _crash_point("downmigration-after-authority-close")
        for table in (*_VERSIONED_AUTHORITY_TABLES, *_V4_ONLY_TABLES):
            connection.execute(f"DROP TABLE {table}")
        _crash_point("downmigration-after-v4-drop")
        from . import store as store_module

        store_module._migrate_v3(connection)
        _crash_point("downmigration-after-v3-schema")
        connection.execute("PRAGMA user_version=3")
        _crash_point("downmigration-before-commit")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return DownmigrationResult(
        schema_version=3,
        activation_store_id=expected.activation_store_id,
        activation_epoch=expected.activation_epoch,
        activation_state_digest=expected.activation_state_digest,
        policy_generation_id=expected.policy_generation_id,
        policy_fingerprint=expected.policy_fingerprint,
        source_documents=expected_source_documents,
        closed_sessions=closed[0],
        expired_grants=closed[1],
        expired_purposes=closed[2],
        expired_tokens=closed[3],
        expired_proposals=closed[4],
        recovery_event_id=event_id,
        recovery_terminal_digest=terminal_digest,
    )


def load_active_state(
    connection: sqlite3.Connection,
    *,
    expected_logical_vault_id: str,
    expected_activation_store_id: str,
    expected_activation_epoch: int,
    expected_activation_state_digest: str,
) -> VerifiedActiveGovernanceState:
    """Verify one complete v4 tuple against immutable rows and external custody."""

    try:
        expected_vault = _text(
            expected_logical_vault_id,
            "expected_logical_vault_id",
        )
        expected_store = _text(
            expected_activation_store_id,
            "expected_activation_store_id",
        )
        expected_epoch = _integer(
            expected_activation_epoch,
            "expected_activation_epoch",
        )
        expected_digest = _digest(
            expected_activation_state_digest,
            "expected_activation_state_digest",
        )
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 4:
            raise SchemaV4Error("unsupported activation schema")
        active_rows = connection.execute(
            "SELECT singleton, policy_generation_id, policy_fingerprint, "
            "projector_schema_version, catalog_generation FROM active_governance_tuple"
        ).fetchall()
        activation_rows = connection.execute(
            "SELECT singleton, activation_store_id, logical_vault_id, activation_epoch, "
            "activation_state_digest FROM governance_activation_store"
        ).fetchall()
        if len(active_rows) != 1 or len(activation_rows) != 1:
            raise SchemaV4Error("activation singleton is incomplete")
        active = tuple(active_rows[0])
        activation = tuple(activation_rows[0])
        if active[0] != 1 or activation[0] != 1:
            raise SchemaV4Error("activation singleton is malformed")
        generation_id = _text(active[1], "active.policy_generation_id")
        policy_fingerprint = _digest(
            active[2],
            "active.policy_fingerprint",
        )
        projector_schema_version = _integer(
            active[3],
            "active.projector_schema_version",
        )
        catalog_generation = _integer(
            active[4],
            "active.catalog_generation",
        )
        activation_store_id = _text(activation[1], "activation.activation_store_id")
        logical_vault_id = _text(activation[2], "activation.logical_vault_id")
        activation_epoch = _integer(activation[3], "activation.activation_epoch")
        stored_activation_digest = _digest(
            activation[4],
            "activation.activation_state_digest",
        )

        policy_rows = connection.execute(
            "SELECT generation_id, source_documents, source_fingerprint, conflict_digest, "
            "compiled_policy, policy_fingerprint, compiler_schema_version, "
            "projector_schema_version, predecessor_generation_id, authoring_event_id, "
            "receipt_event_id, immutable_row_digest, created_at "
            "FROM compiled_policy_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchall()
        catalog_rows = connection.execute(
            "SELECT catalog_generation, descriptor, descriptor_digest, artifact_count, "
            "created_at FROM catalog_generation_descriptors WHERE catalog_generation=?",
            (catalog_generation,),
        ).fetchall()
        namespace_rows = connection.execute(
            "SELECT policy_fingerprint, projector_schema_version, catalog_generation, "
            "namespace_id, namespace_digest, evidence, ready_at "
            "FROM governance_projection_namespaces WHERE policy_fingerprint=? "
            "AND projector_schema_version=? AND catalog_generation=?",
            (policy_fingerprint, projector_schema_version, catalog_generation),
        ).fetchall()
        if not (len(policy_rows) == len(catalog_rows) == len(namespace_rows) == 1):
            raise SchemaV4Error("activation tuple row is missing")
        policy = tuple(policy_rows[0])
        catalog = tuple(catalog_rows[0])
        namespace = tuple(namespace_rows[0])

        recomputed_policy_digest = _stored_policy_row_digest(policy)
        stored_policy_digest = _digest(policy[11], "policy.immutable_row_digest")
        if (
            _digest(policy[5], "policy.policy_fingerprint") != policy_fingerprint
            or _integer(policy[7], "policy.projector_schema_version") != projector_schema_version
            or not hmac.compare_digest(
                recomputed_policy_digest,
                stored_policy_digest,
            )
        ):
            raise SchemaV4Error("active policy row does not verify")

        descriptor = _blob(catalog[1], "catalog.descriptor", allow_empty=True)
        descriptor_digest = _digest(catalog[2], "catalog.descriptor_digest")
        recomputed_descriptor_digest = _framed_digest(
            b"exomem.catalog-generation-descriptor.v1",
            _ascii_integer(_integer(catalog[0], "catalog.catalog_generation")),
            descriptor,
            _ascii_integer(_integer(catalog[3], "catalog.artifact_count", minimum=0)),
            _ascii_integer(_integer(catalog[4], "catalog.created_at")),
        )
        if not hmac.compare_digest(
            descriptor_digest,
            recomputed_descriptor_digest,
        ):
            raise SchemaV4Error("active catalog row does not verify")

        namespace_id = _text(namespace[3], "namespace.namespace_id")
        namespace_digest = _digest(namespace[4], "namespace.namespace_digest")
        recomputed_namespace_digest = _framed_digest(
            b"exomem.authorization-projection-namespace.v1",
            _digest(namespace[0], "namespace.policy_fingerprint").encode("ascii"),
            _ascii_integer(_integer(namespace[1], "namespace.projector_schema_version")),
            _ascii_integer(_integer(namespace[2], "namespace.catalog_generation")),
            namespace_id.encode(),
            _blob(namespace[5], "namespace.evidence", allow_empty=True),
            _ascii_integer(_integer(namespace[6], "namespace.ready_at")),
        )
        if not hmac.compare_digest(namespace_digest, recomputed_namespace_digest):
            raise SchemaV4Error("active projection namespace does not verify")

        recomputed_activation_digest = activation_state_digest(
            logical_vault_id=logical_vault_id,
            activation_store_id=activation_store_id,
            activation_epoch=activation_epoch,
            policy_generation_id=generation_id,
            policy_fingerprint=policy_fingerprint,
            policy_row_digest=stored_policy_digest,
            projector_schema_version=projector_schema_version,
            catalog_generation=catalog_generation,
            catalog_descriptor_digest=descriptor_digest,
            projection_namespace_identity=namespace_digest,
        )
        publication_rows = connection.execute(
            "SELECT event_id, publication_kind, predecessor_activation_state_digest, "
            "target_activation_state_digest, policy_generation_id, policy_fingerprint, "
            "projector_schema_version, catalog_generation, activation_epoch, status, "
            "activated_at FROM governance_tuple_publications "
            "WHERE target_activation_state_digest=?",
            (stored_activation_digest,),
        ).fetchall()
        if len(publication_rows) != 1:
            raise SchemaV4Error("active tuple publication evidence is unavailable")
        publication = tuple(publication_rows[0])
        publication_event_id = _text(publication[0], "publication.event_id")
        publication_kind = _text(publication[1], "publication.publication_kind")
        if publication_kind not in {"migration", "policy", "catalog"}:
            raise SchemaV4Error("active tuple publication kind is invalid")
        predecessor_digest = publication[2]
        if publication_kind == "migration":
            if predecessor_digest is not None:
                raise SchemaV4Error("migration publication has a predecessor")
        else:
            predecessor = _digest(
                predecessor_digest,
                "publication.predecessor_activation_state_digest",
            )
            predecessor_rows = connection.execute(
                "SELECT COUNT(*) FROM governance_tuple_publications "
                "WHERE target_activation_state_digest=? AND activation_epoch=?",
                (predecessor, activation_epoch - 1),
            ).fetchone()
            if predecessor_rows != (1,):
                raise SchemaV4Error("active tuple publication predecessor is unavailable")
        if (
            (
                publication_kind in {"migration", "policy"}
                and publication_event_id
                != _text(policy[10], "policy.receipt_event_id")
            )
            or _digest(
                publication[3], "publication.target_activation_state_digest"
            )
            != stored_activation_digest
            or _text(publication[4], "publication.policy_generation_id")
            != generation_id
            or _digest(publication[5], "publication.policy_fingerprint")
            != policy_fingerprint
            or _integer(
                publication[6], "publication.projector_schema_version"
            )
            != projector_schema_version
            or _integer(publication[7], "publication.catalog_generation")
            != catalog_generation
            or _integer(publication[8], "publication.activation_epoch")
            != activation_epoch
            or publication[9] != "committed"
        ):
            raise SchemaV4Error("active tuple publication evidence does not verify")
        _integer(publication[10], "publication.activated_at")
        for actual, expected in (
            (logical_vault_id, expected_vault),
            (activation_store_id, expected_store),
            (str(activation_epoch), str(expected_epoch)),
            (stored_activation_digest, expected_digest),
            (recomputed_activation_digest, expected_digest),
        ):
            if not hmac.compare_digest(actual.encode(), expected.encode()):
                raise SchemaV4Error("external activation tuple does not match")
    except (IndexError, TypeError, ValueError, sqlite3.Error, SchemaV4Error):
        raise SchemaV4Error("governance activation state is unavailable") from None

    return VerifiedActiveGovernanceState(
        logical_vault_id=logical_vault_id,
        activation_store_id=activation_store_id,
        activation_epoch=activation_epoch,
        activation_state_digest=stored_activation_digest,
        policy_generation_id=generation_id,
        policy_fingerprint=policy_fingerprint,
        projector_schema_version=projector_schema_version,
        catalog_generation=catalog_generation,
        projection_namespace_id=namespace_id,
    )


def load_active_tuple_pointer(
    connection: sqlite3.Connection,
) -> VerifiedActiveGovernanceState:
    """Load only the bounded active pointer from one pinned exact-v4 snapshot.

    Startup performs the full immutable-row verification.  This request-time
    probe exists solely to prove that the SQLite pointer has not crossed a CAS
    boundary while the external registry still names its predecessor; it never
    reads source documents, compiled policy bytes, catalog descriptors, or
    projection evidence.
    """

    try:
        rows = connection.execute(
            "SELECT activation.logical_vault_id, activation.activation_store_id, "
            "activation.activation_epoch, activation.activation_state_digest, "
            "active.policy_generation_id, active.policy_fingerprint, "
            "active.projector_schema_version, active.catalog_generation, "
            "namespace.namespace_id "
            "FROM governance_activation_store AS activation "
            "JOIN active_governance_tuple AS active "
            "ON active.singleton=activation.singleton "
            "JOIN governance_projection_namespaces AS namespace "
            "ON namespace.policy_fingerprint=active.policy_fingerprint "
            "AND namespace.projector_schema_version=active.projector_schema_version "
            "AND namespace.catalog_generation=active.catalog_generation "
            "WHERE activation.singleton=1"
        ).fetchall()
        if len(rows) != 1:
            raise SchemaV4Error("active tuple pointer is incomplete")
        row = tuple(rows[0])
        return VerifiedActiveGovernanceState(
            logical_vault_id=_text(row[0], "active.logical_vault_id"),
            activation_store_id=_text(row[1], "active.activation_store_id"),
            activation_epoch=_integer(row[2], "active.activation_epoch"),
            activation_state_digest=_digest(
                row[3], "active.activation_state_digest"
            ),
            policy_generation_id=_text(row[4], "active.policy_generation_id"),
            policy_fingerprint=_digest(row[5], "active.policy_fingerprint"),
            projector_schema_version=_integer(
                row[6], "active.projector_schema_version"
            ),
            catalog_generation=_integer(row[7], "active.catalog_generation"),
            projection_namespace_id=_text(
                row[8], "active.projection_namespace_id"
            ),
        )
    except (IndexError, TypeError, ValueError, sqlite3.Error, SchemaV4Error):
        raise SchemaV4Error("governance active tuple pointer is unavailable") from None


def load_active_policy(
    connection: sqlite3.Connection,
    *,
    expected_logical_vault_id: str,
    expected_activation_store_id: str,
    expected_activation_epoch: int,
    expected_activation_state_digest: str,
) -> ActivePolicySnapshot:
    """Load one policy/catalog/namespace from the already-pinned active tuple."""

    active = load_active_state(
        connection,
        expected_logical_vault_id=expected_logical_vault_id,
        expected_activation_store_id=expected_activation_store_id,
        expected_activation_epoch=expected_activation_epoch,
        expected_activation_state_digest=expected_activation_state_digest,
    )
    try:
        policy_row = connection.execute(
            "SELECT source_documents, source_fingerprint, compiled_policy, "
            "policy_fingerprint FROM compiled_policy_generations "
            "WHERE generation_id=?",
            (active.policy_generation_id,),
        ).fetchone()
        catalog_row = connection.execute(
            "SELECT descriptor FROM catalog_generation_descriptors "
            "WHERE catalog_generation=?",
            (active.catalog_generation,),
        ).fetchone()
        namespace_row = connection.execute(
            "SELECT evidence FROM governance_projection_namespaces "
            "WHERE policy_fingerprint=? AND projector_schema_version=? "
            "AND catalog_generation=? AND namespace_id=?",
            (
                active.policy_fingerprint,
                active.projector_schema_version,
                active.catalog_generation,
                active.projection_namespace_id,
            ),
        ).fetchone()
        if policy_row is None or catalog_row is None or namespace_row is None:
            raise SchemaV4Error("active policy tuple rows are unavailable")
        documents = _decode_source_document_bytes(policy_row[0])
        from . import policy as policy_module

        compiled = policy_module.compile_documents(dict(documents))
        stored_source_fingerprint = _digest(
            policy_row[1], "policy.source_fingerprint"
        )
        stored_policy_fingerprint = _digest(
            policy_row[3], "policy.policy_fingerprint"
        )
        canonical = policy_module.canonical_compiled_bytes(compiled)
        if (
            compiled.empty
            or compiled.blocked
            or compiled.fingerprint != stored_source_fingerprint
            or compiled.fingerprint != stored_policy_fingerprint
            or compiled.fingerprint != active.policy_fingerprint
            or not hmac.compare_digest(canonical, bytes(policy_row[2]))
        ):
            raise SchemaV4Error("active policy source parity does not verify")
        descriptor = _blob(
            catalog_row[0], "catalog.descriptor", allow_empty=True
        )
        evidence = _blob(
            namespace_row[0], "namespace.evidence", allow_empty=True
        )
    except (IndexError, TypeError, ValueError, sqlite3.Error, SchemaV4Error):
        raise SchemaV4Error("governance active policy is unavailable") from None
    return ActivePolicySnapshot(
        active=active,
        policy=compiled,
        source_documents=documents,
        catalog_descriptor=descriptor,
        projection_namespace_evidence=evidence,
    )


def _projection_namespace_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise SchemaV4Error("projection namespace pins cannot be verified")
    return value


def projection_namespace_ids(connection: sqlite3.Connection) -> frozenset[str]:
    """Return every registered immutable namespace in one caller-held snapshot."""

    try:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 4:
            raise SchemaV4Error("projection namespace inventory requires schema v4")
        return frozenset(
            _projection_namespace_id(row[0])
            for row in connection.execute(
                "SELECT namespace_id FROM governance_projection_namespaces"
            )
        )
    except SchemaV4Error:
        raise
    except (IndexError, sqlite3.Error, TypeError) as error:
        raise SchemaV4Error(
            "projection namespace inventory cannot be verified"
        ) from error


def projection_namespace_pins(connection: sqlite3.Connection) -> frozenset[str]:
    """Return active and recovery-bound namespaces in one caller-held snapshot."""

    try:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 4:
            raise SchemaV4Error("projection namespace pins require schema v4")
        active_rows = connection.execute(
            "SELECT n.namespace_id FROM active_governance_tuple a "
            "JOIN governance_projection_namespaces n "
            "ON n.policy_fingerprint=a.policy_fingerprint "
            "AND n.projector_schema_version=a.projector_schema_version "
            "AND n.catalog_generation=a.catalog_generation "
            "WHERE a.singleton=1"
        ).fetchall()
        if len(active_rows) != 1:
            raise SchemaV4Error("projection namespace pins cannot be verified")
        pins = {_projection_namespace_id(active_rows[0][0])}

        for (proposal_json,) in connection.execute(
            "SELECT proposal_json FROM governance_proposals WHERE status='pending'"
        ):
            try:
                payload = json.loads(proposal_json)
                binding = payload["authority_binding"]
                reviewed = binding["reviewed_active_tuple"]
                target = binding["target"]["projection_namespace"]
                pins.add(_projection_namespace_id(reviewed["projection_namespace_id"]))
                pins.add(_projection_namespace_id(target["namespace_id"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise SchemaV4Error(
                    "projection namespace pins cannot be verified"
                ) from error

        for (value_json,) in connection.execute(
            "SELECT c.value_json FROM governance_operation_components c "
            "JOIN governance_operation_journals j ON j.event_id=c.event_id "
            "WHERE j.phase IN ('allocating','pending') "
            "AND c.component_kind='catalog'"
        ):
            try:
                value = json.loads(value_json)
                pins.add(_projection_namespace_id(value["projection_namespace_id"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise SchemaV4Error(
                    "projection namespace pins cannot be verified"
                ) from error
        return frozenset(pins)
    except SchemaV4Error:
        raise
    except (IndexError, sqlite3.Error, TypeError) as error:
        raise SchemaV4Error("projection namespace pins cannot be verified") from error


def _publication_result(
    connection: sqlite3.Connection,
    active: VerifiedActiveGovernanceState,
) -> TuplePublicationResult:
    policy = connection.execute(
        "SELECT immutable_row_digest FROM compiled_policy_generations "
        "WHERE generation_id=?",
        (active.policy_generation_id,),
    ).fetchone()
    catalog = connection.execute(
        "SELECT descriptor_digest FROM catalog_generation_descriptors "
        "WHERE catalog_generation=?",
        (active.catalog_generation,),
    ).fetchone()
    namespace = connection.execute(
        "SELECT namespace_digest FROM governance_projection_namespaces "
        "WHERE policy_fingerprint=? AND projector_schema_version=? "
        "AND catalog_generation=? AND namespace_id=?",
        (
            active.policy_fingerprint,
            active.projector_schema_version,
            active.catalog_generation,
            active.projection_namespace_id,
        ),
    ).fetchone()
    if policy is None or catalog is None or namespace is None:
        raise SchemaV4Error("committed tuple rows are unavailable")
    return TuplePublicationResult(
        active=active,
        policy_row_digest=_digest(policy[0], "policy.immutable_row_digest"),
        catalog_descriptor_digest=_digest(
            catalog[0], "catalog.descriptor_digest"
        ),
        projection_namespace_digest=_digest(
            namespace[0], "namespace.namespace_digest"
        ),
    )


def _acknowledge_registry(
    active: VerifiedActiveGovernanceState,
    acknowledge_registry: RegistryAcknowledger,
) -> None:
    if not callable(acknowledge_registry):
        raise SchemaV4Error("external registry acknowledgement is required")
    acknowledgement = acknowledge_registry(active)
    if (
        not isinstance(acknowledgement, ActivationRegistryAcknowledgement)
        or acknowledgement.activation_store_id != active.activation_store_id
        or acknowledgement.activation_epoch != active.activation_epoch
        or not hmac.compare_digest(
            acknowledgement.activation_state_digest,
            active.activation_state_digest,
        )
    ):
        raise SchemaV4Error(
            "external registry acknowledgement does not match the committed tuple"
        )


def recover_registry_acknowledgement(
    connection: sqlite3.Connection,
    *,
    expected: VerifiedActiveGovernanceState,
    acknowledge_registry: RegistryAcknowledger,
) -> TuplePublicationResult:
    """Acknowledge only the one receipt-proven successor already committed.

    This recovery seam never writes SQLite state and never recompiles source.
    It accepts exactly one active epoch whose immutable publication row names
    ``expected`` as its predecessor, then performs the external registry CAS.
    """

    if not isinstance(expected, VerifiedActiveGovernanceState):
        raise SchemaV4Error("expected active tuple is invalid")
    if connection.in_transaction:
        raise SchemaV4Error("registry recovery requires a clean transaction boundary")
    activation = connection.execute(
        "SELECT activation_store_id, logical_vault_id, activation_epoch, "
        "activation_state_digest FROM governance_activation_store WHERE singleton=1"
    ).fetchone()
    if activation is None:
        raise SchemaV4Error("committed activation state is unavailable")
    target = load_active_state(
        connection,
        expected_logical_vault_id=_text(activation[1], "activation.logical_vault_id"),
        expected_activation_store_id=_text(
            activation[0], "activation.activation_store_id"
        ),
        expected_activation_epoch=_integer(
            activation[2], "activation.activation_epoch"
        ),
        expected_activation_state_digest=_digest(
            activation[3], "activation.activation_state_digest"
        ),
    )
    predecessor = connection.execute(
        "SELECT predecessor_activation_state_digest FROM governance_tuple_publications "
        "WHERE target_activation_state_digest=?",
        (target.activation_state_digest,),
    ).fetchone()
    if (
        target.logical_vault_id != expected.logical_vault_id
        or target.activation_store_id != expected.activation_store_id
        or target.activation_epoch != expected.activation_epoch + 1
        or predecessor != (expected.activation_state_digest,)
    ):
        raise ActiveTupleStale(
            "committed tuple is not the exact reviewed registry successor"
        )
    _acknowledge_registry(target, acknowledge_registry)
    return _publication_result(connection, target)


def publish_policy_generation(
    connection: sqlite3.Connection,
    *,
    expected: VerifiedActiveGovernanceState,
    policy: PolicyGenerationSeed,
    namespace: ProjectionNamespaceSeed,
    activated_at: int,
    acknowledge_registry: RegistryAcknowledger,
) -> TuplePublicationResult:
    """Insert and CAS one complete policy generation against its reviewed tuple."""

    if not isinstance(expected, VerifiedActiveGovernanceState):
        raise SchemaV4Error("expected active tuple is invalid")
    if connection.in_transaction:
        raise SchemaV4Error("policy publication requires a clean transaction boundary")
    activated = _integer(activated_at, "activated_at")
    source_documents, policy_row_digest = _validate_policy(policy)
    if policy.predecessor_generation_id != expected.policy_generation_id:
        raise ActiveTupleStale("policy predecessor does not match the reviewed tuple")
    if not isinstance(namespace, ProjectionNamespaceSeed):
        raise SchemaV4Error("projection namespace is invalid")
    namespace_id = _text(namespace.namespace_id, "namespace.namespace_id")
    namespace_evidence = _blob(
        namespace.evidence,
        "namespace.evidence",
        allow_empty=True,
    )
    namespace_ready_at = _integer(namespace.ready_at, "namespace.ready_at")
    if namespace_ready_at > activated:
        raise SchemaV4Error("projection namespace is not ready at activation")

    _verify_policy_source_parity(policy, source_documents)

    connection.execute("BEGIN IMMEDIATE")
    try:
        try:
            current = load_active_state(
                connection,
                expected_logical_vault_id=expected.logical_vault_id,
                expected_activation_store_id=expected.activation_store_id,
                expected_activation_epoch=expected.activation_epoch,
                expected_activation_state_digest=expected.activation_state_digest,
            )
        except SchemaV4Error as exc:
            raise ActiveTupleStale(
                "active tuple no longer matches the reviewed predecessor"
            ) from exc
        if current != expected:
            raise ActiveTupleStale(
                "active tuple no longer matches the reviewed predecessor"
            )
        catalog = connection.execute(
            "SELECT descriptor_digest FROM catalog_generation_descriptors "
            "WHERE catalog_generation=?",
            (expected.catalog_generation,),
        ).fetchone()
        if catalog is None:
            raise SchemaV4Error("reviewed catalog descriptor is unavailable")
        catalog_descriptor_digest = _digest(
            catalog[0], "catalog.descriptor_digest"
        )
        namespace_digest = _framed_digest(
            b"exomem.authorization-projection-namespace.v1",
            policy.policy_fingerprint.encode("ascii"),
            _ascii_integer(policy.projector_schema_version),
            _ascii_integer(expected.catalog_generation),
            namespace_id.encode(),
            namespace_evidence,
            _ascii_integer(namespace_ready_at),
        )
        target_epoch = _integer(
            expected.activation_epoch + 1,
            "target_activation_epoch",
        )
        target_digest = activation_state_digest(
            logical_vault_id=expected.logical_vault_id,
            activation_store_id=expected.activation_store_id,
            activation_epoch=target_epoch,
            policy_generation_id=policy.generation_id,
            policy_fingerprint=policy.policy_fingerprint,
            policy_row_digest=policy_row_digest,
            projector_schema_version=policy.projector_schema_version,
            catalog_generation=expected.catalog_generation,
            catalog_descriptor_digest=catalog_descriptor_digest,
            projection_namespace_identity=namespace_digest,
        )
        connection.execute(
            "INSERT INTO compiled_policy_generations "
            "(generation_id, source_documents, source_fingerprint, conflict_digest, "
            "compiled_policy, policy_fingerprint, compiler_schema_version, "
            "projector_schema_version, predecessor_generation_id, authoring_event_id, "
            "receipt_event_id, immutable_row_digest, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                policy.generation_id,
                source_documents,
                policy.source_fingerprint,
                policy.conflict_digest,
                policy.compiled_policy,
                policy.policy_fingerprint,
                policy.compiler_schema_version,
                policy.projector_schema_version,
                policy.predecessor_generation_id,
                policy.authoring_event_id,
                policy.receipt_event_id,
                policy_row_digest,
                policy.created_at,
            ),
        )
        connection.execute(
            "INSERT INTO governance_projection_namespaces "
            "(policy_fingerprint, projector_schema_version, catalog_generation, "
            "namespace_id, namespace_digest, evidence, ready_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                policy.policy_fingerprint,
                policy.projector_schema_version,
                expected.catalog_generation,
                namespace_id,
                namespace_digest,
                namespace_evidence,
                namespace_ready_at,
            ),
        )
        active_update = connection.execute(
            "UPDATE active_governance_tuple SET policy_generation_id=?, "
            "policy_fingerprint=?, projector_schema_version=? "
            "WHERE singleton=1 AND policy_generation_id=? AND policy_fingerprint=? "
            "AND projector_schema_version=? AND catalog_generation=?",
            (
                policy.generation_id,
                policy.policy_fingerprint,
                policy.projector_schema_version,
                expected.policy_generation_id,
                expected.policy_fingerprint,
                expected.projector_schema_version,
                expected.catalog_generation,
            ),
        )
        activation_update = connection.execute(
            "UPDATE governance_activation_store SET activation_epoch=?, "
            "activation_state_digest=? WHERE singleton=1 AND activation_store_id=? "
            "AND logical_vault_id=? AND activation_epoch=? "
            "AND activation_state_digest=?",
            (
                target_epoch,
                target_digest,
                expected.activation_store_id,
                expected.logical_vault_id,
                expected.activation_epoch,
                expected.activation_state_digest,
            ),
        )
        if active_update.rowcount != 1 or activation_update.rowcount != 1:
            raise ActiveTupleStale(
                "active tuple no longer matches the reviewed predecessor"
            )
        connection.execute(
            "INSERT INTO governance_tuple_publications "
            "(event_id, publication_kind, predecessor_activation_state_digest, "
            "target_activation_state_digest, policy_generation_id, policy_fingerprint, "
            "projector_schema_version, catalog_generation, activation_epoch, status, "
            "activated_at) VALUES (?, 'policy', ?, ?, ?, ?, ?, ?, ?, 'committed', ?)",
            (
                policy.receipt_event_id,
                expected.activation_state_digest,
                target_digest,
                policy.generation_id,
                policy.policy_fingerprint,
                policy.projector_schema_version,
                expected.catalog_generation,
                target_epoch,
                activated,
            ),
        )
        _crash_point("policy-publication-before-commit")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    active = load_active_state(
        connection,
        expected_logical_vault_id=expected.logical_vault_id,
        expected_activation_store_id=expected.activation_store_id,
        expected_activation_epoch=target_epoch,
        expected_activation_state_digest=target_digest,
    )
    _crash_point("policy-publication-after-commit-before-registry")
    _acknowledge_registry(active, acknowledge_registry)
    return _publication_result(connection, active)


def preview_catalog_generation(
    connection: sqlite3.Connection,
    *,
    expected: VerifiedActiveGovernanceState,
    catalog: CatalogGenerationSeed,
    namespace: ProjectionNamespaceSeed,
    activated_at: int,
) -> TuplePublicationResult:
    """Derive the exact catalog successor without changing the active tuple."""

    if not isinstance(expected, VerifiedActiveGovernanceState):
        raise SchemaV4Error("expected active tuple is invalid")
    if not isinstance(catalog, CatalogGenerationSeed):
        raise SchemaV4Error("catalog generation is invalid")
    catalog_generation = _integer(
        catalog.catalog_generation,
        "catalog.catalog_generation",
    )
    if catalog_generation != expected.catalog_generation + 1:
        raise ActiveTupleStale("catalog generation is not the reviewed successor")
    descriptor = _blob(catalog.descriptor, "catalog.descriptor", allow_empty=True)
    artifact_count = _integer(
        catalog.artifact_count,
        "catalog.artifact_count",
        minimum=0,
    )
    catalog_created_at = _integer(catalog.created_at, "catalog.created_at")
    activated = _integer(activated_at, "activated_at")
    if not isinstance(namespace, ProjectionNamespaceSeed):
        raise SchemaV4Error("projection namespace is invalid")
    namespace_id = _text(namespace.namespace_id, "namespace.namespace_id")
    namespace_evidence = _blob(
        namespace.evidence,
        "namespace.evidence",
        allow_empty=True,
    )
    namespace_ready_at = _integer(namespace.ready_at, "namespace.ready_at")
    if namespace_ready_at > activated:
        raise SchemaV4Error("projection namespace is not ready at activation")
    try:
        current = load_active_state(
            connection,
            expected_logical_vault_id=expected.logical_vault_id,
            expected_activation_store_id=expected.activation_store_id,
            expected_activation_epoch=expected.activation_epoch,
            expected_activation_state_digest=expected.activation_state_digest,
        )
    except SchemaV4Error as exc:
        raise ActiveTupleStale(
            "active tuple no longer matches the reviewed predecessor"
        ) from exc
    if current != expected:
        raise ActiveTupleStale(
            "active tuple no longer matches the reviewed predecessor"
        )
    policy_row = connection.execute(
        "SELECT immutable_row_digest FROM compiled_policy_generations "
        "WHERE generation_id=? AND policy_fingerprint=? "
        "AND projector_schema_version=?",
        (
            expected.policy_generation_id,
            expected.policy_fingerprint,
            expected.projector_schema_version,
        ),
    ).fetchone()
    if policy_row is None:
        raise SchemaV4Error("reviewed policy generation is unavailable")
    policy_row_digest = _digest(policy_row[0], "policy.immutable_row_digest")
    catalog_descriptor_digest = _framed_digest(
        b"exomem.catalog-generation-descriptor.v1",
        _ascii_integer(catalog_generation),
        descriptor,
        _ascii_integer(artifact_count),
        _ascii_integer(catalog_created_at),
    )
    namespace_digest = _framed_digest(
        b"exomem.authorization-projection-namespace.v1",
        expected.policy_fingerprint.encode("ascii"),
        _ascii_integer(expected.projector_schema_version),
        _ascii_integer(catalog_generation),
        namespace_id.encode(),
        namespace_evidence,
        _ascii_integer(namespace_ready_at),
    )
    target_epoch = _integer(
        expected.activation_epoch + 1,
        "target_activation_epoch",
    )
    target_digest = activation_state_digest(
        logical_vault_id=expected.logical_vault_id,
        activation_store_id=expected.activation_store_id,
        activation_epoch=target_epoch,
        policy_generation_id=expected.policy_generation_id,
        policy_fingerprint=expected.policy_fingerprint,
        policy_row_digest=policy_row_digest,
        projector_schema_version=expected.projector_schema_version,
        catalog_generation=catalog_generation,
        catalog_descriptor_digest=catalog_descriptor_digest,
        projection_namespace_identity=namespace_digest,
    )
    return TuplePublicationResult(
        active=VerifiedActiveGovernanceState(
            logical_vault_id=expected.logical_vault_id,
            activation_store_id=expected.activation_store_id,
            activation_epoch=target_epoch,
            activation_state_digest=target_digest,
            policy_generation_id=expected.policy_generation_id,
            policy_fingerprint=expected.policy_fingerprint,
            projector_schema_version=expected.projector_schema_version,
            catalog_generation=catalog_generation,
            projection_namespace_id=namespace_id,
        ),
        policy_row_digest=policy_row_digest,
        catalog_descriptor_digest=catalog_descriptor_digest,
        projection_namespace_digest=namespace_digest,
    )


def publish_catalog_generation(
    connection: sqlite3.Connection,
    *,
    expected: VerifiedActiveGovernanceState,
    catalog: CatalogGenerationSeed,
    namespace: ProjectionNamespaceSeed,
    receipt_event_id: str,
    activated_at: int,
    acknowledge_registry: RegistryAcknowledger,
) -> TuplePublicationResult:
    """Insert and CAS one complete catalog against its reviewed policy tuple."""

    if not isinstance(expected, VerifiedActiveGovernanceState):
        raise SchemaV4Error("expected active tuple is invalid")
    if connection.in_transaction:
        raise SchemaV4Error("catalog publication requires a clean transaction boundary")
    if not isinstance(catalog, CatalogGenerationSeed):
        raise SchemaV4Error("catalog generation is invalid")
    catalog_generation = _integer(
        catalog.catalog_generation,
        "catalog.catalog_generation",
    )
    if catalog_generation != expected.catalog_generation + 1:
        raise ActiveTupleStale("catalog generation is not the reviewed successor")
    descriptor = _blob(catalog.descriptor, "catalog.descriptor", allow_empty=True)
    artifact_count = _integer(
        catalog.artifact_count,
        "catalog.artifact_count",
        minimum=0,
    )
    catalog_created_at = _integer(catalog.created_at, "catalog.created_at")
    event_id = _text(receipt_event_id, "receipt_event_id")
    activated = _integer(activated_at, "activated_at")
    if not isinstance(namespace, ProjectionNamespaceSeed):
        raise SchemaV4Error("projection namespace is invalid")
    namespace_id = _text(namespace.namespace_id, "namespace.namespace_id")
    namespace_evidence = _blob(
        namespace.evidence,
        "namespace.evidence",
        allow_empty=True,
    )
    namespace_ready_at = _integer(namespace.ready_at, "namespace.ready_at")
    if namespace_ready_at > activated:
        raise SchemaV4Error("projection namespace is not ready at activation")
    connection.execute("BEGIN IMMEDIATE")
    try:
        preview = preview_catalog_generation(
            connection,
            expected=expected,
            catalog=catalog,
            namespace=namespace,
            activated_at=activated,
        )
        target_epoch = preview.active.activation_epoch
        target_digest = preview.active.activation_state_digest
        catalog_descriptor_digest = preview.catalog_descriptor_digest
        namespace_digest = preview.projection_namespace_digest
        connection.execute(
            "INSERT INTO catalog_generation_descriptors "
            "(catalog_generation, descriptor, descriptor_digest, artifact_count, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                catalog_generation,
                descriptor,
                catalog_descriptor_digest,
                artifact_count,
                catalog_created_at,
            ),
        )
        connection.execute(
            "INSERT INTO governance_projection_namespaces "
            "(policy_fingerprint, projector_schema_version, catalog_generation, "
            "namespace_id, namespace_digest, evidence, ready_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                expected.policy_fingerprint,
                expected.projector_schema_version,
                catalog_generation,
                namespace_id,
                namespace_digest,
                namespace_evidence,
                namespace_ready_at,
            ),
        )
        active_update = connection.execute(
            "UPDATE active_governance_tuple SET catalog_generation=? "
            "WHERE singleton=1 AND policy_generation_id=? AND policy_fingerprint=? "
            "AND projector_schema_version=? AND catalog_generation=?",
            (
                catalog_generation,
                expected.policy_generation_id,
                expected.policy_fingerprint,
                expected.projector_schema_version,
                expected.catalog_generation,
            ),
        )
        activation_update = connection.execute(
            "UPDATE governance_activation_store SET activation_epoch=?, "
            "activation_state_digest=? WHERE singleton=1 AND activation_store_id=? "
            "AND logical_vault_id=? AND activation_epoch=? "
            "AND activation_state_digest=?",
            (
                target_epoch,
                target_digest,
                expected.activation_store_id,
                expected.logical_vault_id,
                expected.activation_epoch,
                expected.activation_state_digest,
            ),
        )
        if active_update.rowcount != 1 or activation_update.rowcount != 1:
            raise ActiveTupleStale(
                "active tuple no longer matches the reviewed predecessor"
            )
        connection.execute(
            "INSERT INTO governance_tuple_publications "
            "(event_id, publication_kind, predecessor_activation_state_digest, "
            "target_activation_state_digest, policy_generation_id, policy_fingerprint, "
            "projector_schema_version, catalog_generation, activation_epoch, status, "
            "activated_at) VALUES (?, 'catalog', ?, ?, ?, ?, ?, ?, ?, 'committed', ?)",
            (
                event_id,
                expected.activation_state_digest,
                target_digest,
                expected.policy_generation_id,
                expected.policy_fingerprint,
                expected.projector_schema_version,
                catalog_generation,
                target_epoch,
                activated,
            ),
        )
        _crash_point("catalog-publication-before-commit")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    active = load_active_state(
        connection,
        expected_logical_vault_id=expected.logical_vault_id,
        expected_activation_store_id=expected.activation_store_id,
        expected_activation_epoch=target_epoch,
        expected_activation_state_digest=target_digest,
    )
    _crash_point("catalog-publication-after-commit-before-registry")
    _acknowledge_registry(active, acknowledge_registry)
    return _publication_result(connection, active)


def insert_authorization_session(
    connection: sqlite3.Connection,
    record: authorization_sessions.AuthorizationSessionVerifierRecord,
    *,
    created_at: int,
) -> None:
    """Persist one already-issued bearer-free verifier row in exact schema v4."""

    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 4:
        raise SchemaV4Error("authorization session storage requires schema v4")
    if not isinstance(record, authorization_sessions.AuthorizationSessionVerifierRecord):
        raise SchemaV4Error("authorization session verifier record is invalid")
    if record.status != "active":
        raise SchemaV4Error("authorization session lifecycle must begin active")
    created = _integer(created_at, "created_at")
    binding = record.binding
    if created >= binding.expires_at:
        raise SchemaV4Error("authorization session lifecycle timestamps are invalid")
    activation = connection.execute(
        "SELECT logical_vault_id FROM governance_activation_store WHERE singleton=1"
    ).fetchone()
    if activation is None or str(activation[0]) != binding.logical_vault_id:
        raise SchemaV4Error("authorization session logical vault does not match activation")
    connection.execute(
        "INSERT INTO governance_authorization_sessions "
        "(session_id, locator_digest, verifier, verifier_key_id, credential_generation, "
        "principal_id, issuer_family, cell_id, logical_vault_id, keyring_id, status, "
        "created_at, rotated_at, expires_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, NULL)",
        (
            binding.session_id,
            record.locator_digest,
            record.verifier,
            record.verifier_key_id,
            binding.credential_generation,
            binding.principal_id,
            binding.issuer_family,
            binding.cell_id,
            binding.logical_vault_id,
            binding.keyring_id,
            created,
            binding.expires_at,
        ),
    )
