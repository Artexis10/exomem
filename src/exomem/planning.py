"""Planning semantics over the shared structured-collection substrate."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from . import memory_refs, record_formats, record_governance, records
from . import structured_collections as collections
from .governance import egress
from .structured_collections import CollectionError, parse_plan_ref

_KINDS = frozenset({"area", "outcome", "initiative", "work-item"})
_STATUSES = frozenset({"candidate", "planned", "active", "blocked", "completed", "cancelled"})
_LIFECYCLES = frozenset({"active", "archived"})
_PRIORITIES = frozenset({"critical", "high", "medium", "low", "none"})
_COMMITMENTS = frozenset({"uncommitted", "considering", "committed"})
_HORIZONS = frozenset({"inbox", "week", "month", "quarter", "year", "multi-year"})
_HEALTH = frozenset({"unknown", "on-track", "at-risk", "off-track"})
_EXECUTION_KIND = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_AREA_FORBIDDEN = frozenset({"status", "priority", "commitment", "horizon", "area", "parent"})
_OPTIONAL = frozenset(
    {
        "health",
        "window_start",
        "window_end",
        "area",
        "parent",
        "progress_evidence",
        "execution",
        "tags",
        "motivation",
    }
)


@dataclass(frozen=True, slots=True)
class _PlanningEnvelope:
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def motivation_is_governed(manifest: collections.CollectionManifest) -> bool:
    """True when this collection declares `motivation` as the governed ref list.

    A vault authored before this contract may legally have declared `motivation`
    as its own free-text field. Imposing ref semantics on that value would refuse
    it on every path that normalizes stored records — including the snapshot read
    inside `query` — which would make one legacy item render the whole collection
    unqueryable and unrepairable. So the governed shape is enforced only where the
    manifest actually declares the array form.
    """
    field = manifest.schema.fields.get("motivation")
    return field is not None and field.type == "array"


def normalize_item(
    item: Mapping[str, Any],
    *,
    apply_defaults: bool = True,
    validate_motivation: bool = True,
) -> dict[str, Any]:
    """Validate one authored Planning item without inferring from prose."""
    if not isinstance(item, Mapping):
        _invalid("item must be an object")
    values = dict(item)
    if apply_defaults:
        values.setdefault("kind", "work-item")
        if values["kind"] != "area":
            values.setdefault("status", "candidate")
            values.setdefault("lifecycle", "active")
            values.setdefault("priority", "none")
            values.setdefault("commitment", "uncommitted")
            values.setdefault("horizon", "inbox")
            values.setdefault("health", "unknown")
        else:
            values.setdefault("lifecycle", "active")
    _bounded_string(values.get("title"), "title", 512)
    _enum(values.get("kind"), _KINDS, "kind")
    _enum(values.get("lifecycle"), _LIFECYCLES, "lifecycle")
    if values["kind"] == "area":
        if _AREA_FORBIDDEN & values.keys():
            _invalid("areas cannot carry delivery state or hierarchy")
        _validate_optional(values, validate_motivation=validate_motivation)
        return values
    for name, allowed in (
        ("status", _STATUSES),
        ("priority", _PRIORITIES),
        ("commitment", _COMMITMENTS),
        ("horizon", _HORIZONS),
    ):
        _enum(values.get(name), allowed, name)
    _validate_lifecycle(values)
    _validate_optional(values, validate_motivation=validate_motivation)
    return values


def require_planning_profile(
    manifest: collections.CollectionManifest,
) -> collections.CollectionManifest:
    """Keep Planning operations from mutating observed-state collections."""
    if manifest.semantic_profile != "planning":
        raise CollectionError(
            "PLANNING_PROFILE_REQUIRED", "Planning actions require a planning semantic profile"
        )
    if manifest.storage.strategy != "markdown-items":
        raise CollectionError("INVALID_PLAN", "Planning requires markdown-items storage")
    required_types = {
        "title": "string",
        "kind": "string",
        "status": "string",
        "lifecycle": "string",
        "priority": "string",
        "commitment": "string",
        "horizon": "string",
        "health": "string",
    }
    for name, field_type in required_types.items():
        field = manifest.schema.fields.get(name)
        if field is None or field.type != field_type or (name == "title" and not field.required):
            raise CollectionError("INVALID_PLAN", "Planning manifest has incompatible core fields")
    return manifest


def create_collection(
    vault_root: Path, manifest_path: str, manifest_text: str, *, why: str, scaffold: bool = True
) -> dict[str, Any]:
    """Create one Planning collection through the shared guarded writer."""
    _validate_public_text(why, "why")
    root = Path(vault_root)
    record_governance.require_candidate_manifest_visibility(root, manifest_path)
    manifest = collections.parse_manifest_bytes(root, manifest_path, manifest_text.encode("utf-8"))
    require_planning_profile(manifest)
    if scaffold:
        manifest_text = _with_default_scaffold(manifest_text, manifest)
        manifest = collections.parse_manifest_bytes(
            root, manifest_path, manifest_text.encode("utf-8")
        )
        require_planning_profile(manifest)
    record_governance.require_proposed_manifest_visibility(root, manifest)
    return records.create_collection(
        vault_root, manifest_path, manifest_text, why=why, scaffold=scaffold
    )


def validate(
    vault_root: Path,
    *,
    mode: str,
    manifest_text: str,
    manifest_path: str | None = None,
    collection: str | collections.CollectionManifest | None = None,
    scaffold: bool = True,
) -> dict[str, Any]:
    """Validate a complete Planning manifest in create or revision mode."""
    root = Path(vault_root)
    if mode == "create" and manifest_path is not None and collection is None:
        records._validate_collection_create_for_profile(
            root,
            manifest_path,
            manifest_text,
            semantic_profile="planning",
            scaffold=scaffold,
        )
        manifest = require_planning_profile(
            collections.parse_manifest_bytes(
                root, root / manifest_path, manifest_text.encode("utf-8")
            )
        )
        effective_text = (
            _with_default_scaffold(manifest_text, manifest) if scaffold else manifest_text
        )
        result = records._validate_collection_create_for_profile(
            root,
            manifest_path,
            effective_text,
            semantic_profile="planning",
            scaffold=scaffold,
        )
        require_planning_profile(
            collections.parse_manifest_bytes(
                root, root / manifest_path, effective_text.encode("utf-8")
            )
        )
        return result
    if mode == "revision" and collection is not None and manifest_path is None:
        result = records._validate_collection_revision_for_profile(
            root,
            collection,
            manifest_text,
            semantic_profile="planning",
        )
        current = record_governance.resolve_collection(root, collection)
        require_planning_profile(
            collections.parse_manifest_bytes(
                root, root / current.path, manifest_text.encode("utf-8")
            )
        )
        return result
    raise CollectionError(
        "INVALID_PLAN_ARGUMENTS", "Planning validation mode and selector are invalid"
    )


def revise(
    vault_root: Path,
    collection: str | collections.CollectionManifest,
    *,
    manifest_text: str,
    expected_manifest_hash: str,
    expected_container_hash: str,
    why: str,
) -> dict[str, Any]:
    """Publish one guarded complete Planning manifest revision."""
    return records._lifecycle_mutation(
        vault_root,
        collection,
        action="revise",
        manifest_text=manifest_text,
        expected_manifest_hash=expected_manifest_hash,
        expected_container_hash=expected_container_hash,
        acknowledged_gap_codes=(),
        why=why,
        semantic_profile="planning",
    )


def rebaseline(
    vault_root: Path,
    collection: str | collections.CollectionManifest,
    *,
    expected_manifest_hash: str,
    expected_container_hash: str,
    acknowledged_gap_codes: tuple[str, ...] | list[str],
    why: str,
) -> dict[str, Any]:
    """Acknowledge the exact currently inspected Planning audit gaps."""
    if not isinstance(acknowledged_gap_codes, (tuple, list)) or not all(
        isinstance(code, str) for code in acknowledged_gap_codes
    ):
        raise CollectionError("INVALID_PLAN_GAPS", "gap acknowledgement is invalid")
    return records._lifecycle_mutation(
        vault_root,
        collection,
        action="rebaseline",
        manifest_text=None,
        expected_manifest_hash=expected_manifest_hash,
        expected_container_hash=expected_container_hash,
        acknowledged_gap_codes=tuple(acknowledged_gap_codes),
        why=why,
        semantic_profile="planning",
    )


def _with_default_scaffold(
    manifest_text: str,
    manifest: collections.CollectionManifest,
) -> str:
    newline = "\r\n" if "\r\n" in manifest_text else "\n"
    closing = list(re.finditer(r"(?m)^---\r?$", manifest_text))
    if len(closing) < 2:
        raise CollectionError("INVALID_COLLECTION_MANIFEST", "manifest requires frontmatter")
    additions: list[str] = []
    if manifest.item_filename is None and manifest.schema.natural_key == ("title",):
        additions.extend(("item_filename:", "  version: 1", "  fields: [title]"))
    if manifest.item_presentation is None:
        summary = [
            name
            for name in (
                "kind",
                "status",
                "lifecycle",
                "priority",
                "commitment",
                "horizon",
                "health",
            )
            if name in manifest.schema.fields
        ]
        long_text = [
            name
            for name in ("context", "description", "success_criteria", "notes")
            if (name in manifest.schema.fields and manifest.schema.fields[name].type == "string")
        ]
        relationships = [
            name for name, field in manifest.schema.fields.items() if field.type == "link"
        ][:4]
        additions.extend(
            (
                "item_presentation:",
                "  version: 1",
                "  title: title",
                f"  summary: [{', '.join(summary)}]",
            )
        )
        if long_text:
            additions.append(f"  long_text: [{', '.join(long_text)}]")
        if relationships:
            additions.append(f"  relationships: [{', '.join(relationships)}]")
    if not manifest.views:
        # Kept explicit so the ordinary YAML remains obvious to a human editor.
        additions.append("views:")
        for horizon in ("inbox", "week", "month", "quarter", "year", "multi-year"):
            additions.extend(
                (
                    f"  {horizon}:",
                    "    query:",
                    "      filters:",
                    "        - column: lifecycle",
                    "          op: eq",
                    "          value: active",
                    "        - column: horizon",
                    "          op: eq",
                    f"          value: {horizon}",
                )
            )
    if not additions:
        return manifest_text
    insertion = newline.join(additions) + newline
    index = closing[1].start()
    return manifest_text[:index] + insertion + manifest_text[index:]


def add(
    vault_root: Path,
    collection: str | collections.CollectionManifest,
    *,
    item: Mapping[str, Any],
    plan_id: str | None = None,
    expected_container_hash: str | None = None,
    body: str = "",
    why: str,
) -> dict[str, Any]:
    """Capture one Planning item through the shared Markdown-item writer."""
    manifest = record_governance.resolve_collection_for_mutation(vault_root, collection)
    require_planning_profile(manifest)
    record_governance.require_mutation_visibility(vault_root, manifest)
    _validate_public_text(why, "why")
    _validate_public_text(body, "body")
    values = normalize_item(item, validate_motivation=motivation_is_governed(manifest))
    _validate_declared_text(manifest, values)
    snapshot = record_formats.load_adapter(
        vault_root,
        manifest,
        authorize_path=record_governance.full_release_filter(vault_root),
    ).read()
    _validate_relationships(manifest, snapshot.records, plan_id or "__new__", values)
    return records.append_record(
        vault_root,
        manifest,
        item=values,
        item_key=plan_id,
        expected_container_hash=expected_container_hash,
        body=body,
        why=why,
        validate_snapshot=_validate_final_relationships,
    )


def query(
    vault_root: Path,
    collection: str | collections.CollectionManifest,
    *,
    filters: list[dict[str, Any]] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int = 100,
    aggregate: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column: str | None = None,
    continuation: str | None = None,
    output_format: str = "json",
    view: str | None = None,
    hierarchy_mode: str = "none",
    hierarchy_depth: int = 3,
    hierarchy_limit: int = 100,
    lifecycle: str = "active",
    include_agent_history: bool = False,
    authorize_path: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Run the shared bounded query evaluator over current Planning files."""
    if authorize_path is None:
        manifest = record_governance.resolve_collection(vault_root, collection)
        adapter_authorize_path = record_governance.full_release_filter(vault_root)
    else:
        selector = (
            collection.path
            if isinstance(collection, collections.CollectionManifest)
            else collection
        )
        manifest = collections.resolve_collection(
            vault_root, selector, authorize_path=authorize_path
        )
        adapter_authorize_path = authorize_path
    require_planning_profile(manifest)
    if lifecycle not in {"active", "archived", "all"}:
        raise CollectionError("INVALID_PLAN_ARGUMENTS", "lifecycle selector is invalid")
    if view is not None and (filters or lifecycle != "active"):
        raise CollectionError(
            "INVALID_PLAN_ARGUMENTS", "saved views cannot be combined with filters or lifecycle"
        )
    if hierarchy_mode != "none" and (aggregate is not None or output_format == "csv"):
        raise CollectionError(
            "INVALID_PLAN_ARGUMENTS", "hierarchy cannot be combined with aggregate or CSV"
        )
    _validate_authorized_snapshot(
        vault_root,
        manifest,
        adapter_authorize_path,
        validate_relationships=authorize_path is None,
    )
    effective_filters = None if view is not None else list(filters or [])
    if view is None and lifecycle != "all":
        assert effective_filters is not None
        effective_filters.append({"column": "lifecycle", "op": "eq", "value": lifecycle})
    if authorize_path is None:
        result = record_governance.query_collection(
            vault_root,
            manifest,
            semantic_profile="planning",
            filters=effective_filters,
            columns=columns,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            aggregate=aggregate,
            date_from=date_from,
            date_to=date_to,
            date_column=date_column,
            continuation=continuation,
            output_format=output_format,
            view=view,
            source_versions_for_rows=True,
        )
    else:
        result = record_formats.query_collection(
            vault_root,
            manifest,
            filters=effective_filters,
            columns=columns,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            aggregate=aggregate,
            date_from=date_from,
            date_to=date_to,
            date_column=date_column,
            continuation=continuation,
            output_format=output_format,
            view=view,
            authorize_path=authorize_path,
            source_versions_for_rows=True,
        )
    hierarchy = _hierarchy(
        vault_root,
        manifest,
        result.rows,
        mode=hierarchy_mode,
        depth=hierarchy_depth,
        limit=hierarchy_limit,
        authorize_path=adapter_authorize_path,
    )
    source_versions = list(result.source_versions)
    if isinstance(hierarchy, Mapping) and isinstance(hierarchy.get("nodes"), list):
        hierarchy_ids = {
            node["plan_id"]
            for node in hierarchy["nodes"]
            if isinstance(node, Mapping) and isinstance(node.get("plan_id"), str)
        }
        snapshot = record_formats.load_adapter(
            vault_root, manifest, authorize_path=adapter_authorize_path
        ).read()
        known_paths = {version.path for version in source_versions}
        source_versions.extend(
            record.source
            for record in snapshot.records
            if record.identity.key in hierarchy_ids and record.source.path not in known_paths
        )
    return _project_query(
        {
        "collection_id": result.collection_id,
        "snapshot": result.snapshot,
        "rows": result.rows,
        "returned": result.returned,
        "total_matched": result.total_matched,
        "truncated": result.truncated,
        "continuation": result.continuation,
        "derived": result.derived,
        "generated_at": dt.datetime.now(tz=dt.UTC).isoformat().replace("+00:00", "Z"),
        "rendered": result.rendered,
        "output_format": output_format,
        "aggregate": result.aggregate,
        "query": {
            **result.query,
            "lifecycle": lifecycle,
            "hierarchy_mode": hierarchy_mode,
            "hierarchy_depth": hierarchy_depth,
            "hierarchy_limit": hierarchy_limit,
        },
        "source_versions": [
            {"path": version.path, "hash": version.hash} for version in source_versions
        ],
        "view": result.view,
        "hierarchy": hierarchy,
        "agent_history": (
            records.agent_audit_history(
                vault_root, manifest, authorize_path=adapter_authorize_path
            )
            if include_agent_history
            else None
        ),
        },
        manifest,
    )


