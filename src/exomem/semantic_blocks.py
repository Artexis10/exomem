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
from types import MappingProxyType
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
        "prediction",
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

#: Headings at this level or shallower are page titles rather than blocks. Every
#: page-type template in `references/page-types.md` opens `# <Title>`, and every
#: writer here emits `# {title}` followed by `##` sections, so the parser was the
#: only component reading a level-1 heading as content.
_TITLE_HEADING_LEVEL = 1

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
    "predictions": "prediction",
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
# Leading rows the language itself owns. A block whose only rows are these is
# not "a block with content" — it has no body. `verdict` and `check_by` belong
# here for the same reason `category` does: they are governed metadata, so a
# `## Prediction` carrying only a verdict must still report `empty_rich_unit`
# rather than pass as authored.
_RESERVED_METADATA_KEYS = frozenset(
    {"category", "id", "tags", "context", "relations", "verdict", "check_by"}
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
class SemanticRelationCandidate:
    """One metadata operand before vocabulary recognition."""

    raw_kind: str
    kind: str
    target: str
    raw: str
    line: int
    has_colon: bool

    @property
    def grammar_valid(self) -> bool:
        return self.has_colon and bool(self.target)


@dataclass(frozen=True)
class SemanticBlockCandidate:
    """A heading section; its line identity never grants a semantic-unit ref."""

    title: str
    level: int
    line: int
    end_line: int
    ancestor_line: int | None
    body: str
    metadata: dict[str, str]
    relations: tuple[SemanticRelationCandidate, ...]
    substantive_body: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [block.to_dict() for block in self.blocks],
            "errors": [error.to_dict() for error in self.errors],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


def normalize_label(label: str) -> str:
    """Normalize user-visible heading/relation labels to schema keys."""
    # The stored-key form: graph block anchors and semantic-unit metadata keys
    # are built with it. Unifying it with semantic_language_registry's
    # NFKC+casefold form re-keys labels such as "Straße" or full-width headings,
    # so it needs epistemic_graph.SCHEMA_VERSION and semantic_index.PARSER_VERSION
    # bumped on a release that already rebuilds.
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
    return interpret_semantic_blocks(
        scan_semantic_blocks(markdown), validate=validate, registry=registry,
        kind_resolver=kind_resolver,
    )


def scan_semantic_blocks(markdown: str) -> tuple[SemanticBlockCandidate, ...]:
    """Retain every non-fenced heading before selecting any vocabulary."""
    lines = (markdown or "").splitlines()
    headings: list[tuple[str, int, int, int | None]] = []
    ends: dict[int, int] = {}
    ancestors: list[tuple[int, int]] = []
    fence_char: str | None = None
    fence_length = 0
    for line_number, line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(line)
        if fence_char is not None:
            if _closes_fence(line, fence_char, fence_length):
                fence_char = None
                fence_length = 0
            continue
        if fence is not None:
            marker = fence.group("fence")
            fence_char = marker[0]
            fence_length = len(marker)
            continue
        heading = _HEADING_RE.match(line)
        if heading is None:
            continue
        level = len(heading.group(1))
        while ancestors and ancestors[-1][0] >= level:
            _, previous = ancestors.pop()
            ends[previous] = line_number - 1
        parent = ancestors[-1][1] if ancestors else None
        headings.append((heading.group(2).strip(), level, line_number, parent))
        ancestors.append((level, line_number))
    ends.update((line, len(lines)) for _, line in ancestors)
    candidates: list[SemanticBlockCandidate] = []
    for title, level, line, ancestor in headings:
        end = ends[line]
        metadata, values, body_lines = _split_metadata(
            list(enumerate(lines[line:end], start=line + 1))
        )
        body = "\n".join(body_lines).strip()
        candidates.append(SemanticBlockCandidate(
            title=title, level=level, line=line, end_line=end, ancestor_line=ancestor,
            body=body, metadata=metadata, relations=_relation_candidates(values),
            substantive_body=_has_substantive_body(body),
        ))
    return tuple(candidates)


def interpret_semantic_blocks(
    candidates: tuple[SemanticBlockCandidate, ...],
    *,
    validate: bool = True,
    registry: relation_registry.RelationRegistry | None = None,
    kind_resolver: Callable[[str], str | SemanticBlockKindResolution | None] | None = None,
) -> SemanticBlockDocument:
    """Recognize one selected vocabulary without rescanning heading structure."""
    blocks: list[SemanticBlock] = []
    errors: list[SemanticBlockValidationError] = []
    warnings: list[SemanticBlockValidationError] = []
    suppressed_through = 0
    for candidate in candidates:
        if candidate.line <= suppressed_through or candidate.level <= _TITLE_HEADING_LEVEL:
            continue
        block_type, findings = _resolve_block_type(candidate.title, resolver=kind_resolver)
        if validate:
            warnings.extend(SemanticBlockValidationError(
                code=finding.code, message=finding.message, line=candidate.line,
            ) for finding in findings)
        if block_type is None:
            continue
        # Recognition suppresses descendants even when an empty block emits no range.
        suppressed_through = candidate.end_line
        relations, block_errors = _parse_relations(
            candidate.relations, registry or relation_registry.core_registry()
        )
        block = SemanticBlock(
            type=block_type, title=candidate.title, level=candidate.level,
            line=candidate.line, end_line=candidate.end_line, body=candidate.body,
            metadata=dict(candidate.metadata), relations=relations,
        )
        if candidate.substantive_body:
            blocks.append(block)
        elif validate:
            errors.append(SemanticBlockValidationError(
                code="empty_rich_unit", message="rich semantic-unit body is empty",
                line=candidate.line, block_id=block.id,
            ))
        if validate:
            errors.extend(block_errors)
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


def _relation_candidates(values: list[tuple[str, int]]) -> tuple[SemanticRelationCandidate, ...]:
    candidates: list[SemanticRelationCandidate] = []
    for value, line in values:
        for entry in _split_relation_entries(value) or [""]:
            raw_kind, colon, target = entry.partition(":")
            candidates.append(SemanticRelationCandidate(
                raw_kind=raw_kind.strip(), kind=normalize_label(raw_kind),
                target=target.strip(), raw=entry, line=line, has_colon=bool(colon),
            ))
    return tuple(candidates)


def _parse_relations(
    candidates: tuple[SemanticRelationCandidate, ...],
    registry: relation_registry.RelationRegistry,
) -> tuple[list[SemanticRelation], list[SemanticBlockValidationError]]:
    relations: list[SemanticRelation] = []
    errors: list[SemanticBlockValidationError] = []
    for candidate in candidates:
        if not candidate.has_colon:
            errors.append(SemanticBlockValidationError(
                code="malformed_relation",
                message=(f"malformed relation entry: {candidate.raw}" if candidate.raw
                         else "relations metadata must contain relation: target entries"),
                line=candidate.line,
            ))
            continue
        resolution = registry.resolve(candidate.kind, origin="semantic_relation")
        if resolution.canonical is None:
            errors.append(SemanticBlockValidationError(
                code="unsupported_relation", message=f"unsupported relation: {candidate.raw_kind}",
                line=candidate.line,
            ))
        if not candidate.target:
            errors.append(SemanticBlockValidationError(
                code="malformed_relation", message=f"relation {candidate.kind} is missing a target",
                line=candidate.line,
            ))
            continue
        relations.append(SemanticRelation(
            kind=candidate.kind, target=candidate.target, raw=candidate.raw, line=candidate.line,
        ))
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
