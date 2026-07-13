"""Deterministic parsing for compact and rich semantic units.

Markdown remains the source of truth.  This module only normalizes authored
syntax and composes the existing semantic-block parser; it performs no I/O,
registry mutation, indexing, or model work.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from . import (
    context_refs,
    markdown_relations,
    memory_refs,
    semantic_blocks,
    semantic_language_registry,
)
from .relation_registry import RelationRegistry
from .semantic_blocks import SemanticRelation

_COMPACT_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>[-*+])[ \t]+"
    r"\[(?P<label>[^\]\r\n]*)\](?P<tail>.*)$"
)
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_CATEGORY_SEPARATORS_RE = re.compile(r"[\s_-]+")
_ANCHOR_RE = re.compile(
    r"(?:^|[ \t])\^(?P<anchor>[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)$"
)
_TRAILING_TAG_RE = re.compile(r"(?:^|[ \t])#(?P<tag>[^\s#]+)$")
_RICH_CATEGORY_RE = re.compile(r"^\s*[-*+]\s+category\s*:", re.IGNORECASE)
_TASK_LABELS = frozenset({"", " ", "x", "X", "-"})
_IDENTITY_SCHEMA = "exomem.semantic-unit.identity.v1"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """An authored source slice with 1-based, end-exclusive coordinates."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    start_offset: int
    end_offset: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class SemanticUnitDiagnostic:
    """A stable, source-addressed parser finding."""

    code: str
    message: str
    path: str
    span: SourceSpan | None
    line: int | None
    raw: str
    remediation: str
    severity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "span": self.span.to_dict() if self.span is not None else None,
            "line": self.line,
            "raw": self.raw,
            "remediation": self.remediation,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class SemanticUnit:
    """One normalized compact observation or rich semantic block."""

    form: str
    kind: str
    kind_raw: str
    kind_key: str
    category_raw: str
    category_key: str
    category: str
    content: str
    span: SourceSpan
    source_hash: str
    tags: tuple[str, ...] = ()
    context: str | None = None
    relations: tuple[SemanticRelation, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    anchor: str | None = None
    title: str | None = None
    level: int | None = None
    body: str | None = None
    parent_ref: str | None = None
    unit_ref: str | None = None
    fingerprint: str | None = None
    occurrence: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "relations", tuple(self.relations))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def line(self) -> int:
        return self.span.start_line

    @property
    def end_line(self) -> int:
        return self.span.end_line

    @property
    def source_anchor(self) -> str | None:
        return self.anchor

    @property
    def source_span(self) -> SourceSpan:
        return self.span

    def to_dict(self) -> dict[str, Any]:
        return {
            "form": self.form,
            "kind": self.kind,
            "kind_raw": self.kind_raw,
            "kind_key": self.kind_key,
            "category_raw": self.category_raw,
            "category_key": self.category_key,
            "category": self.category,
            "content": self.content,
            "tags": list(self.tags),
            "context": self.context,
            "relations": [relation.to_dict() for relation in self.relations],
            "metadata": dict(self.metadata),
            "anchor": self.anchor,
            "span": self.span.to_dict(),
            "source_hash": self.source_hash,
            "title": self.title,
            "level": self.level,
            "line": self.line,
            "end_line": self.end_line,
            "body": self.body,
            "parent_ref": self.parent_ref,
            "unit_ref": self.unit_ref,
            "fingerprint": self.fingerprint,
            "occurrence": self.occurrence,
        }

    def to_legacy_block_dict(self) -> dict[str, Any] | None:
        """Return the exact former ``SemanticBlock.to_dict`` shape for rich units."""
        if self.form != "rich":
            return None
        out: dict[str, Any] = {
            "type": self.kind,
            "title": self.title,
            "level": self.level,
            "line": self.line,
            "end_line": self.end_line,
            "body": self.body or "",
            "metadata": dict(self.metadata),
            "relations": [relation.to_dict() for relation in self.relations],
        }
        if self.anchor:
            out["id"] = self.anchor
        return out


