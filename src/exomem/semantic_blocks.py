"""Markdown-readable semantic blocks for Exomem notes.

This module is deliberately small: normal ATX headings name semantic blocks,
optional leading ``- key: value`` bullets carry metadata, and the rest of the
section remains plain Markdown. It performs deterministic parsing and
validation only; no model, sidecar, or graph store is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
    }
)

RELATION_TYPES: frozenset[str] = frozenset(
    {
        "supports",
        "contradicts",
        "refines",
        "supersedes",
        "derived_from",
        "depends_on",
        "evidenced_by",
        "used_for",
        "mitigates",
        "causes",
        "blocks",
        "resolves",
        "cites",
        "implements",
        "tests",
        "owns",
    }
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_METADATA_RE = re.compile(r"^\s*[-*+]\s+([A-Za-z0-9 _-]+):\s*(.*)$")
_NORMALIZE_RE = re.compile(r"[\s-]+")


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
        normalized = normalize_label(block_type)
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


def parse_semantic_blocks(markdown: str, *, validate: bool = True) -> SemanticBlockDocument:
    """Parse semantic blocks from Markdown.

    Unknown headings are treated as normal Markdown structure. A recognized
    semantic heading starts a block and the block ends at the next non-fenced
    ATX heading. Leading metadata bullets are removed from the block body.
    """
    lines = (markdown or "").splitlines()
    blocks: list[SemanticBlock] = []
    errors: list[SemanticBlockValidationError] = []
    current: tuple[str, str, int, int, list[tuple[int, str]]] | None = None
    in_fence = False

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
        )
        blocks.append(block)
        if validate:
            errors.extend(block_errors)
        current = None

    for line_number, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            if current is not None:
                current[4].append((line_number, line))
            in_fence = not in_fence
            continue

        heading = _HEADING_RE.match(line)
        if heading and not in_fence:
            flush(line_number - 1)
            title = heading.group(2).strip()
            block_type = normalize_label(title)
            if block_type in BLOCK_TYPES:
                current = (block_type, title, len(heading.group(1)), line_number, [])
            continue

        if current is not None:
            current[4].append((line_number, line))

    flush(len(lines))

    warnings = _duplicate_id_warnings(blocks) if validate else []
    return SemanticBlockDocument(blocks=blocks, errors=errors, warnings=warnings)


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
) -> tuple[SemanticBlock, list[SemanticBlockValidationError]]:
    metadata, relation_values, body_lines = _split_metadata(lines)
    relations, errors = _parse_relations(relation_values)
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
            if kind not in RELATION_TYPES:
                errors.append(
                    SemanticBlockValidationError(
                        code="unsupported_relation",
                        message=f"unsupported relation: {raw_kind.strip()}",
                        line=line_number,
                    )
                )
                continue
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
