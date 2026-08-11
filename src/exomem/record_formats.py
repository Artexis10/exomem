"""Canonical collection adapters and bounded, read-only record queries."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import json
import math
import re
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from . import memory_refs, query_data, vault
from . import structured_collections as collections
from .collection_profiles import profile_for

_MARKER = re.compile(rb"<!--\s*exomem-record-id:\s*([0-9a-fA-F-]{36})\s*-->")
_HEADING = re.compile(rb"^(#{1,6})\s+(.+?)\s*(?:\r?\n|$)")
_FENCE_OPEN = re.compile(rb"^ {0,3}((?:`{3,}|~{3,}))[^\r\n]*(?:\r?\n|$)")
_MAX_LOG_BYTES = 2 * 1024 * 1024
_MAX_ITEM_FILES = 2_000
_MAX_RAW_ITEM_ENTRIES = 10_000
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


def _profile_system_fields(manifest: collections.CollectionManifest) -> frozenset[str]:
    profile = profile_for(manifest.semantic_profile)
    return frozenset(
        {"collection_id", profile.item_id_property, "item_version", "inferred", "ambiguous", "parent_record_id"}
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
    # Canonical data only.  This deliberately omits the manifest so a saved
    # view can pin source state without invalidating itself on its own edit.
    data_snapshot: str
    insertion_offset: int | None = None
    diagnostics: tuple[collections.CollectionDiagnostic, ...] = ()
    source_versions: tuple[collections.SourceVersion, ...] = ()
    # Markdown-item collections also bind directory-only paths: an empty nested
    # directory is canonical state even though it has no file version.
    source_inventory: tuple[tuple[str, str, str], ...] = ()
    path_guards: tuple[vault.PathGuard, ...] = ()
    directory_guards: tuple[vault.DirectoryCensusGuard, ...] = ()
    source_bytes: tuple[tuple[str, bytes], ...] = ()


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
    authorize_path: Callable[[str], bool] | None = None
    project_values: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None
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
            refreshed = collections.load_manifest(
                self.vault_root, self.vault_root / self.manifest.path
            )
            if (
                replace(
                    self.manifest,
                    manifest_version=refreshed.manifest_version,
                    audit_head=refreshed.audit_head,
                )
                == refreshed
            ):
                return current
            raise collections.CollectionError(
                "STALE_COLLECTION_MANIFEST", "collection manifest changed after it was loaded"
            )
        return self.manifest.manifest_version

    def _require_authorized(self, relative: str) -> bool:
        """Authorize before a canonical path can affect any public state."""
        if self.authorize_path is None or self.authorize_path(relative):
            return True
        return False

    def _validate_values(
        self, values: dict[str, Any], *, allowed_fields: Iterable[str] = ()
    ) -> None:
        if _profile_system_fields(self.manifest).intersection(self.manifest.schema.fields):
            raise collections.CollectionError(
                "RESERVED_RECORD_FIELD", "schema uses a reserved record field"
            )
        self.manifest.schema.validate(values, allowed_fields=allowed_fields)

    def _project_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return dict(values) if self.project_values is None else self.project_values(values)

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
        if not self._require_authorized(self.manifest.storage.source):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        try:
            data, guard = vault.read_bounded_guarded_bytes(
                self.vault_root,
                source.relative_to(self.vault_root).as_posix(),
                limit=_MAX_LOG_BYTES,
            )
        except vault.PathGuardError as error:
            raise collections.CollectionError(
                "SOURCE_NOT_FOUND", "canonical source could not be read"
            ) from error
        return replace(
            self.read_bytes(data, manifest_version=manifest_version), path_guards=(guard,)
        )

    def read_bytes(
        self, data: bytes, *, manifest_version: collections.SourceVersion | None = None
    ) -> AdapterSnapshot:
        """Parse caller-held canonical bytes without reopening the log source."""
        if len(data) > _MAX_LOG_BYTES:
            raise collections.CollectionError(
                "RECORD_SOURCE_TOO_LARGE", "markdown log exceeds the byte limit"
            )
        manifest_version = manifest_version or self.manifest.manifest_version
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
        prefix, delimiter, child_fields, child_container = _child_grammar(
            descriptor.get("child_rows"), self.manifest.schema
        )

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
                values[child_container] = [child.values for child in children]
            if grammar.note is not None:
                note = values[grammar.note.field]
                if note is not None and type(note) is not str:
                    raise collections.CollectionError("SCHEMA_FIELD_TYPE", "heading note must be a string")
                self._validate_values(values, allowed_fields=(grammar.note.field,))
            else:
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
            values = self._project_values(values)
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
            data_snapshot=_snapshot([(source_version.path, source_version.hash)]),
            insertion_offset=insertion_offset,
            diagnostics=diagnostics,
            source_versions=(manifest_version, source_version),
            source_bytes=((self.manifest.storage.source, data),),
        )


class MarkdownItemsAdapter(_BaseAdapter):
    def read(self) -> AdapterSnapshot:
        manifest_version = self._manifest_version()
        source = self.source_path
        source_relative = source.relative_to(self.vault_root).as_posix()
        if not self._require_authorized(source_relative):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        try:
            root_guard = vault.DirectoryCensusGuard.capture(
                self.vault_root, source_relative, max_entries=_MAX_RAW_ITEM_ENTRIES
            )
        except vault.PathGuardError as error:
            if error.code == "PATH_GUARD_LIMIT":
                raise collections.CollectionError(
                    "RECORD_ITEM_LIMIT", "collection has too many item entries"
                ) from error
            raise collections.CollectionError(
                "SOURCE_NOT_FOUND", "canonical item directory could not be read"
            ) from error
        if root_guard.directory_identity is None:
            raise collections.CollectionError(
                "SOURCE_NOT_FOUND", "canonical item directory could not be read"
            )
        records: list[Record] = []
        versions: list[tuple[str, str]] = []
        inventory: list[tuple[str, str, str]] = [
            (source.relative_to(self.vault_root).as_posix(), "directory", "")
        ]
        total_bytes = 0
        paths: list[str] = []
        directory_guards_list: list[vault.DirectoryCensusGuard] = [root_guard]
        path_guards: list[vault.PathGuard] = []
        source_bytes: list[tuple[str, bytes]] = []
        pending = [root_guard]
        candidates = 0
        public_candidates = 0
        while pending:
            directory_guard = pending.pop()
            for entry in directory_guard.entries:
                candidates += 1
                if candidates > _MAX_RAW_ITEM_ENTRIES:
                    raise collections.CollectionError(
                        "RECORD_ITEM_LIMIT", "collection has too many item entries"
                    )
                if stat.S_ISDIR(entry.mode):
                    authorized = self._require_authorized(entry.relative_path)
                    if authorized:
                        public_candidates += 1
                        if public_candidates > _MAX_ITEM_FILES:
                            raise collections.CollectionError(
                                "RECORD_ITEM_LIMIT", "collection has too many item files"
                            )
                        inventory.append((entry.relative_path, "directory", ""))
                    try:
                        child_guard = vault.DirectoryCensusGuard.capture(
                            self.vault_root, entry.relative_path, max_entries=_MAX_RAW_ITEM_ENTRIES
                        )
                    except vault.PathGuardError as error:
                        raise collections.CollectionError(
                            "INVALID_RECORD_ITEM_PATH", "item inventory changed while it was read"
                        ) from error
                    if authorized:
                        directory_guards_list.append(child_guard)
                    pending.append(child_guard)
                elif self._require_authorized(entry.relative_path):
                    public_candidates += 1
                    if public_candidates > _MAX_ITEM_FILES:
                        raise collections.CollectionError(
                            "RECORD_ITEM_LIMIT", "collection has too many item files"
                        )
                    paths.append(entry.relative_path)
        paths.sort()
        for relative in paths:
            # The raw walk ceiling above is deliberately separate from public
            # file/byte limits. Never open an unreleased candidate merely to
            # discover whether it is malformed or large.
            if not self._require_authorized(relative):
                continue
            try:
                data, file_guard = vault.read_bounded_guarded_bytes(
                    self.vault_root,
                    relative,
                    limit=_MAX_ITEM_BYTES,
                )
            except vault.PathGuardError as error:
                raise collections.CollectionError(
                    "SOURCE_NOT_FOUND", "canonical item file could not be read"
                ) from error
            rel = relative
            digest = hashlib.sha256(data).hexdigest()
            path_guards.append(file_guard)
            total_bytes += len(data)
            if total_bytes > _MAX_COLLECTION_BYTES:
                raise collections.CollectionError(
                    "RECORD_SOURCE_TOO_LARGE", "collection exceeds the byte limit"
                )
            versions.append((rel, digest))
            source_bytes.append((rel, data))
            inventory.append((rel, "file", digest))
            if Path(rel).suffix != ".md":
                continue
            text = _decode_item_bytes(data)
            try:
                frontmatter, body, marker = vault.parse_frontmatter(text, strict=True)
            except vault.FrontmatterError as error:
                raise collections.CollectionError(error.code, error.reason) from error
            profile = profile_for(self.manifest.semantic_profile)
            if marker is None or frontmatter.get("type") != profile.item_type:
                continue
            collection_id = memory_refs.normalize_id(str(frontmatter.get("collection_id", "")))
            item_id = memory_refs.normalize_id(str(frontmatter.get(profile.item_id_property, "")))
            if collection_id != self.manifest.collection_id or item_id is None:
                raise collections.CollectionError(
                    "INVALID_RECORD_ITEM" if profile.name == "records" else "INVALID_PLAN",
                    "record item identity is invalid" if profile.name == "records" else "plan item identity is invalid",
                )
            if frontmatter.get("schema_version") != self.manifest.schema.version:
                raise collections.CollectionError(
                    "UNSUPPORTED_ITEM_SCHEMA_VERSION", "item schema version differs"
                )
            values = {
                name: _json_value(value)
                for name, value in frontmatter.items()
                if name not in {"type", "collection_id", profile.item_id_property, "schema_version"}
            }
            self._validate_values(values)
            identity = collections.ItemIdentity(collection_id, item_id)
            values = self._project_values(values)
            records.append(
                Record(
                    identity=identity,
                    values=values,
                    source=collections.SourceVersion(path=rel, hash=digest),
                    span=SourceSpan(0, len(data)),
                    body=body,
                )
            )
        known = {path for path, _kind, _digest in inventory}
        directory_guards = tuple(directory_guards_list)
        if self.authorize_path is None and any(
            entry.relative_path not in known
            for directory_guard in directory_guards
            for entry in directory_guard.entries
        ):
            raise collections.CollectionError(
                "INVALID_RECORD_ITEM_PATH", "item inventory changed while it was read"
            )
        try:
            for path_guard in path_guards:
                path_guard.recheck(self.vault_root)
            for directory_guard in directory_guards:
                directory_guard.recheck(self.vault_root)
        except vault.PathGuardError as error:
            raise collections.CollectionError(
                "INVALID_RECORD_ITEM_PATH", "item inventory changed while it was read"
            ) from error
        records, diagnostics = _mark_ambiguous(records)
        source_versions = (manifest_version,) + tuple(
            collections.SourceVersion(path=path, hash=digest) for path, digest in versions
        )
        inventory.sort()
        return AdapterSnapshot(
            tuple(records),
            _snapshot(
                [(manifest_version.path, manifest_version.hash)]
                + [(f"{kind}:{path}", digest) for path, kind, digest in inventory]
            ),
            _snapshot([(f"{kind}:{path}", digest) for path, kind, digest in inventory]),
            diagnostics=diagnostics,
            source_versions=source_versions,
            source_inventory=tuple(inventory),
            path_guards=tuple(path_guards),
            directory_guards=directory_guards,
            source_bytes=tuple(source_bytes),
        )


class DatasetAdapter(_BaseAdapter):
    mutable = False

    def read(self) -> AdapterSnapshot:
        manifest_version = self._manifest_version()
        source = self.source_path
        if not self._require_authorized(self.manifest.storage.source):
            raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")
        record_path = self.manifest.storage.descriptor.get("record_path")
        if record_path is not None and (not isinstance(record_path, str) or not record_path):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR", "dataset record path must be a non-empty string"
            )
        try:
            source_bytes, source_guard = vault.read_bounded_guarded_bytes(
                self.vault_root,
                self.manifest.storage.source,
                limit=_MAX_COLLECTION_BYTES,
            )
            _format, rows, _columns, _warnings = query_data.load_rows_bytes(
                source_bytes, source.suffix, record_path
            )
        except (query_data.QueryDataError, vault.PathGuardError) as error:
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
            values = self._project_values(values)
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
            _snapshot([(source_version.path, source_version.hash)]),
            diagnostics=diagnostics,
            source_versions=(manifest_version, source_version),
            path_guards=(source_guard,),
            source_bytes=((self.manifest.storage.source, source_bytes),),
        )


def load_adapter(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    authorize_path: Callable[[str], bool] | None = None,
    project_values: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> CollectionAdapter:
    """Return the declared canonical adapter without inferring domain grammar."""
    if manifest.storage.strategy == "markdown-log":
        return MarkdownLogAdapter(Path(vault_root), manifest, authorize_path, project_values)
    if manifest.storage.strategy == "markdown-items":
        return MarkdownItemsAdapter(Path(vault_root), manifest, authorize_path, project_values)
    if manifest.storage.strategy == "dataset":
        return DatasetAdapter(Path(vault_root), manifest, authorize_path, project_values)
    raise collections.CollectionError("UNSUPPORTED_STORAGE", "collection storage is unsupported")


def validate_storage_contract(manifest: collections.CollectionManifest) -> None:
    """Validate strategy-specific descriptor grammar without opening canonical data."""
    descriptor = manifest.storage.descriptor
    if manifest.storage.strategy == "markdown-items":
        return
    if manifest.storage.strategy == "dataset":
        record_path = descriptor.get("record_path")
        if record_path is not None and (not isinstance(record_path, str) or not record_path):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR",
                "dataset record path must be a non-empty string",
            )
        key = descriptor.get("key")
        if key is not None and (
            not isinstance(key, str) or not key or key not in manifest.schema.fields
        ):
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR",
                "dataset key must name a declared field",
            )
        return
    section = descriptor.get("section")
    item_heading = descriptor.get("item_heading")
    if not isinstance(section, Mapping) or not isinstance(item_heading, Mapping):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR",
            "markdown log needs section and item heading grammar",
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
    heading = _heading_grammar(item_heading)
    for heading_field in heading.fields:
        field = manifest.schema.fields.get(heading_field.name)
        if field is None or field.type != heading_field.type:
            raise collections.CollectionError(
                "INVALID_STORAGE_DESCRIPTOR",
                "markdown log heading fields must match declared item fields",
            )
    _status_values(descriptor, None)
    if descriptor.get("insertion") not in {"newest-first", "oldest-first"}:
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR",
            "markdown log needs an insertion direction",
        )
    _child_grammar(descriptor.get("child_rows"), manifest.schema)


def render_markdown_log_item(
    manifest: collections.CollectionManifest,
    values: Mapping[str, Any],
    item_key: str,
    newline: str,
    audit_correlation: str | None = None,
) -> str:
    """Render one declared log block without reading or rewriting its container."""
    if newline not in {"\n", "\r\n"}:
        raise collections.CollectionError("INVALID_RECORD_SOURCE", "unsupported newline sequence")
    descriptor = manifest.storage.descriptor
    heading = descriptor.get("item_heading")
    if not isinstance(heading, Mapping):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "item heading grammar is invalid"
        )
    grammar = _heading_grammar(heading)
    prefix, delimiter, fields, container = _child_grammar(
        descriptor.get("child_rows"), manifest.schema
    )
    parts: list[str] = []
    for heading_field in grammar.fields:
        value = values.get(heading_field.name)
        if heading_field.type == "string":
            rendered = value
        elif heading_field.type == "date":
            rendered = dt.date.fromisoformat(str(value)).strftime(heading_field.format or "")
        else:
            rendered = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
                heading_field.format or ""
            )
        if (
            not isinstance(rendered, str)
            or not rendered
            or "\r" in rendered
            or "\n" in rendered
            or grammar.separator in rendered
        ):
            raise collections.CollectionError(
                "UNREPRESENTABLE_RECORD_VALUE", "heading value cannot render"
            )
        parts.append(rendered)
    title = grammar.separator.join(parts)
    if grammar.note is not None:
        note = values.get(grammar.note.field)
        if note is not None:
            if (
                not isinstance(note, str)
                or not note.strip()
                or "\r" in note
                or "\n" in note
                or grammar.note.open in note
                or grammar.note.close in note
            ):
                raise collections.CollectionError(
                    "UNREPRESENTABLE_RECORD_VALUE", "note cannot render"
                )
            title += f"{grammar.note.open}{note.strip()}{grammar.note.close}"
    derived = _status_values(descriptor, values.get(grammar.note.field) if grammar.note else None)
    for name, value in derived.items():
        if values.get(name) != value:
            raise collections.CollectionError(
                "INVALID_RECORD_STATUS", "derived status is inconsistent"
            )
    rows = values.get(container, [])
    if not isinstance(rows, list):
        raise collections.CollectionError(
            "UNREPRESENTABLE_RECORD_VALUE", "child rows cannot render"
        )
    rendered_rows: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(fields):
            raise collections.CollectionError(
                "UNREPRESENTABLE_RECORD_VALUE", "child row cannot render"
            )
        cells = []
        for row_field in fields:
            value = row[row_field]
            if not isinstance(value, str) or any(
                token in value for token in ("\r", "\n", delimiter)
            ):
                raise collections.CollectionError(
                    "UNREPRESENTABLE_RECORD_VALUE", "child row cannot render"
                )
            cells.append(value)
        rendered_rows.append(prefix + f" {delimiter} ".join(cells))
    marker = collections.ItemIdentity(manifest.collection_id, item_key).key
    lines = [
        "#" * grammar.level + " " + title,
        f"<!-- exomem-record-id: {marker} -->",
        *(
            [f"<!-- exomem-record-audit: {audit_correlation} -->"]
            if audit_correlation is not None
            else []
        ),
        *rendered_rows,
        "",
    ]
    return newline.join(lines) + newline


def render_markdown_item(
    manifest: collections.CollectionManifest,
    values: Mapping[str, Any],
    item_key: str,
    body: str = "",
    audit_correlation: str | None = None,
) -> str:
    """Render a new ordinary record item from bounded structured values."""
    profile = profile_for(manifest.semantic_profile)
    frontmatter: dict[str, Any] = {
        "type": profile.item_type,
        "collection_id": manifest.collection_id,
        profile.item_id_property: collections.ItemIdentity(manifest.collection_id, item_key).key,
        "schema_version": manifest.schema.version,
    }
    frontmatter.update(values)
    audit_line = f"# {profile.item_audit_marker}: {audit_correlation}\n" if audit_correlation else ""
    text = "---\n" + vault.serialize_frontmatter(frontmatter) + "\n" + audit_line + "---\n"
    return text + ("\n" + body if body else "")


def render_markdown_item_update(
    source: str,
    changes: Mapping[str, Any],
    audit_correlation: str | None = None,
    *,
    semantic_profile: str = "records",
    delete_fields: tuple[str, ...] = (),
    body: str | None = None,
) -> str:
    """Replace complete top-level YAML nodes while retaining unrelated source bytes."""
    bom = "\ufeff" if source.startswith("\ufeff") else ""
    text = source[len(bom) :]
    newline = "\r\n" if "\r\n" in text else "\n"
    opening = re.match(r"\A---\r?\n", text)
    if opening is None:
        raise collections.CollectionError("INVALID_RECORD_ITEM", "item frontmatter is invalid")
    closing = re.search(r"(?m)^---\r?$", text[opening.end() :])
    if closing is None:
        raise collections.CollectionError("INVALID_RECORD_ITEM", "item frontmatter is invalid")
    close_start = opening.end() + closing.start()
    yaml_text = text[opening.end() : close_start]
    try:
        document = vault.yaml.compose(yaml_text)
    except vault.yaml.YAMLError as error:
        raise collections.CollectionError(
            "INVALID_RECORD_ITEM", "item frontmatter is invalid"
        ) from error
    if not isinstance(document, vault.yaml.nodes.MappingNode):
        raise collections.CollectionError("INVALID_RECORD_ITEM", "item frontmatter is invalid")
    spans: dict[str, tuple[int, int]] = {}
    for key_node, value_node in document.value:
        if not isinstance(key_node, vault.yaml.nodes.ScalarNode):
            raise collections.CollectionError("INVALID_RECORD_ITEM", "item frontmatter is invalid")
        if key_node.value in spans:
            raise collections.CollectionError("DUPLICATE_FRONTMATTER_KEY", "item key is duplicated")
        spans[key_node.value] = (key_node.start_mark.index, value_node.end_mark.index)
    replacements: list[tuple[int, int, str]] = []
    for name in delete_fields:
        span = spans.get(name)
        if span is None:
            continue
        end = yaml_text.find(newline, span[1])
        replacements.append((span[0], len(yaml_text) if end == -1 else end + len(newline), ""))
    for name, value in changes.items():
        span = spans.get(name)
        rendered = vault.serialize_frontmatter({name: value}).replace("\n", newline)
        if span is None:
            replacements.append((len(yaml_text), len(yaml_text), rendered + newline))
        else:
            replacements.append((*span, rendered))
    updated_yaml = yaml_text
    for start, end, rendered in sorted(replacements, reverse=True):
        updated_yaml = updated_yaml[:start] + rendered + updated_yaml[end:]
    profile = profile_for(semantic_profile)
    marker = re.escape(profile.item_audit_marker)
    updated_yaml = re.sub(rf"(?m)^# {marker}: [0-9a-f]{{24}}\r?\n?", "", updated_yaml)
    audit_line = (
        f"# {profile.item_audit_marker}: {audit_correlation}{newline}"
        if audit_correlation
        else ""
    )
    close_end = close_start + len(closing.group(0))
    suffix = text[close_end:] if body is None else newline + body
    return bom + text[: opening.end()] + updated_yaml + audit_line + text[close_start:close_end] + suffix


def render_manifest_audit_head(
    source: str, transition_id: str, *, semantic_profile: str = "records"
) -> str:
    """Replace only the ordinary manifest audit state while retaining all other bytes."""
    if not re.fullmatch(r"[0-9a-f]{24}", transition_id):
        raise collections.CollectionError("INVALID_RECORD_AUDIT", "audit transition is invalid")
    bom = "\ufeff" if source.startswith("\ufeff") else ""
    text = source[len(bom) :]
    opening = re.match(r"\A---\r?\n", text)
    if opening is None:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest requires frontmatter"
        )
    closing = re.search(r"(?m)^---\r?$", text[opening.end() :])
    if closing is None:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest requires frontmatter"
        )
    newline = "\r\n" if "\r\n" in text else "\n"
    start = opening.end()
    end = start + closing.start()
    frontmatter = text[start:end]
    profile = profile_for(semantic_profile)
    audit_node = collections._validate_audit_source(text, profile.manifest_audit_property)
    try:
        parsed, _body, marker = vault.parse_frontmatter(bom + text, strict=True)
        document = vault.yaml.compose(frontmatter)
    except (vault.FrontmatterError, vault.yaml.YAMLError) as error:
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest requires valid frontmatter"
        ) from error
    if marker is None or not isinstance(document, vault.yaml.nodes.MappingNode):
        raise collections.CollectionError(
            "INVALID_COLLECTION_MANIFEST", "manifest requires frontmatter"
        )
    for key_node, _value_node in document.value:
        if not isinstance(key_node, vault.yaml.nodes.ScalarNode):
            raise collections.CollectionError(
                "INVALID_COLLECTION_MANIFEST", "manifest keys are invalid"
            )
    collections._audit_head(parsed, profile.manifest_audit_property)
    if audit_node is None:
        frontmatter += (
            f"{profile.manifest_audit_property}: {{version: 1, head: {transition_id}}}{newline}"
        )
    else:
        heads = [
            value_node
            for key_node, value_node in audit_node.value
            if isinstance(key_node, vault.yaml.nodes.ScalarNode) and key_node.value == "head"
        ]
        if len(heads) != 1:
            raise collections.CollectionError(
                "INVALID_RECORD_AUDIT", "record audit head is invalid"
            )
        head = heads[0]
        frontmatter = (
            frontmatter[: head.start_mark.index]
            + transition_id
            + frontmatter[head.end_mark.index :]
        )
    return bom + text[:start] + frontmatter + text[end:]


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
    columns: tuple[str, ...] = ()
    view: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CollectionInspection:
    """Read-only adapter evidence, including bounded parse diagnostics."""

    collection_id: str
    snapshot: str | None
    source_versions: tuple[collections.SourceVersion, ...]
    source_hashes: Mapping[str, str]
    diagnostics: tuple[collections.CollectionDiagnostic, ...]
    record_count: int = 0


def inspect_collection(
    vault_root: Path,
    manifest: collections.CollectionManifest,
    *,
    authorize_path: Callable[[str], bool] | None = None,
    project_values: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> CollectionInspection:
    """Inspect one authorized canonical representation without repairing it.

    The adapter is deliberately read once.  On parse failure the manifest version
    remains reportable, while the typed diagnostic preserves the canonical error.
    """
    adapter = load_adapter(
        vault_root, manifest, authorize_path=authorize_path, project_values=project_values
    )
    try:
        parsed = adapter.read()
    except collections.CollectionError as error:
        versions = (manifest.manifest_version,)
        return CollectionInspection(
            collection_id=manifest.collection_id,
            snapshot=None,
            source_versions=versions,
            source_hashes={version.path: version.hash for version in versions},
            diagnostics=(collections.CollectionDiagnostic(error.code, error.reason),),
        )
    diagnostics = list(parsed.diagnostics[:64])
    for name in manifest.views:
        if len(diagnostics) >= 64:
            break
        try:
            view = collections.resolve_saved_view(manifest, name)
        except collections.CollectionError as error:
            diagnostics.append(collections.CollectionDiagnostic(error.code, error.reason))
            continue
        expected = view.definition.get("source_snapshot")
        if expected is not None and expected != parsed.data_snapshot:
            diagnostics.append(
                collections.CollectionDiagnostic(
                    "STALE_SAVED_VIEW", "saved view source snapshot no longer matches canonical data"
                )
            )
    return CollectionInspection(
        collection_id=manifest.collection_id,
        snapshot=parsed.snapshot,
        source_versions=parsed.source_versions,
        source_hashes={version.path: version.hash for version in parsed.source_versions},
        diagnostics=tuple(diagnostics),
        record_count=len(parsed.records),
    )


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
    view: str | None = None,
    authorize_path: Callable[[str], bool] | None = None,
    project_values: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    source_versions_limit: int | None = None,
    source_versions_for_rows: bool = False,
) -> RecordQueryResult:
    """Query a fresh canonical adapter snapshot with a snapshot-bound cursor."""
    view_provenance: dict[str, Any] | None = None
    if view is not None:
        if (
            filters is not None
            or columns is not None
            or sort_by is not None
            or descending
            or aggregate is not None
            or date_from is not None
            or date_to is not None
            or date_column is not None
            or expand_children
        ):
            raise collections.CollectionError(
                "INVALID_RECORD_QUERY", "saved views cannot be mixed with inline shaping"
            )
        saved_view = collections.resolve_saved_view(manifest, view)
        saved_query = saved_view.definition["query"]
        assert isinstance(saved_query, Mapping)
        filters = saved_query.get("filters")
        columns = saved_query.get("columns")
        sort_by = saved_query.get("sort_by")
        descending = saved_query.get("descending", False)
        aggregate = saved_query.get("aggregate")
        date_from = saved_query.get("date_from")
        date_to = saved_query.get("date_to")
        date_column = saved_query.get("date_column")
        expand_children = saved_query.get("expand_children", False)
        limit = saved_query.get("limit", limit)
        view_provenance = {
            "name": saved_view.name,
            "definition": saved_view.definition,
            "identity": saved_view.identity,
        }
    adapter = load_adapter(
        vault_root, manifest, authorize_path=authorize_path, project_values=project_values
    )
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
    cursor_query = query if view_provenance is None else {**query, "limit": limit, "view": view_provenance}
    offset = 0
    if continuation:
        token = _decode_continuation(continuation)
        if token.get("collection_id") != manifest.collection_id:
            raise collections.CollectionError(
                "INVALID_RECORD_CONTINUATION", "continuation does not match query"
            )
        if token.get("snapshot") != parsed.snapshot:
            raise collections.CollectionError("STALE_RECORD_SNAPSHOT", "canonical source changed")
        if token.get("query") != cursor_query:
            raise collections.CollectionError(
                "INVALID_RECORD_CONTINUATION", "continuation does not match query"
            )
        offset = token["offset"]
    profile = profile_for(manifest.semantic_profile)
    rows = _query_rows(
        parsed.records, expand_children, include_item_version=adapter.mutable, item_id_property=profile.item_id_property
    )
    effective_columns = columns
    if columns:
        identity_columns = ["collection_id", profile.item_id_property, "inferred", "ambiguous"]
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
                "query": cursor_query,
                "offset": next_offset,
            }
        )
    response = RecordQueryResult(
        collection_id=manifest.collection_id,
        snapshot=parsed.snapshot,
        rows=result.rows,
        returned=result.returned,
        total_matched=result.total_matched,
        truncated=result.truncated,
        continuation=next_token,
        derived=True,
        rendered="",
        aggregate=result.aggregate,
        query=query,
        source_versions=(
            tuple(
                version
                for version in parsed.source_versions
                if version.path == manifest.path
                or version.path
                in {
                    record.source.path
                    for record in parsed.records
                    if record.identity.key in {str(row.get(profile.item_id_property)) for row in result.rows}
                }
            )
            if source_versions_for_rows
            else (
                parsed.source_versions
                if source_versions_limit is None
                else parsed.source_versions[:source_versions_limit]
            )
        ),
        columns=tuple(result.columns),
        view=view_provenance,
    )
    rendered = render_query_result(response, output_format=output_format)
    if len(rendered.encode("utf-8")) > query_data.MAX_RESPONSE_BYTES:
        raise collections.CollectionError(
            "RECORD_RESPONSE_TOO_LARGE", "rendered query exceeds the response cap"
        )
    return replace(response, rendered=rendered)


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


def _child_grammar(
    value: object, schema: collections.ItemSchema
) -> tuple[str, str, tuple[str, ...], str]:
    if not isinstance(value, Mapping) or set(value) != {
        "prefix",
        "delimiter",
        "fields",
        "container_field",
    }:
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "child row grammar is invalid"
        )
    prefix, delimiter, raw_fields, container_field = (
        value.get("prefix"),
        value.get("delimiter"),
        value.get("fields"),
        value.get("container_field"),
    )
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
    container_spec = (
        schema.fields.get(container_field) if isinstance(container_field, str) else None
    )
    if (
        not isinstance(container_field, str)
        or not _bounded_inline_literal(container_field)
        or container_field in _SYSTEM_FIELDS
        or container_spec is None
        or container_spec.type != "array"
        or container_spec.items is None
        or container_spec.items.type != "object"
    ):
        raise collections.CollectionError(
            "INVALID_STORAGE_DESCRIPTOR", "child row container field is invalid"
        )
    return prefix, delimiter, tuple(fields), container_field


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


def _decode_item_bytes(data: bytes) -> str:
    bom = b"\xef\xbb\xbf"
    if data.startswith(bom + bom):
        raise collections.CollectionError(
            "INVALID_RECORD_ITEM", "record item has multiple UTF-8 BOMs"
        )
    try:
        return data[len(bom) :].decode("utf-8") if data.startswith(bom) else data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise collections.CollectionError(
            "INVALID_RECORD_ITEM", "record item is not UTF-8"
        ) from error


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
    records: tuple[Record, ...], expand_children: bool, *, include_item_version: bool, item_id_property: str = "record_id"
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        system = {
            "collection_id": record.identity.collection_id,
            item_id_property: record.identity.key,
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


def validate_continuation(value: str, *, collection_id: str, snapshot: str, query: Mapping[str, Any]) -> bool:
    """Validate a bounded v1 continuation without treating it as trusted output."""
    try:
        payload = _decode_continuation(value)
    except collections.CollectionError:
        return False
    return (
        payload.get("collection_id") == collection_id
        and payload.get("snapshot") == snapshot
        and payload.get("query") == query
        and type(payload.get("offset")) is int
        and payload["offset"] >= 0
    )


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


def render_query_result(result: RecordQueryResult, *, output_format: str) -> str:
    """Render a typed Records query result without trusting a prior rendering."""
    columns = list(result.columns)
    if not columns:
        columns = list(dict.fromkeys(key for row in result.rows for key in row))
    return _render_query(
        query_data.QueryDataResult(
            path="",
            format="",
            total_rows=result.total_matched,
            total_matched=result.total_matched,
            returned=result.returned,
            columns=columns,
            rows=result.rows,
            aggregate=result.aggregate,
            truncated=result.truncated,
        ),
        result.collection_id,
        result.snapshot,
        {**result.query, **({"view": result.view} if result.view is not None else {})},
        result.source_versions,
        output_format,
    )