@dataclass(frozen=True, slots=True)
class SemanticUnitResolution:
    """Result of exact, non-fuzzy semantic-unit reference resolution."""

    status: str
    unit_ref: str
    unit: SemanticUnit | None = None
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "unit_ref": self.unit_ref,
            "unit": self.unit.to_dict() if self.unit is not None else None,
            "expected_fingerprint": self.expected_fingerprint,
            "actual_fingerprint": self.actual_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SemanticUnitDocument:
    """Source-ordered normalized units and deterministic parser findings."""

    units: tuple[SemanticUnit, ...]
    errors: tuple[SemanticUnitDiagnostic, ...] = ()
    warnings: tuple[SemanticUnitDiagnostic, ...] = ()
    parent_ref: str | None = None
    rich_blocks: tuple[semantic_blocks.SemanticBlock, ...] = ()
    semantic_block_errors: tuple[semantic_blocks.SemanticBlockValidationError, ...] = ()
    semantic_block_warnings: tuple[semantic_blocks.SemanticBlockValidationError, ...] = ()
    note_relations: tuple[markdown_relations.MarkdownRelation, ...] = ()
    note_relation_errors: tuple[markdown_relations.RelationValidationError, ...] = ()
    canonical_section_present: bool = False
    canonical_bullet_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", tuple(self.units))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(
            self,
            "rich_blocks",
            tuple(
                semantic_blocks.SemanticBlock(
                    type=block.type,
                    title=block.title,
                    level=block.level,
                    line=block.line,
                    end_line=block.end_line,
                    body=block.body,
                    metadata=MappingProxyType(dict(block.metadata)),
                    relations=tuple(block.relations),
                )
                for block in self.rich_blocks
            ),
        )
        object.__setattr__(self, "semantic_block_errors", tuple(self.semantic_block_errors))
        object.__setattr__(self, "semantic_block_warnings", tuple(self.semantic_block_warnings))
        object.__setattr__(self, "note_relations", tuple(self.note_relations))
        object.__setattr__(self, "note_relation_errors", tuple(self.note_relation_errors))

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def rich_units(self) -> tuple[SemanticUnit, ...]:
        return tuple(unit for unit in self.units if unit.form == "rich")

    @property
    def semantic_blocks(self) -> list[dict[str, Any]]:
        return [block.to_dict() for block in self.rich_blocks]

    @property
    def legacy_semantic_blocks(self) -> list[dict[str, Any]]:
        return self.semantic_blocks

    @property
    def legacy_semantic_block_errors(self) -> list[dict[str, Any]]:
        return [finding.to_dict() for finding in self.semantic_block_errors]

    @property
    def legacy_semantic_block_warnings(self) -> list[dict[str, Any]]:
        return [finding.to_dict() for finding in self.semantic_block_warnings]

    @property
    def canonical_note_relations(self) -> tuple[markdown_relations.MarkdownRelation, ...]:
        return tuple(relation for relation in self.note_relations if relation.canonical)

    def resolve_unit(
        self,
        unit_ref: str,
        *,
        expected_fingerprint: str | None = None,
    ) -> SemanticUnitResolution:
        """Resolve only an exact current reference, never text/span similarity."""
        requested = str(unit_ref or "")
        matches = [unit for unit in self.units if unit.unit_ref == requested]
        if len(matches) > 1:
            return SemanticUnitResolution(status="ambiguous", unit_ref=requested)
        if matches:
            unit = matches[0]
            if (
                expected_fingerprint is not None
                and unit.fingerprint != expected_fingerprint
            ):
                return SemanticUnitResolution(
                    status="stale",
                    unit_ref=requested,
                    expected_fingerprint=expected_fingerprint,
                    actual_fingerprint=unit.fingerprint,
                )
            return SemanticUnitResolution(
                status="found",
                unit_ref=requested,
                unit=unit,
                expected_fingerprint=expected_fingerprint,
                actual_fingerprint=unit.fingerprint,
            )

        ambiguous = [
            unit
            for unit in self.units
            if self.parent_ref
            and unit.anchor
            and _anchored_unit_ref(self.parent_ref, unit.anchor) == requested
        ]
        if len(ambiguous) > 1:
            return SemanticUnitResolution(status="ambiguous", unit_ref=requested)
        return SemanticUnitResolution(status="missing", unit_ref=requested)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_ref": self.parent_ref,
            "units": [unit.to_dict() for unit in self.units],
            "errors": [error.to_dict() for error in self.errors],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "semantic_blocks": self.semantic_blocks,
            "semantic_block_errors": self.legacy_semantic_block_errors,
            "semantic_block_warnings": self.legacy_semantic_block_warnings,
            "note_relations": [
                {
                    "kind": relation.kind,
                    "target": relation.target,
                    "raw": relation.raw,
                    "line": relation.line,
                    "canonical": relation.canonical,
                }
                for relation in self.note_relations
            ],
            "note_relation_errors": [
                finding.as_dict() for finding in self.note_relation_errors
            ],
        }