def _project_query(
    payload: Mapping[str, Any], manifest: collections.CollectionManifest
) -> dict[str, Any]:
    if not _valid_query_payload(payload, manifest):
        raise CollectionError(
            "PLAN_RESPONSE_TOO_LARGE", "Planning response is not safely representable"
    )
    projected = egress.project(_PlanningEnvelope(payload), egress.LEVEL_FULL, kind="planning_query")
    if projected is None or projected.get("withheld"):
        raise CollectionError(
            "PLAN_RESPONSE_TOO_LARGE", "Planning response is not safely representable"
        )
    return projected


_QUERY_EGRESS_KEYS = frozenset(
    {
        "collection_id",
        "snapshot",
        "rows",
        "returned",
        "total_matched",
        "truncated",
        "continuation",
        "derived",
        "generated_at",
        "rendered",
        "output_format",
        "aggregate",
        "query",
        "source_versions",
        "view",
        "hierarchy",
        "agent_history",
    }
)
_QUERY_DESCRIPTOR_KEYS = frozenset(
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
        "lifecycle",
        "hierarchy_mode",
        "hierarchy_depth",
        "hierarchy_limit",
    }
)
_PLANNING_SYSTEM_FIELDS = frozenset(
    {"collection_id", "plan_id", "item_version", "inferred", "ambiguous", "body"}
)


