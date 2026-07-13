"""Deterministic parsing for compact and rich semantic units.

Markdown remains the source of truth.  This module only normalizes authored
syntax and composes the existing semantic-block parser; it performs no I/O,
registry mutation, indexing, or model work.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from . import semantic_blocks
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
class SemanticUnitDocument:
    """Source-ordered normalized units and deterministic parser findings."""

    units: tuple[SemanticUnit, ...]
    errors: tuple[SemanticUnitDiagnostic, ...] = ()
    warnings: tuple[SemanticUnitDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", tuple(self.units))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def semantic_blocks(self) -> list[dict[str, Any]]:
        return [
            projection
            for unit in self.units
            if (projection := unit.to_legacy_block_dict()) is not None
        ]

    @property
    def legacy_semantic_blocks(self) -> list[dict[str, Any]]:
        return self.semantic_blocks

    def to_dict(self) -> dict[str, Any]:
        return {
            "units": [unit.to_dict() for unit in self.units],
            "errors": [error.to_dict() for error in self.errors],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "semantic_blocks": self.semantic_blocks,
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


def parse_semantic_units(
    markdown: str,
    *,
    path: str = "",
    validate: bool = True,
) -> SemanticUnitDocument:
    """Parse compact observations and rich semantic blocks exactly once each."""
    source = markdown or ""
    source_path = str(path)
    lines = _source_lines(source)
    line_by_number = {line.number: line for line in lines}
    units: list[SemanticUnit] = []
    errors: list[SemanticUnitDiagnostic] = []
    warnings: list[SemanticUnitDiagnostic] = []

    _parse_compact_units(
        lines,
        path=source_path,
        validate=validate,
        units=units,
        errors=errors,
    )

    rich_document = semantic_blocks.parse_semantic_blocks(source, validate=validate)
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
                category_raw=category_raw,
                category_key=category_key,
                category=category_key,
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
                rich_document.warnings,
                path=source_path,
                line_by_number=line_by_number,
                severity="warning",
            )
        )

    units.sort(key=lambda unit: (unit.span.start_offset, unit.form))
    errors.sort(key=_diagnostic_sort_key)
    warnings.sort(key=_diagnostic_sort_key)
    return SemanticUnitDocument(
        units=tuple(units),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


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
        return block.type, block.type, None
    try:
        return explicit.strip(), canonicalize_category(explicit), None
    except ValueError:
        category_line = _find_rich_category_line(block, line_by_number)
        span = _span_for_source_line(category_line) if category_line is not None else None
        raw = category_line.text if category_line is not None else explicit
        return (
            block.type,
            block.type,
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