@dataclass(frozen=True, slots=True)
class _SourceLine:
    number: int
    text: str
    start_offset: int
    end_offset: int


def canonicalize_category(raw: str) -> str:
    """Validate and canonicalize one authored category label.

    Authored identity is NFKC + casefold with runs of spaces, underscores, and
    hyphens collapsed to one underscore.  Registry resolution is deliberately
    outside this parser.
    """
    category = (raw or "").strip()
    if not _is_valid_category(category):
        raise ValueError(
            "category must start with a Unicode letter, contain only letters, "
            "digits, spaces, underscores, or hyphens, and be at most 64 codepoints"
        )
    normalized = unicodedata.normalize("NFKC", category).casefold()
    return _CATEGORY_SEPARATORS_RE.sub("_", normalized).strip("_")


def fingerprint_semantic_unit(
    unit: SemanticUnit,
    *,
    occurrence: int | None = None,
) -> str:
    """Return the versioned authored-state fingerprint for one semantic unit."""
    signature = _semantic_unit_signature(unit)
    payload: dict[str, Any] = {
        "schema": _IDENTITY_SCHEMA,
        "signature": signature,
    }
    if unit.anchor:
        payload.update({"binding": "anchor", "anchor": unit.anchor})
    else:
        if occurrence is None or occurrence < 1:
            raise ValueError("anonymous semantic-unit occurrence must be at least 1")
        payload.update({"binding": "anonymous", "occurrence": occurrence})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_semantic_units(
    markdown: str,
    *,
    path: str = "",
    parent_ref: str | None = None,
    validate: bool = True,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
    relation_registry: RelationRegistry | None = None,
    include_legacy_relations: bool = False,
    retain_unknown_relations: bool = False,
    project: str | None = None,
    page_type: str | None = None,
) -> SemanticUnitDocument:
    """Parse compact observations and rich semantic blocks exactly once each."""
    source = markdown or ""
    source_path = str(path)
    effective_parent_ref = _effective_parent_ref(parent_ref, source_path)
    lines = _source_lines(source)
    line_by_number = {line.number: line for line in lines}
    units: list[SemanticUnit] = []
    errors: list[SemanticUnitDiagnostic] = []
    warnings: list[SemanticUnitDiagnostic] = []
    if validate and language_registry is not None:
        for finding in language_registry.findings:
            diagnostic = _registry_diagnostic(finding, path=source_path)
            (warnings if diagnostic.severity == "warning" else errors).append(diagnostic)

    _parse_compact_units(
        lines,
        path=source_path,
        validate=validate,
        units=units,
        errors=errors,
    )

    kind_resolver = None
    if language_registry is not None:

        def kind_resolver(
            label: str,
        ) -> semantic_blocks.SemanticBlockKindResolution:
            resolution = language_registry.resolve_heading(
                label,
                project=project,
                page_type=page_type,
            )
            return semantic_blocks.SemanticBlockKindResolution(
                kind=resolution.resolved,
                findings=tuple(
                    semantic_blocks.SemanticBlockKindFinding(
                        code=finding["code"],
                        message=finding["detail"],
                    )
                    for finding in (
                        resolution.findings
                        if resolution.status == "scope_violation"
                        else ()
                    )
                ),
            )

    rich_document = semantic_blocks.parse_semantic_blocks(
        source,
        validate=validate,
        registry=relation_registry,
        kind_resolver=kind_resolver,
    )
    note_relation_document = markdown_relations.parse_markdown_relations(
        source,
        include_legacy=include_legacy_relations,
        relation_types=(
            relation_registry.keys | frozenset(relation_registry.aliases)
            if relation_registry is not None
            else None
        ),
        retain_unknown=retain_unknown_relations,
    )
    for block in rich_document.blocks:
        span = _span_for_line_range(source, line_by_number, block.line, block.end_line)
        category_raw, category_key, category_error = _rich_category(
            block,
            path=source_path,
            line_by_number=line_by_number,
        )
        if validate and category_error is not None:
            errors.append(category_error)
        units.append(
            SemanticUnit(
                form="rich",
                kind=block.type,
                kind_raw=block.title,
                kind_key=semantic_language_registry.normalize_label(block.title),
                category_raw=category_raw,
                category_key=category_key,
                category=(
                    category_key
                    if "category" in block.metadata and category_error is None
                    else block.type
                ),
                content=block.body,
                tags=(),
                context=None,
                relations=tuple(block.relations),
                metadata=block.metadata,
                anchor=block.id,
                span=span,
                source_hash=_source_hash(span.text),
                title=block.title,
                level=block.level,
                body=block.body,
            )
        )

    if validate:
        errors.extend(
            _normalize_rich_diagnostics(
                rich_document.errors,
                path=source_path,
                line_by_number=line_by_number,
                severity="error",
            )
        )
        warnings.extend(
            _normalize_rich_diagnostics(
                [
                    warning
                    for warning in rich_document.warnings
                    if warning.code != "duplicate_id"
                ],
                path=source_path,
                line_by_number=line_by_number,
                severity="warning",
            )
        )

    if language_registry is not None:
        resolved_units: list[SemanticUnit] = []
        for unit in units:
            resolution = language_registry.resolve_category(
                unit.category_raw,
                project=project,
                page_type=page_type,
            )
            resolved_category = resolution.resolved or unit.category_key
            if (
                unit.form == "rich"
                and "category" not in unit.metadata
                and resolution.status in {"unregistered", "registry_invalid"}
            ):
                resolved_category = unit.kind
            resolved_units.append(replace(unit, category=resolved_category))
            if validate and resolution.status != "registry_invalid":
                for finding in resolution.findings:
                    diagnostic = _registry_diagnostic(
                        finding,
                        path=source_path,
                        unit=unit,
                    )
                    (warnings if diagnostic.severity == "warning" else errors).append(
                        diagnostic
                    )
        units = resolved_units

    units.sort(key=lambda unit: (unit.span.start_offset, unit.form))
    bound_units, identity_errors = _bind_unit_identities(
        units,
        parent_ref=effective_parent_ref,
        path=source_path,
        validate=validate,
    )
    errors.extend(identity_errors)
    errors.sort(key=_diagnostic_sort_key)
    warnings.sort(key=_diagnostic_sort_key)
    return SemanticUnitDocument(
        units=bound_units,
        errors=tuple(errors),
        warnings=tuple(warnings),
        parent_ref=effective_parent_ref,
        rich_blocks=tuple(rich_document.blocks),
        semantic_block_errors=tuple(rich_document.errors),
        semantic_block_warnings=tuple(rich_document.warnings),
        note_relations=tuple(note_relation_document.relations),
        note_relation_errors=tuple(note_relation_document.errors),
        canonical_section_present=note_relation_document.canonical_section_present,
        canonical_bullet_count=note_relation_document.canonical_bullet_count,
    )


