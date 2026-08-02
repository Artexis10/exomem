"""Canonical collection adapters and bounded, read-only record queries."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import memory_refs, query_data, vault
from . import structured_collections as collections

_MARKER = re.compile(rb"<!--\s*exomem-record-id:\s*([0-9a-fA-F-]{36})\s*-->")
_HEADING = re.compile(rb"^(#{1,6})\s+(.+?)\s*(?:\r?\n|$)")
_FENCE = re.compile(rb"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ChildRow:
    values: dict[str, Any]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Record:
    identity: collections.ItemIdentity
    values: dict[str, Any]
    source: collections.SourceVersion
    span: SourceSpan
    body: str = ""
    children: tuple[ChildRow, ...] = ()
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    records: tuple[Record, ...]
    snapshot: str
    insertion_offset: int | None = None
    diagnostics: tuple[collections.CollectionDiagnostic, ...] = ()


class CollectionAdapter(Protocol):
    mutable: bool

    def read(self) -> AdapterSnapshot: ...

    def refuse_mutation(self, action: str) -> None: ...


@dataclass(slots=True)
class _BaseAdapter:
    vault_root: Path
    manifest: collections.CollectionManifest
    mutable: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.mutable = self.manifest.storage.strategy != "dataset"

    def refuse_mutation(self, action: str) -> None:
        raise collections.CollectionError(
            "UNSUPPORTED_RECORD_MUTATION",
            f"{action} is unsupported for {self.manifest.storage.strategy} storage",
        )

    @property
    def source_path(self) -> Path:
        return self.vault_root / self.manifest.storage.source

    def _identity(self, values: dict[str, Any], marker: str | None) -> collections.ItemIdentity:
        if marker is not None:
            return collections.ItemIdentity(self.manifest.collection_id, marker)
        field_types = {name: spec.type for name, spec in self.manifest.schema.fields.items()}
        serialized = collections.natural_key_serialization(
            self.manifest.schema.version,
            self.manifest.schema.natural_key,
            values,
            field_types=field_types,
        )
        return collections.ItemIdentity(
            self.manifest.collection_id,
            collections.inferred_item_key(self.manifest.collection_id, serialized),
            inferred=True,
        )


class MarkdownLogAdapter(_BaseAdapter):

    def read(self) -> AdapterSnapshot:
        source = self.source_path
        try:
            data = source.read_bytes()
        except OSError as error:
            raise collections.CollectionError("SOURCE_NOT_FOUND", "canonical source could not be read") from error
        descriptor = self.manifest.storage.descriptor
        section = str(descriptor.get("section", ""))
        heading = descriptor.get("heading", {})
        if not isinstance(heading, Mapping) or type(heading.get("level")) is not int:
            raise collections.CollectionError("INVALID_STORAGE_DESCRIPTOR", "markdown log needs a heading level")
        level = heading["level"]
        date_format = str(heading.get("date_format", "%Y-%m-%d"))
        insertion = descriptor.get("insertion")
        if insertion not in {"newest-first", "oldest-first"}:
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "markdown log needs an insertion direction"
            )
        child_spec = descriptor.get("child_rows", {})
        if not isinstance(child_spec, Mapping):
            raise collections.CollectionError("INVALID_STORAGE_DESCRIPTOR", "child_rows must be an object")
        delimiter = str(child_spec.get("delimiter", ""))
        child_fields = tuple(str(value) for value in child_spec.get("fields", ()))

        headings = _headings_outside_fences(data)
        section_heading = next(
            (item for item in headings if item.level == level and item.title == section), None
        )
        if section_heading is None:
            raise collections.CollectionError("COLLECTION_SECTION_NOT_FOUND", "declared markdown section was not found")
        section_end = next(
            (
                item.start
                for item in headings
                if item.start > section_heading.start
                and (
                    item.level < level
                    or (item.level == level and not _is_record_heading(item.title, date_format))
                )
            ),
            len(data),
        )
        entries = [
            item
            for item in headings
            if section_heading.end <= item.start < section_end
            and item.level == level
            and _is_record_heading(item.title, date_format)
        ]
        records: list[Record] = []
        for index, item in enumerate(entries):
            end = entries[index + 1].start if index + 1 < len(entries) else section_end
            block = data[item.start:end]
            values = _log_values(item.title, block, date_format, delimiter, child_fields)
            marker_match = _MARKER.search(block)
            marker = None
            if marker_match:
                marker = memory_refs.normalize_id(marker_match.group(1).decode("ascii"))
                if marker is None:
                    raise collections.CollectionError("INVALID_RECORD_ID", "markdown marker is not a UUID")
            identity = self._identity(values, marker)
            children = _child_rows(block, item.start, delimiter, child_fields)
            if children:
                values["movements"] = [child.values for child in children]
            records.append(
                Record(
                    identity=identity,
                    values=values,
                    source=collections.SourceVersion(
                        path=self.manifest.storage.source,
                        hash=hashlib.sha256(block).hexdigest(),
                    ),
                    span=SourceSpan(item.start, end),
                    children=children,
                )
            )
        records, diagnostics = _mark_ambiguous(records)
        insertion_offset = (
            _first_content_offset(data, section_heading.end, section_end)
            if insertion == "newest-first"
            else section_end
        )
        return AdapterSnapshot(
            records=tuple(records),
            snapshot=hashlib.sha256(data).hexdigest(),
            insertion_offset=insertion_offset,
            diagnostics=diagnostics,
        )


class MarkdownItemsAdapter(_BaseAdapter):

    def read(self) -> AdapterSnapshot:
        source = self.source_path
        if not source.is_dir():
            raise collections.CollectionError("SOURCE_NOT_FOUND", "canonical item directory could not be read")
        records: list[Record] = []
        versions: list[tuple[str, str]] = []
        for path in sorted(source.rglob("*.md")):
            data = path.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise collections.CollectionError("INVALID_RECORD_ITEM", "record item is not UTF-8") from error
            try:
                frontmatter, body, marker = vault.parse_frontmatter(text, strict=True)
            except vault.FrontmatterError as error:
                raise collections.CollectionError(error.code, error.reason) from error
            if marker is None or frontmatter.get("type") != "record":
                continue
            collection_id = memory_refs.normalize_id(str(frontmatter.get("collection_id", "")))
            record_id = memory_refs.normalize_id(str(frontmatter.get("record_id", "")))
            if collection_id != self.manifest.collection_id or record_id is None:
                raise collections.CollectionError("INVALID_RECORD_ITEM", "record item identity is invalid")
            if frontmatter.get("schema_version") != self.manifest.schema.version:
                raise collections.CollectionError("UNSUPPORTED_ITEM_SCHEMA_VERSION", "item schema version differs")
            values = {
                name: _json_value(value)
                for name, value in frontmatter.items()
                if name not in {"type", "collection_id", "record_id", "schema_version"}
            }
            self.manifest.schema.validate(values)
            rel = path.relative_to(self.vault_root).as_posix()
            digest = hashlib.sha256(data).hexdigest()
            versions.append((rel, digest))
            records.append(
                Record(
                    identity=collections.ItemIdentity(collection_id, record_id),
                    values=values,
                    source=collections.SourceVersion(path=rel, hash=digest),
                    span=SourceSpan(0, len(data)),
                    body=body,
                )
            )
        records, diagnostics = _mark_ambiguous(records)
        return AdapterSnapshot(tuple(records), _snapshot(versions), diagnostics=diagnostics)


class DatasetAdapter(_BaseAdapter):
    mutable = False

    def read(self) -> AdapterSnapshot:
        source = self.source_path
        record_path = self.manifest.storage.descriptor.get("record_path")
        if record_path is not None and (not isinstance(record_path, str) or not record_path):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "dataset record path must be a non-empty string"
            )
        try:
            _format, rows, _columns, _warnings = query_data.load_rows(source, record_path)
        except query_data.QueryDataError as error:
            raise collections.CollectionError(error.code, error.reason) from error
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        key_name = self.manifest.storage.descriptor.get("key")
        records: list[Record] = []
        for index, row in enumerate(rows):
            values = {name: _json_value(value) for name, value in row.items()}
            if key_name is not None:
                key = str(values.get(str(key_name), ""))
                if not key:
                    raise collections.CollectionError("INVALID_DATASET_KEY", "declared dataset key is missing")
                identity = collections.ItemIdentity(self.manifest.collection_id, key)
            else:
                identity = self._identity(values, None)
            records.append(
                Record(
                    identity=identity,
                    values=values,
                    source=collections.SourceVersion(path=self.manifest.storage.source, hash=digest),
                    span=SourceSpan(index, index + 1),
                )
            )
        records, diagnostics = _mark_ambiguous(records)
        return AdapterSnapshot(tuple(records), digest, diagnostics=diagnostics)


def load_adapter(vault_root: Path, manifest: collections.CollectionManifest) -> CollectionAdapter:
    """Return the declared canonical adapter without inferring domain grammar."""
    if manifest.storage.strategy == "markdown-log":
        return MarkdownLogAdapter(Path(vault_root), manifest)
    if manifest.storage.strategy == "markdown-items":
        return MarkdownItemsAdapter(Path(vault_root), manifest)
    if manifest.storage.strategy == "dataset":
        return DatasetAdapter(Path(vault_root), manifest)
    raise collections.CollectionError("UNSUPPORTED_STORAGE", "collection storage is unsupported")


@dataclass(frozen=True, slots=True)
class RecordQueryResult:
    collection_id: str
    snapshot: str
    rows: list[dict[str, Any]]
    returned: int
    total_matched: int
    truncated: bool
    continuation: str | None
    derived: bool
    rendered: str
    aggregate: Any = None


def query_collection(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    filters: list[dict] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int | None = query_data.DEFAULT_LIMIT,
    aggregate: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column: str | None = None,
    expand_children: bool = False,
    continuation: str | None = None,
    output_format: str = "json",
) -> RecordQueryResult:
    """Query a fresh canonical adapter snapshot with a snapshot-bound cursor."""
    adapter = load_adapter(vault_root, manifest)
    parsed = adapter.read()
    query = {
        "filters": filters or [],
        "columns": columns,
        "sort_by": sort_by,
        "descending": descending,
        "aggregate": aggregate,
        "date_from": date_from,
        "date_to": date_to,
        "date_column": date_column,
        "expand_children": expand_children,
    }
    offset = 0
    if continuation:
        token = _decode_continuation(continuation)
        if token.get("collection_id") != manifest.collection_id or token.get("query") != query:
            raise collections.CollectionError("INVALID_RECORD_CONTINUATION", "continuation does not match query")
        if token.get("snapshot") != parsed.snapshot:
            raise collections.CollectionError("STALE_RECORD_SNAPSHOT", "canonical source changed")
        offset = token["offset"]
    rows = _query_rows(parsed.records, expand_children)
    result = query_data.evaluate_rows(
        rows,
        path=manifest.storage.source,
        format=manifest.storage.strategy,
        filters=filters,
        columns=columns,
        sort_by=sort_by,
        descending=descending,
        limit=limit,
        offset=offset,
        aggregate=aggregate,
        date_from=date_from,
        date_to=date_to,
        date_column=date_column,
    )
    next_offset = offset + result.returned
    next_token = None
    if result.truncated:
        next_token = _encode_continuation(
            {
                "collection_id": manifest.collection_id,
                "snapshot": parsed.snapshot,
                "query": query,
                "offset": next_offset,
            }
        )
    rendered = _render_query(result, manifest.collection_id, parsed.snapshot, output_format)
    return RecordQueryResult(
        collection_id=manifest.collection_id,
        snapshot=parsed.snapshot,
        rows=result.rows,
        returned=result.returned,
        total_matched=result.total_matched,
        truncated=result.truncated,
        continuation=next_token,
        derived=True,
        rendered=rendered,
        aggregate=result.aggregate,
    )


@dataclass(frozen=True, slots=True)
class _Heading:
    level: int
    title: str
    start: int
    end: int


def _headings_outside_fences(data: bytes) -> list[_Heading]:
    headings: list[_Heading] = []
    offset = 0
    fence: bytes | None = None
    for line in data.splitlines(keepends=True):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[:1]
            elif marker.startswith(fence):
                fence = None
        elif fence is None and (match := _HEADING.match(line)):
            headings.append(
                _Heading(
                    level=len(match.group(1)),
                    title=match.group(2).decode("utf-8").strip(),
                    start=offset,
                    end=offset + len(line),
                )
            )
        offset += len(line)
    return headings


def _log_values(
    title: str, block: bytes, date_format: str, delimiter: str, child_fields: tuple[str, ...]
) -> dict[str, Any]:
    date, separator, name = title.partition(" ")
    if not separator:
        raise collections.CollectionError("INVALID_RECORD_HEADING", "record heading lacks a title")
    try:
        occurred_on = dt.datetime.strptime(date, date_format).date().isoformat()
    except ValueError as error:
        raise collections.CollectionError("INVALID_RECORD_HEADING", "record heading does not match date format") from error
    values: dict[str, Any] = {"occurred_on": occurred_on, "title": name.strip()}
    for raw in block.splitlines():
        line = raw.decode("utf-8").strip()
        if line.startswith("status:"):
            values["status"] = line.partition(":")[2].strip()
    return values


def _is_record_heading(title: str, date_format: str) -> bool:
    date, separator, _name = title.partition(" ")
    if not separator:
        return False
    try:
        dt.datetime.strptime(date, date_format)
    except ValueError:
        return False
    return True


def _child_rows(
    block: bytes, block_start: int, delimiter: str, fields: tuple[str, ...]
) -> tuple[ChildRow, ...]:
    if not delimiter or not fields:
        return ()
    rows: list[ChildRow] = []
    offset = block_start
    for raw in block.splitlines(keepends=True):
        line = raw.decode("utf-8").strip()
        if delimiter in line:
            values = [part.strip() for part in line.split(delimiter)]
            if len(values) == len(fields):
                rows.append(
                    ChildRow(dict(zip(fields, values, strict=True)), SourceSpan(offset, offset + len(raw)))
                )
        offset += len(raw)
    return tuple(rows)


def _first_content_offset(data: bytes, start: int, end: int) -> int:
    match = re.search(rb"[^\r\n]", data[start:end])
    return start + match.start() if match else end


def _mark_ambiguous(records: list[Record]) -> tuple[list[Record], tuple[collections.CollectionDiagnostic, ...]]:
    counts: dict[str, int] = {}
    for record in records:
        if record.identity.inferred:
            counts[record.identity.key] = counts.get(record.identity.key, 0) + 1
    ambiguous = {key for key, count in counts.items() if count > 1}
    diagnostics = tuple(
        collections.CollectionDiagnostic("AMBIGUOUS_RECORD_KEY", "legacy natural key is not unique")
        for _key in sorted(ambiguous)
    )
    return [
        Record(
            identity=record.identity,
            values=record.values,
            source=record.source,
            span=record.span,
            body=record.body,
            children=record.children,
            ambiguous=record.identity.key in ambiguous,
        )
        for record in records
    ], diagnostics


def _snapshot(versions: list[tuple[str, str]]) -> str:
    payload = json.dumps(versions, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _query_rows(records: tuple[Record, ...], expand_children: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        base = {
            **record.values,
            "collection_id": record.identity.collection_id,
            "record_id": record.identity.key,
            "item_version": record.source.hash,
            "inferred": record.identity.inferred,
            "ambiguous": record.ambiguous,
        }
        if expand_children:
            for child in record.children:
                rows.append({"parent_record_id": record.identity.key, **base, **child.values})
        else:
            rows.append(base)
    return rows


def _encode_continuation(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_continuation(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise collections.CollectionError("INVALID_RECORD_CONTINUATION", "continuation is invalid") from error
    if not isinstance(payload, dict) or type(payload.get("offset")) is not int or payload["offset"] < 0:
        raise collections.CollectionError("INVALID_RECORD_CONTINUATION", "continuation is invalid")
    return payload


def _render_query(
    result: query_data.QueryDataResult, collection_id: str, snapshot: str, output_format: str
) -> str:
    if output_format == "json":
        return json.dumps(
            {"collection_id": collection_id, "snapshot": snapshot, "derived": True, "rows": result.rows},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if output_format == "markdown":
        lines = [
            "---",
            f"collection_id: {collection_id}",
            f"snapshot: {snapshot}",
            "derived: true",
            "---",
            "",
        ]
        if result.columns:
            lines.extend(
                [
                    "| " + " | ".join(result.columns) + " |",
                    "| " + " | ".join("---" for _ in result.columns) + " |",
                ]
            )
            lines.extend(
                "| " + " | ".join(str(row.get(column, "")) for column in result.columns) + " |"
                for row in result.rows
            )
        return "\n".join(lines) + "\n"
    if output_format == "csv":
        output: list[str] = []
        class _Writer:
            def write(self, value: str) -> int:
                output.append(value)
                return len(value)

        writer = csv.DictWriter(_Writer(), fieldnames=result.columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result.rows)
        return "".join(output)
    raise collections.CollectionError("INVALID_QUERY_OUTPUT", "output format must be json, markdown, or csv")
