"""Profile-neutral structured collection contracts and identity primitives."""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import math
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
from .collection_profiles import profile_for

COLLECTION_VERSION = 1
STORAGE_FORMAT_VERSION = 1
RECORDS_READER_VERSION = 2
RECORD_REF_PREFIX = "exomem://record/"
PLAN_REF_PREFIX = "exomem://plan/"
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
_PRESENTATION_SCALAR_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "date", "datetime", "enum", "link"}
)
_MAX_DISCOVERY_CANDIDATES = 512
_MAX_SCHEMA_FIELDS = 128
_MAX_SCHEMA_DEPTH = 8
_MAX_FROZEN_VALUES = 256
_MAX_INFERENCE_PROVENANCE = 32
_MAX_PATH_BYTES = 1024
_MAX_ITEM_KEY_BYTES = 512
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_SAVED_VIEWS = 32
_MAX_SAVED_VIEW_NAME_BYTES = 128
_MAX_SAVED_VIEW_FILTERS = 128
_MAX_RECORD_PRESENTATION_FIELDS = 32
_MAX_RECORD_PRESENTATION_TABLES = 8
_MAX_RECORD_PRESENTATION_COLUMNS = 16
_PRESENTATION_SYNTHESIZED_FIELDS = frozenset(
    {"collection_id", "record_id", "plan_id", "item_version", "inferred", "ambiguous", "parent_record_id", "child_field", "child_index"}
)
_SAVED_VIEW_QUERY_KEYS = frozenset(
    {
        "filters",
        "columns",
        "sort_by",
        "descending",
        "aggregate",
        "date_from",
        "date_to",
        "date_column",
        "expand_children",
        "expand_child",
        "limit",
    }
)
_SAVED_VIEW_SHARED_SYSTEM_FIELDS = frozenset(
    {"collection_id", "item_version", "inferred", "ambiguous", "parent_record_id"}
)
_SAVED_VIEW_QUERY_OPS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "icontains",
        "startswith",
        "in",
        "nin",
        "exists",
        "missing",
    }
)
_SAVED_VIEW_AGGREGATES = frozenset({"min", "max", "sum", "avg", "latest", "distinct", "group"})


@dataclass(slots=True)
class CollectionError(ValueError):
    code: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.details = dict(self.details)
        ValueError.__init__(self, f"{self.code}: {self.reason}")


def manifest_authoring_contract() -> dict[str, Any]:
    """Return the deterministic agent-facing contract for collection manifests."""
    field_types = sorted(_SUPPORTED_FIELD_TYPES)
    profiles = sorted(_SUPPORTED_PROFILES)
    storage_strategies = sorted(_SUPPORTED_STORAGE)
    view_operators = sorted(_SAVED_VIEW_QUERY_OPS)
    aggregate_forms = [
        "count",
        "profile",
        *[f"{name}:<field>" for name in sorted(_SAVED_VIEW_AGGREGATES)],
    ]
    field_schema: dict[str, Any] = {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {"enum": field_types},
            "required": {"type": "boolean", "default": False},
            "enum": {
                "type": "array",
                "maxItems": 64,
                "items": {"type": ["string", "integer", "number", "boolean"]},
            },
            "items": {"$ref": "#/$defs/field"},
            "units": {"type": "array", "items": {"type": "string"}},
            "link_kind": {"type": "string"},
        },
        "additionalProperties": True,
    }
    manifest_schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://exomem.dev/contracts/collection-manifest-v1.schema.json",
        "title": "Exomem collection manifest frontmatter",
        "type": "object",
        "required": [
            "type",
            "exomem_id",
            "title",
            "semantic_profile",
            "collection_version",
            "schema_version",
            "lifecycle",
            "storage",
            "item_schema",
        ],
        "properties": {
            "type": {"const": "collection"},
            "exomem_id": {"type": "string", "format": "uuid"},
            "title": {"type": "string", "minLength": 1},
            "semantic_profile": {"enum": profiles},
            "collection_version": {"enum": [COLLECTION_VERSION]},
            "schema_version": {"type": "integer", "minimum": 1},
            "lifecycle": {"type": "string", "minLength": 1, "examples": ["active"]},
            "storage": {
                "type": "object",
                "required": ["strategy", "source", "format_version"],
                "properties": {
                    "strategy": {"enum": storage_strategies},
                    "source": {"type": "string", "minLength": 1},
                    "format_version": {"enum": [STORAGE_FORMAT_VERSION]},
                },
                "additionalProperties": True,
            },
            "item_schema": {
                "type": "object",
                "required": ["natural_key", "fields"],
                "properties": {
                    "natural_key": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {"type": "string"},
                    },
                    "fields": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": _MAX_SCHEMA_FIELDS,
                        "propertyNames": {
                            "not": {"enum": sorted(vault.EXCLUDED_FRONTMATTER_FIELDS)},
                            "description": (
                                "Schema-excluded field names are refused on every "
                                "governed write. Matching ignores case and "
                                "surrounding whitespace, so 'Confidence' and "
                                "' expires_at ' are refused too, and the same names "
                                "are refused as a Markdown-log "
                                "storage.item_heading.note.field."
                            ),
                        },
                        "additionalProperties": {"$ref": "#/$defs/field"},
                    },
                },
                "additionalProperties": False,
            },
            "templates": {"type": "array", "maxItems": 32},
            "views": {"type": "object", "maxProperties": _MAX_SAVED_VIEWS},
            "governance": {"type": "object"},
            "links": {"type": "object"},
            "record_audit": {"type": "object", "readOnly": True},
            "record_presentation": _record_presentation_json_schema(),
        },
        "additionalProperties": True,
        "$defs": {"field": field_schema},
    }
    return {
        "contract_version": COLLECTION_VERSION,
        "manifest_filename": "_collection.md",
        "records_path": f"{vault.kb_dirname()}/Records/",
        "records_action_constraint": {"semantic_profile": "records"},
        "closed_values": {
            "semantic_profile": profiles,
            "collection_version": [COLLECTION_VERSION],
            "storage.strategy": storage_strategies,
            "storage.format_version": [STORAGE_FORMAT_VERSION],
            "item_schema.fields.*.type": field_types,
            "views.*.filters.*.op": view_operators,
            "views.*.aggregate": aggregate_forms,
        },
        "constraints": {
            "lifecycle": {
                "type": "string",
                "min_length": 1,
                "example": "active",
                "closed_enum": False,
            },
            "schema_version": {"type": "integer", "minimum": 1},
            "natural_key": {"declared_fields_only": True, "minimum_items": 1, "maximum_items": 16},
            "records_paths": "manifest and canonical source must stay under Knowledge Base/Records",
        },
        "storage_strategies": {
            "markdown-items": {
                "canonical_source": "directory containing one Markdown file per item",
                "required_storage_fields": ["strategy", "source", "format_version"],
                "append": True,
                "update": True,
            },
            "markdown-log": {
                "canonical_source": "one chronological Markdown log",
                "required_descriptor_fields": ["section", "item_heading", "insertion"],
                "insertion_values": ["newest-first", "oldest-first"],
                "child_rows": {
                    "required_fields": ["prefix", "delimiter", "fields", "container_field"],
                    "container_field": "declared array-of-object item field",
                },
                "append": True,
                "update": True,
            },
            "dataset": {
                "canonical_source": "CSV, TSV, or JSON file",
                "descriptor_fields": ["key", "record_path"],
                "append": False,
                "update": False,
            },
        },
        "record_presentation": {
            "available_when": "semantic_profile=records and storage.strategy=markdown-items",
            "version": 1,
            "sections": ["summary", "tables", "notes", "details"],
            "query": "use expand_child for one declared table; expand_children works only when unambiguous",
            "repair": "direct frontmatter edits are canonical; use guarded update refresh_presentation=true after rebaseline",
        },
        "views": {
            "definition_keys": ["query", "sort", "source_snapshot"],
            "query_keys": sorted(_SAVED_VIEW_QUERY_KEYS),
            "filter_operators": view_operators,
            "aggregate_forms": aggregate_forms,
            "sort_directions": ["asc", "desc"],
            "limit": {"minimum": 1, "maximum": 1_000},
        },
        "json_schema": manifest_schema,
        "examples": _manifest_authoring_examples(),
    }


