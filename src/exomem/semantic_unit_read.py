"""Exact, bounded reads for first-class semantic-unit references."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import semantic_index, semantic_units, vault
from .get_page import GetResult

PARENT_CONTEXT_MAX_CHARS = 2400


@dataclass(frozen=True, slots=True)
class SemanticUnitParentCitation:
    path: str
    ref: str | None
    title: str
    page_type: str | None
    status: str | None
    updated: str
    superseded_by: tuple[str, ...]
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ref": self.ref,
            "title": self.title,
            "type": self.page_type,
            "status": self.status,
            "updated": self.updated,
            "superseded_by": list(self.superseded_by),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class SemanticUnitParentContext:
    markdown: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    truncated_before: bool
    truncated_after: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "truncated_before": self.truncated_before,
            "truncated_after": self.truncated_after,
        }


@dataclass(frozen=True, slots=True)
class SemanticUnitReadResponse:
    status: str
    unit_ref: str
    parent: SemanticUnitParentCitation
    unit: semantic_units.SemanticUnit | None = None
    parent_context: SemanticUnitParentContext | None = None
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "unit_ref": self.unit_ref,
            "parent": self.parent.as_dict(),
        }
        if self.unit is not None:
            out["unit"] = self.unit.to_dict()
        if self.parent_context is not None:
            out["parent_context"] = self.parent_context.as_dict()
        if self.expected_fingerprint is not None:
            out["expected_fingerprint"] = self.expected_fingerprint
        if self.actual_fingerprint is not None:
            out["actual_fingerprint"] = self.actual_fingerprint
        return out


def read_semantic_unit(
    vault_root: Path,
    *,
    page: GetResult,
    unit_ref: str,
) -> SemanticUnitReadResponse:
    """Resolve one current unit exactly; never substitute a nearby unit."""
    state = semantic_index.current_parent_index_state(
        vault_root,
        page.path,
        source=page.content,
    )
    frontmatter, body, _ = vault.parse_frontmatter(page.content)
    resolution = state.document.resolve_unit(unit_ref)
    parent = _parent_citation(
        page,
        state.document.parent_ref,
        frontmatter=frontmatter,
    )
    if resolution.unit is None:
        return SemanticUnitReadResponse(
            status=resolution.status,
            unit_ref=resolution.unit_ref,
            parent=parent,
            expected_fingerprint=resolution.expected_fingerprint,
            actual_fingerprint=resolution.actual_fingerprint,
        )

    status = "superseded" if parent.status == "superseded" else "found"
    return SemanticUnitReadResponse(
        status=status,
        unit_ref=resolution.unit_ref,
        parent=parent,
        unit=resolution.unit,
        parent_context=_bounded_parent_context(body, resolution.unit.span),
        expected_fingerprint=resolution.expected_fingerprint,
        actual_fingerprint=resolution.actual_fingerprint,
    )


def _parent_citation(
    page: GetResult,
    parsed_parent_ref: str | None,
    *,
    frontmatter: dict[str, Any],
) -> SemanticUnitParentCitation:
    return SemanticUnitParentCitation(
        path=page.path,
        ref=parsed_parent_ref,
        title=str(frontmatter.get("title") or page.path),
        page_type=_optional_text(frontmatter.get("type")),
        status=_optional_text(frontmatter.get("status")),
        updated=_date_text(frontmatter.get("updated")),
        superseded_by=tuple(_string_list(frontmatter.get("superseded_by"))),
        content_hash=page.content_hash,
    )


def _bounded_parent_context(
    body: str,
    span: semantic_units.SourceSpan,
) -> SemanticUnitParentContext:
    length = len(body)
    unit_start = min(max(0, span.start_offset), length)
    unit_end = min(max(unit_start, span.end_offset), length)
    unit_length = unit_end - unit_start

    if length <= PARENT_CONTEXT_MAX_CHARS:
        start, end = 0, length
    elif unit_length >= PARENT_CONTEXT_MAX_CHARS:
        start = unit_start
        end = min(length, start + PARENT_CONTEXT_MAX_CHARS)
    else:
        remaining = PARENT_CONTEXT_MAX_CHARS - unit_length
        start = max(0, unit_start - remaining // 2)
        end = min(length, unit_end + (remaining - (unit_start - start)))
        if end - start < PARENT_CONTEXT_MAX_CHARS:
            start = max(0, end - PARENT_CONTEXT_MAX_CHARS)

    markdown = body[start:end]
    start_line = body.count("\n", 0, start) + 1
    end_line = start_line + markdown.count("\n")
    return SemanticUnitParentContext(
        markdown=markdown,
        start_offset=start,
        end_offset=end,
        start_line=start_line,
        end_line=end_line,
        truncated_before=start > 0,
        truncated_after=end < length,
    )


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]