def _valid_query_payload(
    payload: Mapping[str, Any], manifest: collections.CollectionManifest
) -> bool:
    if set(payload) != _QUERY_EGRESS_KEYS:
        return False
    if (
        payload["collection_id"] != manifest.collection_id
        or not _hash(payload["snapshot"])
        or not isinstance(payload["rows"], list)
        or len(payload["rows"]) > 1_000
        or type(payload["returned"]) is not int
        or payload["returned"] != len(payload["rows"])
        or payload["returned"] < 0
        or type(payload["total_matched"]) is not int
        or payload["total_matched"] < payload["returned"]
        or type(payload["truncated"]) is not bool
        or payload["derived"] is not True
        or payload["output_format"] not in {"json", "markdown", "csv"}
        or type(payload["generated_at"]) is not str
        or not payload["generated_at"].endswith("Z")
        or type(payload["rendered"]) is not str
        or (
            payload["continuation"] is not None
            and (type(payload["continuation"]) is not str or len(payload["continuation"]) > 8_192)
        )
        or not _valid_query_descriptor(payload["query"], manifest)
        or not all(_valid_plan_row(row, manifest) for row in payload["rows"])
        or not _valid_source_versions(payload["source_versions"])
        or not _valid_view(payload["view"])
        or not _valid_hierarchy(payload["hierarchy"], manifest)
        or not _valid_agent_history(payload["agent_history"])
        or not _json_value(payload)
    ):
        return False
    try:
        return (
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            <= 64 * 1024
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        return False


def _valid_query_descriptor(value: Any, manifest: collections.CollectionManifest) -> bool:
    if not isinstance(value, Mapping) or set(value) != _QUERY_DESCRIPTOR_KEYS:
        return False
    fields = set(manifest.schema.fields) | _PLANNING_SYSTEM_FIELDS
    filters = value["filters"]
    columns = value["columns"]
    if not isinstance(filters, list) or len(filters) > 128:
        return False
    for filter_value in filters:
        if (
            not isinstance(filter_value, Mapping)
            or set(filter_value) - {"column", "op", "value"}
            or filter_value.get("column") not in fields
            or filter_value.get("op")
            not in {
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
            or not _json_value(filter_value.get("value"))
        ):
            return False
    if columns is not None and (
        not isinstance(columns, list)
        or len(columns) > len(fields)
        or any(type(column) is not str or column not in fields for column in columns)
    ):
        return False
    if any(
        value[name] is not None and value[name] not in fields for name in ("sort_by", "date_column")
    ):
        return False
    return (
        type(value["descending"]) is bool
        and type(value["expand_children"]) is bool
        and value["lifecycle"] in {"active", "archived", "all"}
        and value["hierarchy_mode"] in {"none", "ancestors", "descendants"}
        and type(value["hierarchy_depth"]) is int
        and 0 <= value["hierarchy_depth"] <= 8
        and type(value["hierarchy_limit"]) is int
        and 1 <= value["hierarchy_limit"] <= 500
        and all(
            value[name] is None or type(value[name]) is str
            for name in ("aggregate", "date_from", "date_to")
        )
    )


def _valid_plan_row(value: Any, manifest: collections.CollectionManifest) -> bool:
    if (
        not isinstance(value, Mapping)
        or set(value) - set(manifest.schema.fields) - _PLANNING_SYSTEM_FIELDS
    ):
        return False
    if not {"collection_id", "plan_id", "item_version", "inferred", "ambiguous"} <= set(value):
        return False
    if (
        value["collection_id"] != manifest.collection_id
        or memory_refs.normalize_id(value["plan_id"]) != value["plan_id"]
        or not _hash(value["item_version"])
        or type(value["inferred"]) is not bool
        or type(value["ambiguous"]) is not bool
        or ("body" in value and type(value["body"]) is not str)
    ):
        return False
    try:
        for name, field_value in value.items():
            if name in manifest.schema.fields:
                collections._validate_field_value(name, field_value, manifest.schema.fields[name])
    except (CollectionError, TypeError, ValueError):
        return False
    return _json_value(value)


def _valid_source_versions(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 1_001
        and all(
            isinstance(version, Mapping)
            and set(version) == {"path", "hash"}
            and _safe_path(version.get("path"))
            and _hash(version.get("hash"))
            for version in value
        )
    )


def _valid_view(value: Any) -> bool:
    return value is None or (
        isinstance(value, Mapping)
        and set(value) == {"name", "definition", "identity"}
        and type(value["name"]) is str
        and type(value["identity"]) is str
        and _json_value(value["definition"])
    )


def _valid_hierarchy(value: Any, manifest: collections.CollectionManifest) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping) or set(value) != {
        "mode",
        "roots",
        "nodes",
        "edges",
        "max_depth",
        "max_nodes",
        "truncated",
    }:
        return False
    return (
        value["mode"] in {"ancestors", "descendants"}
        and isinstance(value["roots"], list)
        and all(memory_refs.normalize_id(item) == item for item in value["roots"])
        and isinstance(value["nodes"], list)
        and len(value["nodes"]) <= 500
        and all(_valid_hierarchy_node(row, manifest) for row in value["nodes"])
        and isinstance(value["edges"], list)
        and len(value["edges"]) <= 500
        and all(
            isinstance(edge, Mapping)
            and set(edge) == {"parent", "child"}
            and memory_refs.normalize_id(edge["parent"]) == edge["parent"]
            and memory_refs.normalize_id(edge["child"]) == edge["child"]
            for edge in value["edges"]
        )
        and type(value["max_depth"]) is int
        and 0 <= value["max_depth"] <= 8
        and type(value["max_nodes"]) is int
        and 1 <= value["max_nodes"] <= 500
        and type(value["truncated"]) is bool
    )


def _valid_hierarchy_node(value: Any, manifest: collections.CollectionManifest) -> bool:
    if not isinstance(value, Mapping) or set(value) - set(manifest.schema.fields) - {"plan_id"}:
        return False
    if memory_refs.normalize_id(value.get("plan_id")) != value.get("plan_id"):
        return False
    try:
        normalize_item(
            value,
            apply_defaults=False,
            validate_motivation=motivation_is_governed(manifest),
        )
        for name, field_value in value.items():
            if name in manifest.schema.fields:
                collections._validate_field_value(name, field_value, manifest.schema.fields[name])
    except (CollectionError, TypeError, ValueError):
        return False
    return _json_value(value)


def _valid_agent_history(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping) or set(value) != {
        "status",
        "complete",
        "truncated",
        "events",
    }:
        return False
    if value["status"] not in {"baseline", "ok", "gap", "history_incomplete"}:
        return False
    if type(value["complete"]) is not bool or type(value["truncated"]) is not bool:
        return False
    events = value["events"]
    if not isinstance(events, list) or len(events) > 50:
        return False
    keys = {
        "transition_id",
        "parent_id",
        "operation",
        "item_key",
        "canonical_path",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "rationale",
    }
    for event in events:
        if not isinstance(event, Mapping) or set(event) != keys:
            return False
        if (
            not isinstance(event["transition_id"], str)
            or re.fullmatch(r"[0-9a-f]{24}", event["transition_id"]) is None
            or event["parent_id"] not in {"baseline", "absent"}
            and (
                not isinstance(event["parent_id"], str)
                or re.fullmatch(r"[0-9a-f]{24}", event["parent_id"]) is None
            )
            or event["operation"] not in {"plan_create", "plan_add", "plan_update", "plan_triage"}
            or (
                event["item_key"] is not None
                and memory_refs.normalize_id(event["item_key"]) != event["item_key"]
            )
            or not _safe_path(event["canonical_path"])
            or not isinstance(event["rationale"], str)
            or len(event["rationale"].encode("utf-8")) > 512
            or any(
                event[name] is not None and not _hash(event[name])
                for name in keys
                if name.endswith("hash")
            )
        ):
            return False
    return True


def _safe_path(value: Any) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value.encode("utf-8")) <= 2_048
        and not value.startswith(("/", "\\"))
        and "\\" not in value
        and "\0" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _hash(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _json_value(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> bool:
    nodes = [0] if nodes is None else nodes
    nodes[0] += 1
    if nodes[0] > 4_096 or depth > 8:
        return False
    if value is None or type(value) in {bool, int}:
        return True
    if type(value) is float:
        return value == value and value not in {float("inf"), float("-inf")}
    if type(value) is str:
        return len(value.encode("utf-8")) <= 32 * 1024
    if isinstance(value, list):
        return all(_json_value(item, depth=depth + 1, nodes=nodes) for item in value)
    if isinstance(value, Mapping):
        return all(
            type(key) is str
            and len(key.encode("utf-8")) <= 512
            and _json_value(item, depth=depth + 1, nodes=nodes)
            for key, item in value.items()
        )
    return False


def _hierarchy(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    roots: list[dict[str, Any]],
    *,
    mode: str,
    depth: int,
    limit: int,
    authorize_path: Callable[[str], bool] | None,
) -> dict[str, Any] | None:
    if mode == "none":
        return None
    if mode not in {"ancestors", "descendants"} or not 0 <= depth <= 8 or not 1 <= limit <= 500:
        raise CollectionError("INVALID_PLAN_ARGUMENTS", "hierarchy controls are invalid")
    snapshot = record_formats.load_adapter(
        vault_root, manifest, authorize_path=authorize_path
    ).read()
    rows = {
        record.identity.key: {**record.values, "plan_id": record.identity.key}
        for record in snapshot.records
    }
    root_ids = [row["plan_id"] for row in roots]
    truncated = len(root_ids) > limit
    root_ids = root_ids[:limit]
    parent_of: dict[str, str] = {}
    for key, row in rows.items():
        parent = row.get("parent")
        parsed = parse_plan_ref(parent) if isinstance(parent, str) else None
        if parsed is not None:
            parent_of[key] = parsed[1]
    children: dict[str, list[str]] = {}
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)
    for values in children.values():
        values.sort()
    seen: set[str] = set(root_ids)
    edges: list[dict[str, str]] = []
    frontier = list(root_ids)
    current_depth = 0
    while frontier and current_depth < depth:
        next_frontier: list[str] = []
        for node in frontier:
            if mode == "ancestors":
                related = [parent_of[node]] if node in parent_of else []
            else:
                related = children.get(node, [])
            for other in related:
                if other not in rows:
                    continue
                if other not in seen and len(seen) >= limit:
                    truncated = True
                    break
                edge = (
                    {"parent": other, "child": node}
                    if mode == "ancestors"
                    else {"parent": node, "child": other}
                )
                if edge not in edges:
                    edges.append(edge)
                if other in seen:
                    continue
                seen.add(other)
                next_frontier.append(other)
            if truncated:
                break
        frontier = next_frontier
        current_depth += 1
        if truncated:
            break
    return {
        "mode": mode,
        "roots": root_ids,
        "nodes": [rows[key] for key in sorted(seen)],
        "edges": edges,
        "max_depth": depth,
        "max_nodes": limit,
        "truncated": truncated,
    }


def _validate_authorized_snapshot(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    authorize_path: Callable[[str], bool] | None,
    *,
    validate_relationships: bool = True,
) -> None:
    snapshot = record_formats.load_adapter(
        vault_root, manifest, authorize_path=authorize_path
    ).read()
    governed = motivation_is_governed(manifest)
    for record in snapshot.records:
        normalize_item(record.values, apply_defaults=False, validate_motivation=governed)
    if validate_relationships:
        _validate_relationships(manifest, snapshot.records, None, None)


def inspect(vault_root: Path, collection: str | collections.CollectionManifest) -> dict[str, Any]:
    """Report the current Planning contract without repairing canonical files."""
    manifest = record_governance.resolve_collection(vault_root, collection)
    if manifest.semantic_profile != "planning":
        raise CollectionError(
            "PLANNING_PROFILE_REQUIRED", "Planning actions require a planning semantic profile"
        )
    authorize_path = record_governance.full_release_filter(vault_root)
    result = record_governance.inspect_collection(vault_root, manifest)
    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, list)
    try:
        require_planning_profile(manifest)
    except CollectionError as error:
        diagnostics.append({"code": error.code, "reason": error.reason})
        return _project_inspection(
            {
                "kind": result.get("kind"),
                "report_only": result.get("report_only"),
                "contract": {
                    key: result["contract"].get(key)
                    for key in (
                        "collection_id",
                        "path",
                        "title",
                        "semantic_profile",
                        "schema_version",
                        "storage",
                    )
                },
                "snapshot": result.get("snapshot"),
                "source_versions": result.get("source_versions"),
                "diagnostics": diagnostics[:64],
                "audit": result.get("audit"),
                "saved_views": result.get("saved_views"),
            },
            manifest,
        )
    if len(diagnostics) < 64:
        try:
            _validate_authorized_snapshot(vault_root, manifest, authorize_path)
        except CollectionError as error:
            diagnostics.append({"code": error.code, "reason": error.reason})
    contract = result.get("contract")
    if not isinstance(contract, Mapping):
        raise CollectionError(
            "PLAN_RESPONSE_TOO_LARGE", "Planning inspection is not safely representable"
        )
    payload = {
        "kind": result.get("kind"),
        "report_only": result.get("report_only"),
        "contract": {
            key: contract.get(key)
            for key in (
                "collection_id",
                "path",
                "title",
                "semantic_profile",
                "schema_version",
                "storage",
            )
        },
        "snapshot": result.get("snapshot"),
        "source_versions": result.get("source_versions"),
        "diagnostics": diagnostics,
        "audit": result.get("audit"),
        "saved_views": result.get("saved_views"),
        **(
            {"lifecycle_guards": result["lifecycle_guards"]} if "lifecycle_guards" in result else {}
        ),
        **({"presentation": result["presentation"]} if "presentation" in result else {}),
    }
    return _project_inspection(payload, manifest)


