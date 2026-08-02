"""Canonical collection adapters and bounded, read-only record queries."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import memory_refs, query_data, vault
from . import structured_collections as collections

_MARKER = re.compile(rb"<!--\s*exomem-record-id:\s*([0-9a-fA-F-]{36})\s*-->")
_HEADING = re.compile(rb"^(#{1,6})\s+(.+?)\s*(?:\r?\n|$)")
_FENCE_OPEN = re.compile(rb"^ {0,3}((?:`{3,}|~{3,}))[^\r\n]*(?:\r?\n|$)")
_MAX_LOG_BYTES = 2 * 1024 * 1024
_MAX_ITEM_FILES = 2_000
_MAX_ITEM_BYTES = 512 * 1024
_MAX_COLLECTION_BYTES = 8 * 1024 * 1024
_MAX_TOKEN_PAYLOAD_BYTES = 4 * 1024
_MAX_TOKEN_ENVELOPE_BYTES = 6 * 1024
_CURSOR_VERSION = "v1"
_CURSOR_DOMAIN = b"exomem.record-continuation.v1\0"
_MAX_HEADING_FIELDS = 8
_MAX_GRAMMAR_LITERAL_BYTES = 128
_MAX_NOTE_RULES = 32
_MAX_MARKDOWN_HEADINGS = 10_000
_MAX_RECORDS = 10_000
_MAX_CHILD_ROWS = 10_000
_MAX_CHILD_FIELDS = 16
_SYSTEM_FIELDS = frozenset(
    {"collection_id", "record_id", "item_version", "inferred", "ambiguous", "parent_record_id"}
)


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
    source_versions: tuple[collections.SourceVersion, ...] = ()


@dataclass(frozen=True, slots=True)
class _HeadingField:
    name: str
    type: str
    format: str | None


@dataclass(frozen=True, slots=True)
class _HeadingNote:
    field: str
    open: str
    close: str


@dataclass(frozen=True, slots=True)
class _HeadingGrammar:
    level: int
    fields: tuple[_HeadingField, ...]
    separator: str
    note: _HeadingNote | None


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

    def _manifest_version(self) -> collections.SourceVersion:
        current = collections.source_version(self.vault_root / self.manifest.path)
        if current.hash != self.manifest.manifest_version.hash:
            raise collections.CollectionError(
                "STALE_COLLECTION_MANIFEST", "collection manifest changed after it was loaded"
            )
        return self.manifest.manifest_version

    def _validate_values(self, values: dict[str, Any]) -> None:
        if _SYSTEM_FIELDS.intersection(self.manifest.schema.fields):
            raise collections.CollectionError(
                "RESERVED_RECORD_FIELD", "schema uses a reserved record field"
            )
        self.manifest.schema.validate(values)

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
        manifest_version = self._manifest_version()
        source = self.source_path
        data = _read_bounded(source, _MAX_LOG_BYTES, "markdown log")
        descriptor = self.manifest.storage.descriptor
        section = descriptor.get("section")
        item_heading = descriptor.get("item_heading")
        if not isinstance(section, Mapping) or not isinstance(item_heading, Mapping):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "markdown log needs section and item heading grammar"
            )
        section_level, section_title = section.get("level"), section.get("title")
        if (
            type(section_level) is not int
            or not 1 <= section_level <= 6
            or not _bounded_literal(section_title)
        ):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "markdown log grammar is invalid"
            )
        grammar = _heading_grammar(item_heading)
        _status_values(descriptor, None)
        insertion = descriptor.get("insertion")
        if insertion not in {"newest-first", "oldest-first"}:
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "markdown log needs an insertion direction"
            )
        prefix, delimiter, child_fields = _child_grammar(descriptor.get("child_rows"))

        headings = _headings_outside_fences(data)
        section_heading = next(
            (
                item
                for item in headings
                if item.level == section_level and item.title == section_title
            ),
            None,
        )
        if section_heading is None:
            raise collections.CollectionError(
                "COLLECTION_SECTION_NOT_FOUND", "declared markdown section was not found"
            )
        section_end = next(
            (
                item.start
                for item in headings
                if item.start > section_heading.start and item.level <= section_level
            ),
            len(data),
        )
        entries: list[_Heading] = []
        for item in headings:
            if (
                section_heading.end <= item.start < section_end
                and item.level == grammar.level
                and _log_values(item.title, grammar) is not None
            ):
                if len(entries) >= _MAX_RECORDS:
                    raise collections.CollectionError(
                        "RECORD_LIMIT", "markdown log has too many parsed records"
                    )
                entries.append(item)
        records: list[Record] = []
        child_count = 0
        for index, item in enumerate(entries):
            end = entries[index + 1].start if index + 1 < len(entries) else section_end
            block = data[item.start : end]
            values = _log_values(item.title, grammar)
            if values is None:
                raise collections.CollectionError(
                    "INVALID_RECORD_HEADING", "record heading does not match its manifest grammar"
                )
            values.update(_status_values(descriptor, values.get("note")))
            children = _child_rows(
                block, item.start, prefix, delimiter, child_fields, _MAX_CHILD_ROWS - child_count
            )
            child_count += len(children)
            if children:
                values["movements"] = [child.values for child in children]
            self._validate_values(values)
            marker_match = _marker_outside_fences(block)
            marker = None
            if marker_match:
                marker = memory_refs.normalize_id(marker_match.group(1).decode("ascii"))
                if marker is None:
                    raise collections.CollectionError(
                        "INVALID_RECORD_ID", "markdown marker is not a UUID"
                    )
            identity = self._identity(values, marker)
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
        source_version = collections.SourceVersion(
            path=self.manifest.storage.source, hash=hashlib.sha256(data).hexdigest()
        )
        insertion_offset = (
            _first_content_offset(data, section_heading.end, section_end)
            if insertion == "newest-first"
            else section_end
        )
        return AdapterSnapshot(
            records=tuple(records),
            snapshot=_snapshot(
                [
                    (manifest_version.path, manifest_version.hash),
                    (source_version.path, source_version.hash),
                ]
            ),
            insertion_offset=insertion_offset,
            diagnostics=diagnostics,
            source_versions=(manifest_version, source_version),
        )


class MarkdownItemsAdapter(_BaseAdapter):
    def read(self) -> AdapterSnapshot:
        manifest_version = self._manifest_version()
        source = self.source_path
        if not source.is_dir():
            raise collections.CollectionError(
                "SOURCE_NOT_FOUND", "canonical item directory could not be read"
            )
        records: list[Record] = []
        versions: list[tuple[str, str]] = []
        total_bytes = 0
        paths = list(_bounded_paths(source.rglob("*.md"), _MAX_ITEM_FILES))
        if len(paths) > _MAX_ITEM_FILES:
            raise collections.CollectionError(
                "RECORD_ITEM_LIMIT", "collection has too many item files"
            )
        paths.sort()
        source_root = source.resolve()
        for path in paths:
            if path.is_symlink():
                raise collections.CollectionError(
                    "INVALID_RECORD_ITEM_PATH", "item files cannot be symlinks"
                )
            try:
                path.resolve().relative_to(source_root)
            except ValueError as error:
                raise collections.CollectionError(
                    "INVALID_RECORD_ITEM_PATH", "item file escapes collection"
                ) from error
            data = _read_bounded(path, _MAX_ITEM_BYTES, "item file")
            total_bytes += len(data)
            if total_bytes > _MAX_COLLECTION_BYTES:
                raise collections.CollectionError(
                    "RECORD_SOURCE_TOO_LARGE", "collection exceeds the byte limit"
                )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise collections.CollectionError(
                    "INVALID_RECORD_ITEM", "record item is not UTF-8"
                ) from error
            try:
                frontmatter, body, marker = vault.parse_frontmatter(text, strict=True)
            except vault.FrontmatterError as error:
                raise collections.CollectionError(error.code, error.reason) from error
            if marker is None or frontmatter.get("type") != "record":
                continue
            collection_id = memory_refs.normalize_id(str(frontmatter.get("collection_id", "")))
            record_id = memory_refs.normalize_id(str(frontmatter.get("record_id", "")))
            if collection_id != self.manifest.collection_id or record_id is None:
                raise collections.CollectionError(
                    "INVALID_RECORD_ITEM", "record item identity is invalid"
                )
            if frontmatter.get("schema_version") != self.manifest.schema.version:
                raise collections.CollectionError(
                    "UNSUPPORTED_ITEM_SCHEMA_VERSION", "item schema version differs"
                )
            values = {
                name: _json_value(value)
                for name, value in frontmatter.items()
                if name not in {"type", "collection_id", "record_id", "schema_version"}
            }
            self._validate_values(values)
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
        source_versions = (manifest_version,) + tuple(
            collections.SourceVersion(path=path, hash=digest) for path, digest in versions
        )
        return AdapterSnapshot(
            tuple(records),
            _snapshot([(version.path, version.hash) for version in source_versions]),
            diagnostics=diagnostics,
            source_versions=source_versions,
        )


class DatasetAdapter(_BaseAdapter):
    mutable = False

    def read(self) -> AdapterSnapshot:
        manifest_version = self._manifest_version()
        source = self.source_path
        record_path = self.manifest.storage.descriptor.get("record_path")
        if record_path is not None and (not isinstance(record_path, str) or not record_path):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "dataset record path must be a non-empty string"
            )
        try:
            source_bytes = query_data.read_dataset_bytes(source)
            _format, rows, _columns, _warnings = query_data.load_rows_bytes(
                source_bytes, source.suffix, record_path
            )
        except query_data.QueryDataError as error:
            raise collections.CollectionError(error.code, error.reason) from error
        digest = hashlib.sha256(source_bytes).hexdigest()
        key_name = self.manifest.storage.descriptor.get("key")
        records: list[Record] = []
        for index, row in enumerate(rows):
            values = _schema_values(
                {name: _json_value(value) for name, value in row.items()}, self.manifest
            )
            self._validate_values(values)
            if key_name is not None:
                key = str(values.get(str(key_name), ""))
                if not key:
                    raise collections.CollectionError(
                        "INVALID_DATASET_KEY", "declared dataset key is missing"
                    )
                identity = collections.ItemIdentity(self.manifest.collection_id, key)
            else:
                identity = self._identity(values, None)
            records.append(
                Record(
                    identity=identity,
                    values=values,
                    source=collections.SourceVersion(
                        path=self.manifest.storage.source, hash=digest
                    ),
                    span=SourceSpan(index, index + 1),
                )
            )
        records, diagnostics = _mark_ambiguous(records)
        source_version = collections.SourceVersion(path=self.manifest.storage.source, hash=digest)
        return AdapterSnapshot(
            tuple(records),
            _snapshot(
                [
                    (manifest_version.path, manifest_version.hash),
                    (source_version.path, source_version.hash),
                ]
            ),
            diagnostics=diagnostics,
            source_versions=(manifest_version, source_version),
        )


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
    query: Mapping[str, Any] = field(default_factory=dict)
    source_versions: tuple[collections.SourceVersion, ...] = ()


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
            raise collections.CollectionError(
                "INVALID_RECORD_CONTINUATION", "continuation does not match query"
            )
        if token.get("snapshot") != parsed.snapshot:
            raise collections.CollectionError("STALE_RECORD_SNAPSHOT", "canonical source changed")
        offset = token["offset"]
    rows = _query_rows(parsed.records, expand_children, include_item_version=adapter.mutable)
    effective_columns = columns
    if columns:
        identity_columns = ["collection_id", "record_id", "inferred", "ambiguous"]
        if adapter.mutable:
            identity_columns.append("item_version")
        effective_columns = list(dict.fromkeys([*columns, *identity_columns]))
    result = query_data.evaluate_rows(
        rows,
        path=manifest.storage.source,
        format=manifest.storage.strategy,
        filters=filters,
        columns=effective_columns,
        sort_by=sort_by,
        descending=descending,
        limit=limit,
        offset=offset,
        aggregate=aggregate,
        date_from=date_from,
        date_to=date_to,
        date_column=date_column,
    )
    if aggregate is None and result.truncated and result.returned == 0:
        raise collections.CollectionError(
            "RECORD_RESPONSE_TOO_LARGE", "first result row exceeds the response cap"
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
    rendered = _render_query(
        result,
        manifest.collection_id,
        parsed.snapshot,
        query,
        parsed.source_versions,
        output_format,
    )
    if len(rendered.encode("utf-8")) > query_data.MAX_RESPONSE_BYTES:
        raise collections.CollectionError(
            "RECORD_RESPONSE_TOO_LARGE", "rendered query exceeds the response cap"
        )
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
        query=query,
        source_versions=parsed.source_versions,
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
    fence: tuple[bytes, int] | None = None
    for line in data.splitlines(keepends=True):
        if fence is not None:
            if _closes_fence(line, fence):
                fence = None
        elif (opened := _opens_fence(line)) is not None:
            fence = opened
        elif match := _HEADING.match(line):
            try:
                title = match.group(2).decode("utf-8").strip()
            except UnicodeDecodeError as error:
                raise collections.CollectionError(
                    "INVALID_RECORD_SOURCE", "markdown log is not UTF-8"
                ) from error
            if len(headings) >= _MAX_MARKDOWN_HEADINGS:
                raise collections.CollectionError(
                    "RECORD_HEADING_LIMIT", "markdown log has too many headings"
                )
            headings.append(
                _Heading(
                    level=len(match.group(1)),
                    title=title,
                    start=offset,
                    end=offset + len(line),
                )
            )
        offset += len(line)
    return headings


def _opens_fence(line: bytes) -> tuple[bytes, int] | None:
    match = _FENCE_OPEN.match(line)
    if match is None:
        return None
    marker = match.group(1)
    if marker.startswith(b"`") and b"`" in line[match.end(1) :]:
        return None
    return marker[:1], len(marker)


def _closes_fence(line: bytes, fence: tuple[bytes, int]) -> bool:
    marker, minimum = fence
    if len(line) - len(line.lstrip(b" ")) > 3:
        return False
    stripped = line.lstrip(b" ")
    count = 0
    while count < len(stripped) and stripped[count : count + 1] == marker:
        count += 1
    return count >= minimum and stripped[count:].strip() == b""


def _heading_grammar(value: Mapping[str, Any]) -> _HeadingGrammar:
    if set(value) != {"level", "fields", "separator", "note"} and set(value) != {
        "level",
        "fields",
        "separator",
    }:
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "item heading grammar has unknown keys"
        )
    level = value.get("level")
    fields_value = value.get("fields")
    separator = value.get("separator")
    if (
        type(level) is not int
        or not 1 <= level <= 6
        or not isinstance(fields_value, Sequence)
        or isinstance(fields_value, (str, bytes))
    ):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "item heading grammar is invalid"
        )
    if (
        not 1 <= len(fields_value) <= _MAX_HEADING_FIELDS
        or not isinstance(separator, str)
        or not _bounded_literal(separator)
    ):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "item heading grammar is invalid"
        )
    fields: list[_HeadingField] = []
    names: set[str] = set()
    for item in fields_value:
        if not isinstance(item, Mapping) or set(item) - {"name", "type", "format"}:
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "item heading field is invalid"
            )
        name, type_name, format_value = item.get("name"), item.get("type"), item.get("format")
        if (
            not isinstance(name, str)
            or not _bounded_literal(name)
            or name in names
            or type_name not in {"string", "date", "datetime"}
        ):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "item heading field is invalid"
            )
        if type_name == "string":
            if format_value is not None:
                raise collections.CollectionError(
                    "INVALID_STORAGE_DESCRIPTOR", "string heading field cannot have a format"
                )
        elif not isinstance(format_value, str) or not _bounded_literal(format_value):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "dated heading field needs a format"
            )
        names.add(name)
        fields.append(_HeadingField(name, type_name, format_value))
    note_value = value.get("note")
    note: _HeadingNote | None = None
    if note_value is not None:
        if not isinstance(note_value, Mapping) or set(note_value) != {"field", "open", "close"}:
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "heading note grammar is invalid"
            )
        field_name, opening, closing = (
            note_value.get("field"),
            note_value.get("open"),
            note_value.get("close"),
        )
        if (
            not isinstance(field_name, str)
            or not _bounded_literal(field_name)
            or field_name in names
            or not isinstance(opening, str)
            or not _bounded_literal(opening)
            or not isinstance(closing, str)
            or not _bounded_literal(closing)
        ):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "heading note grammar is invalid"
            )
        note = _HeadingNote(field_name, opening, closing)
    return _HeadingGrammar(level, tuple(fields), separator, note)


def _bounded_literal(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= _MAX_GRAMMAR_LITERAL_BYTES


def _log_values(title: str, grammar: _HeadingGrammar) -> dict[str, Any] | None:
    core = title
    values: dict[str, Any] = {}
    if grammar.note is not None:
        note = grammar.note
        if core.endswith(note.close):
            opening = core.rfind(note.open, 0, len(core) - len(note.close))
            if opening < 0:
                return None
            note_value = core[opening + len(note.open) : -len(note.close)].strip()
            if not note_value:
                return None
            values[note.field] = note_value
            core = core[:opening]
        else:
            values[note.field] = None
    parts = [part.strip() for part in core.split(grammar.separator)]
    if len(parts) != len(grammar.fields) or any(not part for part in parts):
        return None
    for heading_field, part in zip(grammar.fields, parts, strict=True):
        if heading_field.type == "string":
            values[heading_field.name] = part
            continue
        try:
            parsed = dt.datetime.strptime(part, heading_field.format or "")
        except ValueError:
            return None
        values[heading_field.name] = (
            parsed.date().isoformat() if heading_field.type == "date" else parsed.isoformat()
        )
    return values


def _status_values(descriptor: Mapping[str, Any], note: object) -> dict[str, Any]:
    defaults = descriptor.get("defaults", {})
    rules = descriptor.get("note_rules", [])
    if (
        not isinstance(defaults, Mapping)
        or not isinstance(rules, Sequence)
        or isinstance(rules, (str, bytes))
    ):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "record status rules are invalid"
        )
    if len(defaults) > _MAX_HEADING_FIELDS or len(rules) > _MAX_NOTE_RULES:
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "record status rules exceed the limit"
        )
    values = dict(defaults)
    for rule in rules:
        if not isinstance(rule, Mapping) or set(rule) != {"equals", "values"}:
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "record status rule is invalid"
            )
        expected, replacements = rule.get("equals"), rule.get("values")
        if (
            not _bounded_literal(expected)
            or not isinstance(replacements, Mapping)
            or len(replacements) > _MAX_HEADING_FIELDS
        ):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "record status rule is invalid"
            )
        for name in replacements:
            if not isinstance(name, str) or not _bounded_literal(name) or name in _SYSTEM_FIELDS:
                raise collections.CollectionError(
                    "INVALID_STORAGE_DESCRIPTOR", "record status rule is invalid"
                )
        if note == expected:
            values.update(replacements)
    return values


def _child_grammar(value: object) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or set(value) != {"prefix", "delimiter", "fields"}:
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "child row grammar is invalid"
        )
    prefix, delimiter, raw_fields = value.get("prefix"), value.get("delimiter"), value.get("fields")
    if (
        not isinstance(prefix, str)
        or not _bounded_inline_literal(prefix)
        or not isinstance(delimiter, str)
        or not _bounded_inline_literal(delimiter)
    ):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "child row grammar is invalid"
        )
    if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "child row fields are invalid"
        )
    if not 1 <= len(raw_fields) <= _MAX_CHILD_FIELDS:
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "child row fields are invalid"
        )
    fields: list[str] = []
    for name in raw_fields:
        if (
            not isinstance(name, str)
            or not _bounded_inline_literal(name)
            or name in fields
            or name in _SYSTEM_FIELDS
        ):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "child row fields are invalid"
            )
        fields.append(name)
    return prefix, delimiter, tuple(fields)


def _bounded_inline_literal(value: object) -> bool:
    return (
        isinstance(value, str)
        and _bounded_literal(value)
        and "\r" not in value
        and "\n" not in value
    )


def _child_rows(
    block: bytes,
    block_start: int,
    prefix: str,
    delimiter: str,
    fields: tuple[str, ...],
    remaining: int,
) -> tuple[ChildRow, ...]:
    rows: list[ChildRow] = []
    offset = block_start
    fence: tuple[bytes, int] | None = None
    for raw in block.splitlines(keepends=True):
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise collections.CollectionError(
                "INVALID_RECORD_SOURCE", "markdown log is not UTF-8"
            ) from error
        if fence is not None:
            if _closes_fence(raw, fence):
                fence = None
        elif (opened := _opens_fence(raw)) is not None:
            fence = opened
        elif line.startswith(prefix):
            values = [part.strip() for part in line[len(prefix) :].split(delimiter)]
            if len(values) == len(fields):
                if len(rows) >= remaining:
                    raise collections.CollectionError(
                        "RECORD_CHILD_ROW_LIMIT", "markdown log has too many child rows"
                    )
                rows.append(
                    ChildRow(
                        dict(zip(fields, values, strict=True)),
                        SourceSpan(offset, offset + len(raw)),
                    )
                )
        offset += len(raw)
    return tuple(rows)


def _marker_outside_fences(block: bytes) -> re.Match[bytes] | None:
    fence: tuple[bytes, int] | None = None
    marker: re.Match[bytes] | None = None
    seen_content = False
    for raw in block.splitlines(keepends=True)[1:]:
        if fence is not None:
            if _closes_fence(raw, fence):
                fence = None
            continue
        if (opened := _opens_fence(raw)) is not None:
            seen_content = True
            fence = opened
            continue
        if not raw.strip():
            continue
        match = _MARKER.fullmatch(raw.strip())
        if match is not None and not seen_content and marker is None:
            marker = match
            seen_content = True
            continue
        if match is not None:
            raise collections.CollectionError(
                "INVALID_RECORD_MARKER", "record marker must be unique and first"
            )
        seen_content = True
    return marker


def _first_content_offset(data: bytes, start: int, end: int) -> int:
    match = re.search(rb"[^\r\n]", data[start:end])
    return start + match.start() if match else end


def _bounded_paths(paths: Iterable[Path], limit: int) -> Iterable[Path]:
    for path in paths:
        yield path
        limit -= 1
        if limit < 0:
            return


def _read_bounded(path: Path, limit: int, kind: str) -> bytes:
    try:
        if path.stat().st_size > limit:
            raise collections.CollectionError(
                "RECORD_SOURCE_TOO_LARGE", f"{kind} exceeds the byte limit"
            )
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except collections.CollectionError:
        raise
    except OSError as error:
        raise collections.CollectionError(
            "SOURCE_NOT_FOUND", "canonical source could not be read"
        ) from error
    if len(data) > limit:
        raise collections.CollectionError(
            "RECORD_SOURCE_TOO_LARGE", f"{kind} exceeds the byte limit"
        )
    return data


def _mark_ambiguous(
    records: list[Record],
) -> tuple[list[Record], tuple[collections.CollectionDiagnostic, ...]]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.identity.key] = counts.get(record.identity.key, 0) + 1
    ambiguous = {key for key, count in counts.items() if count > 1}
    diagnostics = tuple(
        collections.CollectionDiagnostic(
            "AMBIGUOUS_RECORD_KEY"
            if any(record.identity.inferred and record.identity.key == key for record in records)
            else "DUPLICATE_RECORD_ID",
            "legacy natural key is not unique"
            if any(record.identity.inferred and record.identity.key == key for record in records)
            else "explicit record key is duplicated",
        )
        for key in sorted(ambiguous)
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


def _schema_values(
    values: dict[str, Any], manifest: collections.CollectionManifest
) -> dict[str, Any]:
    for name, spec in manifest.schema.fields.items():
        value = values.get(name)
        if isinstance(value, str) and spec.type == "integer" and value:
            try:
                values[name] = int(value)
            except (ValueError, OverflowError) as error:
                raise collections.CollectionError(
                    "INVALID_DATASET_FIELD", f"invalid integer dataset field: {name}"
                ) from error
        elif isinstance(value, str) and spec.type == "number" and value:
            try:
                parsed = float(value)
            except (ValueError, OverflowError) as error:
                raise collections.CollectionError(
                    "INVALID_DATASET_FIELD", f"invalid number dataset field: {name}"
                ) from error
            if not math.isfinite(parsed):
                raise collections.CollectionError(
                    "INVALID_DATASET_FIELD", f"invalid number dataset field: {name}"
                )
            values[name] = parsed
    return values


def _query_rows(
    records: tuple[Record, ...], expand_children: bool, *, include_item_version: bool
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        system = {
            "collection_id": record.identity.collection_id,
            "record_id": record.identity.key,
            "inferred": record.identity.inferred,
            "ambiguous": record.ambiguous,
        }
        if include_item_version:
            system["item_version"] = record.source.hash
        base = {**record.values, **system}
        if expand_children:
            for child in record.children:
                rows.append(
                    {**base, **child.values, "parent_record_id": record.identity.key, **system}
                )
        else:
            rows.append(base)
    return rows


def _encode_continuation(payload: dict[str, Any]) -> str:
    """Encode an integrity checksum, not an authority token.

    Each request reloads the canonical snapshot and re-evaluates governance, so
    a cursor cannot grant access or bypass current policy.
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    if len(raw) > _MAX_TOKEN_PAYLOAD_BYTES:
        raise collections.CollectionError(
            "INVALID_RECORD_CONTINUATION", "query continuation is too large"
        )
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(_CURSOR_DOMAIN + raw).hexdigest()
    token = f"{_CURSOR_VERSION}.{encoded}.{checksum}"
    if len(token.encode("utf-8")) > _MAX_TOKEN_ENVELOPE_BYTES:
        raise collections.CollectionError(
            "INVALID_RECORD_CONTINUATION", "query continuation is too large"
        )
    return token