def _registry_diagnostic(
    finding: Mapping[str, str],
    *,
    path: str,
    unit: SemanticUnit | None = None,
) -> SemanticUnitDiagnostic:
    severity = finding.get("severity", "error")
    detail = finding.get("detail", "semantic-language registry validation failed")
    return SemanticUnitDiagnostic(
        code=finding.get("code", "invalid_semantic_language_registry"),
        message=detail,
        path=path,
        span=unit.span if unit is not None else None,
        line=unit.line if unit is not None else None,
        raw=unit.span.text if unit is not None else detail,
        remediation=(
            "Review the semantic-language registry definition and its scope before "
            "using this category or kind."
        ),
        severity=severity,
    )


def _effective_parent_ref(parent_ref: str | None, path: str) -> str | None:
    if parent_ref is None:
        legacy_ref = context_refs.vault_ref(path)
        return legacy_ref if legacy_ref != "exomem://vault/" else None
    parsed = memory_refs.parse_memory_ref(str(parent_ref))
    if parsed is None:
        raise ValueError("parent_ref must be a canonical exomem://memory/<uuid> reference")
    return memory_refs.memory_ref(parsed)


def _bind_unit_identities(
    units: list[SemanticUnit],
    *,
    parent_ref: str | None,
    path: str,
    validate: bool,
) -> tuple[tuple[SemanticUnit, ...], list[SemanticUnitDiagnostic]]:
    anchor_groups: dict[str, list[SemanticUnit]] = {}
    for unit in units:
        if unit.anchor:
            anchor_groups.setdefault(unit.anchor, []).append(unit)
    duplicate_anchors = {
        anchor for anchor, members in anchor_groups.items() if len(members) > 1
    }

    occurrences: dict[str, int] = {}
    bound: list[SemanticUnit] = []
    for unit in units:
        if unit.anchor:
            fingerprint = fingerprint_semantic_unit(unit)
            unit_ref = (
                None
                if parent_ref is None or unit.anchor in duplicate_anchors
                else _anchored_unit_ref(parent_ref, unit.anchor)
            )
            occurrence = None
        else:
            signature_key = _stable_json(_semantic_unit_signature(unit))
            occurrence = occurrences.get(signature_key, 0) + 1
            occurrences[signature_key] = occurrence
            fingerprint = fingerprint_semantic_unit(unit, occurrence=occurrence)
            unit_ref = (
                f"{parent_ref}#unit-{fingerprint}"
                if parent_ref is not None
                else None
            )
        bound.append(
            replace(
                unit,
                parent_ref=parent_ref,
                unit_ref=unit_ref,
                fingerprint=fingerprint,
                occurrence=occurrence,
            )
        )

    errors: list[SemanticUnitDiagnostic] = []
    if validate:
        for anchor, members in anchor_groups.items():
            if len(members) < 2:
                continue
            first = members[0]
            errors.append(
                SemanticUnitDiagnostic(
                    code="duplicate_anchor",
                    message=f"duplicate semantic-unit anchor: {anchor}",
                    path=path,
                    span=first.span,
                    line=first.line,
                    raw=first.span.text,
                    remediation=(
                        "Give every compact and rich semantic unit a unique authored "
                        "anchor within this page."
                    ),
                    severity="error",
                )
            )
    return tuple(bound), errors