def _project_inspection(
    payload: Mapping[str, Any], manifest: collections.CollectionManifest
) -> dict[str, Any]:
    if not _valid_inspection(payload, manifest):
        raise CollectionError(
            "PLAN_RESPONSE_TOO_LARGE", "Planning inspection is not safely representable"
        )
    projected = egress.project(
        _PlanningEnvelope(payload), egress.LEVEL_FULL, kind="planning_inspection"
    )
    if projected is None or projected.get("withheld"):
        raise CollectionError(
            "PLAN_RESPONSE_TOO_LARGE", "Planning inspection is not safely representable"
        )
    return projected


def _valid_inspection(payload: Mapping[str, Any], manifest: collections.CollectionManifest) -> bool:
    required = {
        "kind",
        "report_only",
        "contract",
        "snapshot",
        "source_versions",
        "diagnostics",
        "audit",
        "saved_views",
    }
    optional = {name for name in ("presentation", "lifecycle_guards") if name in payload}
    if not required <= set(payload) or set(payload) - required != optional:
        return False
    contract = payload["contract"]
    if not isinstance(contract, Mapping) or set(contract) != {
        "collection_id",
        "path",
        "title",
        "semantic_profile",
        "schema_version",
        "storage",
    }:
        return False
    storage = contract["storage"]
    if (
        payload["kind"] != "collection"
        or payload["report_only"] is not True
        or contract["collection_id"] != manifest.collection_id
        or contract["path"] != manifest.path
        or contract["title"] != manifest.title
        or contract["semantic_profile"] != "planning"
        or type(contract["schema_version"]) is not int
        or not isinstance(storage, Mapping)
        or set(storage) != {"strategy", "source", "format_version"}
        or storage.get("strategy") != manifest.storage.strategy
        or storage.get("source") != manifest.storage.source
        or storage.get("format_version") != manifest.storage.format_version
        or (payload["snapshot"] is not None and not _hash(payload["snapshot"]))
        or not _valid_source_versions(payload["source_versions"])
        or not isinstance(payload["diagnostics"], list)
        or len(payload["diagnostics"]) > 64
        or not all(
            isinstance(item, Mapping)
            and set(item) == {"code", "reason"}
            and isinstance(item["code"], str)
            and isinstance(item["reason"], str)
            and len(item["code"].encode("utf-8")) <= 128
            and len(item["reason"].encode("utf-8")) <= 512
            for item in payload["diagnostics"]
        )
        or not _valid_inspection_audit(payload["audit"])
        or not isinstance(payload["saved_views"], list)
        or len(payload["saved_views"]) > 64
        or not all(_valid_view(view) for view in payload["saved_views"])
        or (
            "presentation" in payload
            and not _valid_presentation_inspection(payload["presentation"])
        )
        or (
            "lifecycle_guards" in payload
            and (
                not isinstance(payload["lifecycle_guards"], Mapping)
                or set(payload["lifecycle_guards"])
                != {"expected_manifest_hash", "expected_container_hash"}
                or not all(_hash(value) for value in payload["lifecycle_guards"].values())
            )
        )
        or not _json_value(payload)
    ):
        return False
    try:
        return (
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            <= 64 * 1024
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        return False


def _valid_presentation_inspection(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"items", "counts", "truncated"}
        or not isinstance(value["items"], list)
        or len(value["items"]) > 128
        or not isinstance(value["counts"], Mapping)
        or len(value["counts"]) > 16
        or type(value["truncated"]) is not bool
    ):
        return False
    if any(
        type(state) is not str
        or len(state.encode("utf-8")) > 64
        or type(count) is not int
        or count < 0
        for state, count in value["counts"].items()
    ):
        return False
    for item in value["items"]:
        if (
            not isinstance(item, Mapping)
            or not {"item_key", "path", "version", "state", "remedy"} <= set(item)
            or set(item) - {"item_key", "path", "version", "state", "remedy", "location"}
            or any(
                type(item[name]) is not str or len(item[name].encode("utf-8")) > 1024
                for name in ("item_key", "path", "version", "state", "remedy")
            )
            or item["state"] not in value["counts"]
        ):
            return False
    return True