def _decode_continuation(value: str) -> dict[str, Any]:
    if len(value.encode("utf-8")) > _MAX_TOKEN_ENVELOPE_BYTES or value.count(".") != 2:
        raise collections.CollectionError("INVALID_RECORD_CONTINUATION", "continuation is invalid")
    try:
        version, encoded, checksum = value.split(".", 2)
        if version != _CURSOR_VERSION or len(checksum) != 64:
            raise ValueError
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded)
        if len(raw) > _MAX_TOKEN_PAYLOAD_BYTES:
            raise ValueError
        expected = hashlib.sha256(_CURSOR_DOMAIN + raw).hexdigest()
        if checksum != expected:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise collections.CollectionError(
            "INVALID_RECORD_CONTINUATION", "continuation is invalid"
        ) from error
    if (
        not isinstance(payload, dict)
        or type(payload.get("offset")) is not int
        or payload["offset"] < 0
    ):
        raise collections.CollectionError("INVALID_RECORD_CONTINUATION", "continuation is invalid")
    return payload


def _render_query(
    result: query_data.QueryDataResult,
    collection_id: str,
    snapshot: str,
    query: Mapping[str, Any],
    source_versions: tuple[collections.SourceVersion, ...],
    output_format: str,
) -> str:
    generated_at = dt.datetime.now(tz=dt.UTC).isoformat().replace("+00:00", "Z")
    query_json = json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    source_hashes = {version.path: version.hash for version in source_versions}
    if output_format == "json":
        return json.dumps(
            {
                "collection_id": collection_id,
                "query": query,
                "snapshot": snapshot,
                "source_hashes": source_hashes,
                "generated_at": generated_at,
                "derived": True,
                "rows": result.rows,
                "aggregate": result.aggregate,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if output_format == "markdown":
        lines = [
            "---",
            f"collection_id: {collection_id}",
            f"snapshot: {snapshot}",
            "source_hashes: " + json.dumps(source_hashes, ensure_ascii=False, sort_keys=True),
            f"query: {query_json}",
            f"generated_at: {generated_at}",
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
        if result.aggregate is not None:
            lines.extend(
                [
                    "",
                    "## Aggregate",
                    "",
                    "```json",
                    json.dumps(result.aggregate, ensure_ascii=False),
                    "```",
                ]
            )
        return "\n".join(lines) + "\n"
    if output_format == "csv":
        output: list[str] = [
            f"# collection_id: {collection_id}\n",
            f"# snapshot: {snapshot}\n",
            "# source_hashes: "
            + json.dumps(source_hashes, ensure_ascii=False, sort_keys=True)
            + "\n",
            f"# query: {query_json}\n",
            f"# generated_at: {generated_at}\n",
            "# derived: true\n",
        ]

        class _Writer:
            def write(self, value: str) -> int:
                output.append(value)
                return len(value)

        if result.aggregate is not None:
            output.append("aggregate\n")
            output.append(json.dumps(result.aggregate, ensure_ascii=False) + "\n")
        else:
            writer = csv.DictWriter(_Writer(), fieldnames=result.columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(result.rows)
        return "".join(output)
    raise collections.CollectionError(
        "INVALID_QUERY_OUTPUT", "output format must be json, markdown, or csv"
    )