def _manifest_authoring_examples() -> dict[str, dict[str, Any]]:
    minimal_text = """---
type: collection
exomem_id: 15dd81cd-9ae2-488c-bd9e-14771d86343e
title: Observed events
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on, label]
  fields:
    occurred_on:
      type: date
      required: true
    label:
      type: string
      required: true
    source:
      type: link
---

One ordinary Markdown file per observed event.
"""
    laboratory_text = """---
type: collection
exomem_id: 372ff95a-36f0-4859-9e11-9e487dd94f4b
title: Laboratory panels
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Panels
  format_version: 1
item_schema:
  natural_key: [panel_on, specimen_id]
  fields:
    panel_on:
      type: date
      required: true
    panel_at:
      type: datetime
    specimen_id:
      type: string
      required: true
    source:
      type: link
      required: true
    analytes:
      type: array
      required: true
      items:
        type: object
---

Each panel is one observed event. Child analytes preserve reported values and provenance without interpretation.
"""
    readable_text = """---
type: collection
exomem_id: 68297085-14fd-4c00-8a95-6814ea671c99
title: Readable observations
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Observations
  format_version: 1
item_schema:
  natural_key: [subject]
  fields:
    subject:
      type: string
      required: true
    observations:
      type: array
      required: true
      items:
        type: object
    note:
      type: string
    provenance:
      type: string
record_presentation:
  version: 1
  summary:
    - field: subject
      label: Subject
  tables:
    - field: observations
      label: Observations
      columns:
        - field: name
          type: string
          label: Name
        - field: value
          type: string
          label: Value
        - field: source
          type: link
          link_kind: note
  notes:
    - field: note
      label: Note
  details:
    - field: provenance
      label: Provenance
---

The frontmatter is canonical; the managed Markdown block is a readable projection.
"""
    return {
        "minimal": {
            "manifest_path": f"{vault.kb_dirname()}/Records/Examples/Events/_collection.md",
            "manifest_text": minimal_text,
            "append_item": {"occurred_on": "2026-01-01", "label": "Example event"},
        },
        "laboratory_panel": {
            "manifest_path": f"{vault.kb_dirname()}/Records/Examples/Laboratory/_collection.md",
            "manifest_text": laboratory_text,
            "child_item_shape": {
                "name": "string",
                "reported_value": "source-preserved string, including inequality text",
                "numeric_value": "optional derived number",
                "comparator": "optional source-preserved comparator such as <",
                "unit": "source-preserved string",
                "reference_range": "source-preserved string",
                "cancelled": "boolean",
                "qualifier": "optional source-preserved string",
            },
            "append_item": {
                "panel_on": "2026-01-01",
                "specimen_id": "SPECIMEN-EXAMPLE",
                "source": "Knowledge Base/Sources/Examples/laboratory-report.md",
                "analytes": [
                    {
                        "name": "Example analyte",
                        "reported_value": "<18.4",
                        "numeric_value": 18.4,
                        "comparator": "<",
                        "unit": "unit/L",
                        "reference_range": "20-80 unit/L",
                        "cancelled": False,
                        "qualifier": "mild haemolysis",
                    },
                    {
                        "name": "Cancelled assay",
                        "reported_value": "cancelled",
                        "unit": "unit/L",
                        "reference_range": "not reported",
                        "cancelled": True,
                        "qualifier": "insufficient specimen",
                    },
                ],
            },
        },
        "readable_nested_records": {
            "manifest_path": f"{vault.kb_dirname()}/Records/Examples/Observations/_collection.md",
            "manifest_text": readable_text,
            "record_presentation": {
                "version": 1,
                "summary": [{"field": "subject", "label": "Subject"}],
                "tables": [
                    {
                        "field": "observations",
                        "label": "Observations",
                        "columns": [
                            {"field": "name", "type": "string", "label": "Name"},
                            {"field": "value", "type": "string", "label": "Value"},
                            {"field": "source", "type": "link", "link_kind": "note"},
                        ],
                    }
                ],
                "notes": [{"field": "note", "label": "Note"}],
                "details": [{"field": "provenance", "label": "Provenance"}],
            },
            "append_item": {
                "subject": "Example subject",
                "observations": [
                    {"name": "Example observation", "value": "<5", "source": "[[Sources/Example]]"}
                ],
                "note": "Source qualifier retained exactly.",
                "provenance": "Imported from the cited source.",
            },
        },
    }


def _record_presentation_json_schema() -> dict[str, Any]:
    """Expose the parser's closed, bounded v1 authoring shape."""
    descriptor = {
        "oneOf": [
            {"type": "string", "minLength": 1, "maxLength": 128},
            {
                "type": "object",
                "required": ["field"],
                "properties": {
                    "field": {"type": "string", "minLength": 1, "maxLength": 128},
                    "label": {"type": "string", "minLength": 1, "maxLength": 256},
                },
                "additionalProperties": False,
            },
        ]
    }
    descriptors = {
        "type": "array",
        "maxItems": _MAX_RECORD_PRESENTATION_FIELDS,
        "items": descriptor,
    }
    column = {
        "type": "object",
        "required": ["field", "type"],
        "properties": {
            "field": {"type": "string", "minLength": 1, "maxLength": 128},
            "label": {"type": "string", "minLength": 1, "maxLength": 256},
            "type": {
                "enum": ["boolean", "date", "datetime", "integer", "link", "number", "string"]
            },
            "link_kind": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "additionalProperties": False,
    }
    table = {
        "type": "object",
        "required": ["field", "columns"],
        "properties": {
            "field": {"type": "string", "minLength": 1, "maxLength": 128},
            "label": {"type": "string", "minLength": 1, "maxLength": 256},
            "columns": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_RECORD_PRESENTATION_COLUMNS,
                "items": column,
            },
        },
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "description": "Records markdown-items derived body recipe; frontmatter remains canonical.",
        "required": ["version", "tables"],
        "properties": {
            "version": {"const": 1},
            "summary": descriptors,
            "tables": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_RECORD_PRESENTATION_TABLES,
                "items": table,
            },
            "notes": descriptors,
            "details": descriptors,
        },
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class CollectionDiagnostic:
    code: str
    reason: str
    location: str = ""


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

    def validate(
        self, item: Mapping[str, Any], *, allowed_fields: Iterable[str] = ()
    ) -> dict[str, Any]:
        if not isinstance(item, Mapping):
            raise CollectionError("INVALID_ITEM", "item must be an object")
        value = dict(item)
        unknown = sorted(set(value) - set(self.fields) - set(allowed_fields))
        if unknown:
            raise CollectionError("SCHEMA_UNKNOWN_FIELD", f"field is not declared: {unknown[0]}")
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
class RecordPresentationColumn:
    field: str
    type: str
    label: str | None = None
    link_kind: str | None = None


@dataclass(frozen=True, slots=True)
class RecordPresentationTable:
    field: str
    label: str | None
    columns: tuple[RecordPresentationColumn, ...]