def _valid_inspection_audit(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    status = value.get("status")
    expected = {"status", "gaps"}
    if status == "acknowledged_gap":
        expected |= {"discontinuity", "discontinuities"}
    if (
        set(value) != expected
        or status not in {"baseline", "ok", "gap", "acknowledged_gap", "history_incomplete"}
        or not isinstance(value.get("gaps"), list)
        or len(value["gaps"]) > 32
        or not all(
            type(gap) is str and len(gap.encode("utf-8")) <= 256 for gap in value["gaps"]
        )
    ):
        return False
    if status != "acknowledged_gap":
        return True
    discontinuity = value["discontinuity"]
    discontinuities = value["discontinuities"]
    return (
        _valid_audit_discontinuity(discontinuity)
        and isinstance(discontinuities, list)
        and 1 <= len(discontinuities) <= 16
        and all(_valid_audit_discontinuity(item) for item in discontinuities)
        and discontinuities[0] == discontinuity
    )


def _valid_audit_discontinuity(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value)
        == {
            "provenance_continuity",
            "prior_head",
            "acknowledged_gap_codes",
            "rationale",
            "checkpoint_transition",
            "gap_fingerprint",
            "checkpoint_snapshot_hash",
        }
        and value["provenance_continuity"] is False
        and isinstance(value["prior_head"], str)
        and (
            value["prior_head"] == "baseline"
            or re.fullmatch(r"[0-9a-f]{24}", value["prior_head"]) is not None
        )
        and isinstance(value["acknowledged_gap_codes"], list)
        and value["acknowledged_gap_codes"]
        == sorted(set(value["acknowledged_gap_codes"]))
        and all(
            type(code) is str and 0 < len(code.encode("utf-8")) <= 256
            for code in value["acknowledged_gap_codes"]
        )
        and isinstance(value["rationale"], str)
        and 0 < len(value["rationale"].encode("utf-8")) <= 512
        and isinstance(value["checkpoint_transition"], str)
        and re.fullmatch(r"[0-9a-f]{24}", value["checkpoint_transition"]) is not None
        and _hash(value["gap_fingerprint"])
        and _hash(value["checkpoint_snapshot_hash"])
    )