def _semantic_unit_signature(unit: SemanticUnit) -> dict[str, Any]:
    metadata = {
        key: _normalize_authored_text(value)
        for key, value in unit.metadata.items()
        if key != "id"
    }
    relations = [
        {
            "kind": relation.kind,
            "target": _normalize_authored_text(relation.target),
            "raw": _normalize_authored_text(relation.raw),
        }
        for relation in unit.relations
    ]
    category_raw_identity = (
        unit.category_key
        if unit.form == "rich" and "category" not in unit.metadata
        else unicodedata.normalize("NFKC", unit.category_raw.strip())
    )
    return {
        "form": unit.form,
        "kind": unit.kind_key,
        "category_raw_nfkc": category_raw_identity,
        "category_key": unit.category_key,
        "content": _normalize_authored_text(unit.content),
        "tags": list(unit.tags),
        "context": (
            _normalize_authored_text(unit.context)
            if unit.context is not None
            else None
        ),
        "metadata": metadata,
        "relations": relations,
    }


def _normalize_authored_text(value: str) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _anchored_unit_ref(parent_ref: str, anchor: str) -> str:
    return f"{parent_ref}#{quote(anchor, safe='')}"


def _parse_compact_units(
    lines: tuple[_SourceLine, ...],
    *,
    path: str,
    validate: bool,
    units: list[SemanticUnit],
    errors: list[SemanticUnitDiagnostic],
) -> None:
    fence_char: str | None = None
    fence_length = 0
    for line in lines:
        fence = _FENCE_RE.match(line.text)
        if fence_char is not None:
            if _closes_fence(line.text, fence_char, fence_length):
                fence_char = None
                fence_length = 0
            continue
        if fence is not None:
            marker = fence.group("fence")
            fence_char = marker[0]
            fence_length = len(marker)
            continue

        match = _COMPACT_RE.match(line.text)
        if match is None:
            continue
        label = match.group("label").strip()
        tail = match.group("tail")
        if label in _TASK_LABELS or not label:
            continue
        if tail and not tail[0].isspace():
            continue

        try:
            category_key = canonicalize_category(label)
        except ValueError:
            if validate and _looks_like_malformed_category(label):
                errors.append(
                    _compact_diagnostic(
                        code="invalid_compact_category",
                        message=f"invalid compact observation category: {label}",
                        remediation=(
                            "Use 1-64 Unicode letters/digits with spaces, underscores, "
                            "or hyphens, beginning with a letter."
                        ),
                        path=path,
                        line=line,
                    )
                )
            continue

        content, tags, context, anchor = _parse_suffixes(tail.strip())
        if not content:
            if validate:
                errors.append(
                    _compact_diagnostic(
                        code="empty_compact_observation",
                        message="compact observation content is empty",
                        remediation="Add content after the category before optional suffixes.",
                        path=path,
                        line=line,
                    )
                )
            continue

        span = _span_for_source_line(line)
        units.append(
            SemanticUnit(
                form="compact",
                kind="observation",
                kind_raw="observation",
                kind_key="observation",
                category_raw=label,
                category_key=category_key,
                category=category_key,
                content=content,
                tags=tags,
                context=context,
                relations=(),
                metadata={},
                anchor=anchor,
                span=span,
                source_hash=_source_hash(span.text),
                title=None,
                level=None,
                body=None,
            )
        )