@dataclass(frozen=True, slots=True)
class RecordPresentation:
    """Closed Records-only recipe for a derived Markdown-item body."""

    version: int
    summary: tuple[tuple[str, str | None], ...]
    tables: tuple[RecordPresentationTable, ...]
    notes: tuple[tuple[str, str | None], ...]
    details: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class CollectionManifest:
    collection_id: str
    title: str
    semantic_profile: str
    collection_version: int
    lifecycle: str
    path: str
    manifest_version: SourceVersion
    storage: StorageSpec
    schema: ItemSchema
    audit_head: str | None = None
    manifest_stable_hash: str = ""
    templates: tuple[TemplateSpec, ...] = ()
    views: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    normalized_views: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    view_diagnostics: tuple[CollectionDiagnostic, ...] = ()
    governance: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    links: CollectionLinks = field(default_factory=CollectionLinks)
    record_presentation: RecordPresentation | None = None


@dataclass(frozen=True, slots=True)
class ItemIdentity:
    collection_id: str
    key: str
    inferred: bool = False

    def __post_init__(self) -> None:
        normalized = memory_refs.normalize_id(self.collection_id)
        if normalized is None:
            raise ValueError("collection_id must be a UUID")
        object.__setattr__(self, "collection_id", normalized)
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


@dataclass(frozen=True, slots=True)
class SavedView:
    """A manifest-owned, canonical query definition and its provenance binding."""

    name: str
    definition: Mapping[str, Any]
    identity: str


def load_manifest(vault_root: Path, path: Path | str) -> CollectionManifest:
    """Parse one explicit collection contract without touching canonical items."""
    root = Path(vault_root)
    manifest_path, rel = _safe_existing_path(root, path)
    if manifest_path.name != "_collection.md":
        raise CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest must be named _collection.md"
        )
    try:
        data, _guard = vault.read_bounded_guarded_bytes(root, rel, limit=_MAX_MANIFEST_BYTES)
    except vault.PathGuardError as error:
        raise CollectionError(
            "COLLECTION_NOT_FOUND", "collection manifest could not be read"
        ) from error
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CollectionError(
            "INVALID_COLLECTION_MANIFEST", "collection manifest is not UTF-8"
        ) from error
    audit_name = _profile_owned_audit_name(text)
    if audit_name is not None:
        _validate_audit_source(text, audit_name)
    try:
        frontmatter, _body, marker = vault.parse_frontmatter(text, strict=True)
    except vault.FrontmatterError as error:
        raise CollectionError(error.code, error.reason) from error
    if marker is None:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest requires YAML frontmatter")
    return _manifest_from_frontmatter(
        root,
        rel,
        frontmatter,
        SourceVersion(path=rel, hash=hashlib.sha256(data).hexdigest()),
        manifest_stable_hash=_manifest_stable_hash(text),
    )


def parse_manifest_bytes(
    vault_root: Path,
    path: Path | str,
    data: bytes,
    *,
    records_reader_version: int = RECORDS_READER_VERSION,
) -> CollectionManifest:
    """Parse a manifest from caller-held bytes bound by a guarded read."""
    root = Path(vault_root)
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    try:
        rel = manifest_path.relative_to(root).as_posix()
    except ValueError as error:
        raise CollectionError(
            "INVALID_COLLECTION_PATH", "collection path is outside the governed vault"
        ) from error
    if not rel.startswith(f"{vault.kb_dirname()}/") or _unsafe_relative(rel):
        raise CollectionError(
            "INVALID_COLLECTION_PATH", "collection path is outside the governed vault"
        )
    if manifest_path.name != "_collection.md":
        raise CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest must be named _collection.md"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest is not UTF-8") from error
    audit_name = _profile_owned_audit_name(text)
    if audit_name is not None:
        _validate_audit_source(text, audit_name)
    try:
        frontmatter, _body, marker = vault.parse_frontmatter(text, strict=True)
    except vault.FrontmatterError as error:
        raise CollectionError(error.code, error.reason) from error
    if marker is None:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest requires YAML frontmatter")
    return _manifest_from_frontmatter(
        root,
        rel,
        frontmatter,
        SourceVersion(path=rel, hash=hashlib.sha256(data).hexdigest()),
        manifest_stable_hash=_manifest_stable_hash(text),
        records_reader_version=records_reader_version,
    )