def _validate_query_egress(value: Mapping[str, Any]) -> dict[str, Any] | None:
    if set(value) != _QUERY_EGRESS_KEYS or not _json_value(value):
        return None
    hierarchy = value.get("hierarchy")
    if hierarchy is not None and (
        not isinstance(hierarchy, Mapping)
        or set(hierarchy)
        != {"mode", "roots", "nodes", "edges", "max_depth", "max_nodes", "truncated"}
    ):
        return None
    if not _valid_source_versions(value.get("source_versions")) or not _valid_view(
        value.get("view")
    ):
        return None
    return dict(value)


def _validate_inspection_egress(value: Mapping[str, Any]) -> dict[str, Any] | None:
    expected = {
        "kind",
        "report_only",
        "contract",
        "snapshot",
        "source_versions",
        "diagnostics",
        "audit",
        "saved_views",
    }
    optional = {name for name in ("presentation", "lifecycle_guards") if name in value}
    if (
        not expected <= set(value)
        or set(value) - expected != optional
        or not _json_value(value)
        or not _valid_source_versions(value.get("source_versions"))
        or ("presentation" in value and not _valid_presentation_inspection(value["presentation"]))
        or (
            "lifecycle_guards" in value
            and (
                not isinstance(value["lifecycle_guards"], Mapping)
                or set(value["lifecycle_guards"])
                != {"expected_manifest_hash", "expected_container_hash"}
                or not all(_hash(item) for item in value["lifecycle_guards"].values())
            )
        )
    ):
        return None
    return dict(value)


def update(
    vault_root: Path,
    collection: str | collections.CollectionManifest,
    *,
    plan_id: str,
    changes: Mapping[str, Any],
    body: str | None = None,
    expected_container_hash: str,
    expected_item_version: str,
    why: str,
) -> dict[str, Any]:
    """Apply a guarded authored-property update through the shared writer."""
    manifest = record_governance.resolve_collection_for_mutation(vault_root, collection)
    require_planning_profile(manifest)
    record_governance.require_mutation_visibility(vault_root, manifest)
    _validate_public_text(why, "why")
    if body is not None:
        _validate_public_text(body, "body")
    snapshot = record_formats.load_adapter(
        vault_root,
        manifest,
        authorize_path=record_governance.full_release_filter(vault_root),
    ).read()
    matches = [record for record in snapshot.records if record.identity.key == plan_id]
    if len(matches) != 1 or matches[0].ambiguous:
        if matches:
            raise CollectionError("AMBIGUOUS_RECORD", "plan identity is ambiguous")
        raise CollectionError("PLAN_NOT_FOUND", "plan was not found")
    changed, deleted = _normalize_changes(manifest, changes)
    final = dict(matches[0].values)
    for name in deleted:
        final.pop(name, None)
    final.update(changed)
    if final == dict(matches[0].values) and (body is None or body == matches[0].body):
        raise CollectionError("INVALID_PLAN", "Planning update does not change authored state")
    _require_same_area_side(matches[0].values, final)
    normalize_item(
        final,
        apply_defaults=False,
        validate_motivation=motivation_is_governed(manifest),
    )
    _validate_declared_text(manifest, final)
    _validate_relationships(manifest, snapshot.records, plan_id, final)
    return records.update_record(
        vault_root,
        manifest,
        item_key=plan_id,
        changes=changed,
        expected_container_hash=expected_container_hash,
        expected_item_version=expected_item_version,
        why=why,
        delete_fields=deleted,
        body=body,
        validate_snapshot=_validate_final_relationships,
    )