def _parse_suffixes(value: str) -> tuple[str, tuple[str, ...], str | None, str | None]:
    remaining, anchor = _take_anchor(value.rstrip())
    remaining, context = _take_context(remaining)
    remaining, tags = _take_tags(remaining)
    return remaining.strip(), tags, context, anchor


def _take_anchor(value: str) -> tuple[str, str | None]:
    match = _ANCHOR_RE.search(value)
    if match is None:
        return value, None
    return value[: match.start()].rstrip(), match.group("anchor")


def _take_context(value: str) -> tuple[str, str | None]:
    stripped = value.rstrip()
    if not stripped.endswith(")") or _is_escaped(stripped, len(stripped) - 1):
        return value, None

    depth = 0
    for index in range(len(stripped) - 1, -1, -1):
        if _is_escaped(stripped, index):
            continue
        char = stripped[index]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                if index == 0 or not stripped[index - 1].isspace():
                    return value, None
                return stripped[:index].rstrip(), stripped[index + 1 : -1].strip()
            if depth < 0:
                return value, None
    return value, None


def _take_tags(value: str) -> tuple[str, tuple[str, ...]]:
    remaining = value.rstrip()
    reversed_tags: list[str] = []
    while match := _TRAILING_TAG_RE.search(remaining):
        tag = match.group("tag")
        if not _is_valid_tag(tag):
            break
        reversed_tags.append(tag)
        remaining = remaining[: match.start()].rstrip()
    reversed_tags.reverse()
    return remaining, tuple(reversed_tags)


def _is_valid_category(value: str) -> bool:
    if not value or len(value) > 64 or not value[0].isalpha():
        return False
    return all(
        char.isalpha()
        or char.isdigit()
        or char in "_-"
        or unicodedata.category(char) == "Zs"
        for char in value
    )


def _looks_like_malformed_category(value: str) -> bool:
    return bool(value and value[0].isalpha() and value != "take:")


