"""Deterministic Markdown-visible note relation parsing.

Canonical note-level edges live under a ``Relations`` heading as one
``- relation_type [[Target]]`` bullet per edge. Legacy typed bullets elsewhere
remain readable by the graph index, but only the canonical section is validated
as an authoring contract.
"""

from __future__ import annotations

import re
from collections.abc import Set
from dataclasses import dataclass

from . import relation_registry

RELATION_TYPES: frozenset[str] = relation_registry.core_registry().keys

_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_CANONICAL_RE = re.compile(
    r"^\s*[-*+]\s+(?P<rel>[a-z][a-z0-9_.-]{1,80})[ \t]+"
    r"(?P<link>\[\[[^\[\]\n]+\]\])\s*$"
)
_LEGACY_RE = re.compile(
    r"^\s*[-*+]\s+(?P<rel>[a-z][a-z0-9_.-]{1,80})\s*(?P<colon>:?)[ \t]+"
    r"(?P<link>\[\[[^\]\n]+\]\])",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")


@dataclass(frozen=True)
class MarkdownRelation:
    kind: str
    target: str
    raw: str
    line: int
    canonical: bool


@dataclass(frozen=True)
class RelationValidationError:
    code: str
    message: str
    line: int
    raw: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class MarkdownRelationDocument:
    relations: list[MarkdownRelation]
    errors: list[RelationValidationError]
    canonical_section_present: bool = False
    canonical_bullet_count: int = 0

    @property
    def canonical_relations(self) -> list[MarkdownRelation]:
        return [relation for relation in self.relations if relation.canonical]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def parse_markdown_relations(
    markdown: str,
    *,
    include_legacy: bool = False,
    relation_types: Set[str] | None = None,
    retain_unknown: bool = False,
) -> MarkdownRelationDocument:
    """Parse canonical note relations and optionally legacy typed bullets.

    Canonical validation applies only inside a ``Relations`` section. Ordinary
    prose and bullets elsewhere stay ordinary Markdown rather than becoming
    accidental schema errors.
    """
    relations: list[MarkdownRelation] = []
    errors: list[RelationValidationError] = []
    in_fence = False
    relations_level: int | None = None
    canonical_section_present = False
    canonical_bullet_count = 0
    allowed_relations = RELATION_TYPES if relation_types is None else relation_types

    for line_no, line in enumerate(markdown.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group("marks"))
            title = _normalize_heading(heading.group("title"))
            if title == "relations":
                relations_level = level
                canonical_section_present = True
            elif relations_level is not None and level <= relations_level:
                relations_level = None
            continue

        canonical = relations_level is not None
        if canonical and _BULLET_RE.match(line):
            canonical_bullet_count += 1
        match = _CANONICAL_RE.match(line) if canonical else None
        if match is None and include_legacy and not canonical:
            match = _LEGACY_RE.match(line)
        if match is None:
            if canonical and _BULLET_RE.match(line):
                errors.append(
                    RelationValidationError(
                        code="malformed_relation",
                        message=(
                            "relation bullets must be `- relation_type [[Target]]` "
                            "with a lower snake_case relation type"
                        ),
                        line=line_no,
                        raw=line.strip(),
                    )
                )
            continue

        raw_kind = match.group("rel")
        kind = raw_kind.lower().replace("-", "_")
        if canonical and raw_kind != kind:
            errors.append(
                RelationValidationError(
                    code="malformed_relation",
                    message=f"relation type must be lower snake_case: {raw_kind}",
                    line=line_no,
                    raw=line.strip(),
                )
            )
            continue
        if kind not in allowed_relations:
            if canonical:
                errors.append(
                    RelationValidationError(
                        code="unsupported_relation",
                        message=f"unsupported relation type: {kind}",
                        line=line_no,
                        raw=line.strip(),
                    )
                )
            if not retain_unknown:
                continue
            if not canonical and not match.groupdict().get("colon"):
                continue

        target = match.group("link")[2:-2].split("|", 1)[0].strip()
        if not target:
            if canonical:
                errors.append(
                    RelationValidationError(
                        code="malformed_relation",
                        message=f"relation {kind} is missing a target",
                        line=line_no,
                        raw=line.strip(),
                    )
                )
            continue
        relations.append(
            MarkdownRelation(
                kind=kind,
                target=target,
                raw=line.strip(),
                line=line_no,
                canonical=canonical,
            )
        )

    return MarkdownRelationDocument(
        relations=relations,
        errors=errors,
        canonical_section_present=canonical_section_present,
        canonical_bullet_count=canonical_bullet_count,
    )


def _normalize_heading(value: str) -> str:
    return re.sub(r"[\s_-]+", "_", value.strip().lower()).strip("_")