def triage(
    vault_root: Path,
    collection: str | collections.CollectionManifest,
    *,
    plan_id: str,
    transition: Mapping[str, Any],
    expected_container_hash: str,
    expected_item_version: str,
    why: str,
) -> dict[str, Any]:
    """Apply the deliberately narrow Planning transition workflow."""
    allowed = {"kind", "status", "priority", "commitment", "horizon", "area", "parent"}
    if not transition or set(transition) - allowed:
        raise CollectionError("INVALID_PLAN_ARGUMENTS", "triage transition is invalid")
    manifest = record_governance.resolve_collection_for_mutation(vault_root, collection)
    require_planning_profile(manifest)
    record_governance.require_mutation_visibility(vault_root, manifest)
    _validate_public_text(why, "why")
    snapshot = record_formats.load_adapter(
        vault_root,
        manifest,
        authorize_path=record_governance.full_release_filter(vault_root),
    ).read()
    matches = [record for record in snapshot.records if record.identity.key == plan_id]
    if len(matches) != 1 or matches[0].ambiguous:
        if matches:
            raise CollectionError("AMBIGUOUS_RECORD", "plan identity is ambiguous")
        raise CollectionError("PLAN_NOT_FOUND", "plan was not found")
    if matches[0].values.get("lifecycle") != "active" or matches[0].values.get("kind") == "area":
        raise CollectionError("INVALID_PLAN", "triage requires an active deliverable plan")
    if any(value is None and name not in {"area", "parent"} for name, value in transition.items()):
        raise CollectionError("INVALID_PLAN_ARGUMENTS", "triage transition is invalid")
    final = dict(matches[0].values)
    for name, value in transition.items():
        if value is None:
            final.pop(name, None)
        else:
            final[name] = value
    if final == dict(matches[0].values):
        raise CollectionError("INVALID_PLAN", "Planning triage does not change authored state")
    _require_same_area_side(matches[0].values, final)
    normalize_item(
        final,
        apply_defaults=False,
        validate_motivation=motivation_is_governed(manifest),
    )
    _validate_declared_text(manifest, final)
    _validate_relationships(manifest, snapshot.records, plan_id, final)
    return records.update_record(
        vault_root,
        manifest,
        item_key=plan_id,
        changes={name: value for name, value in transition.items() if value is not None},
        expected_container_hash=expected_container_hash,
        expected_item_version=expected_item_version,
        why=why,
        operation="triage",
        delete_fields=tuple(name for name, value in transition.items() if value is None),
        validate_snapshot=_validate_final_relationships,
    )


def _validate_lifecycle(values: Mapping[str, Any]) -> None:
    status = values["status"]
    lifecycle = values["lifecycle"]
    commitment = values["commitment"]
    horizon = values["horizon"]
    if status == "candidate" and (
        lifecycle != "active" or commitment != "uncommitted" or horizon != "inbox"
    ):
        _invalid("candidate plans must be active, uncommitted, and inbox")
    if status == "planned" and (
        lifecycle != "active"
        or commitment not in {"considering", "committed"}
        or horizon == "inbox"
    ):
        _invalid("planned plans require active considering or committed non-inbox intent")
    if status in {"active", "blocked", "completed"} and (
        commitment != "committed"
        or horizon == "inbox"
        or (status != "completed" and lifecycle != "active")
    ):
        _invalid("active, blocked, and completed plans require committed non-inbox intent")
    if lifecycle == "archived" and status not in {"completed", "cancelled"}:
        _invalid("only completed or cancelled plans may be archived")