def _is_valid_tag(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    if not (value[0].isalpha() or value[0].isdigit()):
        return False
    if any(
        not (char.isalpha() or char.isdigit() or char in "_-/") for char in value
    ):
        return False
    return not value.endswith("/") and "//" not in value


def _is_escaped(value: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and value[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _closes_fence(line: str, fence_char: str, fence_length: int) -> bool:
    match = _FENCE_RE.match(line)
    if match is None:
        return False
    marker = match.group("fence")
    return (
        marker[0] == fence_char
        and len(marker) >= fence_length
        and not match.group("info").strip()
    )


def _rich_category(
    block: semantic_blocks.SemanticBlock,
    *,
    path: str,
    line_by_number: dict[int, _SourceLine],
) -> tuple[str, str, SemanticUnitDiagnostic | None]:
    explicit = block.metadata.get("category")
    if explicit is None:
        return (
            block.title,
            semantic_language_registry.normalize_label(block.title),
            None,
        )
    try:
        return explicit.strip(), canonicalize_category(explicit), None
    except ValueError:
        category_line = _find_rich_category_line(block, line_by_number)
        span = _span_for_source_line(category_line) if category_line is not None else None
        raw = category_line.text if category_line is not None else explicit
        return (
            block.title,
            semantic_language_registry.normalize_label(block.title),
            SemanticUnitDiagnostic(
                code="invalid_rich_category",
                message=f"invalid rich semantic-unit category: {explicit}",
                path=path,
                span=span,
                line=category_line.number if category_line is not None else block.line,
                raw=raw,
                remediation=(
                    "Use 1-64 Unicode letters/digits with spaces, underscores, or "
                    "hyphens, beginning with a letter; the rich block remains available."
                ),
                severity="error",
            ),
        )


def _find_rich_category_line(
    block: semantic_blocks.SemanticBlock,
    line_by_number: dict[int, _SourceLine],
) -> _SourceLine | None:
    for number in range(block.line + 1, block.end_line + 1):
        line = line_by_number.get(number)
        if line is not None and _RICH_CATEGORY_RE.match(line.text):
            return line
    return None


def _normalize_rich_diagnostics(
    findings: list[semantic_blocks.SemanticBlockValidationError],
    *,
    path: str,
    line_by_number: dict[int, _SourceLine],
    severity: str,
) -> list[SemanticUnitDiagnostic]:
    normalized: list[SemanticUnitDiagnostic] = []
    for finding in findings:
        source_line = line_by_number.get(finding.line) if finding.line is not None else None
        span = _span_for_source_line(source_line) if source_line is not None else None
        normalized.append(
            SemanticUnitDiagnostic(
                code=finding.code,
                message=finding.message,
                path=path,
                span=span,
                line=finding.line,
                raw=source_line.text if source_line is not None else "",
                remediation=_rich_remediation(finding.code),
                severity=severity,
            )
        )
    return normalized


def _rich_remediation(code: str) -> str:
    if code == "unsupported_relation":
        return "Use an active relation kind from the governed relation registry."
    if code == "malformed_relation":
        return "Write relation metadata as `relation_kind: target` entries."
    if code == "duplicate_id":
        return "Give each rich semantic block a unique `id` metadata value."
    return "Review the rich semantic block metadata at this source location."


def _compact_diagnostic(
    *,
    code: str,
    message: str,
    remediation: str,
    path: str,
    line: _SourceLine,
) -> SemanticUnitDiagnostic:
    return SemanticUnitDiagnostic(
        code=code,
        message=message,
        path=path,
        span=_span_for_source_line(line),
        line=line.number,
        raw=line.text,
        remediation=remediation,
        severity="error",
    )


def _source_lines(source: str) -> tuple[_SourceLine, ...]:
    lines: list[_SourceLine] = []
    offset = 0
    for number, raw_line in enumerate(source.splitlines(keepends=True), start=1):
        text = raw_line
        if text.endswith("\r\n"):
            text = text[:-2]
        elif text.endswith(("\n", "\r")):
            text = text[:-1]
        lines.append(
            _SourceLine(
                number=number,
                text=text,
                start_offset=offset,
                end_offset=offset + len(text),
            )
        )
        offset += len(raw_line)
    return tuple(lines)


def _span_for_source_line(line: _SourceLine) -> SourceSpan:
    return SourceSpan(
        start_line=line.number,
        start_column=1,
        end_line=line.number,
        end_column=len(line.text) + 1,
        start_offset=line.start_offset,
        end_offset=line.end_offset,
        text=line.text,
    )


def _span_for_line_range(
    source: str,
    line_by_number: dict[int, _SourceLine],
    start_line: int,
    end_line: int,
) -> SourceSpan:
    start = line_by_number[start_line]
    end = line_by_number[end_line]
    return SourceSpan(
        start_line=start_line,
        start_column=1,
        end_line=end_line,
        end_column=len(end.text) + 1,
        start_offset=start.start_offset,
        end_offset=end.end_offset,
        text=source[start.start_offset : end.end_offset],
    )


def _source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _diagnostic_sort_key(
    diagnostic: SemanticUnitDiagnostic,
) -> tuple[int, int, str, str]:
    offset = diagnostic.span.start_offset if diagnostic.span is not None else len(diagnostic.raw)
    line = diagnostic.line if diagnostic.line is not None else 0
    return offset, line, diagnostic.code, diagnostic.message
