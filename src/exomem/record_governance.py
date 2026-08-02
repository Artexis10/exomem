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


@dataclass(frozen=True, slots=True)
class _RecordEnvelope:
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


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
    ),
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
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "outcome",
        "audit_correlation",
    ),
)


def full_release_filter(vault_root: Path) -> Callable[[str], bool]:
    """Return the Records full-content gate without the normal L5 walk floor."""
    root = Path(vault_root)

    def allowed(relative: str) -> bool:
        return not access.refuse_if_excluded(root, relative) and (
            egress.release_level_for_path_only(root, relative) == egress.LEVEL_FULL
        )

    return allowed


def _authorize(root: Path, relative: str, *, receipt: bool = False) -> bool:
    if access.refuse_if_excluded(root, relative):
        return False
    return (
        egress.release_level_for_path_only(
            root,
            relative,
            receipt_decision="release_authorized" if receipt else None,
        )
        == egress.LEVEL_FULL
    )


@dataclass(slots=True)
class _LinkProjector:
    root: Path
    manifest: collections.CollectionManifest
    resolver: vault.WikilinkResolver
    verdicts: dict[str, bool]

    @classmethod
    def create(cls, root: Path, manifest: collections.CollectionManifest) -> _LinkProjector:
        return cls(root, manifest, vault.WikilinkResolver(root), {})

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

    def _project_value(self, value: Any, spec: collections.FieldSpec) -> Any:
        if spec.type == "array" and spec.items is not None and isinstance(value, list | tuple):
            return [result for item in value if (result := self._project_value(item, spec.items)) is not None]
        if spec.type != "link" or type(value) is not str:
            return value
        return value if self._allowed(value) else None

    def _allowed(self, value: str) -> bool:
        raw = value.strip()
        try:
            if raw.lower().startswith(memory_refs.REF_PREFIX):
                identity = memory_refs.parse_memory_ref(raw)
                if identity is None or raw != memory_refs.memory_ref(identity):
                    return False
                try:
                    target = memory_refs.resolve_identifier_read_only(self.root, raw)
                except memory_refs.ReferenceError:
                    return self._remember(f"memory:{identity}", False)
            elif raw.lower().startswith(("exomem://vault/", "exomem://source/")):
                target = memory_refs.resolve_identifier_read_only(self.root, raw)
            elif (match := re.fullmatch(r"\[\[([^\[\]]+)\]\]", raw)) is not None:
                inner = match.group(1).strip()
                if not inner or inner.count("|") > 1:
                    return False
                try:
                    canonical, _warning = vault.normalize_wikilink(
                        inner.split("|", 1)[0].strip(), self.root, resolver=self.resolver, strict=True
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
        return self._remember(relative, _authorize(self.root, relative, receipt=True))

    def _remember(self, target: str, allowed: bool) -> bool:
        if target in self.verdicts:
            return self.verdicts[target]
        self.verdicts[target] = allowed
        return allowed


def _project_links(
    root: Path, manifest: collections.CollectionManifest, values: Mapping[str, Any]
) -> dict[str, Any]:
    return _LinkProjector.create(root, manifest)(values)


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
) -> collections.CollectionManifest:
    path = selector.path if isinstance(selector, collections.CollectionManifest) else selector
    manifest = collections.resolve_collection(
        root,
        path,
        authorize_path=lambda relative: _authorize(root, relative, receipt=receipt),
    )
    if not _authorize(root, manifest.path, receipt=receipt):
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
    return manifest


def query_collection(
    vault_root: Path,
    collection: str | Path | collections.CollectionManifest,
    **kwargs: Any,
) -> record_formats.RecordQueryResult:
    """Query released Records only; authorization happens before adapter parsing."""
    root = Path(vault_root)
    with egress.disclosure_boundary(root, "record_query", join_existing=True) as collector:
        manifest = _resolve_released_collection(root, collection, receipt=True)
        if not _authorize(
            root, manifest.storage.source, receipt=True
        ):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        links = _LinkProjector.create(root, manifest)
        result = record_formats.query_collection(
            root,
            manifest,
            authorize_path=lambda path: _authorize(root, path, receipt=True),
            project_values=links,
            **kwargs,
        )
        egress.emit_boundary_receipt(collector)
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
    }
)
_SYSTEM_FIELDS = frozenset(
    {"collection_id", "record_id", "item_version", "inferred", "ambiguous", "parent_record_id"}
)
_QUERY_OPS = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "icontains", "startswith", "in", "nin", "exists", "missing"}
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


def _valid_query_descriptor(query: Mapping[str, Any], manifest: collections.CollectionManifest) -> bool:
    if set(query) != _QUERY_KEYS:
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
    if any(query[key] is not None and type(query[key]) is not str for key in ("date_from", "date_to")):
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
    return function in {"min", "max", "sum", "avg", "latest", "distinct", "group"} and column in columns


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
        or type(row["parent_record_id"]) is not str
        or row["parent_record_id"] != row["record_id"]
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
        return set(aggregate) == {"count"} and type(aggregate["count"]) is int and aggregate["count"] == total_matched
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
            and (latest is None or _valid_row(latest, manifest, query, require_item_version=require_item_version) is not None)
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
        elif manifest.storage.strategy != "markdown-items" and version.path != manifest.storage.source:
            return None
        elif manifest.storage.strategy == "markdown-items" and not version.path.startswith(source_prefix):
            return None
        seen.add(version.path)
        projected.append(collections.SourceVersion(version.path, version.hash))
    return tuple(projected) if found_manifest else None


def project_query_result(
    result: record_formats.RecordQueryResult,
    manifest: collections.CollectionManifest,
    *,
    output_format: str = "json",
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
        or (result.continuation is not None and type(result.continuation) is not str)
        or (isinstance(result.continuation, str) and not record_formats.validate_continuation(
            result.continuation,
            collection_id=result.collection_id,
            snapshot=result.snapshot,
            query=result.query,
        ))
    ):
        return _withheld_query()
    columns = tuple(result.columns)
    allowed_columns = _query_value_fields(manifest) | _SYSTEM_FIELDS
    if not columns or len(columns) != len(set(columns)) or any(
        type(column) is not str or column not in allowed_columns for column in columns
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
        aggregate=dict(result.aggregate) if isinstance(result.aggregate, Mapping) else result.aggregate,
        query=dict(result.query),
        source_versions=source_versions,
        columns=columns,
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
            "rendered": rendered,
        }
    )
    return egress.project(payload, egress.LEVEL_FULL, kind="record_query") or {}


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
            "before_container_hash",
            "after_container_hash",
            "affected_paths",
            "outcome",
            "audit_correlation",
        )
        if key in receipt
    }
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
        return (True, value) if len(value.encode("utf-8")) <= _MAX_MANIFEST_VALUE_BYTES else (False, None)
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
    root: Path, manifest: collections.CollectionManifest, query: Mapping[str, Any], *, links: _LinkProjector | None = None
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
    root: Path, manifest: collections.CollectionManifest, plan: collections.PlanLink, *, links: _LinkProjector | None = None
) -> dict[str, Any] | None:
    reference = _opaque_plan_reference(plan.reference)
    if reference is None:
        return None
    query = _project_manifest_query(root, manifest, plan.query, links=links)
    return {"reference": reference, "query": query} if query is not None else None


def _project_views(root: Path, manifest: collections.CollectionManifest, *, links: _LinkProjector | None = None) -> dict[str, Any]:
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
        and all(type(tier) is str and 1 <= len(tier.encode("utf-8")) <= 128 for tier in release["tiers"])
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
                    if (projected := _project_plan_link(root, manifest, plan, links=links)) is not None
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