def discover_collections(
    vault_root: Path,
    *,
    authorize_path: Callable[[str], bool] | None = None,
    max_candidates: int = _MAX_DISCOVERY_CANDIDATES,
    max_raw_candidates: int = _MAX_DISCOVERY_CANDIDATES,
    reject_duplicates: bool = True,
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
    if (
        type(max_raw_candidates) is not int
        or max_raw_candidates < 1
        or max_raw_candidates > _MAX_DISCOVERY_CANDIDATES
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
    if authorize_path is None:
        candidates = list(itertools.islice(kb.rglob("_collection.md"), max_raw_candidates + 1))
        if len(candidates) > max_raw_candidates:
            raise CollectionError(
                "COLLECTION_DISCOVERY_LIMIT", "too many collection manifests to inspect"
            )
    else:
        candidates = []
        for candidate in kb.rglob("_collection.md"):
            safe = _safe_candidate_rel(root, candidate)
            if safe is None or not authorize(safe[1]):
                continue
            candidates.append(candidate)
            if len(candidates) > max_raw_candidates:
                raise CollectionError(
                    "COLLECTION_DISCOVERY_LIMIT", "too many collection manifests to inspect"
                )
    for candidate in sorted(candidates):
        safe = _safe_candidate_rel(root, candidate)
        if safe is None:
            continue
        _candidate_path, rel = safe
        if authorize_path is None and not authorize(rel):
            continue
        if len(manifests) >= max_candidates:
            raise CollectionError(
                "COLLECTION_DISCOVERY_LIMIT", "too many collection manifests to inspect"
            )
        manifests.append(load_manifest(root, candidate))
    if reject_duplicates:
        _raise_duplicate_ids(manifests)
    return tuple(manifests)


def discover_legacy_trackers(
    vault_root: Path,
    *,
    authorize_path: Callable[[str], bool] | None = None,
    max_raw_candidates: int = 4_096,
) -> tuple[tuple[LegacyCollection, ...], bool]:
    """Discover bounded exact-Records-layer tracker manifests without parsing items."""
    if type(max_raw_candidates) is not int or not 1 <= max_raw_candidates <= 16_384:
        raise CollectionError(
            "INVALID_DISCOVERY_LIMIT", "legacy discovery limit is outside supported bounds"
        )
    root = Path(vault_root)
    records_root = vault.kb_root(root) / "Records"
    if not records_root.is_dir():
        return (), False
    authorize = authorize_path or (lambda _path: True)
    if authorize_path is None:
        candidates = list(itertools.islice(records_root.rglob("*.md"), max_raw_candidates + 1))
    else:
        candidates = []
        for candidate in records_root.rglob("*.md"):
            safe = _safe_candidate_rel(root, candidate)
            if safe is None or not authorize(safe[1]):
                continue
            candidates.append(candidate)
            if len(candidates) > max_raw_candidates:
                break
    truncated = len(candidates) > max_raw_candidates
    trackers: list[LegacyCollection] = []
    for candidate in sorted(candidates[:max_raw_candidates]):
        if candidate.name == "_collection.md":
            continue
        safe = _safe_candidate_rel(root, candidate)
        if safe is None:
            continue
        _safe_path, relative = safe
        if authorize_path is None and not authorize(relative):
            continue
        try:
            tracker = inspect_legacy_tracker(root, candidate, authorize_path=authorize)
        except CollectionError:
            continue
        trackers.append(tracker)
    return tuple(trackers), truncated


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
            for manifest in discover_collections(
                vault_root, authorize_path=authorize, reject_duplicates=False
            )
            if manifest.collection_id == identity
        ]
        if not matches:
            raise CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        _raise_duplicate_ids(matches)
        return matches[0]

    root = Path(vault_root)
    try:
        path, rel = _safe_existing_path(root, raw)
    except CollectionError as error:
        if _genuinely_absent_collection_path(root, raw):
            raise CollectionError("COLLECTION_NOT_FOUND", "collection was not found") from error
        raise
    if not authorize(rel):
        raise CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    return load_manifest(root, path)


def record_ref(collection_id: str, item_key: str) -> str:
    return _item_ref(RECORD_REF_PREFIX, collection_id, item_key)


def plan_ref(collection_id: str, item_key: str) -> str:
    """Return a canonical collection-scoped Planning item reference."""
    return _item_ref(PLAN_REF_PREFIX, collection_id, item_key)


def _item_ref(prefix: str, collection_id: str, item_key: str) -> str:
    normalized = memory_refs.normalize_id(collection_id)
    if normalized is None:
        raise ValueError(f"invalid collection id: {collection_id!r}")
    _validate_item_key(item_key)
    return f"{prefix}{normalized}/{quote(item_key, safe='')}"


def parse_record_ref(value: str) -> tuple[str, str] | None:
    return _parse_item_ref(RECORD_REF_PREFIX, value)


def parse_plan_ref(value: str) -> tuple[str, str] | None:
    """Parse only a canonical Planning item reference."""
    return _parse_item_ref(PLAN_REF_PREFIX, value)


def _parse_item_ref(prefix: str, value: str) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw.lower().startswith(prefix):
        return None
    remainder = raw[len(prefix) :]
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
    samples = list(itertools.islice(rows, max_rows))
    provenance = list(itertools.islice(source_paths, _MAX_INFERENCE_PROVENANCE + 1))
    if len(provenance) > _MAX_INFERENCE_PROVENANCE:
        raise CollectionError(
            "SCHEMA_INFERENCE_PROVENANCE_LIMIT", "schema inference has too many source paths"
        )
    observed: dict[str, list[Any]] = {}
    for row in samples:
        if not isinstance(row, Mapping):
            raise ValueError("schema inference rows must be objects")
        for name, value in row.items():
            if type(name) is str and name:
                if name not in observed and len(observed) >= _MAX_SCHEMA_FIELDS:
                    raise CollectionError(
                        "SCHEMA_INFERENCE_FIELD_LIMIT", "schema inference has too many fields"
                    )
                observed.setdefault(name, []).append(value)
    fields = {
        name: FieldSpec(type=_infer_type(values)) for name, values in sorted(observed.items())
    }
    return SchemaInference(
        fields=MappingProxyType(fields),
        sample_count=len(samples),
        provenance=tuple(str(path) for path in provenance),
    )


def inspect_legacy_tracker(
    vault_root: Path,
    path: Path | str,
    *,
    authorize_path: Callable[[str], bool] | None = None,
) -> LegacyCollection:
    """Inspect a manifest-less tracker without making it queryable or mutable."""
    root = Path(vault_root)
    tracker, rel = _safe_existing_path(root, path)
    if authorize_path is not None and not authorize_path(rel):
        raise CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    try:
        data, _guard = vault.read_bounded_guarded_bytes(root, rel, limit=_MAX_MANIFEST_BYTES)
        frontmatter, _body, _marker = vault.parse_frontmatter(data.decode("utf-8"), strict=True)
    except (UnicodeDecodeError, vault.PathGuardError, vault.FrontmatterError) as error:
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
    root: Path,
    manifest_rel: str,
    frontmatter: Mapping[str, Any],
    manifest_version: SourceVersion,
    manifest_stable_hash: str = "",
    records_reader_version: int = RECORDS_READER_VERSION,
) -> CollectionManifest:
    if frontmatter.get("type") != "collection":
        raise CollectionError(
            "INVALID_COLLECTION_MANIFEST",
            "manifest type must be collection",
            {
                "field": "type",
                "received": frontmatter.get("type"),
                "allowed": ["collection"],
                "example": "type: collection",
            },
        )
    collection_id = memory_refs.normalize_id(frontmatter.get("exomem_id"))
    if collection_id is None:
        raise CollectionError(
            "INVALID_COLLECTION_ID",
            "manifest requires a UUID exomem_id",
            {
                "field": "exomem_id",
                "received": frontmatter.get("exomem_id"),
                "expected": "UUID string",
                "example": "exomem_id: 15dd81cd-9ae2-488c-bd9e-14771d86343e",
            },
        )
    title = _nonempty_string(frontmatter.get("title"), "title")
    profile = _nonempty_string(frontmatter.get("semantic_profile"), "semantic_profile")
    if profile not in _SUPPORTED_PROFILES:
        raise CollectionError(
            "UNSUPPORTED_COLLECTION_PROFILE",
            "semantic profile is not supported",
            {
                "field": "semantic_profile",
                "received": profile,
                "allowed": sorted(_SUPPORTED_PROFILES),
                "example": "semantic_profile: records",
            },
        )
    version = frontmatter.get("collection_version")
    if version != COLLECTION_VERSION:
        raise CollectionError(
            "UNSUPPORTED_COLLECTION_VERSION",
            "collection version is not supported",
            {
                "field": "collection_version",
                "received": version,
                "allowed": [COLLECTION_VERSION],
                "example": f"collection_version: {COLLECTION_VERSION}",
            },
        )
    lifecycle = _nonempty_string(frontmatter.get("lifecycle"), "lifecycle")
    schema_version = frontmatter.get("schema_version")
    if type(schema_version) is not int or schema_version < 1:
        raise CollectionError(
            "INVALID_SCHEMA_VERSION",
            "schema_version must be a positive integer",
            {
                "field": "schema_version",
                "received": schema_version,
                "expected": "positive integer",
                "example": "schema_version: 1",
            },
        )
    storage = _parse_storage(root, manifest_rel, frontmatter.get("storage"))
    _require_profile_layer(profile, manifest_rel, "manifest")
    _require_profile_layer(profile, storage.source, "storage.source")
    if _portable_path_key(storage.source) == _portable_path_key(manifest_rel):
        raise CollectionError(
            "INVALID_COLLECTION_PATH", "storage.source must not alias the collection manifest"
        )
    schema = _parse_schema(schema_version, frontmatter.get("item_schema"))
    presentation = _parse_record_presentation(frontmatter.get("record_presentation"), profile, storage, schema)
    audit_head = _audit_head(frontmatter, "record_audit" if profile == "records" else "plan_audit")
    if profile == "records":
        _require_records_reader_version(frontmatter, records_reader_version)
    templates = _parse_templates(root, manifest_rel, frontmatter.get("templates", []))
    views, normalized_views, view_diagnostics = _parse_saved_views(
        frontmatter.get("views", {}), schema, storage, profile, presentation
    )
    links = _parse_links(frontmatter.get("links", {}))
    return CollectionManifest(
        collection_id=collection_id,
        title=title,
        semantic_profile=profile,
        collection_version=version,
        lifecycle=lifecycle,
        path=manifest_rel,
        manifest_version=manifest_version,
        storage=storage,
        schema=schema,
        audit_head=audit_head,
        manifest_stable_hash=manifest_stable_hash,
        templates=templates,
        views=views,
        normalized_views=normalized_views,
        view_diagnostics=view_diagnostics,
        governance=_freeze_mapping(frontmatter.get("governance", {}), "governance"),
        links=links,
        record_presentation=presentation,
    )


