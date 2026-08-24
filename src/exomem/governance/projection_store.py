"""Immutable private persistence for authorization-projection variant rows.

This store is deliberately narrower than an active retrieval namespace.  It
persists the principal-free, content-addressed variant rows which lexical and
model measurement builders consume.  A later activation step must still bind
the required lane roots into the namespace evidence before the governance
tuple may point at it; variant-row readiness alone is never serving authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import reserved_paths
from ..kbdir import kb_dirname
from . import projections, schema_v4

SCHEMA_USER_VERSION = 2
_DESCRIPTOR_ID = "authorization-projections"
_OWNER = "governance.projections"
_STORE_FILENAME = "rows.sqlite"
_STORE_STATUS = "variant-rows-ready"
_ROW_DOMAIN = b"exomem.authorization-projection-row.v1"
_ITEM_DOMAIN = b"exomem.authorization-projection-item.v1"
_STORE_DOMAIN = b"exomem.authorization-projection-rows.v1"
_CATALOG_SCHEMA = "exomem.authorization-projection-catalog/v1"
_EVIDENCE_SCHEMA_V1 = "exomem.authorization-projection-namespace-evidence/v1"
_EVIDENCE_SCHEMA_V2 = "exomem.authorization-projection-namespace-evidence/v2"
_MEASUREMENT_FAMILY_DOMAIN = b"exomem.authorization-projection-measurement-family.v1"
_MEASUREMENT_LANES = frozenset({"vector", "clip", "graph"})
_HEX = frozenset("0123456789abcdef")
_TABLES = frozenset({"namespace_meta", "projection_items", "projection_variants"})
_TRIGGERS = frozenset(
    f"{table}_no_{operation}"
    for table in _TABLES
    for operation in ("insert", "update", "delete")
)


class ProjectionStoreError(RuntimeError):
    """Base class for content-free projection-store refusals."""


class ProjectionStoreMismatch(ProjectionStoreError):
    """Stored rows do not match their immutable namespace commitment."""


@dataclass(frozen=True, slots=True)
class ProjectionItemVariants:
    """All reachable non-L0 variants for one immutable catalog item."""

    item_identity: str
    content_hash: str
    variants: tuple[projections.ProjectionVariant, ...]
    scope_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identity = _bounded_text(self.item_identity, "item identity", maximum=4096)
        content_hash = _digest(self.content_hash, "content hash")
        if not isinstance(self.scope_ids, tuple):
            raise projections.ProjectionCanonicalizationError(
                "item scope ids must be an immutable tuple"
            )
        scope_ids = tuple(
            sorted(
                {_bounded_text(scope, "item scope id", maximum=4096) for scope in self.scope_ids},
                key=lambda value: value.encode("utf-16-be"),
            )
        )
        if len(scope_ids) != len(self.scope_ids):
            raise projections.ProjectionCanonicalizationError(
                "item scope ids must be a canonical set"
            )
        if not isinstance(self.variants, tuple):
            raise projections.ProjectionCanonicalizationError(
                "item variants must be an immutable tuple"
            )
        seen: set[str] = set()
        ordered: list[projections.ProjectionVariant] = []
        for variant in self.variants:
            if not isinstance(variant, projections.ProjectionVariant):
                raise projections.ProjectionCanonicalizationError(
                    "item variant has an invalid type"
                )
            if variant.item_identity != identity:
                raise projections.ProjectionCanonicalizationError(
                    "variant item identity does not match bundle"
                )
            if variant.content_hash != content_hash:
                raise projections.ProjectionCanonicalizationError(
                    "variant content hash does not match bundle"
                )
            if variant.projection_variant_id in seen:
                raise projections.ProjectionCanonicalizationError(
                    "item variant set contains a duplicate"
                )
            seen.add(variant.projection_variant_id)
            ordered.append(variant)
        if len(ordered) > projections.MAX_PROJECTION_VARIANTS_PER_ITEM:
            raise projections.ProjectionVariantOverflow(
                f"item exceeds {projections.MAX_PROJECTION_VARIANTS_PER_ITEM} projection variants"
            )
        object.__setattr__(
            self,
            "variants",
            tuple(sorted(ordered, key=lambda item: item.projection_variant_id)),
        )
        object.__setattr__(self, "scope_ids", scope_ids)


@dataclass(frozen=True, slots=True)
class VariantStoreManifest:
    """Content commitment for one complete immutable variant-row store."""

    namespace_key: projections.ProjectionNamespaceKey
    namespace_id: str
    item_count: int
    variant_count: int
    rows_digest: str


_VERIFIED_NAMESPACE_PROOF = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedProjectionNamespace:
    """One projection catalog proven against the active governance snapshot."""

    namespace_key: projections.ProjectionNamespaceKey
    active_state_digest: str
    manifest: VariantStoreManifest
    items: tuple[ProjectionItemVariants, ...]

    def __init__(
        self,
        namespace_key: projections.ProjectionNamespaceKey,
        active_state_digest: str,
        manifest: VariantStoreManifest,
        items: tuple[ProjectionItemVariants, ...],
        *,
        _proof: object,
    ) -> None:
        if _proof is not _VERIFIED_NAMESPACE_PROOF:
            raise ProjectionStoreMismatch(
                "projection namespace requires verified active-tuple evidence"
            )
        object.__setattr__(self, "namespace_key", namespace_key)
        object.__setattr__(self, "active_state_digest", active_state_digest)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "items", items)


@dataclass(frozen=True, slots=True)
class _VariantRow:
    variant: projections.ProjectionVariant
    search_fields_jcs: bytes
    row_digest: str


@dataclass(frozen=True, slots=True)
class _ItemMaterial:
    item: ProjectionItemVariants
    rows: tuple[_VariantRow, ...]
    scope_ids_jcs: bytes
    item_digest: str


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be bounded non-empty text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise projections.ProjectionCanonicalizationError(
            f"{name} contains an invalid Unicode scalar"
        ) from error
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be one lowercase SHA-256 digest"
        )
    return value


def _framed(domain: bytes, fields: Sequence[bytes]) -> bytes:
    out = bytearray(domain)
    out.append(0)
    for field in fields:
        if len(field) > (1 << 32) - 1:
            raise projections.ProjectionCanonicalizationError(
                "projection-store identity field is too large"
            )
        out.extend(len(field).to_bytes(4, "big"))
        out.extend(field)
    return bytes(out)


def _framed_digest(domain: bytes, fields: Sequence[bytes]) -> str:
    return hashlib.sha256(_framed(domain, fields)).hexdigest()


def projection_measurement_family_id(
    key: projections.ProjectionNamespaceKey,
    *,
    lane: str,
    extractor_version: str,
    model_version: str,
) -> str:
    """Return one canonical measurement-family identity beneath a namespace."""

    if not isinstance(key, projections.ProjectionNamespaceKey):
        raise projections.ProjectionCanonicalizationError(
            "projection measurement namespace key is invalid"
        )
    if lane not in _MEASUREMENT_LANES:
        raise projections.ProjectionCanonicalizationError(
            "projection measurement lane is not registered"
        )
    extractor = _bounded_text(
        extractor_version,
        "projection measurement extractor version",
        maximum=256,
    )
    model = _bounded_text(
        model_version,
        "projection measurement model version",
        maximum=256,
    )
    return _framed_digest(
        _MEASUREMENT_FAMILY_DOMAIN,
        (
            key.policy_fingerprint.encode("ascii"),
            str(key.projector_schema_version).encode("ascii"),
            str(key.catalog_generation).encode("ascii"),
            lane.encode("ascii"),
            extractor.encode("utf-8"),
            model.encode("utf-8"),
        ),
    )


@dataclass(frozen=True, slots=True)
class ProjectionMeasurementRoot:
    """Active-tuple commitment to one complete immutable measurement family."""

    namespace_key: projections.ProjectionNamespaceKey
    family_id: str
    lane: str
    extractor_version: str
    model_version: str
    measurement_count: int
    vector_dimension: int | None
    graph_edge_count: int
    rows_digest: str

    def __post_init__(self) -> None:
        expected_family_id = projection_measurement_family_id(
            self.namespace_key,
            lane=self.lane,
            extractor_version=self.extractor_version,
            model_version=self.model_version,
        )
        if _digest(self.family_id, "measurement family id") != expected_family_id:
            raise projections.ProjectionCanonicalizationError(
                "projection measurement family identity does not verify"
            )
        if type(self.measurement_count) is not int or self.measurement_count < 0:
            raise projections.ProjectionCanonicalizationError(
                "projection measurement count does not verify"
            )
        if type(self.graph_edge_count) is not int or self.graph_edge_count < 0:
            raise projections.ProjectionCanonicalizationError(
                "projection graph edge count does not verify"
            )
        if self.lane == "graph":
            if self.vector_dimension is not None or (
                self.measurement_count == 0 and self.graph_edge_count != 0
            ):
                raise projections.ProjectionCanonicalizationError(
                    "projection graph measurement dimension does not verify"
                )
            projections.require_supported_capacity(graph_edges=self.graph_edge_count)
        else:
            if self.graph_edge_count != 0 or (
                self.measurement_count == 0 and self.vector_dimension is not None
            ) or (
                self.measurement_count > 0
                and (
                    type(self.vector_dimension) is not int
                    or not 1 <= self.vector_dimension <= 4096
                )
            ):
                raise projections.ProjectionCanonicalizationError(
                    "projection vector measurement shape does not verify"
                )
        _digest(self.rows_digest, "projection measurement rows digest")


@dataclass(frozen=True, slots=True)
class ProjectionNamespaceEvidence:
    """Verified lexical manifest plus exact active measurement roots."""

    manifest: VariantStoreManifest
    required_measurement_roots: tuple[ProjectionMeasurementRoot, ...]


def variant_store_path(
    vault_root: Path,
    key: projections.ProjectionNamespaceKey,
) -> Path:
    """Return the sole private path derived from an exact namespace key."""

    if not isinstance(key, projections.ProjectionNamespaceKey):
        raise projections.ProjectionCanonicalizationError(
            "projection namespace key is invalid"
        )
    return (
        Path(vault_root)
        / kb_dirname()
        / ".authorization-projections"
        / key.namespace_id
        / _STORE_FILENAME
    )


def _row_material(variant: projections.ProjectionVariant) -> _VariantRow:
    search_fields_jcs = projections.canonical_jcs(dict(variant.search_fields))
    row_digest = _framed_digest(
        _ROW_DOMAIN,
        (
            variant.projection_variant_id.encode("ascii"),
            variant.value_jcs,
            search_fields_jcs,
            str(variant.decision_level).encode("ascii"),
        ),
    )
    return _VariantRow(variant, search_fields_jcs, row_digest)


def _materialize(
    key: projections.ProjectionNamespaceKey,
    items: Iterable[ProjectionItemVariants],
) -> tuple[tuple[_ItemMaterial, ...], VariantStoreManifest]:
    if not isinstance(key, projections.ProjectionNamespaceKey):
        raise projections.ProjectionCanonicalizationError(
            "projection namespace key is invalid"
        )
    by_identity: dict[str, ProjectionItemVariants] = {}
    for item in items:
        if not isinstance(item, ProjectionItemVariants):
            raise projections.ProjectionCanonicalizationError(
                "projection item bundle is invalid"
            )
        if item.item_identity in by_identity:
            raise projections.ProjectionCanonicalizationError(
                "projection catalog contains a duplicate item identity"
            )
        by_identity[item.item_identity] = item
        projections.require_supported_capacity(catalog_items=len(by_identity))
    material: list[_ItemMaterial] = []
    for identity in sorted(by_identity, key=lambda value: value.encode("utf-16-be")):
        item = by_identity[identity]
        for variant in item.variants:
            value = json.loads(variant.value_jcs)
            if value["projector_schema_version"] != key.projector_schema_version:
                raise projections.ProjectionCanonicalizationError(
                    "variant projector schema does not match namespace"
                )
        rows = tuple(_row_material(variant) for variant in item.variants)
        scope_ids_jcs = projections.canonical_jcs(list(item.scope_ids))
        item_digest = _framed_digest(
            _ITEM_DOMAIN,
            (
                item.item_identity.encode("utf-8"),
                item.content_hash.encode("ascii"),
                scope_ids_jcs,
                str(len(rows)).encode("ascii"),
                *(row.row_digest.encode("ascii") for row in rows),
            ),
        )
        material.append(_ItemMaterial(item, rows, scope_ids_jcs, item_digest))
    variant_count = sum(len(item.rows) for item in material)
    rows_digest = _framed_digest(
        _STORE_DOMAIN,
        (
            key.policy_fingerprint.encode("ascii"),
            str(key.projector_schema_version).encode("ascii"),
            str(key.catalog_generation).encode("ascii"),
            key.namespace_id.encode("ascii"),
            str(len(material)).encode("ascii"),
            str(variant_count).encode("ascii"),
            *(item.item_digest.encode("ascii") for item in material),
        ),
    )
    return tuple(material), VariantStoreManifest(
        namespace_key=key,
        namespace_id=key.namespace_id,
        item_count=len(material),
        variant_count=variant_count,
        rows_digest=rows_digest,
    )


def catalog_descriptor_bytes(
    key: projections.ProjectionNamespaceKey,
    items: Iterable[ProjectionItemVariants],
) -> bytes:
    """Return the canonical immutable content catalog for one generation."""

    material, _manifest = _materialize(key, items)
    return projections.canonical_jcs(
        {
            "schema": _CATALOG_SCHEMA,
            "catalog_generation": key.catalog_generation,
            "items": [
                {
                    "item_identity": entry.item.item_identity,
                    "content_hash": entry.item.content_hash,
                    "scope_ids": list(entry.item.scope_ids),
                }
                for entry in material
            ],
        }
    )


def _canonical_measurement_roots(
    manifest: VariantStoreManifest,
    roots: Iterable[ProjectionMeasurementRoot],
) -> tuple[ProjectionMeasurementRoot, ...]:
    if not isinstance(manifest, VariantStoreManifest):
        raise ProjectionStoreMismatch("projection namespace manifest is invalid")
    by_lane: dict[str, ProjectionMeasurementRoot] = {}
    for root in roots:
        if not isinstance(root, ProjectionMeasurementRoot):
            raise ProjectionStoreMismatch(
                "projection measurement root does not verify"
            )
        if root.namespace_key != manifest.namespace_key or root.lane in by_lane:
            raise ProjectionStoreMismatch(
                "projection measurement roots do not match the namespace"
            )
        by_lane[root.lane] = root
    return tuple(by_lane[lane] for lane in ("vector", "clip", "graph") if lane in by_lane)


def _measurement_root_value(root: ProjectionMeasurementRoot) -> dict[str, object]:
    return {
        "family_id": root.family_id,
        "extractor_version": root.extractor_version,
        "model_version": root.model_version,
        "measurement_count": root.measurement_count,
        "vector_dimension": root.vector_dimension,
        "graph_edge_count": root.graph_edge_count,
        "rows_digest": root.rows_digest,
    }


def projection_namespace_evidence_bytes(
    manifest: VariantStoreManifest,
    *,
    required_measurement_roots: Iterable[ProjectionMeasurementRoot] = (),
) -> bytes:
    """Bind ready lexical and selected measurement roots to one namespace tuple."""

    roots = _canonical_measurement_roots(manifest, required_measurement_roots)
    key = manifest.namespace_key
    required_lane_roots: dict[str, object] = {"lexical": manifest.rows_digest}
    required_lane_roots.update(
        {root.lane: _measurement_root_value(root) for root in roots}
    )
    return projections.canonical_jcs(
        {
            "schema": _EVIDENCE_SCHEMA_V2 if roots else _EVIDENCE_SCHEMA_V1,
            "policy_fingerprint": key.policy_fingerprint,
            "projector_schema_version": key.projector_schema_version,
            "catalog_generation": key.catalog_generation,
            "namespace_id": manifest.namespace_id,
            "item_count": manifest.item_count,
            "variant_count": manifest.variant_count,
            "rows_digest": manifest.rows_digest,
            "required_lane_roots": required_lane_roots,
        }
    )


def namespace_evidence_from_snapshot(
    snapshot: schema_v4.ActivePolicySnapshot,
) -> ProjectionNamespaceEvidence:
    """Decode the exact active lexical and measurement root commitments."""

    if not isinstance(snapshot, schema_v4.ActivePolicySnapshot):
        raise ProjectionStoreMismatch("active governance snapshot is unavailable")
    raw = snapshot.projection_namespace_evidence
    try:
        value = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectionStoreMismatch(
            "projection namespace evidence does not verify"
        ) from error
    expected_fields = {
        "schema",
        "policy_fingerprint",
        "projector_schema_version",
        "catalog_generation",
        "namespace_id",
        "item_count",
        "variant_count",
        "rows_digest",
        "required_lane_roots",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ProjectionStoreMismatch("projection namespace evidence does not verify")
    try:
        key = projections.ProjectionNamespaceKey(
            policy_fingerprint=value["policy_fingerprint"],
            projector_schema_version=value["projector_schema_version"],
            catalog_generation=value["catalog_generation"],
        )
        item_count = value["item_count"]
        variant_count = value["variant_count"]
        if (
            type(item_count) is not int
            or item_count < 0
            or type(variant_count) is not int
            or variant_count < 0
        ):
            raise ProjectionStoreMismatch(
                "projection namespace evidence does not verify"
            )
        manifest = VariantStoreManifest(
            namespace_key=key,
            namespace_id=_digest(value["namespace_id"], "namespace id"),
            item_count=item_count,
            variant_count=variant_count,
            rows_digest=_digest(value["rows_digest"], "projection rows digest"),
        )
    except (projections.ProjectionError, ProjectionStoreMismatch) as error:
        raise ProjectionStoreMismatch(
            "projection namespace evidence does not verify"
        ) from error
    roots_value = value["required_lane_roots"]
    if not isinstance(roots_value, dict) or "lexical" not in roots_value:
        raise ProjectionStoreMismatch("projection namespace evidence does not verify")
    measurement_roots: list[ProjectionMeasurementRoot] = []
    if value["schema"] == _EVIDENCE_SCHEMA_V1:
        if roots_value != {"lexical": manifest.rows_digest}:
            raise ProjectionStoreMismatch(
                "projection namespace evidence does not verify"
            )
    elif value["schema"] == _EVIDENCE_SCHEMA_V2:
        if roots_value.get("lexical") != manifest.rows_digest or not set(
            roots_value
        ).issubset({"lexical", *_MEASUREMENT_LANES}):
            raise ProjectionStoreMismatch(
                "projection namespace evidence does not verify"
            )
        root_fields = {
            "family_id",
            "extractor_version",
            "model_version",
            "measurement_count",
            "vector_dimension",
            "graph_edge_count",
            "rows_digest",
        }
        try:
            for lane in ("vector", "clip", "graph"):
                if lane not in roots_value:
                    continue
                root_value = roots_value[lane]
                if not isinstance(root_value, dict) or set(root_value) != root_fields:
                    raise ProjectionStoreMismatch(
                        "projection measurement root does not verify"
                    )
                measurement_roots.append(
                    ProjectionMeasurementRoot(
                        namespace_key=key,
                        family_id=root_value["family_id"],
                        lane=lane,
                        extractor_version=root_value["extractor_version"],
                        model_version=root_value["model_version"],
                        measurement_count=root_value["measurement_count"],
                        vector_dimension=root_value["vector_dimension"],
                        graph_edge_count=root_value["graph_edge_count"],
                        rows_digest=root_value["rows_digest"],
                    )
                )
        except (projections.ProjectionError, ProjectionStoreMismatch) as error:
            raise ProjectionStoreMismatch(
                "projection namespace evidence does not verify"
            ) from error
        if not measurement_roots:
            raise ProjectionStoreMismatch(
                "projection namespace evidence does not verify"
            )
    else:
        raise ProjectionStoreMismatch("projection namespace evidence does not verify")
    roots = tuple(measurement_roots)
    active = snapshot.active
    if (
        key.policy_fingerprint != active.policy_fingerprint
        or key.projector_schema_version != active.projector_schema_version
        or key.catalog_generation != active.catalog_generation
        or manifest.namespace_id != key.namespace_id
        or active.projection_namespace_id != key.namespace_id
        or projections.canonical_jcs(value) != raw
        or projection_namespace_evidence_bytes(
            manifest,
            required_measurement_roots=roots,
        )
        != raw
    ):
        raise ProjectionStoreMismatch("projection namespace evidence does not verify")
    return ProjectionNamespaceEvidence(
        manifest=manifest,
        required_measurement_roots=roots,
    )


def manifest_from_namespace_evidence(
    snapshot: schema_v4.ActivePolicySnapshot,
) -> VariantStoreManifest:
    """Decode the exact active lexical row-root commitment."""

    return namespace_evidence_from_snapshot(snapshot).manifest


def bind_active_projection_namespace(
    snapshot: schema_v4.ActivePolicySnapshot,
    *,
    manifest: VariantStoreManifest,
    items: Iterable[ProjectionItemVariants],
) -> VerifiedProjectionNamespace:
    """Bind serving rows to one already-verified active policy/catalog tuple."""

    if not isinstance(snapshot, schema_v4.ActivePolicySnapshot):
        raise ProjectionStoreMismatch("active governance snapshot is unavailable")
    if not isinstance(manifest, VariantStoreManifest):
        raise ProjectionStoreMismatch("projection store manifest is unavailable")
    active = snapshot.active
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=active.policy_fingerprint,
        projector_schema_version=active.projector_schema_version,
        catalog_generation=active.catalog_generation,
    )
    material, recomputed = _materialize(key, items)
    canonical_items = tuple(entry.item for entry in material)
    if (
        snapshot.policy.fingerprint != active.policy_fingerprint
        or active.projection_namespace_id != key.namespace_id
        or manifest.namespace_key != key
        or manifest.namespace_id != key.namespace_id
        or recomputed != manifest
    ):
        raise ProjectionStoreMismatch(
            "projection namespace does not match the verified active tuple"
        )
    expected_catalog = catalog_descriptor_bytes(key, canonical_items)
    evidence = namespace_evidence_from_snapshot(snapshot)
    if not hmac.compare_digest(snapshot.catalog_descriptor, expected_catalog):
        raise ProjectionStoreMismatch(
            "projection catalog does not match the verified active tuple"
        )
    if evidence.manifest != manifest:
        raise ProjectionStoreMismatch(
            "projection evidence does not match the verified active tuple"
        )
    return VerifiedProjectionNamespace(
        namespace_key=key,
        active_state_digest=_digest(
            active.activation_state_digest,
            "active state digest",
        ),
        manifest=manifest,
        items=canonical_items,
        _proof=_VERIFIED_NAMESPACE_PROOF,
    )


def _crash_point(_point: str) -> None:
    """Test-only crash barrier hook."""


def _schema_names(connection: sqlite3.Connection, kind: str) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
            (kind,),
        )
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE namespace_meta ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint)=64), "
        "projector_schema_version INTEGER NOT NULL CHECK(projector_schema_version>0), "
        "catalog_generation INTEGER NOT NULL CHECK(catalog_generation>=0), "
        "namespace_id TEXT NOT NULL, item_count INTEGER NOT NULL CHECK(item_count>=0), "
        "variant_count INTEGER NOT NULL CHECK(variant_count>=0), "
        "rows_digest TEXT NOT NULL CHECK(length(rows_digest)=64), "
        "status TEXT NOT NULL CHECK(status='variant-rows-ready'))"
    )
    connection.execute(
        "CREATE TABLE projection_items ("
        "item_identity TEXT PRIMARY KEY, content_hash TEXT NOT NULL "
        "CHECK(length(content_hash)=64), scope_ids_jcs BLOB NOT NULL, "
        "variant_count INTEGER NOT NULL "
        "CHECK(variant_count>=0), item_digest TEXT NOT NULL CHECK(length(item_digest)=64))"
    )
    connection.execute(
        "CREATE TABLE projection_variants ("
        "item_identity TEXT NOT NULL, projection_variant_id TEXT NOT NULL "
        "CHECK(length(projection_variant_id)=64), decision_level INTEGER NOT NULL "
        "CHECK(decision_level BETWEEN 1 AND 6), value_jcs BLOB NOT NULL, "
        "search_fields_jcs BLOB NOT NULL, row_digest TEXT NOT NULL "
        "CHECK(length(row_digest)=64), PRIMARY KEY(item_identity, projection_variant_id), "
        "FOREIGN KEY(item_identity) REFERENCES projection_items(item_identity))"
    )
    connection.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")


def _create_immutable_triggers(connection: sqlite3.Connection) -> None:
    for table in sorted(_TABLES):
        for operation in ("insert", "update", "delete"):
            connection.execute(
                f"CREATE TRIGGER {table}_no_{operation} BEFORE {operation.upper()} "
                f"ON {table} BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END"
            )


def _insert_material(
    connection: sqlite3.Connection,
    material: tuple[_ItemMaterial, ...],
    manifest: VariantStoreManifest,
) -> None:
    key = manifest.namespace_key
    connection.execute(
        "INSERT INTO namespace_meta "
        "(singleton, policy_fingerprint, projector_schema_version, catalog_generation, "
        "namespace_id, item_count, variant_count, rows_digest, status) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key.policy_fingerprint,
            key.projector_schema_version,
            key.catalog_generation,
            manifest.namespace_id,
            manifest.item_count,
            manifest.variant_count,
            manifest.rows_digest,
            _STORE_STATUS,
        ),
    )
    for item in material:
        connection.execute(
            "INSERT INTO projection_items "
            "(item_identity, content_hash, scope_ids_jcs, variant_count, item_digest) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                item.item.item_identity,
                item.item.content_hash,
                item.scope_ids_jcs,
                len(item.rows),
                item.item_digest,
            ),
        )
        connection.executemany(
            "INSERT INTO projection_variants "
            "(item_identity, projection_variant_id, decision_level, value_jcs, "
            "search_fields_jcs, row_digest) VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    item.item.item_identity,
                    row.variant.projection_variant_id,
                    row.variant.decision_level,
                    row.variant.value_jcs,
                    row.search_fields_jcs,
                    row.row_digest,
                )
                for row in item.rows
            ),
        )


def _metadata_manifest(
    connection: sqlite3.Connection,
    key: projections.ProjectionNamespaceKey,
) -> VariantStoreManifest:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_USER_VERSION:
        raise ProjectionStoreMismatch("projection store schema does not verify")
    if _schema_names(connection, "table") != _TABLES:
        raise ProjectionStoreMismatch("projection store table set does not verify")
    if _schema_names(connection, "trigger") != _TRIGGERS:
        raise ProjectionStoreMismatch("projection store immutable trigger set does not verify")
    rows = connection.execute("SELECT * FROM namespace_meta").fetchall()
    if len(rows) != 1:
        raise ProjectionStoreMismatch("projection store metadata does not verify")
    row = rows[0]
    expected_prefix = (
        1,
        key.policy_fingerprint,
        key.projector_schema_version,
        key.catalog_generation,
        key.namespace_id,
    )
    if tuple(row[:5]) != expected_prefix or row[8] != _STORE_STATUS:
        raise ProjectionStoreMismatch("projection store namespace does not verify")
    return VariantStoreManifest(
        namespace_key=key,
        namespace_id=key.namespace_id,
        item_count=int(row[5]),
        variant_count=int(row[6]),
        rows_digest=_digest(row[7], "projection rows digest"),
    )


def _verified_variant(
    row: tuple[object, ...],
    *,
    content_hash: str,
) -> projections.ProjectionVariant:
    try:
        search_fields = json.loads(bytes(row[4]))
        variant = projections.ProjectionVariant(
            projection_variant_id=str(row[1]),
            item_identity=str(row[0]),
            content_hash=content_hash,
            decision_level=int(row[2]),
            value_jcs=bytes(row[3]),
            search_fields=search_fields,
        )
    except (
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        projections.ProjectionError,
    ) as error:
        raise ProjectionStoreMismatch("projection row does not verify") from error
    expected = _row_material(variant)
    if expected.search_fields_jcs != bytes(row[4]) or expected.row_digest != row[5]:
        raise ProjectionStoreMismatch("projection row digest does not verify")
    return variant


def _verified_bundles(
    connection: sqlite3.Connection,
) -> tuple[ProjectionItemVariants, ...]:
    bundles: list[ProjectionItemVariants] = []
    item_rows = connection.execute(
        "SELECT item_identity, content_hash, scope_ids_jcs, variant_count, item_digest "
        "FROM projection_items ORDER BY item_identity"
    ).fetchall()
    for item_row in item_rows:
        item_identity = str(item_row[0])
        content_hash = _digest(item_row[1], "stored content hash")
        try:
            scope_ids_value = json.loads(bytes(item_row[2]))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProjectionStoreMismatch(
                "projection item membership does not verify"
            ) from error
        if (
            not isinstance(scope_ids_value, list)
            or not all(isinstance(scope, str) for scope in scope_ids_value)
            or projections.canonical_jcs(scope_ids_value) != bytes(item_row[2])
        ):
            raise ProjectionStoreMismatch(
                "projection item membership does not verify"
            )
        variant_rows = connection.execute(
            "SELECT item_identity, projection_variant_id, decision_level, value_jcs, "
            "search_fields_jcs, row_digest FROM projection_variants "
            "WHERE item_identity=? ORDER BY projection_variant_id",
            (item_identity,),
        ).fetchall()
        variants = tuple(
            _verified_variant(tuple(row), content_hash=content_hash)
            for row in variant_rows
        )
        bundle = ProjectionItemVariants(
            item_identity,
            content_hash,
            variants,
            tuple(scope_ids_value),
        )
        rows = tuple(_row_material(variant) for variant in bundle.variants)
        scope_ids_jcs = projections.canonical_jcs(list(bundle.scope_ids))
        item_digest = _framed_digest(
            _ITEM_DOMAIN,
            (
                item_identity.encode("utf-8"),
                content_hash.encode("ascii"),
                scope_ids_jcs,
                str(len(rows)).encode("ascii"),
                *(row.row_digest.encode("ascii") for row in rows),
            ),
        )
        if int(item_row[3]) != len(rows) or item_row[4] != item_digest:
            raise ProjectionStoreMismatch("projection item row does not verify")
        bundles.append(bundle)
    return tuple(bundles)


def _verify_connection(
    connection: sqlite3.Connection,
    *,
    key: projections.ProjectionNamespaceKey,
    expected_rows_digest: str,
) -> VariantStoreManifest:
    metadata = _metadata_manifest(connection, key)
    if metadata.rows_digest != _digest(expected_rows_digest, "expected rows digest"):
        raise ProjectionStoreMismatch("projection store digest does not match active tuple")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ProjectionStoreMismatch("projection store row relationships do not verify")
    stored_item_count = int(
        connection.execute("SELECT COUNT(*) FROM projection_items").fetchone()[0]
    )
    stored_variant_count = int(
        connection.execute("SELECT COUNT(*) FROM projection_variants").fetchone()[0]
    )
    if (
        stored_item_count != metadata.item_count
        or stored_variant_count != metadata.variant_count
    ):
        raise ProjectionStoreMismatch("projection store row counts do not verify")
    bundles = _verified_bundles(connection)
    material, recomputed = _materialize(key, bundles)
    del material
    if recomputed != metadata:
        raise ProjectionStoreMismatch("projection store rows do not verify")
    return metadata


def _connect(
    path: Path,
    *,
    readonly: bool,
    vault_root: Path,
    database: Path,
) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    try:
        # Publish the primary identity immediately after pathname resolution,
        # before any pragma can fail or create a journal sibling.
        reserved_paths._publish_sqlite_owner_family(
            vault_root,
            database,
            _DESCRIPTOR_ID,
            connection,
            preserve_existing=True,
        )
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        if readonly:
            connection.execute("BEGIN")
        return connection
    except BaseException:
        connection.close()
        raise


def stage_variant_store(
    vault_root: Path,
    *,
    key: projections.ProjectionNamespaceKey,
    items: Iterable[ProjectionItemVariants],
) -> VariantStoreManifest:
    """Atomically build or exactly replay one immutable variant-row store."""

    material, expected = _materialize(key, items)
    root = Path(vault_root)
    database = variant_store_path(root, key)
    with reserved_paths._subsystem_authority_scope(_OWNER):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=(_DESCRIPTOR_ID,),
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                database,
                _DESCRIPTOR_ID,
                create=True,
            ) as retained:
                connection = _connect(
                    retained,
                    readonly=False,
                    vault_root=root,
                    database=database,
                )
                try:
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version == SCHEMA_USER_VERSION:
                        connection.execute("BEGIN")
                        return _verify_connection(
                            connection,
                            key=key,
                            expected_rows_digest=expected.rows_digest,
                        )
                    if version != 0 or _schema_names(connection, "table"):
                        raise ProjectionStoreMismatch(
                            "projection store immutable state does not verify"
                        )
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _create_schema(connection)
                        _insert_material(connection, material, expected)
                        _create_immutable_triggers(connection)
                        _crash_point("before-commit")
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    _crash_point("after-commit")
                    connection.execute("BEGIN")
                    return _verify_connection(
                        connection,
                        key=key,
                        expected_rows_digest=expected.rows_digest,
                    )
                except sqlite3.Error as error:
                    raise ProjectionStoreMismatch(
                        "projection store transaction does not verify"
                    ) from error
                finally:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        database,
                        _DESCRIPTOR_ID,
                        connection,
                        preserve_existing=True,
                    )
                    connection.close()


def verify_variant_store(
    vault_root: Path,
    *,
    key: projections.ProjectionNamespaceKey,
    expected_rows_digest: str,
) -> VariantStoreManifest:
    """Verify every immutable item and variant row against one exact digest."""

    root = Path(vault_root)
    database = variant_store_path(root, key)
    with reserved_paths._subsystem_authority_scope(_OWNER):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=(_DESCRIPTOR_ID,),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                database,
                _DESCRIPTOR_ID,
                create=False,
            ) as retained:
                connection = _connect(
                    retained,
                    readonly=True,
                    vault_root=root,
                    database=database,
                )
                try:
                    return _verify_connection(
                        connection,
                        key=key,
                        expected_rows_digest=expected_rows_digest,
                    )
                except sqlite3.Error as error:
                    raise ProjectionStoreMismatch(
                        "projection store verification does not verify"
                    ) from error
                finally:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        database,
                        _DESCRIPTOR_ID,
                        connection,
                        preserve_existing=True,
                    )
                    connection.close()


def load_projection_catalog(
    vault_root: Path,
    *,
    key: projections.ProjectionNamespaceKey,
    expected_rows_digest: str,
) -> tuple[VariantStoreManifest, tuple[ProjectionItemVariants, ...]]:
    """Load the complete catalog only after its immutable store verifies."""

    root = Path(vault_root)
    database = variant_store_path(root, key)
    with reserved_paths._subsystem_authority_scope(_OWNER):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=(_DESCRIPTOR_ID,),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                database,
                _DESCRIPTOR_ID,
                create=False,
            ) as retained:
                connection = _connect(
                    retained,
                    readonly=True,
                    vault_root=root,
                    database=database,
                )
                try:
                    manifest = _verify_connection(
                        connection,
                        key=key,
                        expected_rows_digest=expected_rows_digest,
                    )
                    return manifest, _verified_bundles(connection)
                except sqlite3.Error as error:
                    raise ProjectionStoreMismatch(
                        "projection catalog read does not verify"
                    ) from error
                finally:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        database,
                        _DESCRIPTOR_ID,
                        connection,
                        preserve_existing=True,
                    )
                    connection.close()


def load_projection_variant(
    vault_root: Path,
    *,
    key: projections.ProjectionNamespaceKey,
    expected_rows_digest: str,
    item_identity: str,
    expected_content_hash: str,
    projection_variant_id: str,
) -> projections.ProjectionVariant:
    """Load one selected row without sampling any other namespace tuple."""

    identity = _bounded_text(item_identity, "item identity", maximum=4096)
    content_hash = _digest(expected_content_hash, "expected content hash")
    variant_id = _digest(projection_variant_id, "projection variant id")
    root = Path(vault_root)
    database = variant_store_path(root, key)
    with reserved_paths._subsystem_authority_scope(_OWNER):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=(_DESCRIPTOR_ID,),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                database,
                _DESCRIPTOR_ID,
                create=False,
            ) as retained:
                connection = _connect(
                    retained,
                    readonly=True,
                    vault_root=root,
                    database=database,
                )
                try:
                    _verify_connection(
                        connection,
                        key=key,
                        expected_rows_digest=expected_rows_digest,
                    )
                    item = connection.execute(
                        "SELECT content_hash FROM projection_items WHERE item_identity=?",
                        (identity,),
                    ).fetchone()
                    if item is None or item[0] != content_hash:
                        raise ProjectionStoreMismatch(
                            "projection item content does not match catalog"
                        )
                    row = connection.execute(
                        "SELECT item_identity, projection_variant_id, decision_level, "
                        "value_jcs, search_fields_jcs, row_digest FROM projection_variants "
                        "WHERE item_identity=? AND projection_variant_id=?",
                        (identity, variant_id),
                    ).fetchone()
                    if row is None:
                        raise ProjectionStoreMismatch("projection row is unavailable")
                    return _verified_variant(tuple(row), content_hash=content_hash)
                except sqlite3.Error as error:
                    raise ProjectionStoreMismatch(
                        "projection row read does not verify"
                    ) from error
                finally:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        database,
                        _DESCRIPTOR_ID,
                        connection,
                        preserve_existing=True,
                    )
                    connection.close()


__all__ = [
    "ProjectionItemVariants",
    "ProjectionMeasurementRoot",
    "ProjectionNamespaceEvidence",
    "ProjectionStoreError",
    "ProjectionStoreMismatch",
    "VariantStoreManifest",
    "VerifiedProjectionNamespace",
    "bind_active_projection_namespace",
    "catalog_descriptor_bytes",
    "load_projection_catalog",
    "load_projection_variant",
    "namespace_evidence_from_snapshot",
    "projection_measurement_family_id",
    "projection_namespace_evidence_bytes",
    "manifest_from_namespace_evidence",
    "stage_variant_store",
    "variant_store_path",
    "verify_variant_store",
]