def _require_same_area_side(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if (before.get("kind") == "area") != (after.get("kind") == "area"):
        raise CollectionError("INVALID_PLAN", "plans cannot cross the area boundary")


def _normalize_changes(
    manifest: collections.CollectionManifest, changes: Mapping[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if not isinstance(changes, Mapping):
        raise CollectionError("INVALID_PLAN_ARGUMENTS", "changes must be an object")
    system = {"type", "collection_id", "plan_id", "schema_version", "item_version", "plan_audit"}
    if set(changes) & system:
        raise CollectionError("INVALID_PLAN", "system fields cannot be changed")
    unknown = set(changes) - set(manifest.schema.fields)
    if unknown:
        raise CollectionError("INVALID_PLAN", "changes include an undeclared field")
    deleted: list[str] = []
    values: dict[str, Any] = {}
    for name, value in changes.items():
        if value is None:
            spec = manifest.schema.fields.get(name)
            if name not in _OPTIONAL and (spec is None or spec.required):
                raise CollectionError("INVALID_PLAN", "required fields cannot be deleted")
            deleted.append(name)
        else:
            values[name] = value
    return values, tuple(deleted)


def _validate_relationships(
    manifest: collections.CollectionManifest,
    records_in_snapshot: tuple[record_formats.Record, ...],
    plan_id: str | None,
    candidate: Mapping[str, Any] | None,
) -> None:
    """Validate the complete visible graph before a Planning write is staged."""
    plans: dict[str, dict[str, Any]] = {}
    for record in records_in_snapshot:
        if record.ambiguous or record.identity.key in plans:
            _relation_error()
        values = dict(record.values)
        normalize_item(
            values,
            apply_defaults=False,
            validate_motivation=motivation_is_governed(manifest),
        )
        plans[record.identity.key] = values
    if plan_id is not None and candidate is not None:
        plans[plan_id] = dict(candidate)
    parents: dict[str, str] = {}
    for key, values in plans.items():
        kind = values["kind"]
        active = values["lifecycle"] == "active"
        parent = _relation_target(manifest, values.get("parent"))
        area = _relation_target(manifest, values.get("area"))
        if kind == "area":
            if parent is not None or area is not None:
                _relation_error()
            continue
        if area is not None:
            target = plans.get(area)
            if (
                target is None
                or target["kind"] != "area"
                or (active and target["lifecycle"] != "active")
            ):
                _relation_error()
        required_parent = (
            active and values["commitment"] == "committed" and kind in {"initiative", "work-item"}
        )
        if kind == "outcome":
            if parent is not None:
                _relation_error()
        elif parent is None:
            if required_parent:
                _relation_error()
        else:
            target = plans.get(parent)
            expected = "outcome" if kind == "initiative" else "initiative"
            if (
                target is None
                or target["kind"] != expected
                or (active and target["lifecycle"] != "active")
            ):
                _relation_error()
            if (
                area is not None
                and target.get("area") is not None
                and area != _relation_target(manifest, target["area"])
            ):
                _relation_error()
            parents[key] = parent
    for start in parents:
        visited: set[str] = set()
        current = start
        while current in parents:
            current = parents[current]
            if current == start or current in visited:
                _relation_error()
            visited.add(current)
    for key, values in plans.items():
        if values["lifecycle"] != "archived":
            continue
        if values["kind"] == "area":
            if any(
                other["lifecycle"] == "active"
                and _relation_target(manifest, other.get("area")) == key
                for other in plans.values()
            ):
                _relation_error()
        elif any(
            other["lifecycle"] == "active"
            and _relation_target(manifest, other.get("parent")) == key
            for other in plans.values()
        ):
            _relation_error()


def _validate_final_relationships(
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot,
    plan_id: str,
    values: Mapping[str, Any],
) -> None:
    before = next(
        (record.values for record in snapshot.records if record.identity.key == plan_id), values
    )
    _require_same_area_side(before, values)
    normalize_item(
        values,
        apply_defaults=False,
        validate_motivation=motivation_is_governed(manifest),
    )
    _validate_declared_text(manifest, values)
    _validate_relationships(manifest, snapshot.records, plan_id, values)


def _validate_declared_text(
    manifest: collections.CollectionManifest, values: Mapping[str, Any]
) -> None:
    """Refuse unrenderable authored text before shared record hashing."""
    for name, value in values.items():
        if name in manifest.schema.fields:
            _validate_authored_text(value, name)


def _validate_authored_text(value: Any, name: str) -> None:
    if type(value) is str:
        try:
            valid = len(value.encode("utf-8")) <= 32 * 1024
        except UnicodeEncodeError:
            valid = False
        if not valid:
            _invalid(f"{name} is invalid")
    elif isinstance(value, list):
        for item in value:
            _validate_authored_text(item, name)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _validate_authored_text(key, name)
            _validate_authored_text(item, name)


def _relation_target(manifest: collections.CollectionManifest, value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _relation_error()
    parsed = parse_plan_ref(value)
    if parsed is None or parsed[0] != manifest.collection_id:
        _relation_error()
    return parsed[1]


def _relation_error() -> Never:
    raise CollectionError("INVALID_PLAN_RELATION", "Planning relationship is not available")


def _validate_optional(values: Mapping[str, Any], *, validate_motivation: bool = True) -> None:
    for name in ("health",):
        if name in values:
            _enum(values[name], _HEALTH, name)
    start = _date(values.get("window_start"), "window_start") if "window_start" in values else None
    end = _date(values.get("window_end"), "window_end") if "window_end" in values else None
    if start is not None and end is not None and start > end:
        _invalid("window_start must not be after window_end")
    for name in ("area", "parent"):
        if name in values and (
            type(values[name]) is not str or parse_plan_ref(values[name]) is None
        ):
            if name == "area":
                _invalid("area must be exomem://plan/<collection-uuid>/<plan-uuid>")
            _invalid(f"{name} must be a Planning reference")
    if "tags" in values:
        tags = values["tags"]
        if (
            not isinstance(tags, list)
            or len(tags) > 32
            or not all(type(tag) is str for tag in tags)
            or len(set(tags)) != len(tags)
        ):
            _invalid("tags must be a distinct bounded list")
        for tag in tags:
            _bounded_string(tag, "tag", 128)
    _validate_evidence(values.get("progress_evidence"))
    _validate_execution(values.get("execution"))
    if validate_motivation:
        _validate_motivation(values.get("motivation"))


def _validate_motivation(value: Any) -> None:
    """Refuse anything but a bounded list of `exomem://memory/` refs.

    Motivation is a reference from a plan to the knowledge that motivates it,
    never the reverse: only `exomem://memory/` refs are accepted, so a
    `exomem://plan/...` reference (or any other malformed value) is refused
    the same way an invalid `progress_evidence` collection ref is refused.
    Planning items stay outside recall and the graph regardless — this field
    never becomes a relation-graph edge, it is validated shape only.
    """
    if value is None:
        return
    if not isinstance(value, list) or len(value) > 16:
        _invalid("motivation must be a bounded list")
    for ref in value:
        if memory_refs.parse_memory_ref(ref) is None:
            _invalid("motivation reference is invalid")


def _validate_evidence(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) > 16:
        _invalid("progress_evidence must be a bounded list")
    for descriptor in value:
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"collection", "role", "view"}:
            _invalid("progress evidence descriptor is invalid")
        if memory_refs.parse_memory_ref(descriptor["collection"]) is None:
            _invalid("progress evidence collection is invalid")
        if descriptor["role"] not in {"progress", "completion"}:
            _invalid("progress evidence role is invalid")
        _bounded_string(descriptor["view"], "progress evidence view", 128)


def _validate_execution(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) > 16:
        _invalid("execution must be a bounded list")
    for descriptor in value:
        if not isinstance(descriptor, Mapping) or set(descriptor) not in (
            {"kind", "ref"},
            {"kind", "ref", "label"},
        ):
            _invalid("execution descriptor is invalid")
        kind = descriptor.get("kind")
        if (
            not isinstance(kind, str)
            or len(kind.encode("ascii", "ignore")) != len(kind)
            or len(kind) > 64
            or not _EXECUTION_KIND.fullmatch(kind)
        ):
            _invalid("execution kind is invalid")
        _bounded_string(descriptor.get("ref"), "execution ref", 2048)
        if "label" in descriptor:
            _bounded_string(descriptor["label"], "execution label", 256)


def _date(value: Any, name: str) -> dt.date:
    if type(value) is not str:
        _invalid(f"{name} must be an ISO date")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        _invalid(f"{name} must be an ISO date")


def _enum(value: Any, allowed: set[str] | frozenset[str], name: str) -> None:
    if type(value) is not str or value not in allowed:
        if name == "kind":
            _invalid("kind must be one of: area, outcome, initiative, work-item")
        _invalid(f"{name} is invalid")


def _bounded_string(value: Any, name: str, maximum: int) -> None:
    try:
        valid = type(value) is str and bool(value) and len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        valid = False
    if not valid:
        _invalid(f"{name} is invalid")


def _validate_public_text(value: Any, name: str) -> None:
    """Keep Planning's public mutation boundary inside its bounded error plane."""
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _invalid(f"{name} is invalid")


def _invalid(reason: str) -> Never:
    raise CollectionError("INVALID_PLAN", reason)


egress.register_projector("planning_query", _QUERY_EGRESS_KEYS, validator=_validate_query_egress)
egress.register_projector(
    "planning_inspection",
    (
        "kind",
        "report_only",
        "contract",
        "snapshot",
        "source_versions",
        "diagnostics",
        "audit",
        "saved_views",
        "presentation",
        "lifecycle_guards",
    ),
    validator=_validate_inspection_egress,
)
