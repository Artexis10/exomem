"""Profile-neutral structured collection contracts and identity primitives."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import stat
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, unquote

from . import memory_refs, vault

COLLECTION_VERSION = 1
STORAGE_FORMAT_VERSION = 1
RECORD_REF_PREFIX = "exomem://record/"
_SUPPORTED_PROFILES = frozenset({"records", "planning"})
_SUPPORTED_STORAGE = frozenset({"markdown-log", "markdown-items", "dataset"})
_SUPPORTED_FIELD_TYPES = frozenset(
    {
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "enum",
        "array",
        "object",
        "link",
    }
)
_MAX_DISCOVERY_CANDIDATES = 512
_MAX_SCHEMA_FIELDS = 128
_MAX_SCHEMA_DEPTH = 8
_MAX_PATH_BYTES = 1024
_MAX_ITEM_KEY_BYTES = 512


@dataclass(slots=True)
class CollectionError(ValueError):
    code: str
    reason: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")


@dataclass(frozen=True, slots=True)
class CollectionDiagnostic:
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class FieldSpec:
    type: str
    required: bool = False
    enum: tuple[str | int | float | bool, ...] = ()
    items: FieldSpec | None = None
    units: tuple[str, ...] = ()
    link_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ItemSchema:
    version: int
    fields: Mapping[str, FieldSpec]
    natural_key: tuple[str, ...] = ()

    def validate(self, item: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(item, Mapping):
            raise CollectionError("INVALID_ITEM", "item must be an object")
        value = dict(item)
        for name, spec in self.fields.items():
            if spec.required and name not in value:
                raise CollectionError("SCHEMA_REQUIRED_FIELD", f"required field is missing: {name}")
            if name in value:
                _validate_field_value(name, value[name], spec)
        return value


@dataclass(frozen=True, slots=True)
class StorageSpec:
    strategy: str
    source: str
    format_version: int
    descriptor: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    path: str
    default_properties: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class PlanLink:
    reference: str
    query: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CollectionLinks:
    plans: tuple[PlanLink, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionManifest:
    collection_id: str
    title: str
    semantic_profile: str
    collection_version: int
    lifecycle: str
    path: str
    storage: StorageSpec
    schema: ItemSchema
    templates: tuple[TemplateSpec, ...] = ()
    views: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    governance: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    links: CollectionLinks = field(default_factory=CollectionLinks)


@dataclass(frozen=True, slots=True)
class ItemIdentity:
    collection_id: str
    key: str
    inferred: bool = False

    def __post_init__(self) -> None:
        if memory_refs.normalize_id(self.collection_id) is None:
            raise ValueError("collection_id must be a UUID")
        _validate_item_key(self.key)

    def reference(self) -> str:
        return record_ref(self.collection_id, self.key)


@dataclass(frozen=True, slots=True)
class SourceVersion:
    path: str
    hash: str


@dataclass(frozen=True, slots=True)
class SchemaInference:
    fields: Mapping[str, FieldSpec]
    sample_count: int
    provenance: tuple[str, ...]
    advisory: bool = True


@dataclass(frozen=True, slots=True)
class LegacyCollection:
    collection_id: str
    path: str
    inspect_only: bool = True


def load_manifest(vault_root: Path, path: Path | str) -> CollectionManifest:
    """Parse one explicit collection contract without touching canonical items."""
    root = Path(vault_root)
    manifest_path, rel = _safe_existing_path(root, path)
    if manifest_path.name != "_collection.md":
        raise CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest must be named _collection.md"
        )
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        raise CollectionError(
            "COLLECTION_NOT_FOUND", "collection manifest could not be read"
        ) from error
    try:
        frontmatter, _body, marker = vault.parse_frontmatter(text, strict=True)
    except vault.FrontmatterError as error:
        raise CollectionError(error.code, error.reason) from error
    if marker is None:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest requires YAML frontmatter")
    return _manifest_from_frontmatter(root, rel, frontmatter)


def discover_collections(
    vault_root: Path,
    *,
    authorize_path: Callable[[str], bool] | None = None,
    max_candidates: int = _MAX_DISCOVERY_CANDIDATES,
) -> tuple[CollectionManifest, ...]:
    """Discover releasable manifests, authorizing each candidate before parsing it."""
    if (
        type(max_candidates) is not int
        or max_candidates < 1
        or max_candidates > _MAX_DISCOVERY_CANDIDATES
    ):
        raise CollectionError(
            "INVALID_DISCOVERY_LIMIT", "discovery limit is outside supported bounds"
        )
    root = Path(vault_root)
    kb = vault.kb_root(root)
    if not kb.is_dir():
        return ()
    authorize = authorize_path or (lambda _path: True)
    manifests: list[CollectionManifest] = []
    candidates = sorted(kb.rglob("_collection.md"))
    if len(candidates) > max_candidates:
        raise CollectionError(
            "COLLECTION_DISCOVERY_LIMIT", "too many collection manifests to inspect"
        )
    for candidate in candidates:
        safe = _safe_candidate_rel(root, candidate)
        if safe is None:
            continue
        _candidate_path, rel = safe
        if not authorize(rel):
            continue
        manifests.append(load_manifest(root, candidate))
    _raise_duplicate_ids(manifests)
    return tuple(manifests)


def resolve_collection(
    vault_root: Path,
    selector: str | Path,
    *,
    authorize_path: Callable[[str], bool] | None = None,
) -> CollectionManifest:
    """Resolve a collection by manifest path, collection UUID, or memory reference."""
    raw = str(selector).strip()
    if not raw:
        raise CollectionError("INVALID_COLLECTION_REFERENCE", "collection selector is required")
    authorize = authorize_path or (lambda _path: True)
    identity = memory_refs.parse_memory_ref(raw) or memory_refs.normalize_id(raw)
    if identity is not None:
        matches = [
            manifest
            for manifest in discover_collections(vault_root, authorize_path=authorize)
            if manifest.collection_id == identity
        ]
        if not matches:
            raise CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        _raise_duplicate_ids(matches)
        return matches[0]

    root = Path(vault_root)
    path, rel = _safe_existing_path(root, raw)
    if not authorize(rel):
        raise CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    return load_manifest(root, path)


def record_ref(collection_id: str, item_key: str) -> str:
    normalized = memory_refs.normalize_id(collection_id)
    if normalized is None:
        raise ValueError(f"invalid collection id: {collection_id!r}")
    _validate_item_key(item_key)
    return f"{RECORD_REF_PREFIX}{normalized}/{quote(item_key, safe='')}"


def parse_record_ref(value: str) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw.lower().startswith(RECORD_REF_PREFIX):
        return None
    remainder = raw[len(RECORD_REF_PREFIX) :]
    collection_id, separator, encoded_key = remainder.partition("/")
    normalized = memory_refs.normalize_id(collection_id)
    if not separator or normalized is None:
        return None
    key = unquote(encoded_key)
    try:
        _validate_item_key(key)
    except ValueError:
        return None
    return normalized, key


def natural_key_serialization(
    schema_version: int,
    fields_in_order: Iterable[str],
    values: Mapping[str, Any],
    *,
    field_types: Mapping[str, str] | None = None,
) -> str:
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError("schema_version must be a positive integer")
    field_types = field_types or {}
    serialized: list[list[Any]] = []
    for name in fields_in_order:
        if type(name) is not str or not name:
            raise ValueError("natural key field names must be non-empty strings")
        if name not in values:
            raise ValueError(f"natural key field is missing: {name}")
        serialized.append([name, _natural_key_value(values[name], field_types.get(name))])
    return json.dumps([schema_version, serialized], ensure_ascii=False, separators=(",", ":"))


def inferred_item_key(collection_id: str, serialized_natural_key: str) -> str:
    normalized = memory_refs.normalize_id(collection_id)
    if normalized is None:
        raise ValueError(f"invalid collection id: {collection_id!r}")
    return str(uuid.uuid5(uuid.UUID(normalized), serialized_natural_key))


def source_version(path: Path | str) -> SourceVersion:
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as error:
        raise CollectionError("SOURCE_NOT_FOUND", "canonical source could not be read") from error
    return SourceVersion(path=str(file_path), hash=hashlib.sha256(data).hexdigest())


def infer_schema(
    rows: Iterable[Mapping[str, Any]],
    *,
    source_paths: Iterable[Path | str] = (),
    max_rows: int = 128,
) -> SchemaInference:
    if type(max_rows) is not int or max_rows < 1 or max_rows > 1024:
        raise ValueError("max_rows is outside supported bounds")
    samples = list(rows)[:max_rows]
    observed: dict[str, list[Any]] = {}
    for row in samples:
        if not isinstance(row, Mapping):
            raise ValueError("schema inference rows must be objects")
        for name, value in row.items():
            if type(name) is str and name:
                observed.setdefault(name, []).append(value)
    fields = {
        name: FieldSpec(type=_infer_type(values)) for name, values in sorted(observed.items())
    }
    return SchemaInference(
        fields=MappingProxyType(fields),
        sample_count=len(samples),
        provenance=tuple(str(path) for path in source_paths),
    )


def inspect_legacy_tracker(vault_root: Path, path: Path | str) -> LegacyCollection:
    root = Path(vault_root)
    tracker, rel = _safe_existing_path(root, path)
    try:
        frontmatter, _body, _marker = vault.parse_frontmatter(
            tracker.read_text(encoding="utf-8"), strict=True
        )
    except (OSError, vault.FrontmatterError) as error:
        raise CollectionError(
            "INVALID_LEGACY_TRACKER", "legacy tracker could not be read"
        ) from error
    if frontmatter.get("type") not in {"tracker", "record-index"}:
        raise CollectionError("NOT_LEGACY_TRACKER", "path is not a legacy tracker")
    return LegacyCollection(
        collection_id=f"legacy-{uuid.uuid5(uuid.NAMESPACE_URL, rel)}",
        path=rel,
    )


def _manifest_from_frontmatter(
    root: Path, manifest_rel: str, frontmatter: Mapping[str, Any]
) -> CollectionManifest:
    if frontmatter.get("type") != "collection":
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest type must be collection")
    collection_id = memory_refs.normalize_id(frontmatter.get("exomem_id"))
    if collection_id is None:
        raise CollectionError("INVALID_COLLECTION_ID", "manifest requires a UUID exomem_id")
    title = _nonempty_string(frontmatter.get("title"), "title")
    profile = _nonempty_string(frontmatter.get("semantic_profile"), "semantic_profile")
    if profile not in _SUPPORTED_PROFILES:
        raise CollectionError("UNSUPPORTED_COLLECTION_PROFILE", "semantic profile is not supported")
    version = frontmatter.get("collection_version")
    if version != COLLECTION_VERSION:
        raise CollectionError(
            "UNSUPPORTED_COLLECTION_VERSION", "collection version is not supported"
        )
    lifecycle = _nonempty_string(frontmatter.get("lifecycle"), "lifecycle")
    schema_version = frontmatter.get("schema_version")
    if type(schema_version) is not int or schema_version < 1:
        raise CollectionError("INVALID_SCHEMA_VERSION", "schema_version must be a positive integer")
    storage = _parse_storage(root, manifest_rel, frontmatter.get("storage"))
    schema = _parse_schema(schema_version, frontmatter.get("item_schema"))
    templates = _parse_templates(root, manifest_rel, frontmatter.get("templates", []))
    links = _parse_links(frontmatter.get("links", {}))
    return CollectionManifest(
        collection_id=collection_id,
        title=title,
        semantic_profile=profile,
        collection_version=version,
        lifecycle=lifecycle,
        path=manifest_rel,
        storage=storage,
        schema=schema,
        templates=templates,
        views=_mapping(frontmatter.get("views", {}), "views"),
        governance=_mapping(frontmatter.get("governance", {}), "governance"),
        links=links,
    )


def _parse_storage(root: Path, manifest_rel: str, value: object) -> StorageSpec:
    storage = _mapping(value, "storage")
    strategy = _nonempty_string(storage.get("strategy"), "storage.strategy")
    if strategy not in _SUPPORTED_STORAGE:
        raise CollectionError("UNSUPPORTED_STORAGE_STRATEGY", "storage strategy is not supported")
    format_version = storage.get("format_version")
    if format_version != STORAGE_FORMAT_VERSION:
        raise CollectionError(
            "UNSUPPORTED_STORAGE_FORMAT_VERSION", "storage format version is not supported"
        )
    source = _vault_relative_path(root, manifest_rel, storage.get("source"), "storage.source")
    descriptor = {
        key: value
        for key, value in storage.items()
        if key not in {"strategy", "source", "format_version"}
    }
    return StorageSpec(strategy, source, format_version, MappingProxyType(descriptor))


def _parse_schema(version: int, value: object) -> ItemSchema:
    schema = _mapping(value, "item_schema")
    raw_fields = _mapping(schema.get("fields"), "item_schema.fields")
    if not raw_fields or len(raw_fields) > _MAX_SCHEMA_FIELDS:
        raise CollectionError("INVALID_ITEM_SCHEMA", "item schema has an invalid field count")
    fields: dict[str, FieldSpec] = {}
    for name, raw_spec in raw_fields.items():
        if type(name) is not str or not name or len(name.encode("utf-8")) > 128:
            raise CollectionError(
                "INVALID_ITEM_SCHEMA", "item schema contains an invalid field name"
            )
        fields[name] = _parse_field_spec(raw_spec)
    natural_raw = schema.get("natural_key", ())
    if not isinstance(natural_raw, list) or not natural_raw:
        raise CollectionError("INVALID_NATURAL_KEY", "item schema requires a natural_key list")
    natural_key = tuple(natural_raw)
    if len(natural_key) > 16 or any(name not in fields for name in natural_key):
        raise CollectionError("INVALID_NATURAL_KEY", "natural key must name declared fields")
    return ItemSchema(version, MappingProxyType(fields), natural_key)


def _parse_field_spec(value: object, depth: int = 0) -> FieldSpec:
    if depth > _MAX_SCHEMA_DEPTH:
        raise CollectionError("INVALID_ITEM_SCHEMA", "item schema nesting is too deep")
    raw = _mapping(value, "item_schema field")
    kind = _nonempty_string(raw.get("type"), "item_schema field type")
    if kind not in _SUPPORTED_FIELD_TYPES:
        raise CollectionError("UNSUPPORTED_FIELD_TYPE", "item schema field type is not supported")
    required = raw.get("required", False)
    if type(required) is not bool:
        raise CollectionError("INVALID_ITEM_SCHEMA", "required must be boolean")
    enum_raw = raw.get("enum", [])
    if enum_raw is None:
        enum_raw = []
    if not isinstance(enum_raw, list) or len(enum_raw) > 64:
        raise CollectionError("INVALID_ITEM_SCHEMA", "enum values must be bounded")
    enum = tuple(enum_raw)
    if any(type(value) not in {str, int, float, bool} for value in enum):
        raise CollectionError("INVALID_ITEM_SCHEMA", "enum values must be scalar")
    items = _parse_field_spec(raw["items"], depth + 1) if "items" in raw else None
    if kind == "array" and items is None:
        raise CollectionError("INVALID_ITEM_SCHEMA", "array field requires an items schema")
    units_raw = raw.get("units", [])
    if units_raw is None:
        units_raw = []
    if not isinstance(units_raw, list) or any(type(unit) is not str for unit in units_raw):
        raise CollectionError("INVALID_ITEM_SCHEMA", "units must be a string list")
    link_kind = raw.get("link_kind")
    if link_kind is not None and type(link_kind) is not str:
        raise CollectionError("INVALID_ITEM_SCHEMA", "link_kind must be a string")
    return FieldSpec(kind, required, enum, items, tuple(units_raw), link_kind)


def _parse_templates(root: Path, manifest_rel: str, value: object) -> tuple[TemplateSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 32:
        raise CollectionError("INVALID_TEMPLATES", "templates must be a bounded list")
    templates: list[TemplateSpec] = []
    for raw in value:
        template = _mapping(raw, "template")
        templates.append(
            TemplateSpec(
                path=_vault_relative_path(
                    root, manifest_rel, template.get("path"), "template.path"
                ),
                default_properties=_mapping(
                    template.get("default_properties", {}), "template.default_properties"
                ),
            )
        )
    return tuple(templates)


def _parse_links(value: object) -> CollectionLinks:
    links = _mapping(value, "links")
    plans = links.get("plans", [])
    if plans is None:
        return CollectionLinks()
    if not isinstance(plans, list) or len(plans) > 32:
        raise CollectionError("INVALID_COLLECTION_LINKS", "plans must be a bounded list")
    result: list[PlanLink] = []
    for raw in plans:
        link = _mapping(raw, "plan link")
        reference = _nonempty_string(link.get("reference"), "plan link reference")
        query = _mapping(link.get("query"), "plan link query")
        if _nested_size(query) > 128:
            raise CollectionError("INVALID_COLLECTION_LINKS", "plan query is too large")
        result.append(PlanLink(reference, query))
    return CollectionLinks(tuple(result))


def _validate_field_value(name: str, value: Any, spec: FieldSpec) -> None:
    if value is None:
        return
    if spec.type == "string" and type(value) is not str:
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "integer" and (type(value) is not int):
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "number" and type(value) not in {int, float}:
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "boolean" and type(value) is not bool:
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "date":
        _normalize_date(value)
    if spec.type == "datetime":
        _normalize_datetime(value)
    if spec.type == "array":
        if not isinstance(value, list):
            raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
        assert spec.items is not None
        for item in value:
            _validate_field_value(name, item, spec.items)
    if spec.type == "object" and not isinstance(value, Mapping):
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "link" and type(value) is not str:
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.enum and value not in spec.enum:
        raise CollectionError("SCHEMA_ENUM", f"field is outside its enum: {name}")


def _natural_key_value(value: Any, field_type: str | None) -> Any:
    if value is None:
        return None
    if field_type == "date":
        return _normalize_date(value)
    if field_type == "datetime":
        return _normalize_datetime(value)
    if type(value) is str:
        return unicodedata.normalize("NFC", value)
    if type(value) in {int, float, bool}:
        return value
    if type(value) is dt.datetime:
        return _normalize_datetime(value)
    if type(value) is dt.date:
        return _normalize_date(value)
    raise ValueError("natural key values must be JSON scalars")


def _normalize_date(value: Any) -> str:
    if type(value) is dt.datetime:
        raise CollectionError("SCHEMA_FIELD_TYPE", "date field cannot contain a datetime")
    if type(value) is dt.date:
        return value.isoformat()
    if type(value) is not str:
        raise CollectionError("SCHEMA_FIELD_TYPE", "date field must be an ISO date")
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise CollectionError("SCHEMA_FIELD_TYPE", "date field must be an ISO date") from error


def _normalize_datetime(value: Any) -> str:
    if type(value) is dt.datetime:
        return value.isoformat()
    if type(value) is not str:
        raise CollectionError("SCHEMA_FIELD_TYPE", "datetime field must be ISO 8601")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError as error:
        raise CollectionError("SCHEMA_FIELD_TYPE", "datetime field must be ISO 8601") from error


def _infer_type(values: list[Any]) -> str:
    non_null = [value for value in values if value is not None]
    if not non_null:
        return "string"
    if all(type(value) is bool for value in non_null):
        return "boolean"
    if all(type(value) is int for value in non_null):
        return "integer"
    if all(type(value) in {int, float} for value in non_null):
        return "number"
    if all(isinstance(value, list) for value in non_null):
        return "array"
    if all(isinstance(value, Mapping) for value in non_null):
        return "object"
    if all(_is_date(value) for value in non_null):
        return "date"
    if all(_is_datetime(value) for value in non_null):
        return "datetime"
    return "string"


def _is_date(value: Any) -> bool:
    try:
        _normalize_date(value)
    except CollectionError:
        return False
    return True


def _is_datetime(value: Any) -> bool:
    try:
        _normalize_datetime(value)
    except CollectionError:
        return False
    return True


def _safe_existing_path(root: Path, path: Path | str) -> tuple[Path, str]:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    safe = _safe_candidate_rel(root, candidate)
    if safe is None:
        raise CollectionError(
            "INVALID_COLLECTION_PATH", "collection path is outside the governed vault"
        )
    return safe


def _safe_candidate_rel(root: Path, candidate: Path) -> tuple[Path, str] | None:
    try:
        rel = candidate.relative_to(root).as_posix()
    except ValueError:
        return None
    if not rel.startswith(f"{vault.kb_dirname()}/") or _unsafe_relative(rel):
        return None
    current = root
    try:
        for part in Path(rel).parts:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None
        if not stat.S_ISREG(current.stat().st_mode):
            return None
    except OSError:
        return None
    return candidate, rel


def _vault_relative_path(root: Path, manifest_rel: str, value: object, name: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise CollectionError("INVALID_COLLECTION_PATH", f"{name} must be a bounded relative path")
    raw = value.replace("\\", "/")
    if raw.startswith(f"{vault.kb_dirname()}/"):
        rel = raw
    else:
        rel = (Path(manifest_rel).parent / raw).as_posix()
    if _unsafe_relative(rel) or not rel.startswith(f"{vault.kb_dirname()}/"):
        raise CollectionError(
            "INVALID_COLLECTION_PATH", f"{name} must stay under the Knowledge Base"
        )
    target = root / rel
    if target.exists() and not _is_safe_vault_target(root, target):
        raise CollectionError("INVALID_COLLECTION_PATH", f"{name} must not traverse a symlink")
    return rel


def _is_safe_vault_target(root: Path, target: Path) -> bool:
    try:
        rel = target.relative_to(root)
        current = root
        for part in rel.parts:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        return True
    except (OSError, ValueError):
        return False


def _unsafe_relative(value: str) -> bool:
    return (
        not value
        or value.startswith("/")
        or "\x00" in value
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(part in {"", ".", ".."} for part in value.split("/"))
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CollectionError("INVALID_COLLECTION_MANIFEST", f"{name} must be an object")
    if len(value) > _MAX_SCHEMA_FIELDS:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", f"{name} is too large")
    return MappingProxyType(dict(value))


def _nonempty_string(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise CollectionError("INVALID_COLLECTION_MANIFEST", f"{name} must be a non-empty string")
    return value


def _nested_size(value: Mapping[str, Any], depth: int = 0) -> int:
    if depth > _MAX_SCHEMA_DEPTH:
        return _MAX_SCHEMA_FIELDS + 1
    total = 0
    for _key, item in value.items():
        total += 1
        if isinstance(item, Mapping):
            total += _nested_size(item, depth + 1)
        elif isinstance(item, list):
            total += len(item)
    return total


def _validate_item_key(value: object) -> None:
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_ITEM_KEY_BYTES:
        raise ValueError("item key must be a bounded non-empty string")


def _raise_duplicate_ids(manifests: Iterable[CollectionManifest]) -> None:
    seen: dict[str, int] = {}
    for manifest in manifests:
        seen[manifest.collection_id] = seen.get(manifest.collection_id, 0) + 1
    duplicate_count = max(seen.values(), default=0)
    if duplicate_count > 1:
        raise CollectionError(
            "AMBIGUOUS_COLLECTION",
            f"collection identity appears in {duplicate_count} releasable manifests",
        )
