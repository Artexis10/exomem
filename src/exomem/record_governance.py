"""L6-only governance boundary shared by future Records command surfaces."""

from __future__ import annotations

import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, unquote

from . import access, memory_refs, mutation_terminal, query_data, record_formats, vault
from . import structured_collections as collections
from .governance import egress
from .governance.principal import OWNER_AUDIENCE, effective_principal

_PUBLIC_LINK_INDEX_RAW_CANDIDATES = 512
_INTERNAL_LINK_INDEX_RAW_CANDIDATES = 4_096
_PUBLIC_LINK_INDEX_AUTHORIZED_CANDIDATES = 256
_INTERNAL_LINK_INDEX_AUTHORIZED_CANDIDATES = 2_048
_PUBLIC_LINK_INDEX_AUTHORIZED_BYTES = 4 * 1024 * 1024
_INTERNAL_LINK_INDEX_AUTHORIZED_BYTES = 32 * 1024 * 1024
_MAX_LINK_INDEX_ENTRY_BYTES = 256 * 1024
_PRESENTATION_FINDING_LIMIT = 128


@dataclass(frozen=True, slots=True)
class _RecordEnvelope:
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


_INSPECTION_KEYS = frozenset(
    {
        "kind",
        "report_only",
        "contract",
        "legacy",
        "snapshot",
        "source_versions",
        "diagnostics",
        "audit",
        "saved_views",
        "lifecycle_guards",
        "presentation",
        "observed_values",
    }
)
_INSPECTION_VIEW_QUERY_KEYS = frozenset(
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
_INSPECTION_VIEW_FILTER_KEYS = frozenset({"column", "op", "value"})
_INSPECTION_VIEW_OPS = frozenset(
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
_INSPECTION_INVALID = object()


def _inspection_string(value: Any, *, maximum: int, nonempty: bool = True) -> str | None:
    if type(value) is not str:
        return None
    try:
        if len(value.encode("utf-8")) > maximum:
            return None
    except UnicodeEncodeError:
        return None
    if nonempty and not value:
        return None
    return value


def _inspection_path(value: Any) -> str | None:
    path = _inspection_string(value, maximum=2_048)
    if (
        path is None
        or path.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", path) is not None
        or "\\" in path
        or "\0" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        return None
    return path


def _inspection_hash(value: Any) -> str | None:
    return value if type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _inspection_identifier(value: Any) -> str | None:
    normalized = memory_refs.normalize_id(value)
    return normalized if normalized == value else None


def _inspection_json(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> Any:
    """Return only bounded plain JSON; never stringify an arbitrary object."""
    nodes = [0] if nodes is None else nodes
    nodes[0] += 1
    if nodes[0] > 256 or depth > 8:
        return _INSPECTION_INVALID
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        return value if isfinite(value) else _INSPECTION_INVALID
    if type(value) is str:
        return value if len(value.encode("utf-8")) <= 16 * 1024 else _INSPECTION_INVALID
    if isinstance(value, list | tuple):
        list_result = [_inspection_json(item, depth=depth + 1, nodes=nodes) for item in value]
        return (
            list_result
            if all(item is not _INSPECTION_INVALID for item in list_result)
            else _INSPECTION_INVALID
        )
    if isinstance(value, Mapping):
        mapping_result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or len(key.encode("utf-8")) > 512:
                return _INSPECTION_INVALID
            projected = _inspection_json(item, depth=depth + 1, nodes=nodes)
            if projected is _INSPECTION_INVALID:
                return _INSPECTION_INVALID
            mapping_result[key] = projected
        return mapping_result
    return _INSPECTION_INVALID


def _inspection_field_name(value: Any) -> str | None:
    return _inspection_string(value, maximum=128)


def _inspection_observed_values(value: Any) -> dict[str, dict[str, Any]] | None:
    """Re-validate the free-string vocabulary summary before it reaches egress."""
    if not isinstance(value, Mapping) or len(value) > collections._MAX_SCHEMA_FIELDS:
        return None
    normalized: dict[str, dict[str, Any]] = {}
    for name, summary in value.items():
        field = _inspection_field_name(name)
        if (
            field is None
            or not isinstance(summary, Mapping)
            or set(summary) != {"values", "truncated"}
            or type(summary.get("truncated")) is not bool
            or not isinstance(summary.get("values"), list)
            or len(summary["values"]) > record_formats._MAX_OBSERVED_VALUES
        ):
            return None
        entries: list[dict[str, Any]] = []
        for entry in summary["values"]:
            if not isinstance(entry, Mapping) or set(entry) != {
                "value",
                "count",
                "value_truncated",
            }:
                return None
            # Deliberately looser than the producer's 120-CHARACTER cut: this is a
            # byte ceiling on a string that may be 120 wide characters, so it must
            # not double as a re-assertion of the cut.
            observed = _inspection_string(entry.get("value"), maximum=512)
            count, cut = entry.get("count"), entry.get("value_truncated")
            if observed is None or type(count) is not int or count < 1 or type(cut) is not bool:
                return None
            # Distinctness is NOT asserted here: two distinct values sharing the
            # display window collapse to the same display string, and both are
            # flagged. The invariant lives at the producer instead --
            # _observed_field_values keys its counter on the full stripped
            # value, so one entry per distinct full value is structural.
            entries.append({"value": observed, "count": count, "value_truncated": cut})
        normalized[field] = {"values": entries, "truncated": summary["truncated"]}
    return normalized


def _inspection_legacy_identifier(value: Any) -> str | None:
    if type(value) is not str or not value.startswith("legacy-"):
        return None
    suffix = value.removeprefix("legacy-")
    return value if _inspection_identifier(suffix) == suffix else None


def _inspection_saved_view(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != {"name", "definition", "identity"}:
        return None
    name = _inspection_string(value.get("name"), maximum=128)
    identity = _inspection_hash(value.get("identity"))
    definition = value.get("definition")
    if name is None or identity is None or not isinstance(definition, Mapping):
        return None
    if set(definition) - {"query", "source_snapshot"} or "query" not in definition:
        return None
    query = definition.get("query")
    if not isinstance(query, Mapping) or not query or set(query) - _INSPECTION_VIEW_QUERY_KEYS:
        return None
    normalized_query: dict[str, Any] = {}
    for key, value_ in query.items():
        if key == "filters":
            if not isinstance(value_, list) or len(value_) > 32:
                return None
            filters: list[dict[str, Any]] = []
            for raw in value_:
                if not isinstance(raw, Mapping) or set(raw) - _INSPECTION_VIEW_FILTER_KEYS:
                    return None
                column = _inspection_field_name(raw.get("column"))
                operation = raw.get("op")
                if (
                    column is None
                    or type(operation) is not str
                    or operation not in _INSPECTION_VIEW_OPS
                ):
                    return None
                if operation not in {"exists", "missing"} and "value" not in raw:
                    return None
                if "value" in raw:
                    projected = _inspection_json(raw["value"])
                    if projected is _INSPECTION_INVALID:
                        return None
                    filters.append({"column": column, "op": operation, "value": projected})
                else:
                    filters.append({"column": column, "op": operation})
            normalized_query[key] = filters
        elif key == "columns":
            if not isinstance(value_, list) or not value_ or len(value_) > 128:
                return None
            columns = [_inspection_field_name(column) for column in value_]
            if any(column is None for column in columns) or len(set(columns)) != len(columns):
                return None
            normalized_query[key] = [column for column in columns if column is not None]
        elif key in {"sort_by", "date_column"}:
            column = _inspection_field_name(value_)
            if column is None:
                return None
            normalized_query[key] = column
        elif key in {"date_from", "date_to", "aggregate"}:
            text = _inspection_string(value_, maximum=256)
            if text is None:
                return None
            normalized_query[key] = text
        elif key in {"descending", "expand_children"}:
            if type(value_) is not bool:
                return None
            normalized_query[key] = value_
        elif key == "expand_child":
            column = _inspection_field_name(value_)
            if column is None:
                return None
            normalized_query[key] = column
        elif key == "limit":
            if type(value_) is not int or not 1 <= value_ <= query_data.HARD_ROW_CAP:
                return None
            normalized_query[key] = value_
    normalized: dict[str, Any] = {
        "name": name,
        "definition": {"query": normalized_query},
        "identity": identity,
    }
    if "source_snapshot" in definition:
        snapshot = _inspection_hash(definition["source_snapshot"])
        if snapshot is None:
            return None
        normalized["definition"]["source_snapshot"] = snapshot
    return normalized


def _inspection_plan_descriptor(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, Mapping)
        or not set(value) <= {"reference", "query", "join"}
        or not {
        "reference",
        "query",
        }
        <= set(value)
    ):
        return None
    join = _inspection_plan_join(value["join"]) if "join" in value else None
    if "join" in value and join is None:
        return None
    reference = _opaque_plan_reference(value.get("reference"))
    query = value.get("query")
    if (
        reference is None
        or not isinstance(query, Mapping)
        or not query
        or set(query) - {"filters", "limit"}
    ):
        return None
    limit = query.get("limit")
    if type(limit) is not int or not 1 <= limit <= query_data.HARD_ROW_CAP:
        return None
    normalized_query: dict[str, Any] = {"limit": limit}
    if "filters" in query:
        filters = query["filters"]
        if not isinstance(filters, Mapping) or len(filters) > 128:
            return None
        normalized_filters: dict[str, Any] = {}
        for name, filter_value in filters.items():
            field = _inspection_field_name(name)
            projected = _inspection_json(filter_value)
            if field is None or projected is _INSPECTION_INVALID:
                return None
            normalized_filters[field] = projected
        normalized_query = {"filters": normalized_filters, "limit": limit}
    descriptor = {"reference": reference, "query": normalized_query}
    if join is not None:
        descriptor["join"] = join
    return descriptor


def _inspection_plan_join(value: Any) -> dict[str, str] | None:
    """Re-validate the authored join on the way out; no Planning side is read."""
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 4:
        return None
    join: dict[str, str] = {}
    for record_field, plan_field in value.items():
        field = _inspection_field_name(record_field)
        if (
            field is None
            or type(plan_field) is not str
            or not plan_field.strip()
            or len(plan_field.encode("utf-8")) > 128
        ):
            return None
        join[field] = plan_field
    return join


def _validate_record_inspection(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reconstruct the report-only inspection union before it reaches egress."""
    kind = payload.get("kind")
    if (
        set(payload) - _INSPECTION_KEYS
        or type(kind) is not str
        or kind not in {"collection", "legacy_tracker"}
    ):
        return None
    if payload.get("report_only") is not True or not isinstance(
        payload.get("source_versions"), list
    ):
        return None
    versions = payload["source_versions"]
    if len(versions) > 2_000:
        return None
    normalized_versions: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for value in versions:
        if not isinstance(value, Mapping) or set(value) != {"path", "hash"}:
            return None
        path, digest = _inspection_path(value.get("path")), _inspection_hash(value.get("hash"))
        if path is None or digest is None or path in seen_paths:
            return None
        seen_paths.add(path)
        normalized_versions.append({"path": path, "hash": digest})
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list) or len(diagnostics) > 64:
        return None
    normalized_diagnostics: list[dict[str, str]] = []
    for value in diagnostics:
        if not isinstance(value, Mapping) or set(value) != {"code", "reason"}:
            return None
        code, reason = (
            _inspection_string(value.get("code"), maximum=128),
            _inspection_string(value.get("reason"), maximum=512),
        )
        if code is None or reason is None or re.fullmatch(r"[A-Z][A-Z0-9_]*", code) is None:
            return None
        normalized_diagnostics.append({"code": code, "reason": reason})
    audit = payload.get("audit")
    if not isinstance(audit, Mapping) or set(audit) - {
        "status",
        "gaps",
        "discontinuity",
        "discontinuities",
    }:
        return None
    status, gaps = audit.get("status"), audit.get("gaps")
    if (
        type(status) is not str
        or status
        not in {"baseline", "ok", "gap", "acknowledged_gap", "history_incomplete", "not_applicable"}
        or not isinstance(gaps, list)
        or len(gaps) > 32
    ):
        return None
    if status in {"history_incomplete", "not_applicable"} and gaps:
        return None
    normalized_gaps = [_inspection_string(gap, maximum=256) for gap in gaps]
    if any(gap is None for gap in normalized_gaps):
        return None
    guards = payload.get("lifecycle_guards")
    if guards is not None and (
        not isinstance(guards, Mapping)
        or set(guards) != {"expected_manifest_hash", "expected_container_hash"}
        or any(_inspection_hash(guards.get(name)) is None for name in guards)
    ):
        return None
    views = payload.get("saved_views")
    if not isinstance(views, list) or len(views) > 32:
        return None
    normalized_views = [_inspection_saved_view(view) for view in views]
    if any(view is None for view in normalized_views):
        return None
    presentation = payload.get("presentation")
    if presentation is not None and (
        not isinstance(presentation, Mapping)
        or set(presentation) != {"items", "counts", "truncated"}
    ):
        return None
    items, counts = (
        (presentation.get("items"), presentation.get("counts"))
        if isinstance(presentation, Mapping)
        else ([], {})
    )
    states = {
        "missing",
        "stale",
        "stale_recipe",
        "stale_item",
        "authored_presentation",
        "malformed",
        "unrenderable",
        "unresolved_relationship",
        "filename_drift",
        "filename_collision",
        "orphan_presentation",
    }
    if (
        not isinstance(items, list)
        or len(items) > 128
        or not isinstance(counts, Mapping)
        or (presentation is not None and type(presentation.get("truncated")) is not bool)
    ):
        return None
    normalized_presentation: list[dict[str, Any]] = []
    for item in items:
        if (
            not isinstance(item, Mapping)
            or set(item) - {"item_key", "path", "version", "state", "remedy", "location"}
            or not {"item_key", "path", "version", "state", "remedy"} <= set(item)
        ):
            return None
        key, path, version, state = (
            _inspection_string(item.get("item_key"), maximum=512),
            _inspection_path(item.get("path")),
            _inspection_hash(item.get("version")),
            item.get("state"),
        )
        if key is None or path is None or version is None or state not in states:
            return None
        remedies = {
            "guarded_refresh",
            "rebaseline_then_refresh",
            "repair_markers_then_refresh",
            "guarded_value_update",
            "guarded_manifest_revision",
            "structured_files_preview",
        }
        remedy = item.get("remedy")
        if remedy not in remedies:
            return None
        normalized_item: dict[str, Any] = {
            "item_key": key,
            "path": path,
            "version": version,
            "state": state,
            "remedy": remedy,
        }
        location = item.get("location")
        if location is not None:
            if (
                state != "unrenderable"
                or not isinstance(location, Mapping)
                or set(location) != {"table", "column", "child_index"}
                or _inspection_string(location.get("table"), maximum=128) is None
                or _inspection_string(location.get("column"), maximum=128) is None
                or type(location.get("child_index")) is not int
                or location["child_index"] < 0
            ):
                return None
            normalized_item["location"] = dict(location)
        normalized_presentation.append(normalized_item)
    if presentation is not None and (not counts or not set(counts) <= states):
        return None
    normalized_counts = {state: counts[state] for state in counts}
    if any(type(value) is not int or value < 0 for value in normalized_counts.values()):
        return None
    snapshot = payload.get("snapshot")
    if snapshot is not None and _inspection_hash(snapshot) is None:
        return None
    observed_values = payload.get("observed_values")
    normalized_observed = (
        None if observed_values is None else _inspection_observed_values(observed_values)
    )
    if observed_values is not None and normalized_observed is None:
        return None
    contract, legacy = payload.get("contract"), payload.get("legacy")
    if kind == "collection":
        if legacy is not None or not isinstance(contract, Mapping):
            return None
        if set(contract) != {
            "collection_id",
            "path",
            "title",
            "semantic_profile",
            "schema_version",
            "storage",
            "plans",
        }:
            return None
        collection_id = _inspection_identifier(contract.get("collection_id"))
        path, title = (
            _inspection_path(contract.get("path")),
            _inspection_string(contract.get("title"), maximum=512),
        )
        profile, version, storage = (
            contract.get("semantic_profile"),
            contract.get("schema_version"),
            contract.get("storage"),
        )
        if (
            collection_id is None
            or path is None
            or not path.endswith("/_collection.md")
            or title is None
            or type(profile) is not str
            or profile not in {"records", "planning"}
            or type(version) is not int
            or not 1 <= version <= 1_000_000
            or not isinstance(storage, Mapping)
            or set(storage) != {"strategy", "source", "format_version"}
        ):
            return None
        strategy, source, format_version = (
            storage.get("strategy"),
            _inspection_path(storage.get("source")),
            storage.get("format_version"),
        )
        if (
            type(strategy) is not str
            or strategy not in {"markdown-log", "markdown-items", "dataset"}
            or source is None
            or format_version != 1
        ):
            return None
        plans = contract.get("plans")
        if not isinstance(plans, list) or len(plans) > 32:
            return None
        normalized_plans = [_inspection_plan_descriptor(plan) for plan in plans]
        if any(plan is None for plan in normalized_plans):
            return None
        normalized_contract: dict[str, Any] | None = {
            "collection_id": collection_id,
            "path": path,
            "title": title,
            "semantic_profile": profile,
            "schema_version": version,
            "storage": {"strategy": strategy, "source": source, "format_version": format_version},
            "plans": [plan for plan in normalized_plans if plan is not None],
        }
        normalized_legacy: dict[str, Any] | None = None
        if status == "not_applicable":
            return None
    else:
        if (
            contract is not None
            or not isinstance(legacy, Mapping)
            or set(legacy) != {"collection_id", "path", "inspect_only"}
        ):
            return None
        collection_id, path = (
            _inspection_legacy_identifier(legacy.get("collection_id")),
            _inspection_path(legacy.get("path")),
        )
        if (
            collection_id is None
            or path is None
            or legacy.get("inspect_only") is not True
            or snapshot is not None
            or normalized_observed is not None
            or normalized_versions
            or normalized_views
            or status != "not_applicable"
        ):
            return None
        normalized_contract = None
        normalized_legacy = {"collection_id": collection_id, "path": path, "inspect_only": True}
    normalized_audit: dict[str, Any] = {
        "status": status,
        "gaps": [gap for gap in normalized_gaps if gap is not None],
    }
    if status == "acknowledged_gap" and isinstance(audit.get("discontinuity"), Mapping):
        discontinuity = audit["discontinuity"]
        required = {
            "provenance_continuity",
            "prior_head",
            "acknowledged_gap_codes",
            "rationale",
            "checkpoint_transition",
            "gap_fingerprint",
            "checkpoint_snapshot_hash",
        }
        if (
            set(discontinuity) != required
            or discontinuity.get("provenance_continuity") is not False
        ):
            return None
        normalized_audit["discontinuity"] = dict(discontinuity)
        discontinuities = audit.get("discontinuities", [discontinuity])
        if not isinstance(discontinuities, list) or not 1 <= len(discontinuities) <= 16:
            return None
        normalized_audit["discontinuities"] = [
            dict(item) for item in discontinuities if isinstance(item, Mapping)
        ]
        if len(normalized_audit["discontinuities"]) != len(discontinuities):
            return None
    elif status == "acknowledged_gap":
        return None
    result: dict[str, Any] = {
        "kind": kind,
        "report_only": True,
        "contract": normalized_contract,
        "legacy": normalized_legacy,
        "snapshot": snapshot,
        "source_versions": normalized_versions,
        "diagnostics": normalized_diagnostics,
        "audit": normalized_audit,
        "saved_views": [view for view in normalized_views if view is not None],
        **(
            {
                "presentation": {
                    "items": normalized_presentation,
                    "counts": normalized_counts,
                    "truncated": presentation["truncated"],
                }
            }
            if presentation is not None
            else {}
        ),
        **({"lifecycle_guards": dict(guards)} if guards is not None else {}),
        **({"observed_values": normalized_observed} if normalized_observed is not None else {}),
    }
    return result


egress.register_projector(
    "record_query",
    (
        "collection_id",
        "snapshot",
        "rows",
        "returned",
        "total_matched",
        "truncated",
        "continuation",
        "derived",
        "rendered",
        "aggregate",
        "query",
        "source_versions",
        "view",
        "agent_history",
    ),
)
egress.register_projector(
    "record_inspection",
    (
        "kind",
        "report_only",
        "contract",
        "legacy",
        "snapshot",
        "source_versions",
        "diagnostics",
        "audit",
        "saved_views",
        "lifecycle_guards",
        "presentation",
        "observed_values",
    ),
    validator=_validate_record_inspection,
)
egress.register_projector(
    "record_manifest",
    ("collection_id", "path", "title", "storage", "templates", "plans", "views", "governance"),
)
egress.register_projector("record_template", ("collection_id", "path", "content"))
egress.register_projector(
    "record_mutation",
    (
        "operation",
        "collection_id",
        "item_key",
        "before_item_hash",
        "after_item_hash",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "payload_hash",
        "outcome",
        "audit_correlation",
        "continuity",
        "acknowledged_gap_codes",
        "gap_fingerprint",
        "checkpoint_snapshot_hash",
        "minimum_reader_version",
    ),
)


def full_release_filter(vault_root: Path) -> Callable[[str], bool]:
    """Return the Records full-content gate without the normal L5 walk floor.

    The policy is loaded ONCE for the life of the filter and handed to every
    decision, the way `egress.release_walk_filter` already does for a walk. The
    plane does not move while one pass runs, and the per-path load was 7,480
    authoring-guard probes -- 8.9 s -- of a single structured write's delta.
    Semantics are untouched: the same `refuse_if_excluded` and the same
    `LEVEL_FULL` floor, decided against the same policy every path would have
    loaded for itself.
    """
    root = Path(vault_root)
    policy = egress.policy_module.load(root)

    def allowed(relative: str) -> bool:
        return not access.refuse_if_excluded(root, relative) and (
            egress.release_level_for_path_only(root, relative, policy=policy) == egress.LEVEL_FULL
        )

    return allowed


def _authorize(
    root: Path, relative: str, *, receipt: bool = False, policy: Any | None = None
) -> bool:
    if access.refuse_if_excluded(root, relative):
        return False
    return (
        egress.release_level_for_path_only(
            root,
            relative,
            receipt_decision="release_authorized" if receipt else None,
            policy=policy,
        )
        == egress.LEVEL_FULL
    )


@dataclass(slots=True)
class _LinkProjector:
    root: Path
    manifest: collections.CollectionManifest
    resolver: vault.WikilinkResolver
    memory_targets: Mapping[str, tuple[str, ...]]
    admitted: Mapping[str, bool]
    candidate_index_complete: bool | None
    verdicts: dict[str, bool]
    policy: Any | None

    @classmethod
    def create(
        cls, root: Path, manifest: collections.CollectionManifest, *, policy: Any | None = None
    ) -> _LinkProjector:
        # Keep the common numeric/event query path independent of vault-wide
        # link lookup. Even link-bearing collections defer that lookup until a
        # bare title or memory identity actually needs it.
        empty = vault.WikilinkResolver.from_entries(root, ())
        return cls(root, manifest, empty, {}, {}, None, {}, policy)

    def _candidate_index_available(self) -> bool:
        if self.candidate_index_complete is not None:
            return self.candidate_index_complete
        entries: list[tuple[str, str | None]] = []
        memory_targets: dict[str, list[str]] = {}
        admitted: dict[str, bool] = {}
        raw_limit, authorized_limit, byte_limit = _link_index_limits()
        raw_count = authorized_count = authorized_bytes = 0
        complete = True
        for candidate in vault.walk_vault_md(self.root):
            raw_count += 1
            if raw_count > raw_limit:
                complete = False
                break
            try:
                relative = candidate.relative_to(self.root).as_posix()
                path, relative = cast(
                    tuple[Path, str],
                    vault.resolve_under_vault(
                        self.root, relative, must_exist=True, must_be_file=True
                    ),
                )
            except (ValueError, vault.VaultPathError):
                continue
            # The resolver's title and identity indexes must not learn from a
            # path that this principal cannot read. Otherwise a hidden name or
            # duplicate identity can change an otherwise public link result.
            allowed = _authorize(self.root, relative, policy=self.policy)
            admitted[relative] = allowed
            if not allowed:
                continue
            authorized_count += 1
            if authorized_count > authorized_limit or authorized_bytes >= byte_limit:
                complete = False
                break
            title: str | None = None
            identity: str | None = None
            try:
                data, _guard = vault.read_bounded_guarded_bytes(
                    self.root,
                    relative,
                    limit=min(_MAX_LINK_INDEX_ENTRY_BYTES, byte_limit - authorized_bytes),
                )
                authorized_bytes += len(data)
                text = data.decode("utf-8")
                frontmatter, body, _marker = vault.parse_frontmatter(text)
                display_title = vault.resolve_display_title(frontmatter, body, path)
                title = display_title.lower() if display_title else None
                if relative.startswith(f"{vault.kb_dirname()}/"):
                    identity = memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD))
            except (UnicodeDecodeError, vault.PathGuardError, vault.FrontmatterError):
                complete = False
                break
            entries.append((relative, title))
            if identity is not None:
                memory_targets.setdefault(identity, []).append(relative)
        if complete:
            self.resolver = vault.WikilinkResolver.from_entries(self.root, entries)
            self.memory_targets = {
                identity: tuple(sorted(paths)) for identity, paths in memory_targets.items()
            }
            self.admitted = admitted
        self.candidate_index_complete = complete
        return complete

    def __call__(self, values: Mapping[str, Any]) -> dict[str, Any]:
        projected = dict(values)
        for name, spec in self.manifest.schema.fields.items():
            if name not in projected:
                continue
            value = self._project_value(projected[name], spec)
            if value is None or (spec.type == "array" and not value):
                projected.pop(name)
            else:
                projected[name] = value
        return projected

    def project_presentation_value(
        self, value: Any, column: collections.RecordPresentationColumn
    ) -> Any:
        """Project only declared nested links at the audience egress boundary."""
        if column.type != "link":
            return value
        return self._project_value(value, collections.FieldSpec("link", link_kind=column.link_kind))

    def _project_value(self, value: Any, spec: collections.FieldSpec) -> Any:
        if spec.type == "array" and spec.items is not None and isinstance(value, list | tuple):
            return [
                result
                for item in value
                if (result := self._project_value(item, spec.items)) is not None
            ]
        if spec.type != "link" or type(value) is not str:
            return value
        return value if self._allowed(value) else None

    def _allowed(self, value: str) -> bool:
        raw = value.strip()
        try:
            if raw.lower().startswith(memory_refs.REF_PREFIX):
                if not self._candidate_index_available():
                    return False
                identity = memory_refs.parse_memory_ref(raw)
                if identity is None or raw != memory_refs.memory_ref(identity):
                    return False
                matches = self.memory_targets.get(identity, ())
                if len(matches) != 1:
                    return self._remember(f"memory:{identity}", False)
                target = matches[0]
            elif raw.lower().startswith(("exomem://vault/", "exomem://source/")):
                target = memory_refs.resolve_identifier_read_only(self.root, raw)
            elif (match := re.fullmatch(r"\[\[([^\[\]]+)\]\]", raw)) is not None:
                inner = match.group(1).strip()
                if not inner or inner.count("|") > 1:
                    return False
                target_text = inner.split("|", 1)[0].strip().split("#", 1)[0].strip()
                if "/" not in target_text and not self._candidate_index_available():
                    return False
                try:
                    canonical, _warning = vault.normalize_wikilink(
                        inner.split("|", 1)[0].strip(),
                        self.root,
                        resolver=self.resolver,
                        strict=True,
                    )
                except vault.UnresolvedWikilinkError:
                    canonical, _warning = vault.normalize_wikilink(
                        inner.split("|", 1)[0].strip(), self.root, resolver=self.resolver
                    )
                except vault.WikilinkError:
                    return False
                target = canonical.split("#", 1)[0] + ".md"
            elif "/" in raw and not raw.startswith(("/", "\\")):
                canonical, _warning = vault.normalize_wikilink(
                    raw, self.root, resolver=self.resolver
                )
                if not canonical or "#" in canonical:
                    return False
                target = canonical + ".md"
            else:
                return False
            _path, relative = cast(
                tuple[Path, str],
                vault.resolve_under_vault(self.root, target, must_exist=True, must_be_file=True),
            )
            vault.PathGuard.capture(self.root, relative, leaf_policy="stable")
        except vault.VaultPathError as error:
            if error.code != "NOT_FOUND":
                return False
            try:
                _path, relative = cast(
                    tuple[Path, str], vault.resolve_under_vault(self.root, target)
                )
                vault.PathGuard.capture(self.root, relative, leaf_policy="absent")
            except (vault.VaultPathError, vault.PathGuardError):
                return False
            return self._remember(f"forward:{relative}", False)
        except (memory_refs.ReferenceError, vault.PathGuardError):
            return False
        if relative in self.verdicts:
            return self.verdicts[relative]
        if relative in self.admitted and not self.admitted[relative]:
            return self._remember(relative, False)
        return self._remember(
            relative, _authorize(self.root, relative, receipt=True, policy=self.policy)
        )

    def _remember(self, target: str, allowed: bool) -> bool:
        if target in self.verdicts:
            return self.verdicts[target]
        self.verdicts[target] = allowed
        return allowed


def _link_index_limits() -> tuple[int, int, int]:
    owner = effective_principal().audience_id == OWNER_AUDIENCE
    return (
        _INTERNAL_LINK_INDEX_RAW_CANDIDATES if owner else _PUBLIC_LINK_INDEX_RAW_CANDIDATES,
        _INTERNAL_LINK_INDEX_AUTHORIZED_CANDIDATES
        if owner
        else _PUBLIC_LINK_INDEX_AUTHORIZED_CANDIDATES,
        _INTERNAL_LINK_INDEX_AUTHORIZED_BYTES if owner else _PUBLIC_LINK_INDEX_AUTHORIZED_BYTES,
    )


def _project_links(
    root: Path, manifest: collections.CollectionManifest, values: Mapping[str, Any]
) -> dict[str, Any]:
    return _LinkProjector.create(root, manifest)(values)


def require_records_profile(
    manifest: collections.CollectionManifest,
) -> collections.CollectionManifest:
    """Keep the Records product surface from claiming Planning collections."""
    if manifest.semantic_profile != "records":
        raise collections.CollectionError(
            "RECORDS_PROFILE_REQUIRED", "Records actions require a records semantic profile"
        )
    return manifest


def resolve_collection(
    vault_root: Path, selector: str | Path | collections.CollectionManifest
) -> collections.CollectionManifest:
    """Resolve only a fully released manifest, treating every other case as absent."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_resolve", join_existing=True) as collector:
        manifest = _resolve_released_collection(root, selector, receipt=True)
        egress.emit_boundary_receipt(collector)
        return manifest


def resolve_collection_for_mutation(
    vault_root: Path, selector: str | Path | collections.CollectionManifest
) -> collections.CollectionManifest:
    """Resolve a mutable collection without disclosing provisional decisions."""
    return _resolve_released_collection(Path(vault_root), selector, receipt=False)


def _resolve_released_collection(
    root: Path,
    selector: str | Path | collections.CollectionManifest,
    *,
    receipt: bool,
    policy: Any | None = None,
) -> collections.CollectionManifest:
    path = selector.path if isinstance(selector, collections.CollectionManifest) else selector
    manifest = collections.resolve_collection(
        root,
        path,
        authorize_path=lambda relative: _authorize(root, relative, receipt=receipt, policy=policy),
    )
    if not _authorize(root, manifest.path, receipt=receipt, policy=policy):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    return manifest


def query_collection(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    *,
    semantic_profile: str = "records",
    **kwargs: Any,
) -> record_formats.RecordQueryResult:
    """Query released Records only; authorization happens before adapter parsing."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_query", join_existing=True) as collector:
        policy = egress.policy_module.load(root)
        manifest = _resolve_released_collection(root, collection, receipt=True, policy=policy)
        if manifest.semantic_profile != semantic_profile:
            error_code = (
                "RECORDS_PROFILE_REQUIRED"
                if semantic_profile == "records"
                else "PLANNING_PROFILE_REQUIRED"
            )
            raise collections.CollectionError(
                error_code,
                "collection profile is not available",
            )
        if not _authorize(root, manifest.storage.source, receipt=True, policy=policy):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        links = _LinkProjector.create(root, manifest, policy=policy)
        view = kwargs.get("view")
        if view is not None:
            _authorize_saved_view(root, manifest, view, links)
        result = record_formats.query_collection(
            root,
            manifest,
            authorize_path=lambda path: _authorize(root, path, receipt=True, policy=policy),
            project_values=links,
            project_child_value=links.project_presentation_value,
            **kwargs,
        )
        egress.emit_boundary_receipt(collector)
        return result


def _authorize_saved_view(
    root: Path,
    manifest: collections.CollectionManifest,
    name: object,
    links: _LinkProjector,
) -> collections.SavedView:
    """Refuse a saved view if link projection would change its query meaning."""
    if type(name) is not str:
        raise collections.CollectionError("INVALID_SAVED_VIEW", "saved view name is invalid")
    view = collections.resolve_saved_view(manifest, name)
    query = view.definition.get("query")
    if not isinstance(query, Mapping):
        raise collections.CollectionError("INVALID_SAVED_VIEW", "saved view definition is invalid")
    child_requested = query.get("expand_children") is True or query.get("expand_child") is not None
    selected_child = _saved_view_child_selector(manifest, query)
    if child_requested and selected_child is None:
        raise collections.CollectionError("SAVED_VIEW_NOT_AVAILABLE", "saved view is not available")
    allowed_fields = collections._saved_view_selected_fields(
        collections._saved_view_fields(
            manifest.schema,
            manifest.storage,
            manifest.semantic_profile,
            manifest.record_presentation,
        ),
        collections._saved_view_child_shapes(manifest.storage, manifest.record_presentation),
        selected_child,
    )
    if any(field not in allowed_fields for field in _saved_view_query_fields(query)):
        raise collections.CollectionError("SAVED_VIEW_NOT_AVAILABLE", "saved view is not available")
    filters = query.get("filters", ())
    if not isinstance(filters, list):
        raise collections.CollectionError("INVALID_SAVED_VIEW", "saved view filters are invalid")
    for raw in filters:
        if not isinstance(raw, Mapping) or "value" not in raw:
            continue
        column = raw.get("column")
        spec = _saved_view_field_spec(manifest, query, column) if type(column) is str else None
        if spec is None:
            continue
        if not _saved_view_filter_links_are_authorized(raw["value"], spec, links):
            raise collections.CollectionError(
                "SAVED_VIEW_NOT_AVAILABLE", "saved view is not available"
            )
    return view


def _saved_view_field_spec(
    manifest: collections.CollectionManifest,
    query: Mapping[str, Any],
    column: str,
) -> collections.FieldSpec | None:
    """Resolve a saved-view field in the row shape the view actually selects."""
    selected = _saved_view_child_selector(manifest, query)
    child_requested = query.get("expand_children") is True or query.get("expand_child") is not None
    if child_requested and selected is None:
        return None
    recipe = manifest.record_presentation
    if type(selected) is str and recipe is not None:
        table = next((item for item in recipe.tables if item.field == selected), None)
        if table is not None:
            child = next((item for item in table.columns if item.field == column), None)
            if child is not None:
                return collections.FieldSpec(child.type, link_kind=child.link_kind)
            if column in {
                candidate.field
                for candidate_table in recipe.tables
                for candidate in candidate_table.columns
            }:
                return None
            if column == selected:
                return None
    return manifest.schema.fields.get(column)


def _saved_view_child_selector(
    manifest: collections.CollectionManifest, query: Mapping[str, Any]
) -> str | None:
    explicit = query.get("expand_child")
    if type(explicit) is str:
        candidates = collections._saved_view_child_fields(
            manifest.storage, manifest.record_presentation
        )
        return explicit if explicit in candidates else None
    if query.get("expand_children") is not True:
        return None
    candidates = collections._saved_view_child_fields(
        manifest.storage, manifest.record_presentation
    )
    return next(iter(candidates)) if len(candidates) == 1 else None


def _saved_view_query_fields(query: Mapping[str, Any]) -> set[str]:
    fields: set[str] = set()
    filters = query.get("filters", ())
    if isinstance(filters, list):
        fields.update(
            raw["column"]
            for raw in filters
            if isinstance(raw, Mapping) and type(raw.get("column")) is str
        )
    columns = query.get("columns", ())
    if isinstance(columns, list):
        fields.update(column for column in columns if type(column) is str)
    for key in ("sort_by", "date_column"):
        if type(query.get(key)) is str:
            fields.add(query[key])
    aggregate = query.get("aggregate")
    if type(aggregate) is str and ":" in aggregate:
        fields.add(aggregate.split(":", 1)[1])
    return fields


def _saved_view_filter_links_are_authorized(
    value: Any, spec: collections.FieldSpec, links: _LinkProjector
) -> bool:
    """Check every link-shaped filter value against the field's element type."""
    if spec.type == "link":
        if isinstance(value, list | tuple):
            return all(_saved_view_filter_links_are_authorized(item, spec, links) for item in value)
        return links._project_value(value, spec) == value
    if spec.type == "array" and spec.items is not None:
        if isinstance(value, list | tuple):
            return all(
                _saved_view_filter_links_are_authorized(item, spec.items, links) for item in value
            )
        return _saved_view_filter_links_are_authorized(value, spec.items, links)
    return True


def inspect_collection(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
) -> dict[str, Any]:
    """Return bounded, report-only evidence for one fully released collection."""
    root = Path(vault_root)
    # `records` already imports this governance boundary for mutations.
    from . import records

    with egress.disclosure_boundary(root, "record_inspection", join_existing=True) as collector:
        manifest = _resolve_released_collection(root, collection, receipt=True)
        if not _authorize(root, manifest.storage.source, receipt=True):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        links = _LinkProjector.create(root, manifest)
        try:
            inspection = record_formats.inspect_collection(
                root,
                manifest,
                authorize_path=lambda path: _authorize(root, path, receipt=True),
            )
        except collections.CollectionError as error:
            inspection = record_formats.CollectionInspection(
                collection_id=manifest.collection_id,
                snapshot=None,
                source_versions=(manifest.manifest_version,),
                source_hashes={manifest.path: manifest.manifest_version.hash},
                diagnostics=(collections.CollectionDiagnostic(error.code, error.reason),),
            )
        diagnostics = _inspection_diagnostics(inspection.diagnostics)
        _inspection_templates(root, manifest, diagnostics)
        saved_views = _inspection_saved_views(root, manifest, links, diagnostics)
        try:
            audit = records.inspect_audit_gap(
                root,
                manifest,
                authorize_path=lambda path: _authorize(root, path, receipt=True),
            )
        except collections.CollectionError:
            audit = {"status": "history_incomplete", "gaps": []}
        guards = None
        if not diagnostics:
            try:
                snapshot = record_formats.load_adapter(
                    root,
                    manifest,
                    authorize_path=lambda path: _authorize(root, path, receipt=True),
                ).read()
                if not snapshot.diagnostics and all(
                    _authorize(root, version.path, receipt=True)
                    for version in snapshot.source_versions
                ):
                    guards = records.lifecycle_guards(manifest, snapshot)
            except collections.CollectionError:
                pass
        payload = _RecordEnvelope(
            {
                "kind": "collection",
                "report_only": True,
                "contract": {
                    **_inspection_contract(manifest),
                    "plans": [
                        projected
                        for plan in manifest.links.plans
                        if (projected := _project_plan_link(root, manifest, plan, links=links))
                        is not None
                    ],
                },
                "legacy": None,
                "snapshot": inspection.snapshot,
                "source_versions": [
                    {"path": version.path, "hash": version.hash}
                    for version in inspection.source_versions[:2_000]
                ],
                "diagnostics": diagnostics,
                "audit": _inspection_audit(audit),
                "saved_views": saved_views,
                "lifecycle_guards": guards,
                # Item-derived, so it rides the same authorized item pass the rest
                # of this payload does; nothing recomputes it from unfiltered bytes.
                **(
                    {"observed_values": dict(inspection.observed_values)}
                    if inspection.observed_values is not None
                    else {}
                ),
                **(
                    {"presentation": _presentation_inspection(inspection.presentation, manifest)}
                    if (
                        manifest.record_presentation is not None
                        or manifest.item_presentation is not None
                        or manifest.item_filename is not None
                        or inspection.presentation
                    )
                    else {}
                ),
            }
        )
        projected = egress.project(payload, egress.LEVEL_FULL, kind="record_inspection") or {}
        egress.emit_boundary_receipt(collector)
        return projected


def _presentation_inspection(
    findings: tuple[dict[str, Any], ...],
    manifest: collections.CollectionManifest,
) -> dict[str, Any]:
    states = (
        ("missing", "stale", "malformed", "unrenderable")
        if manifest.record_presentation is not None
        else (
            "missing",
            "stale_recipe",
            "stale_item",
            "authored_presentation",
            "malformed",
            "unrenderable",
            "unresolved_relationship",
            "filename_drift",
            "filename_collision",
            "orphan_presentation",
        )
    )
    counts = {state: sum(item.get("state") == state for item in findings) for state in states}
    return {
        "items": list(findings[:_PRESENTATION_FINDING_LIMIT]),
        "counts": counts,
        "truncated": len(findings) > _PRESENTATION_FINDING_LIMIT,
    }


def inventory_collections(vault_root: Path, *, semantic_profile: str = "records") -> dict[str, Any]:
    """Return a bounded authorized inventory without opening canonical item data.

    Both profiles answer "what is here?" the same way and under the same
    disclosure filtering; only the profile selected and the legacy-tracker sweep
    (a Records-layer artifact) differ.
    """
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_inspection", join_existing=True) as collector:

        def authorize(path: str) -> bool:
            return _authorize(root, path, receipt=True)

        manifests = [
            manifest
            for manifest in collections.discover_collections(root, authorize_path=authorize)
            if manifest.semantic_profile == semantic_profile and authorize(manifest.storage.source)
        ]
        legacy: tuple[collections.LegacyCollection, ...] = ()
        legacy_truncated = False
        if semantic_profile == "records":
            legacy, legacy_truncated = collections.discover_legacy_trackers(
                root, authorize_path=authorize
            )
        payload = {
            "kind": f"{semantic_profile}_inventory",
            "report_only": True,
            "collections": [
                {
                    "collection_id": manifest.collection_id,
                    "title": manifest.title,
                    "manifest_path": manifest.path,
                    "semantic_profile": manifest.semantic_profile,
                    "lifecycle": manifest.lifecycle,
                    "storage_strategy": manifest.storage.strategy,
                    "natural_key": list(manifest.schema.natural_key),
                }
                for manifest in manifests
            ],
            # Records-layer only. Planning has no legacy trackers to sweep, and an
            # empty list there reads as "swept, found none" -- a claim about a
            # sweep that never ran. Absent is the honest shape.
            **(
                {
                    "legacy_trackers": [
                        {"path": tracker.path, "inspect_only": tracker.inspect_only}
                        for tracker in legacy
                    ]
                }
                if semantic_profile == "records"
                else {}
            ),
            "truncated": {
                "collections": False,
                **({"legacy_trackers": legacy_truncated} if semantic_profile == "records" else {}),
            },
            "contract_route": {
                "tool": "record_memory",
                "arguments": {"action": "describe"},
            },
        }
        egress.emit_boundary_receipt(collector)
        return payload


def inspect_legacy_tracker(vault_root: Path, path: str | Path) -> dict[str, Any]:
    """Inspect a manifest-less tracker at collection granularity without parsing items."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_inspection", join_existing=True) as collector:
        legacy = collections.inspect_legacy_tracker(
            root, path, authorize_path=lambda relative: _authorize(root, relative, receipt=True)
        )
        payload = _RecordEnvelope(
            {
                "kind": "legacy_tracker",
                "report_only": True,
                "contract": None,
                "legacy": {
                    "collection_id": legacy.collection_id,
                    "path": legacy.path,
                    "inspect_only": legacy.inspect_only,
                },
                "snapshot": None,
                "source_versions": [],
                "diagnostics": [],
                "audit": {"status": "not_applicable", "gaps": []},
                "saved_views": [],
            }
        )
        projected = egress.project(payload, egress.LEVEL_FULL, kind="record_inspection") or {}
        egress.emit_boundary_receipt(collector)
        return projected


def _inspection_contract(manifest: collections.CollectionManifest) -> dict[str, Any]:
    return {
        "collection_id": manifest.collection_id,
        "path": manifest.path,
        "title": manifest.title,
        "semantic_profile": manifest.semantic_profile,
        "schema_version": manifest.schema.version,
        "storage": {
            "strategy": manifest.storage.strategy,
            "source": manifest.storage.source,
            "format_version": manifest.storage.format_version,
        },
        "plans": [],
    }


def _inspection_diagnostics(
    diagnostics: Iterable[collections.CollectionDiagnostic],
) -> list[dict[str, str]]:
    projected: list[dict[str, str]] = []
    for diagnostic in diagnostics:
        if len(projected) >= 64:
            break
        if (
            not isinstance(diagnostic, collections.CollectionDiagnostic)
            or type(diagnostic.code) is not str
            or type(diagnostic.reason) is not str
            or not diagnostic.code
            or len(diagnostic.code) > 128
            or len(diagnostic.reason.encode("utf-8")) > 512
        ):
            continue
        projected.append({"code": diagnostic.code, "reason": diagnostic.reason})
    return projected


def _inspection_saved_views(
    root: Path,
    manifest: collections.CollectionManifest,
    links: _LinkProjector,
    diagnostics: list[dict[str, str]],
) -> list[dict[str, Any]]:
    saved_views: list[dict[str, Any]] = []
    for name in list(manifest.views)[:32]:
        try:
            view = _authorize_saved_view(root, manifest, name, links)
        except collections.CollectionError as error:
            if len(diagnostics) < 64:
                diagnostics.append({"code": error.code, "reason": error.reason})
            continue
        saved_views.append(
            {"name": view.name, "definition": dict(view.definition), "identity": view.identity}
        )
    return saved_views


def _inspection_templates(
    root: Path, manifest: collections.CollectionManifest, diagnostics: list[dict[str, str]]
) -> None:
    """Check declared template availability only after its own L6 decision."""
    for template in manifest.templates[:32]:
        unavailable = not _authorize(root, template.path, receipt=True)
        if not unavailable:
            try:
                vault.PathGuard.capture(root, template.path, leaf_policy="stable")
            except vault.PathGuardError:
                unavailable = True
        if unavailable and len(diagnostics) < 64:
            diagnostics.append(
                {"code": "TEMPLATE_UNAVAILABLE", "reason": "declared template is unavailable"}
            )


def _inspection_audit(audit: Any) -> dict[str, Any]:
    if not isinstance(audit, Mapping):
        return {"status": "history_incomplete", "gaps": []}
    status = audit.get("status")
    gaps = audit.get("gaps")
    if status not in {
        "baseline",
        "ok",
        "gap",
        "acknowledged_gap",
        "history_incomplete",
    } or not isinstance(gaps, list):
        return {"status": "history_incomplete", "gaps": []}
    result: dict[str, Any] = {
        "status": status,
        "gaps": [gap for gap in gaps[:32] if type(gap) is str and len(gap) <= 256],
    }
    if status == "acknowledged_gap" and isinstance(audit.get("discontinuity"), Mapping):
        result["discontinuity"] = dict(audit["discontinuity"])
        if isinstance(audit.get("discontinuities"), list):
            result["discontinuities"] = [
                dict(item) for item in audit["discontinuities"] if isinstance(item, Mapping)
            ]
    return result


_QUERY_KEYS = frozenset(
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
    }
)
_SYSTEM_FIELDS = frozenset(
    {
        "collection_id",
        "record_id",
        "item_version",
        "inferred",
        "ambiguous",
        "parent_record_id",
        "child_field",
        "child_index",
    }
)
_QUERY_OPS = frozenset(
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
_MAX_QUERY_DEPTH = 8
_MAX_QUERY_NODES = 4_096


def _withheld_query() -> dict[str, Any]:
    return {"withheld": True, "reason": "invalid_record_query"}


def _json_value(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> bool:
    """Accept only bounded JSON values; rendering must never coerce arbitrary objects."""
    nodes = [0] if nodes is None else nodes
    nodes[0] += 1
    if nodes[0] > _MAX_QUERY_NODES or depth > _MAX_QUERY_DEPTH:
        return False
    if value is None or type(value) in {bool, int}:
        return True
    if type(value) is float:
        return value == value and value not in {float("inf"), float("-inf")}
    if type(value) is str:
        return len(value.encode("utf-8")) <= 16 * 1024
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


def _query_value_fields(manifest: collections.CollectionManifest) -> set[str]:
    """Return schema fields plus the manifest-declared markdown-log projections."""
    fields = set(manifest.schema.fields)
    if manifest.record_presentation is not None:
        for table in manifest.record_presentation.tables:
            fields.update(column.field for column in table.columns)
    if manifest.storage.strategy != "markdown-log":
        return fields
    descriptor = manifest.storage.descriptor
    heading = descriptor.get("item_heading")
    if isinstance(heading, Mapping):
        note = heading.get("note")
        if isinstance(note, Mapping) and type(note.get("field")) is str:
            fields.add(note["field"])
    children = descriptor.get("child_rows")
    if isinstance(children, Mapping) and isinstance(children.get("fields"), (list, tuple)):
        fields.update(field for field in children["fields"] if type(field) is str)
    return fields


def _valid_query_descriptor(
    query: Mapping[str, Any], manifest: collections.CollectionManifest
) -> bool:
    if set(query) not in {_QUERY_KEYS, _QUERY_KEYS - {"expand_child"}}:
        return False
    columns = _query_value_fields(manifest) | _SYSTEM_FIELDS
    filters = query["filters"]
    if not isinstance(filters, list) or len(filters) > 128:
        return False
    for raw in filters:
        if not isinstance(raw, Mapping) or set(raw) - {"column", "op", "value"}:
            return False
        if type(raw.get("column")) is not str or raw["column"] not in columns:
            return False
        if "op" in raw and raw["op"] not in _QUERY_OPS:
            return False
        if "value" in raw and not _json_value(raw["value"]):
            return False
    requested = query["columns"]
    if requested is not None and (
        not isinstance(requested, list)
        or len(requested) > len(columns)
        or any(type(column) is not str or column not in columns for column in requested)
    ):
        return False
    for key in ("sort_by", "date_column"):
        if query[key] is not None and (type(query[key]) is not str or query[key] not in columns):
            return False
    if type(query["descending"]) is not bool or type(query["expand_children"]) is not bool:
        return False
    if query.get("expand_child") is not None and (
        type(query.get("expand_child")) is not str or query["expand_children"]
    ):
        return False
    if any(
        query[key] is not None and type(query[key]) is not str for key in ("date_from", "date_to")
    ):
        return False
    aggregate = query["aggregate"]
    if aggregate is None:
        return True
    if type(aggregate) is not str:
        return False
    aggregate = aggregate.strip()
    if aggregate in {"count", "profile"}:
        return True
    if ":" not in aggregate:
        return False
    function, column = (piece.strip() for piece in aggregate.split(":", 1))
    return (
        function in {"min", "max", "sum", "avg", "latest", "distinct", "group"}
        and column in columns
    )


def _valid_row(
    row: Any,
    manifest: collections.CollectionManifest,
    query: Mapping[str, Any],
    *,
    require_item_version: bool,
) -> dict[str, Any] | None:
    query_fields = _query_value_fields(manifest)
    if not isinstance(row, dict) or set(row) - query_fields - _SYSTEM_FIELDS:
        return None
    required = {"collection_id", "record_id", "inferred", "ambiguous"}
    if require_item_version:
        required.add("item_version")
    if not required <= set(row) or (not require_item_version and "item_version" in row):
        return None
    if row["collection_id"] != manifest.collection_id or type(row["record_id"]) is not str:
        return None
    try:
        if manifest.storage.strategy == "dataset":
            collections._validate_item_key(row["record_id"])
        elif memory_refs.normalize_id(row["record_id"]) is None:
            return None
    except ValueError:
        return None
    if type(row["inferred"]) is not bool or type(row["ambiguous"]) is not bool:
        return None
    if require_item_version and (
        type(row["item_version"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", row["item_version"])
    ):
        return None
    if "parent_record_id" in row and (
        query["expand_children"] is not True
        and query.get("expand_child") is None
        or type(row["parent_record_id"]) is not str
        or row["parent_record_id"] != row["record_id"]
    ):
        return None
    if "child_field" in row and (
        type(row["child_field"]) is not str
        or type(row.get("child_index")) is not int
        or row["child_index"] < 0
    ):
        return None
    values = {name: value for name, value in row.items() if name in manifest.schema.fields}
    try:
        for name, value in values.items():
            collections._validate_field_value(name, value, manifest.schema.fields[name])
    except (collections.CollectionError, TypeError, ValueError):
        return None
    if any(
        name not in manifest.schema.fields and not _json_value(value)
        for name, value in row.items()
        if name not in _SYSTEM_FIELDS
    ) or not _json_value(row):
        return None
    return dict(row)


def _valid_aggregate(
    aggregate: Any,
    query: Mapping[str, Any],
    manifest: collections.CollectionManifest,
    total_matched: int,
    *,
    require_item_version: bool,
) -> bool:
    specification = query["aggregate"]
    if specification is None:
        return aggregate is None
    if not isinstance(aggregate, Mapping) or not _json_value(aggregate):
        return False
    specification = specification.strip()
    if specification == "count":
        return (
            set(aggregate) == {"count"}
            and type(aggregate["count"]) is int
            and aggregate["count"] == total_matched
        )
    if specification == "profile":
        profile = aggregate.get("profile")
        return (
            set(aggregate) == {"profile", "dataset_card"}
            and type(aggregate["dataset_card"]) is str
            and isinstance(profile, Mapping)
            and set(profile) == {"path", "format", "total_rows", "columns"}
            and profile["path"] == manifest.storage.source
            and profile["format"] == manifest.storage.strategy
            and type(profile["total_rows"]) is int
            and profile["total_rows"] == total_matched
            and isinstance(profile["columns"], list)
        )
    function, column = (piece.strip() for piece in specification.split(":", 1))
    if function in {"min", "max", "sum", "avg"}:
        allowed = {function, "n"} if aggregate.get("n") else {function, "n", "note"}
        return (
            set(aggregate) == allowed
            and type(aggregate.get("n")) is int
            and 0 <= aggregate["n"] <= total_matched
            and (aggregate[function] is None or type(aggregate[function]) in {int, float})
            and ("note" not in aggregate or type(aggregate["note"]) is str)
        )
    if function == "distinct":
        return (
            set(aggregate) == {"distinct", "n", "truncated"}
            and isinstance(aggregate["distinct"], list)
            and type(aggregate["n"]) is int
            and aggregate["n"] >= len(aggregate["distinct"])
            and type(aggregate["truncated"]) is bool
        )
    if function == "group":
        return (
            set(aggregate) == {"groups", "n", "truncated"}
            and isinstance(aggregate["groups"], list)
            and all(
                isinstance(group, Mapping)
                and set(group) == {"value", "count"}
                and type(group["count"]) is int
                and group["count"] >= 0
                for group in aggregate["groups"]
            )
            and type(aggregate["n"]) is int
            and aggregate["n"] >= len(aggregate["groups"])
            and type(aggregate["truncated"]) is bool
        )
    if function == "latest":
        expected_column = query["date_column"] or column
        latest = aggregate.get("row")
        return (
            set(aggregate) == {"latest_by", "row"}
            and aggregate["latest_by"] == expected_column
            and (
                latest is None
                or _valid_row(latest, manifest, query, require_item_version=require_item_version)
                is not None
            )
        )
    return False


def _valid_source_versions(
    versions: tuple[collections.SourceVersion, ...], manifest: collections.CollectionManifest
) -> tuple[collections.SourceVersion, ...] | None:
    source_prefix = manifest.storage.source.rstrip("/") + "/"
    seen: set[str] = set()
    projected: list[collections.SourceVersion] = []
    found_manifest = False
    for version in versions:
        if (
            not isinstance(version, collections.SourceVersion)
            or type(version.path) is not str
            or version.path in seen
            or version.path.startswith("/")
            or ".." in version.path.split("/")
            or not re.fullmatch(r"[0-9a-f]{64}", version.hash)
        ):
            return None
        if version.path == manifest.manifest_version.path:
            if version.hash != manifest.manifest_version.hash:
                return None
            found_manifest = True
        elif (
            manifest.storage.strategy != "markdown-items"
            and version.path != manifest.storage.source
        ):
            return None
        elif manifest.storage.strategy == "markdown-items" and not version.path.startswith(
            source_prefix
        ):
            return None
        seen.add(version.path)
        projected.append(collections.SourceVersion(version.path, version.hash))
    return tuple(projected) if found_manifest else None


def project_query_result(
    result: record_formats.RecordQueryResult,
    manifest: collections.CollectionManifest,
    *,
    output_format: str = "json",
    agent_history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Default-deny wire envelope reconstructed from a typed query contract."""
    if (
        result.collection_id != manifest.collection_id
        or type(result.snapshot) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", result.snapshot)
        or not isinstance(result.rows, list)
        or not isinstance(result.source_versions, tuple)
        or not isinstance(result.columns, tuple)
        or type(result.returned) is not int
        or type(result.total_matched) is not int
        or type(result.truncated) is not bool
        or result.returned != len(result.rows)
        or result.returned < 0
        or result.total_matched < result.returned
        or output_format not in {"json", "markdown", "csv"}
        or not isinstance(result.query, Mapping)
        or not _valid_query_descriptor(result.query, manifest)
        or result.derived is not True
        or not _valid_saved_view(result.view, manifest)
        or not _valid_agent_history(agent_history)
        or (result.continuation is not None and type(result.continuation) is not str)
        or (
            isinstance(result.continuation, str)
            and not record_formats.validate_continuation(
            result.continuation,
            collection_id=result.collection_id,
            snapshot=result.snapshot,
            query=_cursor_query(result, manifest),
            )
        )
    ):
        return _withheld_query()
    columns = tuple(result.columns)
    allowed_columns = _query_value_fields(manifest) | _SYSTEM_FIELDS
    if (
        not columns
        or len(columns) != len(set(columns))
        or any(type(column) is not str or column not in allowed_columns for column in columns)
    ):
        return _withheld_query()
    require_item_version = manifest.storage.strategy != "dataset"
    rows = [
        _valid_row(row, manifest, result.query, require_item_version=require_item_version)
        for row in result.rows
    ]
    if any(row is None or set(row) - set(columns) for row in rows):
        return _withheld_query()
    source_versions = _valid_source_versions(result.source_versions, manifest)
    if source_versions is None or not _valid_aggregate(
        result.aggregate,
        result.query,
        manifest,
        result.total_matched,
        require_item_version=require_item_version,
    ):
        return _withheld_query()
    typed = record_formats.RecordQueryResult(
        collection_id=result.collection_id,
        snapshot=result.snapshot,
        rows=[row for row in rows if row is not None],
        returned=result.returned,
        total_matched=result.total_matched,
        truncated=result.truncated,
        continuation=result.continuation,
        derived=True,
        rendered="",
        aggregate=dict(result.aggregate)
        if isinstance(result.aggregate, Mapping)
        else result.aggregate,
        query=dict(result.query),
        source_versions=source_versions,
        columns=columns,
        view=dict(result.view) if isinstance(result.view, Mapping) else None,
    )
    try:
        rendered = record_formats.render_query_result(typed, output_format=output_format)
    except (collections.CollectionError, TypeError, ValueError):
        return _withheld_query()
    payload = _RecordEnvelope(
        {
            "collection_id": typed.collection_id,
            "snapshot": typed.snapshot,
            "rows": typed.rows,
            "returned": typed.returned,
            "total_matched": typed.total_matched,
            "truncated": typed.truncated,
            "continuation": typed.continuation,
            "derived": typed.derived,
            "aggregate": typed.aggregate,
            "query": typed.query,
            "source_versions": [
                {"path": version.path, "hash": version.hash} for version in typed.source_versions
            ],
            "view": typed.view,
            **({"agent_history": dict(agent_history)} if agent_history is not None else {}),
            "rendered": rendered,
        }
    )
    return egress.project(payload, egress.LEVEL_FULL, kind="record_query") or {}


def _cursor_query(
    result: record_formats.RecordQueryResult, manifest: collections.CollectionManifest
) -> Mapping[str, Any]:
    if result.view is None:
        return result.query
    view = collections.resolve_saved_view(manifest, result.view["name"])
    query = view.definition["query"]
    assert isinstance(query, Mapping)
    return {
        **result.query,
        "limit": query.get("limit", query_data.DEFAULT_LIMIT),
        "view": dict(result.view),
    }


def _valid_saved_view(value: Any, manifest: collections.CollectionManifest) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping) or set(value) != {"name", "definition", "identity"}:
        return False
    name = value.get("name")
    definition = value.get("definition")
    identity = value.get("identity")
    if type(name) is not str or not isinstance(definition, Mapping) or type(identity) is not str:
        return False
    try:
        expected = collections.resolve_saved_view(manifest, name)
    except collections.CollectionError:
        return False
    return dict(definition) == dict(expected.definition) and identity == expected.identity


def _valid_agent_history(value: Mapping[str, Any] | None) -> bool:
    if value is None:
        return True
    if set(value) - {
        "status",
        "complete",
        "truncated",
        "events",
        "discontinuity",
        "discontinuities",
    }:
        return False
    if value["status"] not in ("baseline", "ok", "gap", "acknowledged_gap", "history_incomplete"):
        return False
    if type(value["complete"]) is not bool or type(value["truncated"]) is not bool:
        return False
    events = value["events"]
    if not isinstance(events, list) or len(events) > 50:
        return False
    allowed = {
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
    lifecycle = allowed | {
        "continuity",
        "acknowledged_gap_codes",
        "gap_fingerprint",
        "checkpoint_snapshot_hash",
        "minimum_reader_version",
    }
    for event in events:
        if not isinstance(event, Mapping) or (set(event) != allowed and set(event) != lifecycle):
            return False
        if event["operation"] not in ("create", "append", "update", "revise", "rebaseline"):
            return False
        if type(event["transition_id"]) is not str or not re.fullmatch(
            r"[0-9a-f]{24}", event["transition_id"]
        ):
            return False
        parent = event["parent_id"]
        if parent not in ("baseline", "absent") and (
            type(parent) is not str or re.fullmatch(r"[0-9a-f]{24}", parent) is None
        ):
            return False
        if event["item_key"] is not None and memory_refs.normalize_id(event["item_key"]) is None:
            return False
        if (
            type(event["canonical_path"]) is not str
            or event["canonical_path"].startswith("/")
            or ".." in event["canonical_path"].split("/")
        ):
            return False
        for field in (
            "before_manifest_hash",
            "after_manifest_hash",
            "before_item_hash",
            "after_item_hash",
            "before_manifest_hash",
            "after_manifest_hash",
            "before_container_hash",
            "after_container_hash",
        ):
            if event[field] is not None and (
                type(event[field]) is not str or re.fullmatch(r"[0-9a-f]{64}", event[field]) is None
            ):
                return False
        if type(event["rationale"]) is not str or len(event["rationale"].encode("utf-8")) > 512:
            return False

    def valid_discontinuity(item: Any) -> bool:
        return (
            isinstance(item, Mapping)
            and set(item)
            == {
                "provenance_continuity",
                "prior_head",
                "acknowledged_gap_codes",
                "rationale",
                "checkpoint_transition",
                "gap_fingerprint",
                "checkpoint_snapshot_hash",
            }
            and item["provenance_continuity"] is False
            and type(item["prior_head"]) is str
            and (
                item["prior_head"] == "baseline"
                or re.fullmatch(r"[0-9a-f]{24}", item["prior_head"]) is not None
            )
            and isinstance(item["acknowledged_gap_codes"], list)
            and item["acknowledged_gap_codes"] == sorted(set(item["acknowledged_gap_codes"]))
            and all(
                type(code) is str and 0 < len(code.encode("utf-8")) <= 256
                for code in item["acknowledged_gap_codes"]
            )
            and type(item["rationale"]) is str
            and 0 < len(item["rationale"].encode("utf-8")) <= 512
            and type(item["checkpoint_transition"]) is str
            and re.fullmatch(r"[0-9a-f]{24}", item["checkpoint_transition"]) is not None
            and all(
                type(item[name]) is str and re.fullmatch(r"[0-9a-f]{64}", item[name]) is not None
                for name in ("gap_fingerprint", "checkpoint_snapshot_hash")
        )
        )

    discontinuity = value.get("discontinuity")
    discontinuities = value.get("discontinuities")
    if discontinuity is not None and not valid_discontinuity(discontinuity):
        return False
    if discontinuities is not None and (
        not isinstance(discontinuities, list)
        or len(discontinuities) > 16
        or not all(valid_discontinuity(item) for item in discontinuities)
        or (
            discontinuity is not None
            and (not discontinuities or discontinuity != discontinuities[0])
        )
    ):
        return False
    return True


def project_mutation_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Default-deny terminal mutation receipt without arbitrary nested payloads."""
    if not mutation_terminal.valid_record_receipt(receipt):
        return {"withheld": True, "reason": "invalid_record_receipt"}
    allowed = {
        key: receipt[key]
        for key in (
            "operation",
            "collection_id",
            "item_key",
            "before_item_hash",
            "after_item_hash",
            "before_manifest_hash",
            "after_manifest_hash",
            "before_container_hash",
            "after_container_hash",
            "affected_paths",
            "outcome",
            "audit_correlation",
            "continuity",
            "acknowledged_gap_codes",
            "gap_fingerprint",
            "checkpoint_snapshot_hash",
            "minimum_reader_version",
        )
        if key in receipt
    }
    if receipt.get("receipt_version") == 2:
        allowed["payload_hash"] = receipt["payload_hash"]
    return egress.project(_RecordEnvelope(allowed), egress.LEVEL_FULL, kind="record_mutation") or {}


_MAX_MANIFEST_VALUE_DEPTH = 8
_MAX_MANIFEST_VALUE_NODES = 256
_MAX_MANIFEST_VALUE_BYTES = 16 * 1024
_TEMPLATE_METADATA = frozenset({"type", "collection_id", "schema_version", "record_id"})


def _normalize_manifest_value(
    value: Any,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
    active: set[int] | None = None,
) -> tuple[bool, Any]:
    """Return a bounded plain JSON value without coercing arbitrary objects."""
    nodes = [0] if nodes is None else nodes
    active = set() if active is None else active
    nodes[0] += 1
    if nodes[0] > _MAX_MANIFEST_VALUE_NODES or depth > _MAX_MANIFEST_VALUE_DEPTH:
        return False, None
    if value is None or type(value) in {bool, int}:
        return True, value
    if type(value) is float:
        return (True, value) if isfinite(value) else (False, None)
    if type(value) is str:
        return (
            (True, value)
            if len(value.encode("utf-8")) <= _MAX_MANIFEST_VALUE_BYTES
            else (False, None)
        )
    if isinstance(value, Mapping):
        if id(value) in active:
            return False, None
        active.add(id(value))
        try:
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if type(key) is not str or len(key.encode("utf-8")) > 512:
                    return False, None
                valid, projected = _normalize_manifest_value(
                    item, depth=depth + 1, nodes=nodes, active=active
                )
                if not valid:
                    return False, None
                normalized[key] = projected
            return True, normalized
        finally:
            active.remove(id(value))
    if isinstance(value, (list, tuple)):
        if id(value) in active:
            return False, None
        active.add(id(value))
        try:
            normalized_items: list[Any] = []
            for item in value:
                valid, projected = _normalize_manifest_value(
                    item, depth=depth + 1, nodes=nodes, active=active
                )
                if not valid:
                    return False, None
                normalized_items.append(projected)
            return True, normalized_items
        finally:
            active.remove(id(value))
    return False, None


def _project_schema_values(
    root: Path,
    manifest: collections.CollectionManifest,
    values: Mapping[str, Any],
    *,
    template_metadata: bool = False,
    links: _LinkProjector | None = None,
) -> dict[str, Any] | None:
    valid, normalized = _normalize_manifest_value(values)
    if not valid or not isinstance(normalized, dict):
        return None
    projected: dict[str, Any] = {}
    for name, value in normalized.items():
        spec = manifest.schema.fields.get(name)
        if spec is not None:
            try:
                collections._validate_field_value(name, value, spec)
            except collections.CollectionError:
                return None
            projected[name] = value
            continue
        if template_metadata and name in _TEMPLATE_METADATA:
            if (
                (name == "type" and value == manifest.semantic_profile.removesuffix("s"))
                or (name == "collection_id" and value == manifest.collection_id)
                or (name == "schema_version" and value == manifest.schema.version)
                or (name == "record_id" and type(value) is str and memory_refs.normalize_id(value))
            ):
                projected[name] = value
    return (links or _LinkProjector.create(root, manifest))(projected)


def _project_manifest_query(
    root: Path,
    manifest: collections.CollectionManifest,
    query: Mapping[str, Any],
    *,
    links: _LinkProjector | None = None,
) -> dict[str, Any] | None:
    valid, normalized = _normalize_manifest_value(query)
    if not valid or not isinstance(normalized, dict) or set(normalized) - {"filters", "limit"}:
        return None
    limit = normalized.get("limit")
    if type(limit) is not int or not 1 <= limit <= query_data.HARD_ROW_CAP:
        return None
    filters = normalized.get("filters", {})
    if not isinstance(filters, dict) or len(filters) > len(manifest.schema.fields):
        return None
    if any(name not in manifest.schema.fields for name in filters):
        return None
    projected_filters = _project_schema_values(root, manifest, filters, links=links)
    if projected_filters is None:
        return None
    result: dict[str, Any] = {"limit": limit}
    if projected_filters:
        result = {"filters": projected_filters, **result}
    return result


def _opaque_plan_reference(reference: object) -> str | None:
    """Validate a manifest-authored Planning descriptor without resolving it."""
    if type(reference) is not str or not 1 <= len(reference.encode("utf-8")) <= 2_048:
        return None
    stable_id = memory_refs.parse_memory_ref(reference)
    if reference.startswith(memory_refs.REF_PREFIX):
        return reference if stable_id and reference == memory_refs.memory_ref(stable_id) else None
    for prefix in ("exomem://vault/", "exomem://source/"):
        if not reference.startswith(prefix):
            continue
        encoded = reference[len(prefix) :]
        decoded = unquote(encoded)
        if (
            not encoded
            or quote(decoded, safe="/") != encoded
            or decoded.startswith("/")
            or re.match(r"^[A-Za-z]:", decoded) is not None
            or "\\" in decoded
            or "\0" in decoded
            or any(part in {"", ".", ".."} for part in decoded.split("/"))
        ):
            return None
        return reference
    return None


def _project_plan_link(
    root: Path,
    manifest: collections.CollectionManifest,
    plan: collections.PlanLink,
    *,
    links: _LinkProjector | None = None,
) -> dict[str, Any] | None:
    reference = _opaque_plan_reference(plan.reference)
    if reference is None:
        return None
    query = _project_manifest_query(root, manifest, plan.query, links=links)
    if query is None:
        return None
    projected = {"reference": reference, "query": query}
    if plan.join:
        projected["join"] = dict(plan.join)
    return projected


def _project_views(
    root: Path, manifest: collections.CollectionManifest, *, links: _LinkProjector | None = None
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    if len(manifest.views) > 32:
        return projected
    for name, view in manifest.views.items():
        if type(name) is not str or not name or len(name.encode("utf-8")) > 128:
            continue
        valid, normalized = _normalize_manifest_value(view)
        if not valid or not isinstance(normalized, dict):
            continue
        if set(normalized) == {"query"} and isinstance(normalized["query"], dict):
            query = _project_manifest_query(root, manifest, normalized["query"], links=links)
            if query is not None:
                projected[name] = {"query": query}
        elif set(normalized) == {"sort"} and isinstance(normalized["sort"], list):
            sort = normalized["sort"]
            if (
                len(sort) == 2
                and type(sort[0]) is str
                and sort[0] in manifest.schema.fields
                and sort[1] in {"asc", "desc"}
            ):
                projected[name] = {"sort": sort}
    return projected


def _project_governance(value: Mapping[str, Any]) -> dict[str, Any]:
    valid, normalized = _normalize_manifest_value(value)
    if not valid or not isinstance(normalized, dict):
        return {}
    projected: dict[str, Any] = {}
    classification = normalized.get("classification")
    if type(classification) in {str, int, bool} and (
        type(classification) is not str or 1 <= len(classification.encode("utf-8")) <= 128
    ):
        projected["classification"] = classification
    release = normalized.get("release")
    if (
        isinstance(release, dict)
        and set(release) == {"tiers"}
        and isinstance(release["tiers"], list)
        and len(release["tiers"]) <= 16
        and all(
            type(tier) is str and 1 <= len(tier.encode("utf-8")) <= 128 for tier in release["tiers"]
        )
    ):
        projected["release"] = {"tiers": release["tiers"]}
    return projected


def project_manifest(
    vault_root: Path, collection: str | Path | collections.CollectionManifest
) -> dict[str, Any]:
    """Project a manifest only after every returned template target is released."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_manifest", join_existing=True) as collector:
        manifest = _resolve_released_collection(root, collection, receipt=True)
        if not _authorize(root, manifest.storage.source, receipt=True) or any(
            not _authorize(root, template.path, receipt=True) for template in manifest.templates
        ):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        templates: list[dict[str, Any]] = []
        links = _LinkProjector.create(root, manifest)
        for template in manifest.templates:
            defaults = _project_schema_values(
                root, manifest, template.default_properties, template_metadata=True, links=links
            )
            if defaults is None:
                continue
            templates.append({"path": template.path, "default_properties": defaults})
        payload = _RecordEnvelope(
            {
                "collection_id": manifest.collection_id,
                "path": manifest.path,
                "title": manifest.title,
                "storage": {
                    "strategy": manifest.storage.strategy,
                    "source": manifest.storage.source,
                    "format_version": manifest.storage.format_version,
                },
                "templates": templates,
                "plans": [
                    projected
                    for plan in manifest.links.plans
                    if (projected := _project_plan_link(root, manifest, plan, links=links))
                    is not None
                ],
                "views": _project_views(root, manifest, links=links),
                "governance": _project_governance(manifest.governance),
            }
        )
        projected = egress.project(payload, egress.LEVEL_FULL, kind="record_manifest") or {}
        egress.emit_boundary_receipt(collector)
        return projected


def read_template(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    template_path: str,
) -> bytes:
    """Return an explicitly declared template only after its L6 path decision."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_template", join_existing=True) as collector:
        manifest = _resolve_released_collection(root, collection, receipt=True)
        declared = {template.path for template in manifest.templates}
        if (
            not _authorize(root, manifest.path, receipt=True)
            or template_path not in declared
            or not _authorize(root, template_path, receipt=True)
        ):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        try:
            data, _guard = vault.read_bounded_guarded_bytes(root, template_path, limit=512 * 1024)
        except vault.PathGuardError as error:
            raise collections.CollectionError(
                "COLLECTION_NOT_FOUND", "collection was not found"
            ) from error
        egress.emit_boundary_receipt(collector)
        return data


def precommit_authorize_mutation(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    snapshot: record_formats.AdapterSnapshot | None,
    *,
    planned_paths: Iterable[str] = (),
) -> None:
    """Record a pre-publication authorization decision for the complete CAS set.

    A subset snapshot is never sufficient for a Records mutation. The receipt
    is intentionally emitted before publication and describes authorization,
    not a committed write.
    """
    root = Path(vault_root)
    paths = {manifest.path, manifest.storage.source, *planned_paths}
    require_mutation_visibility(root, manifest, planned_paths=paths)
    if snapshot is not None:
        paths.update(version.path for version in snapshot.source_versions)
        paths.update(path for path, kind, _digest in snapshot.source_inventory if kind == "file")
    with egress.disclosure_boundary(root, "record_mutation_precommit") as collector:
        for path in sorted(paths):
            if not _authorize(root, path, receipt=True):
                raise collections.CollectionError(
                    "COLLECTION_NOT_FOUND", "collection was not found"
                )
        egress.emit_boundary_receipt(collector)


def require_mutation_visibility(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    planned_paths: Iterable[str] = (),
) -> None:
    """Refuse before parsing when a mutation cannot see the entire CAS set."""
    root = Path(vault_root)
    allowed = full_release_filter(root)
    if not all(allowed(path) for path in (manifest.path, manifest.storage.source, *planned_paths)):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    if manifest.storage.strategy != "markdown-items":
        return
    pending = [vault.DirectoryCensusGuard.capture(root, manifest.storage.source, max_entries=2_000)]
    candidates = 0
    while pending:
        directory = pending.pop()
        for entry in directory.entries:
            candidates += 1
            if candidates > 2_000:
                raise collections.CollectionError(
                    "RECORD_ITEM_LIMIT", "collection has too many item entries"
                )
            if not allowed(entry.relative_path):
                raise collections.CollectionError(
                    "COLLECTION_NOT_FOUND", "collection was not found"
                )
            if stat.S_ISDIR(entry.mode):
                pending.append(
                    vault.DirectoryCensusGuard.capture(root, entry.relative_path, max_entries=2_000)
                )


def require_candidate_manifest_visibility(vault_root: Path, manifest_path: str) -> None:
    """Gate create preflight before caller-controlled manifest bytes are parsed."""
    if not full_release_filter(Path(vault_root))(manifest_path):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")


def require_proposed_manifest_visibility(
    vault_root: Path, manifest: collections.CollectionManifest
) -> None:
    """Admit every path directly declared by a revised manifest before publication."""
    allowed = full_release_filter(Path(vault_root))
    paths = [
        manifest.path,
        manifest.storage.source,
        *(template.path for template in manifest.templates),
    ]
    if not all(allowed(path) for path in paths):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    links = _LinkProjector.create(Path(vault_root), manifest)
    for view_name in manifest.views:
        _authorize_saved_view(Path(vault_root), manifest, view_name, links)
    if any(
        (reference := _opaque_plan_reference(plan.reference)) is None
        or (
            reference.lower().startswith(("exomem://vault/", "exomem://source/"))
            and not links._allowed(reference)
        )
        for plan in manifest.links.plans
    ):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