def resolve_saved_view(manifest: CollectionManifest, name: str) -> SavedView:
    """Resolve one manifest view into the exact definition used for query provenance."""
    if type(name) is not str or not name or len(name.encode("utf-8")) > _MAX_SAVED_VIEW_NAME_BYTES:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view name is invalid")
    for diagnostic in manifest.view_diagnostics:
        if diagnostic.location == f"views.{name}":
            raise CollectionError(diagnostic.code, diagnostic.reason)
    definition = manifest.normalized_views.get(name)
    if definition is None:
        definition = manifest.views.get(name)
    if definition is None:
        raise CollectionError("SAVED_VIEW_NOT_FOUND", "saved view was not found")
    if not isinstance(definition, Mapping):  # Defensive: manifests normalize this on load.
        raise CollectionError("INVALID_SAVED_VIEW", "saved view definition is invalid")
    plain_definition = _plain_json_value(definition)
    assert isinstance(plain_definition, dict)
    normalized_definition = _normalize_saved_view(
        plain_definition,
        _saved_view_fields(manifest.schema, manifest.storage, manifest.semantic_profile, manifest.record_presentation),
        _saved_view_child_shapes(manifest.storage, manifest.record_presentation),
    )
    canonical = json.dumps(
        {
            "collection_id": manifest.collection_id,
            "manifest_path": manifest.path,
            "manifest_hash": manifest.manifest_version.hash,
            "name": name,
            "definition": normalized_definition,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return SavedView(
        name=name,
        definition=normalized_definition,
        identity=hashlib.sha256(canonical).hexdigest(),
    )


def _parse_saved_views(
    value: object, schema: ItemSchema, storage: StorageSpec, semantic_profile: str, presentation: RecordPresentation | None = None
) -> tuple[Mapping[str, Any], Mapping[str, Any], tuple[CollectionDiagnostic, ...]]:
    views = _mapping(value, "views")
    if len(views) > _MAX_SAVED_VIEWS:
        raise CollectionError("INVALID_SAVED_VIEW", "too many saved views")
    fields = _saved_view_fields(schema, storage, semantic_profile, presentation)
    child_shapes = _saved_view_child_shapes(storage, presentation)
    accepted: dict[str, Any] = {}
    normalized: dict[str, Any] = {}
    diagnostics: list[CollectionDiagnostic] = []
    for name, definition in views.items():
        location = f"views.{name}" if type(name) is str else "views"
        try:
            if type(name) is not str or not name or len(name.encode("utf-8")) > _MAX_SAVED_VIEW_NAME_BYTES:
                raise CollectionError("INVALID_SAVED_VIEW", "saved view name is invalid")
            normalized_view = _freeze_mapping(
                _normalize_saved_view(definition, fields, child_shapes)
            )
            accepted[name] = definition
            normalized[name] = normalized_view
        except CollectionError as error:
            diagnostics.append(CollectionDiagnostic(error.code, error.reason, location))
    return _freeze_mapping(accepted, "views"), _freeze_mapping(normalized, "views"), tuple(diagnostics)


def _saved_view_fields(
    schema: ItemSchema, storage: StorageSpec, semantic_profile: str, presentation: RecordPresentation | None = None
) -> set[str]:
    fields = set(schema.fields) | set(_SAVED_VIEW_SHARED_SYSTEM_FIELDS)
    fields.add(profile_for(semantic_profile).item_id_property)
    if storage.strategy != "markdown-log":
        return fields
    heading = storage.descriptor.get("item_heading")
    if isinstance(heading, Mapping):
        note = heading.get("note")
        if isinstance(note, Mapping) and type(note.get("field")) is str:
            fields.add(note["field"])
    return fields


def _saved_view_child_shapes(
    storage: StorageSpec, presentation: RecordPresentation | None
) -> Mapping[str, frozenset[str]]:
    if storage.strategy == "markdown-items":
        return MappingProxyType(
            {
                table.field: frozenset(column.field for column in table.columns)
                for table in (() if presentation is None else presentation.tables)
            }
        )
    if storage.strategy == "markdown-log":
        children = storage.descriptor.get("child_rows")
        if (
            isinstance(children, Mapping)
            and type(children.get("container_field")) is str
            and isinstance(children.get("fields"), (list, tuple))
        ):
            return MappingProxyType(
                {
                    children["container_field"]: frozenset(
                        field for field in children["fields"] if type(field) is str
                    )
                }
            )
    return MappingProxyType({})


def _saved_view_child_fields(
    storage: StorageSpec, presentation: RecordPresentation | None
) -> frozenset[str]:
    return frozenset(_saved_view_child_shapes(storage, presentation))


def _saved_view_selected_fields(
    fields: set[str],
    child_shapes: Mapping[str, frozenset[str]],
    selected_child: str | None,
) -> set[str]:
    if selected_child is None:
        return set(fields)
    selected = child_shapes.get(selected_child)
    if selected is None:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view expand_child is invalid")
    all_child_columns = set().union(*child_shapes.values()) if child_shapes else set()
    return (
        set(fields)
        - all_child_columns
        - {selected_child}
        | set(selected)
        | {"parent_record_id", "child_field", "child_index"}
    )


def _normalize_saved_view(
    value: object,
    fields: set[str],
    child_shapes: Mapping[str, frozenset[str]] = MappingProxyType({}),
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value or not _mapping_has_only_keys(
        value, {"query", "sort", "source_snapshot"}
    ):
        raise CollectionError("INVALID_SAVED_VIEW", "saved view definition is invalid")
    raw_query = value.get("query", {})
    if not isinstance(raw_query, Mapping):
        raise CollectionError("INVALID_SAVED_VIEW", "saved view query is invalid")
    if not _mapping_has_only_keys(raw_query, _SAVED_VIEW_QUERY_KEYS):
        raise CollectionError("INVALID_SAVED_VIEW", "saved view query has unknown fields")
    expand_children = raw_query.get("expand_children")
    if expand_children is not None and type(expand_children) is not bool:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view expand_children is invalid")
    expand_child = raw_query.get("expand_child")
    if expand_child is not None and (
        type(expand_child) is not str or expand_child not in child_shapes
    ):
        raise CollectionError("INVALID_SAVED_VIEW", "saved view expand_child is invalid")
    if expand_children is True and expand_child is not None:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view child selectors conflict")
    if expand_children is True and len(child_shapes) != 1:
        reason = (
            "saved view expand_children is ambiguous"
            if child_shapes
            else "saved view expand_children has no eligible child field"
        )
        raise CollectionError("INVALID_SAVED_VIEW", reason)
    selected_child = (
        expand_child
        if type(expand_child) is str
        else next(iter(child_shapes))
        if expand_children is True
        else None
    )
    fields = _saved_view_selected_fields(fields, child_shapes, selected_child)
    query: dict[str, Any] = {"filters": []}
    if "expand_children" in raw_query:
        query["expand_children"] = expand_children
    if type(expand_child) is str:
        query["expand_child"] = expand_child
    if "filters" in raw_query:
        query["filters"] = _normalize_saved_view_filters(raw_query["filters"], fields)
    if "columns" in raw_query:
        columns = raw_query["columns"]
        if (
            not isinstance(columns, list)
            or not columns
            or len(columns) > len(fields)
            or any(type(column) is not str or column not in fields for column in columns)
            or len(set(columns)) != len(columns)
        ):
            raise CollectionError("INVALID_SAVED_VIEW", "saved view columns are invalid")
        query["columns"] = list(columns)
    for key in ("sort_by", "date_column"):
        if key in raw_query:
            column = raw_query[key]
            if type(column) is not str or column not in fields:
                raise CollectionError("INVALID_SAVED_VIEW", f"saved view {key} is invalid")
            query[key] = column
    for key in ("date_from", "date_to"):
        if key in raw_query:
            bound = raw_query[key]
            if type(bound) is not str or not bound or len(bound.encode("utf-8")) > 128:
                raise CollectionError("INVALID_SAVED_VIEW", f"saved view {key} is invalid")
            query[key] = bound
    for key in ("descending",):
        if key in raw_query:
            if type(raw_query[key]) is not bool:
                raise CollectionError("INVALID_SAVED_VIEW", f"saved view {key} is invalid")
            query[key] = raw_query[key]
    if "aggregate" in raw_query:
        aggregate = _normalize_saved_view_aggregate(raw_query["aggregate"], fields)
        query["aggregate"] = aggregate
    if "limit" in raw_query:
        limit = raw_query["limit"]
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise CollectionError("INVALID_SAVED_VIEW", "saved view limit is invalid")
        query["limit"] = limit
    if "sort" in value:
        sort = value["sort"]
        if (
            not isinstance(sort, (list, tuple))
            or len(sort) != 2
            or type(sort[0]) is not str
            or sort[0] not in fields
            or type(sort[1]) is not str
            or sort[1] not in {"asc", "desc"}
            or "sort_by" in query
            or "descending" in query
        ):
            raise CollectionError("INVALID_SAVED_VIEW", "saved view sort is invalid")
        query["sort_by"] = sort[0]
        query["descending"] = sort[1] == "desc"
    if not query:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view requires query or sort")
    normalized: dict[str, Any] = {"query": query}
    if "source_snapshot" in value:
        snapshot = value["source_snapshot"]
        if type(snapshot) is not str or re.fullmatch(r"[0-9a-f]{64}", snapshot) is None:
            raise CollectionError("INVALID_SAVED_VIEW", "saved view source snapshot is invalid")
        normalized["source_snapshot"] = snapshot
    return normalized


def _normalize_saved_view_filters(value: object, fields: set[str]) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        if not all(type(column) is str for column in value):
            raise CollectionError("INVALID_SAVED_VIEW", "saved view filters are invalid")
        raw_filters = [
            {"column": column, "op": "eq", "value": item}
            for column, item in sorted(value.items())
        ]
    elif isinstance(value, list):
        raw_filters = value
    else:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view filters are invalid")
    if len(raw_filters) > _MAX_SAVED_VIEW_FILTERS:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view has too many filters")
    filters: list[dict[str, Any]] = []
    for raw in raw_filters:
        if not isinstance(raw, Mapping) or not _mapping_has_only_keys(
            raw, {"column", "op", "value"}
        ):
            raise CollectionError("INVALID_SAVED_VIEW", "saved view filter is invalid")
        column = raw.get("column")
        operator = raw.get("op", "eq")
        if (
            type(column) is not str
            or column not in fields
            or type(operator) is not str
            or operator not in _SAVED_VIEW_QUERY_OPS
        ):
            raise CollectionError("INVALID_SAVED_VIEW", "saved view filter is invalid")
        if operator not in {"exists", "missing"} and "value" not in raw:
            raise CollectionError("INVALID_SAVED_VIEW", "saved view filter value is required")
        if "value" in raw and not _is_json_value(raw["value"]):
            raise CollectionError("INVALID_SAVED_VIEW", "saved view filter value is invalid")
        normalized = {"column": column, "op": operator}
        if "value" in raw:
            normalized["value"] = raw["value"]
        filters.append(normalized)
    return filters


def _mapping_has_only_keys(value: Mapping[Any, Any], allowed: set[str] | frozenset[str]) -> bool:
    return all(type(key) is str and key in allowed for key in value)


def _normalize_saved_view_aggregate(value: object, fields: set[str]) -> str:
    if type(value) is not str:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view aggregate is invalid")
    aggregate = value.strip()
    if aggregate in {"count", "profile"}:
        return aggregate
    function, separator, column = aggregate.partition(":")
    if not separator or function not in _SAVED_VIEW_AGGREGATES or column not in fields:
        raise CollectionError("INVALID_SAVED_VIEW", "saved view aggregate is invalid")
    return f"{function}:{column}"


def record_audit_head(frontmatter: Mapping[str, Any]) -> str | None:
    """Validate the optional manifest audit mapping and return its head."""
    return _audit_head(frontmatter, "record_audit")


def _require_records_reader_version(frontmatter: Mapping[str, Any], reader_version: int) -> None:
    if type(reader_version) is not int or reader_version < 1:
        raise CollectionError(
            "RECORDS_READER_VERSION_UNSUPPORTED", "Records reader version is unsupported"
        )
    audit = frontmatter.get("record_audit")
    if isinstance(audit, Mapping) and type(audit.get("version")) is int and audit["version"] > reader_version:
        raise CollectionError(
            "RECORDS_READER_VERSION_UNSUPPORTED", "Records reader version is unsupported"
        )


def plan_audit_head(frontmatter: Mapping[str, Any]) -> str | None:
    """Validate the optional Planning manifest audit mapping and return its head."""
    return _audit_head(frontmatter, "plan_audit")


def _audit_head(frontmatter: Mapping[str, Any], name: str) -> str | None:
    code = "INVALID_RECORD_AUDIT" if name == "record_audit" else "INVALID_COLLECTION_AUDIT"
    audit = frontmatter.get(name)
    if audit is None:
        return None
    if not isinstance(audit, Mapping) or set(audit) != {"version", "head"}:
        raise CollectionError(code, "collection audit state is invalid")
    head = audit.get("head")
    supported_versions = {1, 2} if name == "record_audit" else {1}
    if (
        type(audit.get("version")) is not int
        or audit["version"] not in supported_versions
        or not isinstance(head, str)
    ):
        raise CollectionError(code, "collection audit state is invalid")
    if re.fullmatch(r"[0-9a-f]{24}", head) is None:
        raise CollectionError(code, "collection audit head is invalid")
    return head


def _validate_record_audit_source(text: str) -> vault.yaml.nodes.MappingNode | None:
    """Require an audit mapping's keys to be authored rather than YAML-merged."""
    return _validate_audit_source(text, "record_audit")


def _profile_owned_audit_name(text: str) -> str | None:
    """Find the declared profile without constructing YAML audit mappings."""
    text = text.removeprefix("\ufeff")
    opening = re.match(r"\A---\r?\n", text)
    if opening is None:
        return None
    closing = re.search(r"(?m)^---\r?$", text[opening.end() :])
    if closing is None:
        return None
    try:
        document = vault.yaml.compose(text[opening.end() : opening.end() + closing.start()])
    except vault.yaml.YAMLError:
        return None
    if not isinstance(document, vault.yaml.nodes.MappingNode):
        return None
    for key, value in document.value:
        if (
            isinstance(key, vault.yaml.nodes.ScalarNode)
            and key.value == "semantic_profile"
            and isinstance(value, vault.yaml.nodes.ScalarNode)
        ):
            if value.value == "records":
                return "record_audit"
            if value.value == "planning":
                return "plan_audit"
    return None


def _validate_audit_source(
    text: str, expected_name: str | None = None
) -> vault.yaml.nodes.MappingNode | None:
    """Validate profile audit mappings without assuming Records semantics."""
    text = text.removeprefix("\ufeff")
    opening = re.match(r"\A---\r?\n", text)
    if opening is None:
        return None
    closing = re.search(r"(?m)^---\r?$", text[opening.end() :])
    if closing is None:
        return None
    try:
        document = vault.yaml.compose(text[opening.end() : opening.end() + closing.start()])
    except vault.yaml.YAMLError as error:
        raise CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest requires valid frontmatter"
        ) from error
    if not isinstance(document, vault.yaml.nodes.MappingNode):
        return None
    names = (expected_name,) if expected_name is not None else ("record_audit", "plan_audit")
    result = None
    for name in names:
        node = _validate_audit_document(document, name)
        if node is not None:
            result = node
    return result


def _validate_record_audit_document(
    document: vault.yaml.nodes.MappingNode,
) -> vault.yaml.nodes.MappingNode | None:
    return _validate_audit_document(document, "record_audit")


def _validate_audit_document(
    document: vault.yaml.nodes.MappingNode, name: str
) -> vault.yaml.nodes.MappingNode | None:
    code = "INVALID_RECORD_AUDIT" if name == "record_audit" else "INVALID_COLLECTION_AUDIT"
    matches = [
        value
        for key, value in document.value
        if isinstance(key, vault.yaml.nodes.ScalarNode) and key.value == name
    ]
    if len(matches) > 1:
        raise CollectionError(code, "collection audit state is duplicated")
    if not matches:
        return None
    audit = matches[0]
    if not isinstance(audit, vault.yaml.nodes.MappingNode):
        raise CollectionError(code, "collection audit state is invalid")
    keys = [
        key.value for key, _value in audit.value if isinstance(key, vault.yaml.nodes.ScalarNode)
    ]
    if len(keys) != 2 or set(keys) != {"version", "head"}:
        raise CollectionError(code, "collection audit state is invalid")
    return audit


def _manifest_stable_hash(text: str) -> str:
    """Hash manifest bytes with only its optional audit mapping removed."""
    opening = re.match(r"\A---\r?\n", text)
    if opening is None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    closing = re.search(r"(?m)^---\r?$", text[opening.end() :])
    if closing is None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    start = opening.end()
    end = start + closing.start()
    frontmatter = text[start:end]
    try:
        document = vault.yaml.compose(frontmatter)
    except vault.yaml.YAMLError:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not isinstance(document, vault.yaml.nodes.MappingNode):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    profile = next(
        (
            value.value
            for key, value in document.value
            if isinstance(key, vault.yaml.nodes.ScalarNode)
            and key.value == "semantic_profile"
            and isinstance(value, vault.yaml.nodes.ScalarNode)
        ),
        None,
    )
    audit_name = "plan_audit" if profile == "planning" else "record_audit"
    matches = [
        (key, value)
        for key, value in document.value
        if isinstance(key, vault.yaml.nodes.ScalarNode) and key.value == audit_name
    ]
    if len(matches) != 1:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    key, value = matches[0]
    remove_end = value.end_mark.index
    if frontmatter[remove_end : remove_end + 2] == "\r\n":
        remove_end += 2
    elif frontmatter[remove_end : remove_end + 1] == "\n":
        remove_end += 1
    stable = (
        text[:start] + frontmatter[: key.start_mark.index] + frontmatter[remove_end:] + text[end:]
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _parse_storage(root: Path, manifest_rel: str, value: object) -> StorageSpec:
    storage = _mapping(value, "storage")
    strategy = _nonempty_string(storage.get("strategy"), "storage.strategy")
    if strategy not in _SUPPORTED_STORAGE:
        raise CollectionError(
            "UNSUPPORTED_STORAGE_STRATEGY",
            "storage strategy is not supported",
            {
                "field": "storage.strategy",
                "received": strategy,
                "allowed": sorted(_SUPPORTED_STORAGE),
                "example": "strategy: markdown-items",
            },
        )
    format_version = storage.get("format_version")
    if format_version != STORAGE_FORMAT_VERSION:
        raise CollectionError(
            "UNSUPPORTED_STORAGE_FORMAT_VERSION",
            "storage format version is not supported",
            {
                "field": "storage.format_version",
                "received": format_version,
                "allowed": [STORAGE_FORMAT_VERSION],
                "example": f"format_version: {STORAGE_FORMAT_VERSION}",
            },
        )
    source = _vault_relative_path(root, manifest_rel, storage.get("source"), "storage.source")
    descriptor = {
        key: value
        for key, value in storage.items()
        if key not in {"strategy", "source", "format_version"}
    }
    return StorageSpec(strategy, source, format_version, _freeze_mapping(descriptor))


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


def _parse_record_presentation(
    value: object, profile: str, storage: StorageSpec, schema: ItemSchema
) -> RecordPresentation | None:
    """Parse the small derived-body contract without widening item authority."""
    if value is None:
        return None
    if profile != "records" or storage.strategy != "markdown-items":
        raise CollectionError(
            "INVALID_RECORD_PRESENTATION",
            "record_presentation is available only to Records markdown-items",
        )
    raw = _mapping(value, "record_presentation")
    if set(raw) - {"version", "summary", "tables", "notes", "details"}:
        raise CollectionError("INVALID_RECORD_PRESENTATION", "record_presentation has unknown fields")
    if raw.get("version") != 1:
        raise CollectionError("INVALID_RECORD_PRESENTATION", "record_presentation version is unsupported")
    summary = _parse_presentation_descriptors(raw.get("summary", []), schema, "summary")
    notes = _parse_presentation_descriptors(raw.get("notes", []), schema, "notes")
    details = _parse_presentation_descriptors(raw.get("details", []), schema, "details")
    tables_raw = raw.get("tables", [])
    if not isinstance(tables_raw, list) or len(tables_raw) > _MAX_RECORD_PRESENTATION_TABLES:
        raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation tables are invalid")
    tables: list[RecordPresentationTable] = []
    names: set[str] = set()
    for table_raw in tables_raw:
        table = _mapping(table_raw, "presentation table")
        if set(table) - {"field", "label", "columns"}:
            raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation table has unknown fields")
        field = _presentation_field(table.get("field"), schema, "table field")
        if field in names:
            raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation table field is duplicated")
        names.add(field)
        spec = schema.fields[field]
        if spec.type != "array" or spec.items is None or spec.items.type != "object":
            raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation table must name an array of objects")
        label = _presentation_label(table.get("label"))
        columns_raw = table.get("columns")
        if not isinstance(columns_raw, list) or not columns_raw or len(columns_raw) > _MAX_RECORD_PRESENTATION_COLUMNS:
            raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation table columns are invalid")
        columns: list[RecordPresentationColumn] = []
        column_names: set[str] = set()
        for column_raw in columns_raw:
            column = _mapping(column_raw, "presentation column")
            if set(column) - {"field", "label", "type", "link_kind"}:
                raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation column has unknown fields")
            name = _nonempty_string(column.get("field"), "presentation column field")
            if len(name.encode("utf-8")) > 128 or name in column_names:
                raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation column field is invalid")
            if name in _PRESENTATION_SYNTHESIZED_FIELDS:
                raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation column collides with a system field")
            column_names.add(name)
            kind = column.get("type")
            if type(kind) is not str or kind not in _PRESENTATION_SCALAR_TYPES - {"enum"}:
                raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation column type is invalid")
            link_kind = column.get("link_kind")
            if kind == "link":
                if type(link_kind) is not str or not link_kind or len(link_kind.encode("utf-8")) > 128:
                    raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation link kind is invalid")
            elif link_kind is not None:
                raise CollectionError("INVALID_RECORD_PRESENTATION", "only link columns may declare link_kind")
            columns.append(RecordPresentationColumn(name, kind, _presentation_label(column.get("label")), link_kind))
        tables.append(RecordPresentationTable(field, label, tuple(columns)))
    if not tables:
        raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation requires a table")
    return RecordPresentation(1, summary, tuple(tables), notes, details)


def _presentation_field(value: object, schema: ItemSchema, name: str) -> str:
    field = _nonempty_string(value, name)
    if field not in schema.fields:
        raise CollectionError("INVALID_RECORD_PRESENTATION", f"presentation {name} must name an item field")
    return field


def _presentation_label(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 256:
        raise CollectionError("INVALID_RECORD_PRESENTATION", "presentation label is invalid")
    return value


def _parse_presentation_descriptors(
    value: object, schema: ItemSchema, name: str
) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, list) or len(value) > _MAX_RECORD_PRESENTATION_FIELDS:
        raise CollectionError("INVALID_RECORD_PRESENTATION", f"presentation {name} is invalid")
    result: list[tuple[str, str | None]] = []
    fields: set[str] = set()
    for raw in value:
        if type(raw) is str:
            field, label = _presentation_field(raw, schema, name), None
        else:
            descriptor = _mapping(raw, f"presentation {name} descriptor")
            if set(descriptor) - {"field", "label"}:
                raise CollectionError("INVALID_RECORD_PRESENTATION", f"presentation {name} has unknown fields")
            field, label = _presentation_field(descriptor.get("field"), schema, name), _presentation_label(descriptor.get("label"))
        if field in fields:
            raise CollectionError("INVALID_RECORD_PRESENTATION", f"presentation {name} field is duplicated")
        if schema.fields[field].type not in _PRESENTATION_SCALAR_TYPES:
            raise CollectionError(
                "INVALID_RECORD_PRESENTATION",
                f"presentation {name} must name a scalar-compatible item field",
            )
        fields.add(field)
        result.append((field, label))
    return tuple(result)


def _parse_field_spec(value: object, depth: int = 0) -> FieldSpec:
    if depth > _MAX_SCHEMA_DEPTH:
        raise CollectionError("INVALID_ITEM_SCHEMA", "item schema nesting is too deep")
    raw = _mapping(value, "item_schema field")
    kind = _nonempty_string(raw.get("type"), "item_schema field type")
    if kind not in _SUPPORTED_FIELD_TYPES:
        raise CollectionError(
            "UNSUPPORTED_FIELD_TYPE",
            "item schema field type is not supported",
            {
                "field": "item_schema.fields.*.type",
                "received": kind,
                "allowed": sorted(_SUPPORTED_FIELD_TYPES),
                "example": "type: string",
            },
        )
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
    if kind == "enum" and not enum:
        raise CollectionError("INVALID_ITEM_SCHEMA", "enum fields require values")
    if kind != "enum" and enum:
        raise CollectionError("INVALID_ITEM_SCHEMA", "only enum fields may declare values")
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
                default_properties=_freeze_mapping(
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
        query = _freeze_mapping(link.get("query"), "plan link query")
        result.append(PlanLink(reference, query))
    return CollectionLinks(tuple(result))


def _validate_field_value(name: str, value: Any, spec: FieldSpec) -> None:
    if value is None:
        return
    if spec.type not in _SUPPORTED_FIELD_TYPES:
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has unsupported type: {name}")
    if spec.type == "string" and type(value) is not str:
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "integer" and (type(value) is not int):
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "number" and (
        type(value) not in {int, float} or (type(value) is float and not math.isfinite(value))
    ):
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
    if spec.type == "object" and (not isinstance(value, Mapping) or not _is_json_value(value)):
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.type == "link" and type(value) is not str:
        raise CollectionError("SCHEMA_FIELD_TYPE", f"field has wrong type: {name}")
    if spec.enum and not any(
        type(value) is type(option) and value == option for option in spec.enum
    ):
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
    if type(value) is float and not math.isfinite(value):
        raise ValueError("natural key numbers must be finite")
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


def _genuinely_absent_collection_path(root: Path, raw: str) -> bool:
    """Recognize only a safe, missing leaf as an absent collection selector."""
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            normalized = candidate.relative_to(root).as_posix()
        except ValueError:
            return False
    else:
        normalized = raw.replace("\\", "/")
    if not normalized.startswith(f"{vault.kb_dirname()}/") or _unsafe_relative(normalized):
        return False
    current = root
    parts = Path(normalized).parts
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            return False
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return False
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            return False
    return False


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
    if not _is_safe_vault_target(root, target):
        raise CollectionError("INVALID_COLLECTION_PATH", f"{name} must not traverse a symlink")
    return rel


def _require_records_layer(path: str, name: str) -> None:
    """Keep Records paths in the exact portable layer used by recall policy."""
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != vault.kb_dirname() or parts[1] != "Records":
        raise CollectionError(
            "INVALID_COLLECTION_PATH", f"{name} must stay under Knowledge Base/Records"
        )


def _require_profile_layer(profile: str, path: str, name: str) -> None:
    layer = profile_for(profile).placement_layer
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != vault.kb_dirname() or parts[1] != layer:
        raise CollectionError(
            "INVALID_COLLECTION_PATH", f"{name} must stay under Knowledge Base/{layer}"
        )


def _portable_path_key(path: str) -> str:
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in path.split("/"))


def _is_safe_vault_target(root: Path, target: Path) -> bool:
    try:
        rel = target.relative_to(root)
        current = root
        for part in rel.parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return True
            if stat.S_ISLNK(info.st_mode):
                return False
            if getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
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


def _freeze_mapping(value: object, name: str = "value") -> Mapping[str, Any]:
    mapping = _mapping(value, name)
    frozen = _freeze(mapping, active=set(), count=[0])
    assert isinstance(frozen, Mapping)
    return frozen


def _freeze(value: Any, *, active: set[int], count: list[int], depth: int = 0) -> Any:
    count[0] += 1
    if count[0] > _MAX_FROZEN_VALUES:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest value is too large")
    if depth > _MAX_SCHEMA_DEPTH:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest value is too deep")
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise CollectionError(
                "INVALID_COLLECTION_MANIFEST", "manifest value contains an alias cycle"
            )
        active.add(identity)
        try:
            return MappingProxyType(
                {
                    key: _freeze(item, active=active, count=count, depth=depth + 1)
                    for key, item in value.items()
                }
            )
        finally:
            active.remove(identity)
    if isinstance(value, list | tuple):
        identity = id(value)
        if identity in active:
            raise CollectionError(
                "INVALID_COLLECTION_MANIFEST", "manifest value contains an alias cycle"
            )
        active.add(identity)
        try:
            return tuple(
                _freeze(item, active=active, count=count, depth=depth + 1) for item in value
            )
        finally:
            active.remove(identity)
    return value


def _is_json_value(value: Any) -> bool:
    if value is None or type(value) in {str, int, bool}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list | tuple):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False


def _plain_json_value(value: Any) -> Any:
    """Detach frozen manifest values before hashing or returning provenance."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain_json_value(item) for item in value]
    return value


def _nonempty_string(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        examples = {
            "title": "title: Observed events",
            "semantic_profile": "semantic_profile: records",
            "lifecycle": "lifecycle: active",
            "storage.strategy": "strategy: markdown-items",
        }
        raise CollectionError(
            "INVALID_COLLECTION_MANIFEST",
            f"{name} must be a non-empty string",
            {
                "field": name,
                "received": value,
                "expected": "non-empty string",
                "example": examples.get(name, f"{name}: <value>"),
            },
        )
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
