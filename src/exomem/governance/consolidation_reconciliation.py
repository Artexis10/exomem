"""Pure, exhaustive reconciliation for governed vault consolidation.

The module consumes already-authenticated source and destination inventories.
It never opens a vault, mutates content, or accepts caller-supplied inventory
facts.  Its output is the deterministic C1-C8 classification and a finite,
lossless tentative map that later plan materialization can bind.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from .. import (
    markdown_relations,
    memory_refs,
    record_formats,
    relation_registry,
    semantic_units,
    structured_collections,
    vault,
)
from ..kbdir import kb_dirname
from .projections import ProjectionCanonicalizationError, canonical_jcs

RECONCILIATION_INVENTORY_SCHEMA = "exomem.consolidation-inventory/v1"
RECONCILIATION_SCHEMA = "exomem.consolidation-reconciliation/v1"
TENTATIVE_MAP_SCHEMA = "exomem.consolidation-tentative-map/v1"
C1_MAPPING_SCHEMA = "exomem.consolidation-c1-mapping-set/v1"

_INVENTORY_DOMAIN = RECONCILIATION_INVENTORY_SCHEMA.encode("ascii")
_RECONCILIATION_DOMAIN = RECONCILIATION_SCHEMA.encode("ascii")
_TENTATIVE_MAP_DOMAIN = TENTATIVE_MAP_SCHEMA.encode("ascii")
_C1_MAPPING_DOMAIN = C1_MAPPING_SCHEMA.encode("ascii")
_OBJECT_DOMAIN = b"exomem.consolidation-inventory-object/v1"
_MATCH_DOMAIN = b"exomem.consolidation-destination-match-set/v1"
_ROW_DOMAIN = b"exomem.consolidation-reconciliation-row/v1"
_DEPENDENCY_SET_DOMAIN = b"exomem.consolidation-dependency-map/v1"

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_OBJECTS = 100_000
_MAX_DEPENDENCIES_PER_OBJECT = 4_096
_MAX_CONTENT_BYTES = 64 * 1024 * 1024

_PRIMARY_CLASSES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8")
_DEPENDENCY_KINDS = frozenset(
    {
        "citation",
        "history",
        "media_pair",
        "record_identity",
        "reverse_citation",
        "semantic_anchor",
        "supersession",
        "typed_relation",
        "wikilink",
    }
)
_OBJECT_KINDS = frozenset(
    {
        "access_control",
        "audience",
        "authorization_session",
        "cache",
        "content",
        "derived_index",
        "evidence",
        "grant",
        "media",
        "media_frame",
        "media_sidecar",
        "policy",
        "receipt",
        "record_item",
        "record_manifest",
        "release_approval",
        "review_authority",
        "review_state",
        "run_state",
        "runtime_binding",
        "source",
        "token",
    }
)
_AUTHORITY_KINDS = frozenset(
    {
        "access_control",
        "audience",
        "authorization_session",
        "cache",
        "derived_index",
        "grant",
        "policy",
        "receipt",
        "release_approval",
        "review_authority",
        "review_state",
        "run_state",
        "runtime_binding",
        "token",
    }
)

_DEFAULT_ACTIONS = {
    "C1": "deduplicate_exact",
    "C2": "add",
    "C3": "reuse_destination",
}

__all__ = [
    "C1_MAPPING_SCHEMA",
    "RECONCILIATION_INVENTORY_SCHEMA",
    "RECONCILIATION_SCHEMA",
    "TENTATIVE_MAP_SCHEMA",
    "DependencyFinding",
    "DependencyMapping",
    "DependencyResolution",
    "InvalidResolution",
    "InventoryInvalid",
    "InventoryObject",
    "ObjectDependency",
    "OwnerResolution",
    "ReconciliationInventory",
    "ReconciliationResult",
    "ReconciliationRow",
    "ReconciliationUnresolved",
    "TentativeEntry",
    "TentativeMap",
    "build_inventory",
    "inventory_object_from_bytes",
    "reconcile_inventories",
    "validate_tentative_map",
]


class ReconciliationError(RuntimeError):
    """Base for content-free reconciliation refusals."""

    code = "CONSOLIDATION_RECONCILIATION_BLOCKED"


class InventoryInvalid(ReconciliationError):
    code = "CONSOLIDATION_INVENTORY_INVALID"


class ReconciliationUnresolved(ReconciliationError):
    code = "CONSOLIDATION_RECONCILIATION_UNRESOLVED"


class InvalidResolution(ReconciliationError):
    code = "CONSOLIDATION_RESOLUTION_INVALID"


@dataclass(frozen=True, slots=True)
class ObjectDependency:
    dependency_ref: str
    dependency_kind: str
    target_identity: str | None = None
    target_path: str | None = None
    target_anchor: str | None = None
    relation_type: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryObject:
    object_ref: str
    path: str
    entry_type: str
    size: int
    sha256: str
    bundle_sha256: str
    durable_identity: str | None
    logical_content_sha256: str | None
    object_kind: str
    append_only: bool
    lifecycle: str | None = None
    record_collection_id: str | None = None
    record_schema_version: int | None = None
    record_storage_path: str | None = None
    record_storage_strategy: str | None = None
    record_storage_components: tuple[str, ...] = ()
    record_audit_head: str | None = None
    record_schema_contract_jcs: str | None = None
    record_values_jcs: str | None = None
    anchors: tuple[str, ...] = ()
    dependencies: tuple[ObjectDependency, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationInventory:
    schema: str
    role: str
    snapshot_digest: str
    objects: tuple[InventoryObject, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class DependencyFinding:
    code: str
    dependency_ref: str
    dependency_kind: str
    candidate_object_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationRow:
    source_object_ref: str
    source_path: str
    source_object_digest: str
    destination_object_refs: tuple[str, ...]
    destination_match_digest: str
    primary_class: str
    dependency_findings: tuple[DependencyFinding, ...]
    allowed_resolutions: tuple[str, ...]
    default_action: str | None
    row_digest: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    schema: str
    source_snapshot_digest: str
    destination_snapshot_digest: str
    source_inventory_digest: str
    destination_inventory_digest: str
    source_objects: tuple[InventoryObject, ...]
    destination_objects: tuple[InventoryObject, ...]
    rows: tuple[ReconciliationRow, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class DependencyResolution:
    dependency_ref: str
    target_source_object_ref: str | None = None
    target_destination_object_ref: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerResolution:
    source_object_ref: str
    action: str
    destination_object_ref: str | None = None
    destination_path: str | None = None
    destination_identity: str | None = None
    dependency_targets: tuple[DependencyResolution, ...] = ()


@dataclass(frozen=True, slots=True)
class TentativeEntry:
    source_object_ref: str
    source_path: str
    source_identity: str | None
    source_sha256: str
    primary_class: str
    action: str
    publish: bool
    destination_object_ref: str | None
    destination_path: str | None
    destination_identity: str | None
    destination_sha256: str | None
    source_bundle_sha256: str
    matched_destination_sha256: str | None
    matched_destination_bundle_sha256: str | None
    destination_bundle_sha256: str | None
    source_object_kind: str
    matched_destination_object_kind: str | None
    destination_object_kind: str | None
    source_lifecycle: str | None
    matched_destination_lifecycle: str | None
    destination_lifecycle: str | None


@dataclass(frozen=True, slots=True)
class DependencyMapping:
    source_object_ref: str
    dependency_ref: str
    dependency_kind: str
    target_object_ref: str
    target_path: str
    target_identity: str | None
    target_anchor: str | None


@dataclass(frozen=True, slots=True)
class _FinalCandidate:
    object_ref: str
    path: str
    identity: str | None
    anchors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TentativeMap:
    schema: str
    reconciliation_digest: str
    entries: tuple[TentativeEntry, ...]
    dependency_map: tuple[DependencyMapping, ...]
    unresolved_count: int
    c1_mapping_digest: str
    dependency_map_digest: str
    digest: str


def _fail_inventory() -> None:
    raise InventoryInvalid("consolidation inventory is invalid") from None


def _fail_resolution() -> None:
    raise InvalidResolution("consolidation resolution is invalid") from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail_inventory()
    return value


def _identifier(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _fail_inventory()
    return value


def _bounded_integer(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_SAFE_INTEGER
    ):
        _fail_inventory()
    return value


def _normalized_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        _fail_inventory()
    normalized = unicodedata.normalize("NFC", value)
    candidate = PurePosixPath(normalized)
    if (
        normalized != value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _fail_inventory()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail_inventory()
    return candidate.as_posix()


def _bounded_text(value: object, *, maximum: int = 1_024) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        _fail_inventory()
    if unicodedata.normalize("NFC", value) != value or "\x00" in value:
        _fail_inventory()
    return value


def _canonical(value: object) -> bytes:
    try:
        return canonical_jcs(value)
    except ProjectionCanonicalizationError:
        _fail_inventory()


def _framed_digest(domain: bytes, value: object) -> str:
    payload = _canonical(value)
    framed = len(domain).to_bytes(4, "big") + domain + len(payload).to_bytes(8, "big") + payload
    return hashlib.sha256(framed).hexdigest()


def _dependency_value(dependency: ObjectDependency) -> dict[str, object]:
    return {
        "dependency_ref": dependency.dependency_ref,
        "dependency_kind": dependency.dependency_kind,
        "target_identity": dependency.target_identity,
        "target_path": dependency.target_path,
        "target_anchor": dependency.target_anchor,
        "relation_type": dependency.relation_type,
    }


def _object_value(item: InventoryObject) -> dict[str, object]:
    return {
        "object_ref": item.object_ref,
        "path": item.path,
        "entry_type": item.entry_type,
        "size": item.size,
        "sha256": item.sha256,
        "bundle_sha256": item.bundle_sha256,
        "durable_identity": item.durable_identity,
        "logical_content_sha256": item.logical_content_sha256,
        "object_kind": item.object_kind,
        "append_only": item.append_only,
        "lifecycle": item.lifecycle,
        "record_collection_id": item.record_collection_id,
        "record_schema_version": item.record_schema_version,
        "record_storage_path": item.record_storage_path,
        "record_storage_strategy": item.record_storage_strategy,
        "record_storage_components": list(item.record_storage_components),
        "record_audit_head": item.record_audit_head,
        "record_schema_contract_jcs": item.record_schema_contract_jcs,
        "record_values_jcs": item.record_values_jcs,
        "anchors": list(item.anchors),
        "dependencies": [_dependency_value(value) for value in item.dependencies],
    }


def _validate_dependency(value: ObjectDependency) -> ObjectDependency:
    if not isinstance(value, ObjectDependency):
        _fail_inventory()
    dependency_ref = _bounded_text(value.dependency_ref)
    if value.dependency_kind not in _DEPENDENCY_KINDS:
        _fail_inventory()
    target_identity = _identifier(value.target_identity, optional=True)
    target_path = _normalized_path(value.target_path) if value.target_path is not None else None
    if target_identity is None and target_path is None:
        _fail_inventory()
    target_anchor = (
        _bounded_text(value.target_anchor, maximum=256) if value.target_anchor is not None else None
    )
    relation_type = (
        _bounded_text(value.relation_type, maximum=128) if value.relation_type is not None else None
    )
    if value.dependency_kind == "typed_relation" and relation_type is None:
        _fail_inventory()
    return ObjectDependency(
        dependency_ref=dependency_ref,
        dependency_kind=value.dependency_kind,
        target_identity=target_identity,
        target_path=target_path,
        target_anchor=target_anchor,
        relation_type=relation_type,
    )


def _validate_object(value: InventoryObject) -> InventoryObject:
    if not isinstance(value, InventoryObject):
        _fail_inventory()
    object_ref = _bounded_text(value.object_ref, maximum=1_024)
    path = _normalized_path(value.path)
    if value.entry_type != "file":
        _fail_inventory()
    size = _bounded_integer(value.size)
    sha256 = _digest(value.sha256)
    bundle_sha256 = _digest(value.bundle_sha256)
    identity = _identifier(value.durable_identity, optional=True)
    logical = _digest(value.logical_content_sha256) if value.logical_content_sha256 else None
    if value.object_kind not in _OBJECT_KINDS or type(value.append_only) is not bool:
        _fail_inventory()
    canonical_append_only = bool(vault.in_append_only_tree(path))
    if value.append_only is not canonical_append_only:
        _fail_inventory()
    lifecycle = _bounded_text(value.lifecycle, maximum=128) if value.lifecycle is not None else None
    record_collection_id = _identifier(value.record_collection_id, optional=True)
    record_schema_version = (
        _bounded_integer(value.record_schema_version)
        if value.record_schema_version is not None
        else None
    )
    if record_schema_version == 0:
        _fail_inventory()
    record_storage_path = (
        _normalized_path(value.record_storage_path)
        if value.record_storage_path is not None
        else None
    )
    record_storage_strategy = value.record_storage_strategy
    if record_storage_strategy is not None and record_storage_strategy not in {
        "dataset",
        "markdown-items",
        "markdown-log",
    }:
        _fail_inventory()
    if (
        not isinstance(value.record_storage_components, tuple)
        or len(value.record_storage_components) > 64
    ):
        _fail_inventory()
    record_storage_components = tuple(
        sorted({_normalized_path(item) for item in value.record_storage_components})
    )
    if len(record_storage_components) != len(value.record_storage_components):
        _fail_inventory()
    record_audit_head = (
        _bounded_text(value.record_audit_head, maximum=256)
        if value.record_audit_head is not None
        else None
    )
    record_schema_contract_jcs = (
        _bounded_text(value.record_schema_contract_jcs, maximum=256 * 1024)
        if value.record_schema_contract_jcs is not None
        else None
    )
    if record_schema_contract_jcs is not None:
        try:
            decoded_schema_contract = json.loads(record_schema_contract_jcs)
        except (TypeError, ValueError):
            _fail_inventory()
        if (
            not isinstance(decoded_schema_contract, dict)
            or _record_canonical_json(decoded_schema_contract) != record_schema_contract_jcs
        ):
            _fail_inventory()
    record_values_jcs = (
        _bounded_text(value.record_values_jcs, maximum=_MAX_CONTENT_BYTES)
        if value.record_values_jcs is not None
        else None
    )
    if record_values_jcs is not None:
        try:
            decoded_record_values = json.loads(record_values_jcs)
        except (TypeError, ValueError):
            _fail_inventory()
        if (
            not isinstance(decoded_record_values, dict)
            or _record_canonical_json(decoded_record_values) != record_values_jcs
        ):
            _fail_inventory()
    if not isinstance(value.anchors, tuple) or len(value.anchors) > _MAX_DEPENDENCIES_PER_OBJECT:
        _fail_inventory()
    anchors = tuple(sorted({_bounded_text(item, maximum=256) for item in value.anchors}))
    if not isinstance(value.dependencies, tuple) or len(value.dependencies) > (
        _MAX_DEPENDENCIES_PER_OBJECT
    ):
        _fail_inventory()
    dependencies = tuple(
        sorted(
            (_validate_dependency(item) for item in value.dependencies),
            key=lambda item: (item.dependency_ref, item.dependency_kind),
        )
    )
    if len({item.dependency_ref for item in dependencies}) != len(dependencies):
        _fail_inventory()
    return InventoryObject(
        object_ref=object_ref,
        path=path,
        entry_type="file",
        size=size,
        sha256=sha256,
        bundle_sha256=bundle_sha256,
        durable_identity=identity,
        logical_content_sha256=logical,
        object_kind=value.object_kind,
        append_only=canonical_append_only,
        lifecycle=lifecycle,
        record_collection_id=record_collection_id,
        record_schema_version=record_schema_version,
        record_storage_path=record_storage_path,
        record_storage_strategy=record_storage_strategy,
        record_storage_components=record_storage_components,
        record_audit_head=record_audit_head,
        record_schema_contract_jcs=record_schema_contract_jcs,
        record_values_jcs=record_values_jcs,
        anchors=anchors,
        dependencies=dependencies,
    )


def _records_relative(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if len(parts) < 2:
        return None
    if parts[0].casefold() != kb_dirname().casefold() or parts[1].casefold() != "records":
        return None
    return PurePosixPath(*parts[2:]).as_posix() if len(parts) > 2 else ""


def _field_spec_value(spec: structured_collections.FieldSpec) -> dict[str, object]:
    return {
        "type": spec.type,
        "required": spec.required,
        "enum": list(spec.enum),
        "items": _field_spec_value(spec.items) if spec.items is not None else None,
        "units": list(spec.units),
        "link_kind": spec.link_kind,
    }


def _schema_contract_value(
    schema: structured_collections.ItemSchema,
) -> dict[str, object]:
    return {
        "version": schema.version,
        "natural_key": list(schema.natural_key),
        "fields": {name: _field_spec_value(spec) for name, spec in sorted(schema.fields.items())},
    }


def _field_spec_from_value(value: object) -> structured_collections.FieldSpec:
    if not isinstance(value, dict) or set(value) != {
        "enum",
        "items",
        "link_kind",
        "required",
        "type",
        "units",
    }:
        _fail_inventory()
    raw_type = value.get("type")
    required = value.get("required")
    enum = value.get("enum")
    units = value.get("units")
    link_kind = value.get("link_kind")
    if (
        not isinstance(raw_type, str)
        or type(required) is not bool
        or not isinstance(enum, list)
        or not isinstance(units, list)
        or any(not isinstance(item, str) for item in units)
        or (link_kind is not None and not isinstance(link_kind, str))
    ):
        _fail_inventory()
    items = value.get("items")
    return structured_collections.FieldSpec(
        type=raw_type,
        required=required,
        enum=tuple(enum),
        items=_field_spec_from_value(items) if items is not None else None,
        units=tuple(units),
        link_kind=link_kind,
    )


def _schema_from_contract(value: str) -> structured_collections.ItemSchema:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        _fail_inventory()
    if not isinstance(payload, dict) or set(payload) != {"fields", "natural_key", "version"}:
        _fail_inventory()
    version = payload.get("version")
    natural_key = payload.get("natural_key")
    fields = payload.get("fields")
    if (
        type(version) is not int
        or version < 1
        or not isinstance(natural_key, list)
        or any(not isinstance(item, str) for item in natural_key)
        or not isinstance(fields, dict)
        or any(not isinstance(name, str) for name in fields)
    ):
        _fail_inventory()
    return structured_collections.ItemSchema(
        version=version,
        fields={name: _field_spec_from_value(spec) for name, spec in fields.items()},
        natural_key=tuple(natural_key),
    )


def _record_json_value(value: object) -> object:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if type(value) is float:
        if not math.isfinite(value):
            _fail_inventory()
        return value
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, list):
        return [_record_json_value(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            _fail_inventory()
        return {
            unicodedata.normalize("NFC", key): _record_json_value(item)
            for key, item in value.items()
        }
    _fail_inventory()


def _record_canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _record_json_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        _fail_inventory()


def _record_storage_components(
    manifest: structured_collections.CollectionManifest,
) -> tuple[str, ...]:
    paths = [_normalized_path(manifest.storage.source)]
    if manifest.storage.strategy == "markdown-log":
        archive = manifest.storage.descriptor.get("archive")
        if archive is not None:
            if not isinstance(archive, str) or not archive:
                _fail_inventory()
            if archive.startswith(f"{kb_dirname()}/"):
                resolved = archive
            else:
                resolved = (PurePosixPath(manifest.path).parent / archive).as_posix()
            normalized = _normalized_path(resolved)
            if _records_relative(normalized) is None:
                _fail_inventory()
            paths.append(normalized)
    components = tuple(sorted(paths))
    if len({value.casefold() for value in components}) != len(components):
        _fail_inventory()
    return components


def _validate_records_inventory(
    objects: tuple[InventoryObject, ...],
) -> tuple[InventoryObject, ...]:
    manifests = tuple(item for item in objects if item.object_kind == "record_manifest")
    for manifest in manifests:
        if (
            _records_relative(manifest.path) is None
            or PurePosixPath(manifest.path).name != "_collection.md"
            or manifest.record_collection_id is None
            or manifest.record_schema_version is None
            or manifest.record_storage_path is None
            or manifest.record_storage_strategy is None
            or not manifest.record_storage_components
            or manifest.record_schema_contract_jcs is None
            or manifest.record_values_jcs is not None
            or manifest.lifecycle is None
        ):
            _fail_inventory()
    manifest_ids = [item.record_collection_id for item in manifests]
    if len(manifest_ids) != len(set(manifest_ids)):
        _fail_inventory()

    normalized: list[InventoryObject] = []
    for item in objects:
        records_relative = _records_relative(item.path)
        if records_relative is None:
            if item.object_kind in {"record_item", "record_manifest"} or any(
                value is not None
                for value in (
                    item.record_collection_id,
                    item.record_schema_version,
                    item.record_storage_path,
                    item.record_storage_strategy,
                    *item.record_storage_components,
                    item.record_audit_head,
                    item.record_schema_contract_jcs,
                    item.record_values_jcs,
                )
            ):
                _fail_inventory()
            normalized.append(item)
            continue
        if item.object_kind == "record_manifest":
            normalized.append(item)
            continue

        owners: list[InventoryObject] = []
        for manifest in manifests:
            assert manifest.record_storage_path is not None
            assert manifest.record_storage_strategy is not None
            storage_key = manifest.record_storage_path.casefold()
            item_key = item.path.casefold()
            if manifest.record_storage_strategy == "markdown-items":
                if item_key.startswith(f"{storage_key.rstrip('/')}/"):
                    owners.append(manifest)
            elif item_key in {
                component.casefold() for component in manifest.record_storage_components
            }:
                owners.append(manifest)
        if len(owners) != 1:
            _fail_inventory()
        owner = owners[0]
        assert owner.record_collection_id is not None
        assert owner.record_schema_version is not None
        assert owner.record_schema_contract_jcs is not None
        assert owner.record_storage_strategy is not None
        if owner.record_storage_strategy == "markdown-items":
            if (
                item.object_kind != "record_item"
                or item.record_collection_id != owner.record_collection_id
                or item.record_schema_version != owner.record_schema_version
                or item.record_storage_path is not None
                or item.record_storage_strategy is not None
                or item.record_storage_components
                or item.record_schema_contract_jcs is not None
                or item.record_values_jcs is None
            ):
                _fail_inventory()
            schema = _schema_from_contract(owner.record_schema_contract_jcs)
            if schema.version != owner.record_schema_version:
                _fail_inventory()
            reserved_fields = {
                "ambiguous",
                "collection_id",
                "inferred",
                "item_version",
                "parent_record_id",
                "record_id",
            }
            if reserved_fields.intersection(schema.fields):
                _fail_inventory()
            try:
                record_values = json.loads(item.record_values_jcs)
                schema.validate(record_values)
            except (TypeError, ValueError, structured_collections.CollectionError):
                _fail_inventory()
        else:
            if (
                item.object_kind not in {"content", "record_item"}
                or item.record_collection_id not in {None, owner.record_collection_id}
                or item.record_schema_version not in {None, owner.record_schema_version}
                or item.record_storage_path is not None
                or item.record_storage_strategy is not None
                or item.record_storage_components
                or item.record_audit_head is not None
                or item.record_schema_contract_jcs is not None
                or item.record_values_jcs is not None
            ):
                _fail_inventory()
        normalized.append(
            _validate_object(
                replace(
                    item,
                    object_kind="record_item",
                    record_collection_id=owner.record_collection_id,
                    record_schema_version=owner.record_schema_version,
                )
            )
        )
    return tuple(normalized)


def build_inventory(
    objects: tuple[InventoryObject, ...], *, role: str, snapshot_digest: str
) -> ReconciliationInventory:
    """Canonicalize a trusted, already-acquired object inventory."""

    if not isinstance(objects, tuple) or len(objects) > _MAX_OBJECTS:
        _fail_inventory()
    if role not in {"source", "destination"}:
        _fail_inventory()
    snapshot = _digest(snapshot_digest)
    normalized = tuple(
        sorted(
            (_validate_object(item) for item in objects),
            key=lambda item: (item.path, item.object_ref),
        )
    )
    normalized = _validate_records_inventory(normalized)
    if len({item.object_ref for item in normalized}) != len(normalized):
        _fail_inventory()
    value = {
        "schema": RECONCILIATION_INVENTORY_SCHEMA,
        "role": role,
        "snapshot_digest": snapshot,
        "objects": [_object_value(item) for item in normalized],
    }
    return ReconciliationInventory(
        schema=RECONCILIATION_INVENTORY_SCHEMA,
        role=role,
        snapshot_digest=snapshot,
        objects=normalized,
        digest=_framed_digest(_INVENTORY_DOMAIN, value),
    )


def _clean_link_target(
    target: str,
    *,
    current_path: str | None = None,
    markdown_default: bool = True,
) -> tuple[str | None, str | None]:
    cleaned = target.strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2]
    cleaned = cleaned.split("|", 1)[0].strip()
    path, separator, anchor = cleaned.partition("#")
    path = path.strip().strip("/")
    if not path:
        return current_path, anchor.strip() if separator and anchor.strip() else None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path):
        return None, None
    if markdown_default:
        path = path.removesuffix(".md") + ".md"
    if not path.startswith(f"{kb_dirname()}/"):
        path = f"{kb_dirname()}/{path}"
    try:
        return _normalized_path(path), anchor.strip() if separator and anchor.strip() else None
    except InventoryInvalid:
        return None, anchor.strip() if separator and anchor.strip() else None


def _logical_markdown_digest(text: str, frontmatter: Mapping[object, object]) -> str:
    logical = text.replace("\r\n", "\n").replace("\r", "\n")
    if "exomem_id" not in frontmatter:
        return hashlib.sha256(logical.encode("utf-8")).hexdigest()
    lines = logical.splitlines(keepends=True)
    end = next(
        (index for index in range(1, len(lines)) if lines[index].rstrip("\n") == "---"), None
    )
    if end is None:
        _fail_inventory()
    matches = [
        index
        for index, line in enumerate(lines[1:end], start=1)
        if re.match(r"^exomem_id\s*:", line) is not None
    ]
    if len(matches) != 1:
        _fail_inventory()
    del lines[matches[0]]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _kind_for_path(path: str) -> str:
    folded = tuple(part.casefold() for part in PurePosixPath(path).parts)
    name = folded[-1]
    if "_governance" in folded:
        return "policy"
    if "_consolidation" in folded:
        return "run_state"
    if name == "_access.yaml":
        return "access_control"
    if name == ".review-state.json":
        return "review_state"
    if "sources" in folded:
        return "source"
    if "evidence" in folded:
        return "evidence"
    if "records" in folded:
        return "record_manifest" if name == "_collection.md" else "content"
    return "content"


def _dependency_ref(
    kind: str,
    *,
    target_path: str | None,
    target_identity: str | None,
    target_anchor: str | None,
    relation_type: str | None,
    occurrence: str,
) -> str:
    return hashlib.sha256(
        (
            f"{kind}\0{target_path or ''}\0{target_identity or ''}\0"
            f"{target_anchor or ''}\0{relation_type or ''}\0{occurrence}"
        ).encode()
    ).hexdigest()


def _frontmatter_strings(frontmatter: Mapping[object, object], key: str) -> tuple[str, ...]:
    raw = frontmatter.get(key)
    if raw is None:
        return ()
    values = (raw,) if isinstance(raw, str) else tuple(raw) if isinstance(raw, list) else None
    if values is None or any(not isinstance(value, str) or not value.strip() for value in values):
        _fail_inventory()
    return tuple(value.strip() for value in values)


def _frontmatter_dependencies(
    frontmatter: Mapping[object, object], *, current_path: str
) -> tuple[ObjectDependency, ...]:
    collected: dict[str, ObjectDependency] = {}

    def add_path(
        kind: str,
        target: str,
        *,
        key: str,
        index: int,
        relation_type: str | None = None,
    ) -> None:
        target_path, target_anchor = _clean_link_target(
            target,
            current_path=current_path,
            markdown_default=kind != "media_pair",
        )
        if target_path is None:
            return
        dependency_ref = _dependency_ref(
            kind,
            target_path=target_path,
            target_identity=None,
            target_anchor=target_anchor,
            relation_type=relation_type,
            occurrence=f"frontmatter:{key}:{index}",
        )
        collected[dependency_ref] = ObjectDependency(
            dependency_ref=dependency_ref,
            dependency_kind=kind,
            target_path=target_path,
            target_anchor=target_anchor,
            relation_type=relation_type,
        )

    for key in ("sources", "citations", "evidence", "evidences", "evidence_paths"):
        for index, target in enumerate(_frontmatter_strings(frontmatter, key)):
            add_path("citation", target, key=key, index=index)
    for index, target in enumerate(_frontmatter_strings(frontmatter, "ingested_into")):
        add_path("reverse_citation", target, key="ingested_into", index=index)
    for key in ("supersedes", "superseded_by"):
        for index, target in enumerate(_frontmatter_strings(frontmatter, key)):
            add_path(
                "supersession",
                target,
                key=key,
                index=index,
                relation_type=key,
            )
    for key in ("history", "previous_versions"):
        for index, target in enumerate(_frontmatter_strings(frontmatter, key)):
            add_path("history", target, key=key, index=index)
    for key in ("evidence_file", "media_file", "media_path"):
        for index, target in enumerate(_frontmatter_strings(frontmatter, key)):
            add_path("media_pair", target, key=key, index=index)

    raw_type = frontmatter.get("type")
    record_type = raw_type.casefold() if isinstance(raw_type, str) else ""
    collection_id = memory_refs.normalize_id(frontmatter.get("collection_id"))
    if frontmatter.get("collection_id") is not None and collection_id is None:
        _fail_inventory()
    if collection_id is not None:
        dependency_ref = _dependency_ref(
            "record_identity",
            target_path=None,
            target_identity=collection_id,
            target_anchor=None,
            relation_type="collection" if record_type == "record" else None,
            occurrence="frontmatter:collection_id:0",
        )
        collected[dependency_ref] = ObjectDependency(
            dependency_ref=dependency_ref,
            dependency_kind="record_identity",
            target_identity=collection_id,
            relation_type="collection" if record_type == "record" else None,
        )
    parent_record_id = memory_refs.normalize_id(frontmatter.get("parent_record_id"))
    if frontmatter.get("parent_record_id") is not None and parent_record_id is None:
        _fail_inventory()
    if parent_record_id is not None:
        if collection_id is None:
            _fail_inventory()
        parent_identity = f"record:{collection_id}:{parent_record_id}"
        dependency_ref = _dependency_ref(
            "record_identity",
            target_path=None,
            target_identity=parent_identity,
            target_anchor=None,
            relation_type="parent",
            occurrence="frontmatter:parent_record_id:0",
        )
        collected[dependency_ref] = ObjectDependency(
            dependency_ref=dependency_ref,
            dependency_kind="record_identity",
            target_identity=parent_identity,
            relation_type="parent",
        )
    return tuple(sorted(collected.values(), key=lambda item: item.dependency_ref))


def _markdown_dependencies(body: str, *, current_path: str) -> tuple[ObjectDependency, ...]:
    collected: dict[tuple[str, str, str | None, str | None], ObjectDependency] = {}

    def add(kind: str, target: str, *, line: int, relation_type: str | None = None) -> None:
        target_path, target_anchor = _clean_link_target(target, current_path=current_path)
        if target_path is None:
            return
        key = (kind, target_path, target_anchor, relation_type)
        collected.setdefault(
            key,
            ObjectDependency(
                dependency_ref=_dependency_ref(
                    kind,
                    target_path=target_path,
                    target_identity=None,
                    target_anchor=target_anchor,
                    relation_type=relation_type,
                    occurrence=f"body:{line}",
                ),
                dependency_kind=kind,
                target_path=target_path,
                target_anchor=target_anchor,
                relation_type=relation_type,
            ),
        )

    relation_document = markdown_relations.parse_markdown_relations(
        body,
        include_legacy=True,
        relation_types=relation_registry.core_registry().keys,
        retain_unknown=True,
    )
    relation_lines = {item.line for item in relation_document.relations}
    for item in relation_document.relations:
        kind = "supersession" if item.kind in {"supersedes", "superseded_by"} else "typed_relation"
        add(kind, item.target, line=item.line, relation_type=item.kind)
    for match in vault.find_body_wikilinks(body):
        line = body.count("\n", 0, match.start()) + 1
        if line not in relation_lines:
            add("wikilink", match.group(0), line=line)
    return tuple(sorted(collected.values(), key=lambda item: item.dependency_ref))


def inventory_object_from_bytes(
    *,
    object_ref: str,
    path: str,
    content: bytes,
    bundle_sha256: str | None = None,
    object_kind: str | None = None,
    dependencies: tuple[ObjectDependency, ...] = (),
) -> InventoryObject:
    """Build one detached inventory row from bytes using canonical Markdown parsers."""

    if not isinstance(content, bytes) or len(content) > _MAX_CONTENT_BYTES:
        _fail_inventory()
    normalized_path = _normalized_path(path)
    content_digest = hashlib.sha256(content).hexdigest()
    identity: str | None = None
    logical: str | None = None
    anchors: tuple[str, ...] = ()
    parsed_dependencies: tuple[ObjectDependency, ...] = ()
    lifecycle: str | None = None
    record_collection_id: str | None = None
    record_schema_version: int | None = None
    record_storage_path: str | None = None
    record_storage_strategy: str | None = None
    record_storage_components: tuple[str, ...] = ()
    record_audit_head: str | None = None
    record_schema_contract_jcs: str | None = None
    record_values_jcs: str | None = None
    kind = _kind_for_path(normalized_path)
    if normalized_path.casefold().endswith(".md"):
        try:
            source = content.decode("utf-8")
            frontmatter, body, _raw = vault.parse_frontmatter(source, strict=True)
        except (UnicodeDecodeError, vault.FrontmatterError):
            _fail_inventory()
        raw_identity = frontmatter.get("exomem_id")
        if raw_identity is not None:
            identity = memory_refs.normalize_id(raw_identity)
            if identity is None:
                _fail_inventory()
        logical = _logical_markdown_digest(source, frontmatter)
        try:
            document = semantic_units.parse_semantic_units(
                body,
                path=normalized_path,
                parent_ref=memory_refs.memory_ref(identity) if identity else None,
                validate=True,
                relation_registry=relation_registry.core_registry(),
                include_legacy_relations=True,
                retain_unknown_relations=True,
            )
        except (TypeError, ValueError):
            _fail_inventory()
        if document.errors or document.semantic_block_errors or document.note_relation_errors:
            _fail_inventory()
        anchors = tuple(sorted({unit.anchor for unit in document.units if unit.anchor is not None}))
        raw_type = frontmatter.get("type")
        record_id = memory_refs.normalize_id(frontmatter.get("record_id"))
        collection_id = memory_refs.normalize_id(frontmatter.get("collection_id"))
        records_relative = _records_relative(normalized_path)
        if records_relative is not None and PurePosixPath(normalized_path).name == "_collection.md":
            try:
                manifest = structured_collections.parse_manifest_bytes(
                    Path("/"), Path("/") / normalized_path, content
                )
            except structured_collections.CollectionError:
                _fail_inventory()
            try:
                record_formats.validate_storage_contract(manifest)
            except structured_collections.CollectionError:
                _fail_inventory()
            identity = manifest.collection_id
            lifecycle = manifest.lifecycle
            record_collection_id = manifest.collection_id
            record_schema_version = manifest.schema.version
            record_storage_path = manifest.storage.source
            record_storage_strategy = manifest.storage.strategy
            record_storage_components = _record_storage_components(manifest)
            record_audit_head = manifest.audit_head
            record_schema_contract_jcs = _record_canonical_json(
                _schema_contract_value(manifest.schema)
            )
            kind = "record_manifest"
        elif isinstance(raw_type, str) and raw_type.casefold() == "record":
            if record_id is None or collection_id is None:
                _fail_inventory()
            schema_version = frontmatter.get("schema_version")
            if type(schema_version) is not int or schema_version < 1:
                _fail_inventory()
            identity = f"record:{collection_id}:{record_id}"
            record_collection_id = collection_id
            record_schema_version = schema_version
            record_values = {
                key: _record_json_value(value)
                for key, value in frontmatter.items()
                if key not in {"type", "collection_id", "record_id", "schema_version"}
            }
            record_values_jcs = _record_canonical_json(record_values)
            kind = "record_item"
        lifecycle = lifecycle or frontmatter.get("lifecycle")
        if lifecycle is not None and not isinstance(lifecycle, str):
            _fail_inventory()
        parsed_dependencies = (
            *_markdown_dependencies(body, current_path=normalized_path),
            *_frontmatter_dependencies(frontmatter, current_path=normalized_path),
        )
    if object_kind is not None:
        if _records_relative(normalized_path) is not None and object_kind != kind:
            _fail_inventory()
        kind = object_kind
    item = InventoryObject(
        object_ref=object_ref,
        path=normalized_path,
        entry_type="file",
        size=len(content),
        sha256=content_digest,
        bundle_sha256=bundle_sha256 or content_digest,
        durable_identity=identity,
        logical_content_sha256=logical,
        object_kind=kind,
        append_only=bool(vault.in_append_only_tree(normalized_path)),
        lifecycle=lifecycle,
        record_collection_id=record_collection_id,
        record_schema_version=record_schema_version,
        record_storage_path=record_storage_path,
        record_storage_strategy=record_storage_strategy,
        record_storage_components=record_storage_components,
        record_audit_head=record_audit_head,
        record_schema_contract_jcs=record_schema_contract_jcs,
        record_values_jcs=record_values_jcs,
        anchors=anchors,
        dependencies=tuple(parsed_dependencies) + tuple(dependencies),
    )
    return _validate_object(item)


def _is_authority(item: InventoryObject) -> bool:
    if item.object_kind in _AUTHORITY_KINDS:
        return True
    folded = tuple(part.casefold() for part in PurePosixPath(item.path).parts)
    name = folded[-1]
    return bool(
        "_governance" in folded
        or "_consolidation" in folded
        or name in {"_access.yaml", ".review-state.json"}
        or name.startswith(".governance.sqlite")
        or "receipt" in name
    )


@dataclass(frozen=True, slots=True)
class _Indexes:
    by_path: Mapping[str, tuple[InventoryObject, ...]]
    by_identity: Mapping[str, tuple[InventoryObject, ...]]
    by_logical: Mapping[str, tuple[InventoryObject, ...]]
    by_ref: Mapping[str, InventoryObject]


def _indexes(objects: tuple[InventoryObject, ...]) -> _Indexes:
    by_path: defaultdict[str, list[InventoryObject]] = defaultdict(list)
    by_identity: defaultdict[str, list[InventoryObject]] = defaultdict(list)
    by_logical: defaultdict[str, list[InventoryObject]] = defaultdict(list)
    by_ref: dict[str, InventoryObject] = {}
    for item in objects:
        by_path[item.path.casefold()].append(item)
        if item.durable_identity is not None:
            by_identity[item.durable_identity].append(item)
        if item.logical_content_sha256 is not None:
            by_logical[item.logical_content_sha256].append(item)
        by_ref[item.object_ref] = item

    def ordered(values: list[InventoryObject]) -> tuple[InventoryObject, ...]:
        return tuple(sorted(values, key=lambda item: (item.path, item.object_ref)))

    return _Indexes(
        by_path={key: ordered(values) for key, values in by_path.items()},
        by_identity={key: ordered(values) for key, values in by_identity.items()},
        by_logical={key: ordered(values) for key, values in by_logical.items()},
        by_ref=by_ref,
    )


def _validate_inventory_pair(
    source: ReconciliationInventory, destination: ReconciliationInventory
) -> None:
    if (
        not isinstance(source, ReconciliationInventory)
        or not isinstance(destination, ReconciliationInventory)
        or source.schema != RECONCILIATION_INVENTORY_SCHEMA
        or destination.schema != RECONCILIATION_INVENTORY_SCHEMA
        or source.role != "source"
        or destination.role != "destination"
    ):
        _fail_inventory()
    for inventory in (source, destination):
        rebuilt = build_inventory(
            inventory.objects,
            role=inventory.role,
            snapshot_digest=inventory.snapshot_digest,
        )
        if rebuilt != inventory:
            _fail_inventory()
        folded = [item.path.casefold() for item in inventory.objects]
        if len(set(folded)) != len(folded):
            _fail_inventory()


def _direct_class(
    item: InventoryObject, destination: _Indexes
) -> tuple[str, tuple[InventoryObject, ...]]:
    path_matches = destination.by_path.get(item.path.casefold(), ())
    identity_matches = (
        destination.by_identity.get(item.durable_identity, ())
        if item.durable_identity is not None
        else ()
    )
    logical_matches = (
        destination.by_logical.get(item.logical_content_sha256, ())
        if item.logical_content_sha256 is not None
        else ()
    )
    if _is_authority(item):
        return "C8", tuple(
            sorted(set(path_matches + identity_matches), key=lambda row: row.object_ref)
        )
    divergent_identity = tuple(
        row
        for row in identity_matches
        if row.bundle_sha256 != item.bundle_sha256 or row.sha256 != item.sha256
    )
    if divergent_identity:
        return "C6", divergent_identity
    divergent_path = tuple(
        row
        for row in path_matches
        if row.bundle_sha256 != item.bundle_sha256 or row.sha256 != item.sha256
    )
    if divergent_path:
        return "C5", divergent_path
    divergent_logical = tuple(
        row
        for row in logical_matches
        if row.durable_identity != item.durable_identity
        or row.durable_identity is None
        or item.durable_identity is None
    )
    if divergent_logical:
        return "C4", divergent_logical
    relocated = tuple(
        row
        for row in identity_matches
        if row.bundle_sha256 == item.bundle_sha256
        and row.sha256 == item.sha256
        and row.path.casefold() != item.path.casefold()
    )
    if relocated:
        return "C3", relocated
    exact = tuple(
        row
        for row in path_matches
        if row.bundle_sha256 == item.bundle_sha256 and row.sha256 == item.sha256
    )
    if exact:
        return "C1", exact
    return "C2", ()


def _candidate_objects(
    dependency: ObjectDependency,
    source: _Indexes,
    destination: _Indexes,
) -> tuple[InventoryObject, ...]:
    candidates: dict[tuple[str, str], InventoryObject] = {}
    if dependency.target_identity is not None:
        for role, index in (("source", source), ("destination", destination)):
            for item in index.by_identity.get(dependency.target_identity, ()):
                candidates[(role, item.object_ref)] = item
    if dependency.target_path is not None:
        key = dependency.target_path.casefold()
        direct = [
            *(("source", item) for item in source.by_path.get(key, ())),
            *(("destination", item) for item in destination.by_path.get(key, ())),
        ]
        if not direct:
            stem = PurePosixPath(dependency.target_path).stem.casefold()
            for role, index in (("source", source), ("destination", destination)):
                for item in index.by_ref.values():
                    if PurePosixPath(item.path).stem.casefold() == stem:
                        direct.append((role, item))
        for role, item in direct:
            candidates[(role, item.object_ref)] = item
    return tuple(sorted(candidates.values(), key=lambda item: (item.path, item.object_ref)))


def _dependency_findings(
    item: InventoryObject,
    source: _Indexes,
    destination: _Indexes,
    direct_classes: Mapping[str, str],
) -> tuple[DependencyFinding, ...]:
    findings: list[DependencyFinding] = []
    for dependency in item.dependencies:
        candidates = _candidate_objects(dependency, source, destination)
        candidate_refs = tuple(sorted({candidate.object_ref for candidate in candidates}))
        if not candidates:
            code = "DEPENDENCY_TARGET_MISSING"
        else:
            logical_targets = {
                candidate.durable_identity or candidate.path.casefold() for candidate in candidates
            }
            if len(logical_targets) > 1:
                code = "DEPENDENCY_TARGET_AMBIGUOUS"
            elif dependency.target_anchor is not None:
                anchor_owners = [
                    candidate
                    for candidate in candidates
                    if dependency.target_anchor in candidate.anchors
                ]
                if not anchor_owners:
                    code = "DEPENDENCY_ANCHOR_MISSING"
                elif len(anchor_owners) > 1:
                    code = "DEPENDENCY_ANCHOR_AMBIGUOUS"
                else:
                    code = ""
            else:
                code = ""
            if not code:
                source_candidates = [
                    candidate for candidate in candidates if candidate.object_ref in direct_classes
                ]
                if any(
                    direct_classes[candidate.object_ref] in {"C4", "C5", "C6", "C7", "C8"}
                    for candidate in source_candidates
                ):
                    code = "DEPENDENCY_TARGET_UNRESOLVED"
        if code:
            findings.append(
                DependencyFinding(
                    code=code,
                    dependency_ref=dependency.dependency_ref,
                    dependency_kind=dependency.dependency_kind,
                    candidate_object_refs=candidate_refs,
                )
            )
    return tuple(sorted(findings, key=lambda finding: (finding.dependency_ref, finding.code)))


def _aggregate_record_container(item: InventoryObject) -> bool:
    return (
        item.object_kind == "record_item"
        and item.record_collection_id is not None
        and item.record_values_jcs is None
    )


def _allowed_resolutions(primary_class: str, *, item: InventoryObject) -> tuple[str, ...]:
    if primary_class == "C1":
        return ("deduplicate_exact",)
    if primary_class == "C2":
        return ("add",)
    if primary_class == "C3":
        return ("reuse_destination",)
    if primary_class == "C4":
        if item.append_only or _aggregate_record_container(item):
            return ("relocate_preserving_bytes",)
        return (
            "keep_both_reidentify_source",
            "supersede_destination",
            "use_destination_identity",
        )
    if primary_class == "C5":
        return (
            ("relocate_preserving_bytes",)
            if item.append_only or _aggregate_record_container(item)
            else ("relocate_preserving_bytes", "replace_destination_exact")
        )
    if primary_class == "C6":
        if _aggregate_record_container(item):
            return ("relocate_preserving_bytes",)
        return (
            ("retain_provenance_only",)
            if item.append_only
            else ("reidentify_and_relocate", "replace_destination_exact")
        )
    if primary_class == "C7":
        return ("map_dependencies",)
    if primary_class == "C8":
        return ("retain_provenance_only",)
    _fail_inventory()


def _finding_value(finding: DependencyFinding) -> dict[str, object]:
    return {
        "code": finding.code,
        "dependency_ref": finding.dependency_ref,
        "dependency_kind": finding.dependency_kind,
        "candidate_object_refs": list(finding.candidate_object_refs),
    }


def _row_value(row: ReconciliationRow, *, include_digest: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "source_object_ref": row.source_object_ref,
        "source_path": row.source_path,
        "source_object_digest": row.source_object_digest,
        "destination_object_refs": list(row.destination_object_refs),
        "destination_match_digest": row.destination_match_digest,
        "primary_class": row.primary_class,
        "dependency_findings": [_finding_value(item) for item in row.dependency_findings],
        "allowed_resolutions": list(row.allowed_resolutions),
        "default_action": row.default_action,
    }
    if include_digest:
        value["row_digest"] = row.row_digest
    return value


def reconcile_inventories(
    source: ReconciliationInventory, destination: ReconciliationInventory
) -> ReconciliationResult:
    """Assign exactly one C1-C8 class to every authenticated source object."""

    _validate_inventory_pair(source, destination)
    source_indexes = _indexes(source.objects)
    destination_indexes = _indexes(destination.objects)
    direct: dict[str, tuple[str, tuple[InventoryObject, ...]]] = {
        item.object_ref: _direct_class(item, destination_indexes) for item in source.objects
    }
    classes = {ref: value[0] for ref, value in direct.items()}
    # Dependency conflict propagation is monotone: only C2 can become C7.
    findings_by_ref: dict[str, tuple[DependencyFinding, ...]] = {}
    for _attempt in range(len(source.objects) + 1):
        changed = False
        for item in source.objects:
            findings = _dependency_findings(item, source_indexes, destination_indexes, classes)
            findings_by_ref[item.object_ref] = findings
            if classes[item.object_ref] == "C2" and findings:
                classes[item.object_ref] = "C7"
                changed = True
        if not changed:
            break
    else:  # pragma: no cover - monotone C2->C7 cannot exceed one pass per row
        _fail_inventory()

    rows: list[ReconciliationRow] = []
    for item in source.objects:
        primary = classes[item.object_ref]
        matches = direct[item.object_ref][1]
        source_object_digest = _framed_digest(_OBJECT_DOMAIN, _object_value(item))
        destination_refs = tuple(sorted({match.object_ref for match in matches}))
        destination_match_digest = _framed_digest(
            _MATCH_DOMAIN,
            {
                "destination_objects": [
                    _object_value(destination_indexes.by_ref[ref]) for ref in destination_refs
                ]
            },
        )
        partial = ReconciliationRow(
            source_object_ref=item.object_ref,
            source_path=item.path,
            source_object_digest=source_object_digest,
            destination_object_refs=destination_refs,
            destination_match_digest=destination_match_digest,
            primary_class=primary,
            dependency_findings=findings_by_ref.get(item.object_ref, ()),
            allowed_resolutions=_allowed_resolutions(primary, item=item),
            default_action=_DEFAULT_ACTIONS.get(primary),
            row_digest="",
        )
        rows.append(
            ReconciliationRow(
                **{
                    **{
                        field: getattr(partial, field)
                        for field in partial.__dataclass_fields__
                        if field != "row_digest"
                    },
                    "row_digest": _framed_digest(
                        _ROW_DOMAIN, _row_value(partial, include_digest=False)
                    ),
                }
            )
        )
    ordered = tuple(sorted(rows, key=lambda row: (row.source_path, row.source_object_ref)))
    value = {
        "schema": RECONCILIATION_SCHEMA,
        "source_snapshot_digest": source.snapshot_digest,
        "destination_snapshot_digest": destination.snapshot_digest,
        "source_inventory_digest": source.digest,
        "destination_inventory_digest": destination.digest,
        "rows": [_row_value(row) for row in ordered],
    }
    return ReconciliationResult(
        schema=RECONCILIATION_SCHEMA,
        source_snapshot_digest=source.snapshot_digest,
        destination_snapshot_digest=destination.snapshot_digest,
        source_inventory_digest=source.digest,
        destination_inventory_digest=destination.digest,
        source_objects=source.objects,
        destination_objects=destination.objects,
        rows=ordered,
        digest=_framed_digest(_RECONCILIATION_DOMAIN, value),
    )


def _resolution_map(
    resolutions: tuple[OwnerResolution, ...],
) -> dict[str, OwnerResolution]:
    if not isinstance(resolutions, tuple) or len(resolutions) > _MAX_OBJECTS:
        _fail_resolution()
    mapped: dict[str, OwnerResolution] = {}
    for value in resolutions:
        if not isinstance(value, OwnerResolution):
            _fail_resolution()
        if (
            not isinstance(value.source_object_ref, str)
            or not value.source_object_ref
            or len(value.source_object_ref.encode("utf-8")) > 1_024
            or not isinstance(value.action, str)
        ):
            _fail_resolution()
        if value.source_object_ref in mapped:
            _fail_resolution()
        if not isinstance(value.dependency_targets, tuple):
            _fail_resolution()
        if value.destination_object_ref is not None:
            try:
                _bounded_text(value.destination_object_ref, maximum=1_024)
            except InventoryInvalid:
                _fail_resolution()
        mapped[value.source_object_ref] = value
    return mapped


def _matched_destination(
    row: ReconciliationRow,
    destination: Mapping[str, InventoryObject],
    resolution: OwnerResolution | None,
) -> InventoryObject | None:
    if not row.destination_object_refs:
        if resolution is not None and resolution.destination_object_ref is not None:
            _fail_resolution()
        return None
    selected = resolution.destination_object_ref if resolution is not None else None
    if selected is None:
        if len(row.destination_object_refs) != 1:
            _fail_resolution()
        selected = row.destination_object_refs[0]
    if selected not in row.destination_object_refs:
        _fail_resolution()
    try:
        return destination[selected]
    except KeyError:
        _fail_resolution()


def _require_resolution_path(value: str | None) -> str:
    if value is None:
        _fail_resolution()
    try:
        return _normalized_path(value)
    except InventoryInvalid:
        _fail_resolution()


def _require_resolution_identity(value: str | None) -> str:
    if value is None:
        _fail_resolution()
    try:
        resolved = _identifier(value)
    except InventoryInvalid:
        _fail_resolution()
    assert resolved is not None
    return resolved


def _entry_for_row(
    row: ReconciliationRow,
    source: InventoryObject,
    destination: Mapping[str, InventoryObject],
    resolution: OwnerResolution | None,
) -> TentativeEntry:
    action = row.default_action if resolution is None else resolution.action
    if action not in row.allowed_resolutions:
        _fail_resolution()
    matched = _matched_destination(row, destination, resolution)

    def entry(
        *,
        publish: bool,
        target_path: str | None,
        target_identity: str | None,
        target_sha256: str | None,
        target_bundle_sha256: str | None,
        target_object_kind: str | None,
        target_lifecycle: str | None,
    ) -> TentativeEntry:
        return TentativeEntry(
            source_object_ref=source.object_ref,
            source_path=source.path,
            source_identity=source.durable_identity,
            source_sha256=source.sha256,
            primary_class=row.primary_class,
            action=action,
            publish=publish,
            destination_object_ref=matched.object_ref if matched is not None else None,
            destination_path=target_path,
            destination_identity=target_identity,
            destination_sha256=target_sha256,
            source_bundle_sha256=source.bundle_sha256,
            matched_destination_sha256=matched.sha256 if matched is not None else None,
            matched_destination_bundle_sha256=(
                matched.bundle_sha256 if matched is not None else None
            ),
            destination_bundle_sha256=target_bundle_sha256,
            source_object_kind=source.object_kind,
            matched_destination_object_kind=(matched.object_kind if matched is not None else None),
            destination_object_kind=target_object_kind,
            source_lifecycle=source.lifecycle,
            matched_destination_lifecycle=(matched.lifecycle if matched is not None else None),
            destination_lifecycle=target_lifecycle,
        )

    if action == "deduplicate_exact":
        if (
            matched is None
            or matched.bundle_sha256 != source.bundle_sha256
            or matched.object_kind != source.object_kind
            or matched.lifecycle != source.lifecycle
        ):
            _fail_resolution()
        return entry(
            publish=False,
            target_path=matched.path,
            target_identity=matched.durable_identity,
            target_sha256=matched.sha256,
            target_bundle_sha256=matched.bundle_sha256,
            target_object_kind=matched.object_kind,
            target_lifecycle=matched.lifecycle,
        )
    if action == "add" or action == "map_dependencies":
        return entry(
            publish=True,
            target_path=source.path,
            target_identity=source.durable_identity,
            target_sha256=source.sha256,
            target_bundle_sha256=source.bundle_sha256,
            target_object_kind=source.object_kind,
            target_lifecycle=source.lifecycle,
        )
    if action == "reuse_destination" or action == "use_destination_identity":
        if matched is None:
            _fail_resolution()
        if matched.object_kind != source.object_kind or matched.lifecycle != source.lifecycle:
            _fail_resolution()
        return entry(
            publish=False,
            target_path=matched.path,
            target_identity=matched.durable_identity,
            target_sha256=matched.sha256,
            target_bundle_sha256=matched.bundle_sha256,
            target_object_kind=matched.object_kind,
            target_lifecycle=matched.lifecycle,
        )
    if action == "retain_provenance_only":
        return entry(
            publish=False,
            target_path=None,
            target_identity=None,
            target_sha256=None,
            target_bundle_sha256=None,
            target_object_kind=None,
            target_lifecycle=None,
        )
    if action in {"relocate_preserving_bytes", "supersede_destination"}:
        assert resolution is not None
        target_path = _require_resolution_path(resolution.destination_path)
        target_identity = resolution.destination_identity or source.durable_identity
        return entry(
            publish=True,
            target_path=target_path,
            target_identity=target_identity,
            target_sha256=source.sha256,
            target_bundle_sha256=source.bundle_sha256,
            target_object_kind=source.object_kind,
            target_lifecycle=source.lifecycle,
        )
    if action in {"keep_both_reidentify_source", "reidentify_and_relocate"}:
        assert resolution is not None
        return entry(
            publish=True,
            target_path=_require_resolution_path(resolution.destination_path),
            target_identity=_require_resolution_identity(resolution.destination_identity),
            target_sha256=source.sha256,
            target_bundle_sha256=source.bundle_sha256,
            target_object_kind=source.object_kind,
            target_lifecycle=source.lifecycle,
        )
    if action == "replace_destination_exact":
        if (
            matched is None
            or source.append_only
            or source.object_kind != matched.object_kind
            or source.lifecycle != matched.lifecycle
        ):
            _fail_resolution()
        return entry(
            publish=True,
            target_path=matched.path,
            target_identity=source.durable_identity,
            target_sha256=source.sha256,
            target_bundle_sha256=source.bundle_sha256,
            target_object_kind=source.object_kind,
            target_lifecycle=source.lifecycle,
        )
    _fail_resolution()


def _entry_value(entry: TentativeEntry) -> dict[str, object]:
    return {
        "source_object_ref": entry.source_object_ref,
        "source_path": entry.source_path,
        "source_identity": entry.source_identity,
        "source_sha256": entry.source_sha256,
        "primary_class": entry.primary_class,
        "action": entry.action,
        "publish": entry.publish,
        "destination_object_ref": entry.destination_object_ref,
        "destination_path": entry.destination_path,
        "destination_identity": entry.destination_identity,
        "destination_sha256": entry.destination_sha256,
        "source_bundle_sha256": entry.source_bundle_sha256,
        "matched_destination_sha256": entry.matched_destination_sha256,
        "matched_destination_bundle_sha256": entry.matched_destination_bundle_sha256,
        "destination_bundle_sha256": entry.destination_bundle_sha256,
        "source_object_kind": entry.source_object_kind,
        "matched_destination_object_kind": entry.matched_destination_object_kind,
        "destination_object_kind": entry.destination_object_kind,
        "source_lifecycle": entry.source_lifecycle,
        "matched_destination_lifecycle": entry.matched_destination_lifecycle,
        "destination_lifecycle": entry.destination_lifecycle,
    }


def _validate_publication_collisions(
    entries: tuple[TentativeEntry, ...], destination: tuple[InventoryObject, ...]
) -> None:
    replaced_refs = {
        entry.destination_object_ref
        for entry in entries
        if entry.action == "replace_destination_exact" and entry.destination_object_ref is not None
    }
    surviving = tuple(item for item in destination if item.object_ref not in replaced_refs)
    destination_paths = {item.path.casefold(): item for item in surviving}
    destination_identities = {
        item.durable_identity: item for item in surviving if item.durable_identity is not None
    }
    published_paths: dict[str, TentativeEntry] = {}
    published_identities: dict[str, TentativeEntry] = {}
    for entry in entries:
        if not entry.publish:
            continue
        if entry.destination_path is None or entry.destination_sha256 is None:
            _fail_resolution()
        path_key = entry.destination_path.casefold()
        if path_key in published_paths:
            _fail_resolution()
        published_paths[path_key] = entry
        if destination_paths.get(path_key) is not None:
            _fail_resolution()
        if entry.destination_identity is not None:
            if entry.destination_identity in published_identities:
                _fail_resolution()
            published_identities[entry.destination_identity] = entry
            if destination_identities.get(entry.destination_identity) is not None:
                _fail_resolution()


def _explicit_dependency_targets(
    resolution: OwnerResolution | None,
) -> dict[str, DependencyResolution]:
    if resolution is None:
        return {}
    mapped: dict[str, DependencyResolution] = {}
    for item in resolution.dependency_targets:
        if not isinstance(item, DependencyResolution) or item.dependency_ref in mapped:
            _fail_resolution()
        if (
            not isinstance(item.dependency_ref, str)
            or not item.dependency_ref
            or len(item.dependency_ref.encode("utf-8")) > 1_024
        ):
            _fail_resolution()
        if (item.target_source_object_ref is None) == (item.target_destination_object_ref is None):
            _fail_resolution()
        target_ref = item.target_source_object_ref or item.target_destination_object_ref
        if (
            not isinstance(target_ref, str)
            or not target_ref
            or len(target_ref.encode("utf-8")) > 1_024
        ):
            _fail_resolution()
        mapped[item.dependency_ref] = item
    return mapped


def _dependency_map(
    result: ReconciliationResult,
    entries: tuple[TentativeEntry, ...],
    resolutions: Mapping[str, OwnerResolution],
) -> tuple[DependencyMapping, ...]:
    source = {item.object_ref: item for item in result.source_objects}
    entry_by_source = {item.source_object_ref: item for item in entries}
    replaced_refs = {
        item.destination_object_ref
        for item in entries
        if item.action == "replace_destination_exact" and item.destination_object_ref is not None
    }
    final_by_ref: dict[str, _FinalCandidate] = {}
    final_by_identity: defaultdict[str, list[_FinalCandidate]] = defaultdict(list)
    final_by_path: defaultdict[str, list[_FinalCandidate]] = defaultdict(list)

    def index_candidate(
        candidate: _FinalCandidate,
        *,
        alias_paths: tuple[str, ...] = (),
        alias_identities: tuple[str, ...] = (),
    ) -> None:
        existing = final_by_ref.get(candidate.object_ref)
        if existing is not None and existing != candidate:
            _fail_resolution()
        final_by_ref[candidate.object_ref] = candidate
        for path in (candidate.path, *alias_paths):
            if candidate not in final_by_path[path.casefold()]:
                final_by_path[path.casefold()].append(candidate)
        for identity in ((candidate.identity,) if candidate.identity else ()) + alias_identities:
            if candidate not in final_by_identity[identity]:
                final_by_identity[identity].append(candidate)

    for item in result.destination_objects:
        if item.object_ref in replaced_refs:
            continue
        index_candidate(
            _FinalCandidate(item.object_ref, item.path, item.durable_identity, item.anchors)
        )
    for item in entries:
        original = source[item.source_object_ref]
        if item.publish:
            if item.destination_path is None:
                _fail_resolution()
            index_candidate(
                _FinalCandidate(
                    item.source_object_ref,
                    item.destination_path,
                    item.destination_identity,
                    original.anchors,
                ),
                alias_paths=(original.path,),
                alias_identities=(
                    (original.durable_identity,) if original.durable_identity is not None else ()
                ),
            )
        elif item.destination_object_ref is not None:
            candidate = final_by_ref.get(item.destination_object_ref)
            if candidate is None:
                _fail_resolution()
            index_candidate(
                candidate,
                alias_paths=(original.path,),
                alias_identities=(
                    (original.durable_identity,) if original.durable_identity is not None else ()
                ),
            )

    mappings: list[DependencyMapping] = []
    for source_object in result.source_objects:
        resolution = resolutions.get(source_object.object_ref)
        explicit = _explicit_dependency_targets(resolution)
        if set(explicit) - {item.dependency_ref for item in source_object.dependencies}:
            _fail_resolution()
        for dependency in source_object.dependencies:
            candidate: tuple[str, str, str | None, tuple[str, ...]] | None = None
            selected = explicit.get(dependency.dependency_ref)
            if selected is not None:
                if selected.target_source_object_ref is not None:
                    entry = entry_by_source.get(selected.target_source_object_ref)
                    if entry is None:
                        _fail_resolution()
                    final_ref = (
                        entry.source_object_ref if entry.publish else entry.destination_object_ref
                    )
                    candidate = final_by_ref.get(final_ref or "")
                    if candidate is None:
                        _fail_resolution()
                else:
                    candidate = final_by_ref.get(selected.target_destination_object_ref or "")
                    if candidate is None:
                        _fail_resolution()
            else:
                candidates: dict[str, _FinalCandidate] = {}
                if dependency.target_identity is not None:
                    for item in final_by_identity.get(dependency.target_identity, ()):
                        candidates[item.object_ref] = item
                if dependency.target_path is not None:
                    for item in final_by_path.get(dependency.target_path.casefold(), ()):
                        candidates[item.object_ref] = item
                    if not candidates:
                        stem = PurePosixPath(dependency.target_path).stem.casefold()
                        for items in final_by_path.values():
                            for item in items:
                                if PurePosixPath(item.path).stem.casefold() == stem:
                                    candidates[item.object_ref] = item
                if len(candidates) == 1:
                    candidate = next(iter(candidates.values()))
            if candidate is None:
                raise ReconciliationUnresolved(
                    "consolidation dependency remains unresolved"
                ) from None
            if (
                dependency.target_anchor is not None
                and dependency.target_anchor not in candidate.anchors
            ):
                raise ReconciliationUnresolved(
                    "consolidation dependency remains unresolved"
                ) from None
            mappings.append(
                DependencyMapping(
                    source_object_ref=source_object.object_ref,
                    dependency_ref=dependency.dependency_ref,
                    dependency_kind=dependency.dependency_kind,
                    target_object_ref=candidate.object_ref,
                    target_path=candidate.path,
                    target_identity=candidate.identity,
                    target_anchor=dependency.target_anchor,
                )
            )
    return tuple(sorted(mappings, key=lambda item: (item.source_object_ref, item.dependency_ref)))


def _dependency_mapping_value(value: DependencyMapping) -> dict[str, object]:
    return {
        "source_object_ref": value.source_object_ref,
        "dependency_ref": value.dependency_ref,
        "dependency_kind": value.dependency_kind,
        "target_object_ref": value.target_object_ref,
        "target_path": value.target_path,
        "target_identity": value.target_identity,
        "target_anchor": value.target_anchor,
    }


def _require_unique_durable_identities(objects: tuple[InventoryObject, ...]) -> None:
    identities = [item.durable_identity for item in objects if item.durable_identity is not None]
    if len(identities) != len(set(identities)):
        _fail_resolution()


def validate_tentative_map(
    result: ReconciliationResult,
    *,
    resolutions: tuple[OwnerResolution, ...],
) -> TentativeMap:
    """Validate finite owner choices and emit one complete no-loss target map."""

    if not isinstance(result, ReconciliationResult) or result.schema != RECONCILIATION_SCHEMA:
        _fail_resolution()
    try:
        expected = reconcile_inventories(
            build_inventory(
                result.source_objects,
                role="source",
                snapshot_digest=result.source_snapshot_digest,
            ),
            build_inventory(
                result.destination_objects,
                role="destination",
                snapshot_digest=result.destination_snapshot_digest,
            ),
        )
    except InventoryInvalid:
        _fail_resolution()
    if expected != result:
        _fail_resolution()
    source = {item.object_ref: item for item in result.source_objects}
    destination = {item.object_ref: item for item in result.destination_objects}
    _require_unique_durable_identities(result.source_objects)
    _require_unique_durable_identities(result.destination_objects)
    if {row.source_object_ref for row in result.rows} != set(source):
        raise ReconciliationUnresolved("reconciliation inventory coverage is incomplete")
    resolved = _resolution_map(resolutions)
    if set(resolved) - set(source):
        _fail_resolution()
    entries: list[TentativeEntry] = []
    for row in result.rows:
        choice = resolved.get(row.source_object_ref)
        if row.default_action is None and choice is None:
            raise ReconciliationUnresolved(
                "consolidation reconciliation has unresolved conflicts"
            ) from None
        if row.default_action is not None and choice is not None:
            if (
                choice.action != row.default_action
                or choice.destination_path is not None
                or choice.destination_identity is not None
                or (not choice.dependency_targets and choice.destination_object_ref is None)
            ):
                _fail_resolution()
        entries.append(_entry_for_row(row, source[row.source_object_ref], destination, choice))
    ordered_entries = tuple(
        sorted(entries, key=lambda item: (item.source_path, item.source_object_ref))
    )
    _validate_publication_collisions(ordered_entries, result.destination_objects)
    dependency_map = _dependency_map(result, ordered_entries, resolved)
    rows_by_source = {row.source_object_ref: row for row in result.rows}
    c1_rows: list[dict[str, object]] = []
    for item in ordered_entries:
        if item.primary_class != "C1":
            continue
        if item.destination_object_ref is None:
            _fail_resolution()
        destination_object = destination.get(item.destination_object_ref)
        row = rows_by_source.get(item.source_object_ref)
        if destination_object is None or row is None:
            _fail_resolution()
        c1_rows.append(
            {
                "source_object": _object_value(source[item.source_object_ref]),
                "source_object_digest": row.source_object_digest,
                "destination_object": _object_value(destination_object),
                "destination_match_digest": row.destination_match_digest,
                "tentative_entry": _entry_value(item),
            }
        )
    c1_mapping_digest = _framed_digest(
        _C1_MAPPING_DOMAIN,
        {
            "schema": C1_MAPPING_SCHEMA,
            "source_snapshot_digest": result.source_snapshot_digest,
            "destination_snapshot_digest": result.destination_snapshot_digest,
            "source_inventory_digest": result.source_inventory_digest,
            "destination_inventory_digest": result.destination_inventory_digest,
            "reconciliation_digest": result.digest,
            "mappings": c1_rows,
        },
    )
    dependency_map_digest = _framed_digest(
        _DEPENDENCY_SET_DOMAIN,
        {"mappings": [_dependency_mapping_value(item) for item in dependency_map]},
    )
    value = {
        "schema": TENTATIVE_MAP_SCHEMA,
        "reconciliation_digest": result.digest,
        "entries": [_entry_value(item) for item in ordered_entries],
        "dependency_map": [_dependency_mapping_value(item) for item in dependency_map],
        "unresolved_count": 0,
        "c1_mapping_digest": c1_mapping_digest,
        "dependency_map_digest": dependency_map_digest,
    }
    return TentativeMap(
        schema=TENTATIVE_MAP_SCHEMA,
        reconciliation_digest=result.digest,
        entries=ordered_entries,
        dependency_map=dependency_map,
        unresolved_count=0,
        c1_mapping_digest=c1_mapping_digest,
        dependency_map_digest=dependency_map_digest,
        digest=_framed_digest(_TENTATIVE_MAP_DOMAIN, value),
    )
