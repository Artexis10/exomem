"""Markdown-readable semantic blocks for Exomem notes.

This module is deliberately small: normal ATX headings name semantic blocks,
optional leading ``- key: value`` bullets carry metadata, and the rest of the
section remains plain Markdown. It performs deterministic parsing and
validation only; no model, sidecar, or graph store is involved.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import relation_registry

BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "claim",
        "finding",
        "evidence",
        "decision",
        "assumption",
        "inference",
        "constraint",
        "risk",
        "open_question",
        "hypothesis",
        "result",
        "metric",
        "failure",
        "pattern",
        "record",
        "case",
        "timeline_event",
        "requirement",
        "action",
        "definition",
        "procedure",
        "source",
        "experiment",
        "entity",
        "project",
        "media_segment",
    }
)

_BLOCK_TYPE_ALIASES: dict[str, str] = {
    "claims": "claim",
    "findings": "finding",
    "proof": "evidence",
    "proofs": "evidence",
    "evidences": "evidence",
    "decisions": "decision",
    "assumptions": "assumption",
    "inferences": "inference",
    "constraints": "constraint",
    "risks": "risk",
    "open_questions": "open_question",
    "questions": "open_question",
    "hypotheses": "hypothesis",
    "results": "result",
    "outcome": "result",
    "outcomes": "result",
    "metrics": "metric",
    "failures": "failure",
    "patterns": "pattern",
    "records": "record",
    "cases": "case",
    "timeline": "timeline_event",
    "timelines": "timeline_event",
    "timeline_events": "timeline_event",
    "events": "timeline_event",
    "requirements": "requirement",
    "actions": "action",
    "todo": "action",
    "todos": "action",
    "definitions": "definition",
    "procedures": "procedure",
    "sources": "source",
    "experiments": "experiment",
    "entities": "entity",
    "projects": "project",
    "media_segments": "media_segment",
    "segments": "media_segment",
}

RELATION_TYPES: frozenset[str] = relation_registry.core_registry().keys

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_METADATA_RE = re.compile(r"^\s*[-*+]\s+([A-Za-z0-9 _-]+):\s*(.*)$")
_NORMALIZE_RE = re.compile(r"[\s-]+")
_RESERVED_METADATA_KEYS = frozenset(
    {"category", "id", "tags", "context", "relations"}
)


@dataclass(frozen=True)
class SemanticRelation:
    kind: str
    target: str
    raw: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "raw": self.raw,
            "line": self.line,
        }


@dataclass(frozen=True)
class SemanticBlockValidationError:
    code: str
    message: str
    line: int | None = None
    block_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.line is not None:
            out["line"] = self.line
        if self.block_id:
            out["block_id"] = self.block_id
        return out


@dataclass(frozen=True)
class SemanticBlockKindFinding:
    """One resolver finding awaiting source-line binding by the parser."""

    code: str
    message: str


@dataclass(frozen=True)
class SemanticBlockKindResolution:
    """Optional rich-kind resolution plus non-blocking governance findings."""

    kind: str | None
    findings: tuple[SemanticBlockKindFinding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))


@dataclass(frozen=True)
class SemanticBlock:
    type: str
    title: str
    level: int
    line: int
    end_line: int
    body: str
    metadata: dict[str, str] = field(default_factory=dict)
    relations: list[SemanticRelation] = field(default_factory=list)

    @property
    def id(self) -> str | None:
        return self.metadata.get("id")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "level": self.level,
            "line": self.line,
            "end_line": self.end_line,
            "body": self.body,
            "metadata": dict(self.metadata),
            "relations": [r.to_dict() for r in self.relations],
        }
        if self.id:
            out["id"] = self.id
        return out


@dataclass(frozen=True)
class SemanticBlockDocument:
    blocks: list[SemanticBlock]
    errors: list[SemanticBlockValidationError] = field(default_factory=list)
    warnings: list[SemanticBlockValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def blocks_by_type(self, block_type: str) -> list[SemanticBlock]:
        normalized = normalize_block_type(block_type)
        if normalized is None:
            return []
        return [block for block in self.blocks if block.type == normalized]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [block.to_dict() for block in self.blocks],
            "errors": [error.to_dict() for error in self.errors],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def normalize_label(label: str) -> str:
    """Normalize user-visible heading/relation labels to schema keys."""
    normalized = (label or "").strip().lower().rstrip(":").strip()
    normalized = _NORMALIZE_RE.sub("_", normalized)
    normalized = re.sub(r"_+", "_", normalized)
    return normalized.strip("_")


def normalize_block_type(
    label: str,
    *,
    resolver: Callable[
        [str], str | SemanticBlockKindResolution | None
    ]
    | None = None,
) -> str | None:
    """Return the canonical semantic block type for a heading label."""
    block_type, _ = _resolve_block_type(label, resolver=resolver)
    return block_type


def _resolve_block_type(
    label: str,
    *,
    resolver: Callable[
        [str], str | SemanticBlockKindResolution | None
    ]
    | None,
) -> tuple[str | None, tuple[SemanticBlockKindFinding, ...]]:
    normalized = normalize_label(label)
    block_type = _BLOCK_TYPE_ALIASES.get(normalized, normalized)
    if block_type in BLOCK_TYPES:
        return block_type, ()
    if resolver is None:
        return None, ()
    result = resolver(label)
    if isinstance(result, SemanticBlockKindResolution):
        candidate = result.kind
        findings = result.findings
    else:
        candidate = result
        findings = ()
    if candidate is not None and not isinstance(candidate, str):
        candidate = None
    if candidate is not None and normalize_label(candidate) == "observation":
        candidate = None
    return candidate, findings


def parse_semantic_blocks(
    markdown: str,
    *,
    validate: bool = True,
    registry: relation_registry.RelationRegistry | None = None,
    kind_resolver: Callable[
        [str], str | SemanticBlockKindResolution | None
    ]
    | None = None,
) -> SemanticBlockDocument:
    """Parse semantic blocks from Markdown.

    Unknown headings are treated as normal Markdown structure. A recognized
    semantic heading at level N starts a block and the block ends at the next
    non-fenced ATX heading whose level is less than or equal to N. Deeper
    headings remain part of the block body. Leading metadata bullets are
    removed from the block body.
    """
    lines = (markdown or "").splitlines()
    blocks: list[SemanticBlock] = []
    errors: list[SemanticBlockValidationError] = []
    warnings: list[SemanticBlockValidationError] = []
    current: tuple[str, str, int, int, list[tuple[int, str]]] | None = None
    fence_char: str | None = None
    fence_length = 0

    def flush(end_line: int) -> None:
        nonlocal current
        if current is None:
            return
        block_type, title, level, start_line, body_lines = current
        block, block_errors = _build_block(
            block_type=block_type,
            title=title,
            level=level,
            start_line=start_line,
            end_line=max(start_line, end_line),
            lines=body_lines,
            registry=registry or relation_registry.core_registry(),
        )
        if _has_substantive_body(block.body):
            blocks.append(block)
        elif validate:
            errors.append(
                SemanticBlockValidationError(
                    code="empty_rich_unit",
                    message="rich semantic-unit body is empty",
                    line=start_line,
                    block_id=block.id,
                )
            )
        if validate:
            errors.extend(block_errors)
        current = None

    for line_number, line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(line)
        if fence_char is not None:
            if current is not None:
                current[4].append((line_number, line))
            if _closes_fence(line, fence_char, fence_length):
                fence_char = None
                fence_length = 0
            continue
        if fence is not None:
            if current is not None:
                current[4].append((line_number, line))
            marker = fence.group("fence")
            fence_char = marker[0]
            fence_length = len(marker)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            if current is not None and level > current[2]:
                current[4].append((line_number, line))
                continue
            flush(line_number - 1)
            title = heading.group(2).strip()
            block_type, kind_findings = _resolve_block_type(
                title, resolver=kind_resolver
            )
            if validate:
                warnings.extend(
                    SemanticBlockValidationError(
                        code=finding.code,
                        message=finding.message,
                        line=line_number,
                    )
                    for finding in kind_findings
            )
            if block_type is not None:
                current = (block_type, title, level, line_number, [])
            continue

        if current is not None:
            current[4].append((line_number, line))

    flush(len(lines))

    if validate:
        warnings.extend(_duplicate_id_warnings(blocks))
    return SemanticBlockDocument(blocks=blocks, errors=errors, warnings=warnings)


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


def _has_substantive_body(body: str) -> bool:
    """Return whether a metadata-stripped rich body contains authored content."""
    fence_char: str | None = None
    fence_length = 0
    for line in body.splitlines():
        fence = _FENCE_RE.match(line)
        if fence_char is not None:
            if _closes_fence(line, fence_char, fence_length):
                fence_char = None
                fence_length = 0
                continue
            if line.strip():
                return True
            continue
        if fence is not None:
            marker = fence.group("fence")
            fence_char = marker[0]
            fence_length = len(marker)
            continue
        if (
            line.strip()
            and _HEADING_RE.match(line) is None
            and not _is_reserved_metadata_row(line)
        ):
            return True
    return False


def _is_reserved_metadata_row(line: str) -> bool:
    match = _METADATA_RE.match(line)
    return bool(
        match and normalize_label(match.group(1)) in _RESERVED_METADATA_KEYS
    )


def first_block_body(markdown: str, block_type: str) -> str | None:
    """Body text for the first parsed block of `block_type`, or None."""
    document = parse_semantic_blocks(markdown, validate=False)
    normalized = normalize_label(block_type)
    for block in document.blocks:
        if block.type == normalized and block.body.strip():
            return block.body.strip()
    return None


def _build_block(
    *,
    block_type: str,
    title: str,
    level: int,
    start_line: int,
    end_line: int,
    lines: list[tuple[int, str]],
    registry: relation_registry.RelationRegistry,
) -> tuple[SemanticBlock, list[SemanticBlockValidationError]]:
    metadata, relation_values, body_lines = _split_metadata(lines)
    relations, errors = _parse_relations(relation_values, registry)
    body = "\n".join(body_lines).strip()
    block = SemanticBlock(
        type=block_type,
        title=title,
        level=level,
        line=start_line,
        end_line=end_line,
        body=body,
        metadata=metadata,
        relations=relations,
    )
    return block, errors


def _split_metadata(
    lines: list[tuple[int, str]],
) -> tuple[dict[str, str], list[tuple[str, int]], list[str]]:
    metadata: dict[str, str] = {}
    relation_values: list[tuple[str, int]] = []
    i = 0

    while i < len(lines) and not lines[i][1].strip():
        i += 1

    while i < len(lines):
        line_number, line = lines[i]
        if not line.strip():
            i += 1
            continue
        match = _METADATA_RE.match(line)
        if not match:
            break
        key = normalize_label(match.group(1))
        value = match.group(2).strip()
        metadata[key] = value
        if key == "relations":
            relation_values.append((value, line_number))
        i += 1

    return metadata, relation_values, [line for _, line in lines[i:]]


def _parse_relations(
    values: list[tuple[str, int]],
    registry: relation_registry.RelationRegistry,
) -> tuple[list[SemanticRelation], list[SemanticBlockValidationError]]:
    relations: list[SemanticRelation] = []
    errors: list[SemanticBlockValidationError] = []

    for value, line_number in values:
        entries = _split_relation_entries(value)
        if not entries:
            errors.append(
                SemanticBlockValidationError(
                    code="malformed_relation",
                    message="relations metadata must contain relation: target entries",
                    line=line_number,
                )
            )
            continue
        for entry in entries:
            if ":" not in entry:
                errors.append(
                    SemanticBlockValidationError(
                        code="malformed_relation",
                        message=f"malformed relation entry: {entry}",
                        line=line_number,
                    )
                )
                continue
            raw_kind, raw_target = entry.split(":", 1)
            kind = normalize_label(raw_kind)
            target = raw_target.strip()
            resolution = registry.resolve(kind, origin="semantic_relation")
            if resolution.canonical is None:
                errors.append(
                    SemanticBlockValidationError(
                        code="unsupported_relation",
                        message=f"unsupported relation: {raw_kind.strip()}",
                        line=line_number,
                    )
                )
            if not target:
                errors.append(
                    SemanticBlockValidationError(
                        code="malformed_relation",
                        message=f"relation {kind} is missing a target",
                        line=line_number,
                    )
                )
                continue
            relations.append(
                SemanticRelation(kind=kind, target=target, raw=entry, line=line_number)
            )

    return relations, errors


def _split_relation_entries(value: str) -> list[str]:
    entries: list[str] = []
    buf: list[str] = []
    wikilink_depth = 0
    i = 0
    while i < len(value):
        pair = value[i : i + 2]
        if pair == "[[":
            wikilink_depth += 1
            buf.append(pair)
            i += 2
            continue
        if pair == "]]" and wikilink_depth:
            wikilink_depth -= 1
            buf.append(pair)
            i += 2
            continue
        char = value[i]
        if char == "," and wikilink_depth == 0:
            entry = "".join(buf).strip()
            if entry:
                entries.append(entry)
            buf = []
        else:
            buf.append(char)
        i += 1

    entry = "".join(buf).strip()
    if entry:
        entries.append(entry)
    return entries


def _duplicate_id_warnings(blocks: list[SemanticBlock]) -> list[SemanticBlockValidationError]:
    seen: dict[str, SemanticBlock] = {}
    warnings: list[SemanticBlockValidationError] = []
    for block in blocks:
        if not block.id:
            continue
        if block.id in seen:
            warnings.append(
                SemanticBlockValidationError(
                    code="duplicate_id",
                    message=f"duplicate semantic block id: {block.id}",
                    line=block.line,
                    block_id=block.id,
                )
            )
            continue
        seen[block.id] = block
    return warnings
