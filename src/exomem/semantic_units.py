"""Deterministic parsing for compact and rich semantic units.

Markdown remains the source of truth.  This module only normalizes authored
syntax and composes the existing semantic-block parser; it performs no I/O,
registry mutation, indexing, or model work.
"""

from __future__ import annotations

import datetime as dt
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
_ANCHOR_ID = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
_ANCHOR_RE = re.compile(rf"(?:^|[ \t])\^(?P<anchor>{_ANCHOR_ID})$")
_UNIT_IDENTITY_RE = re.compile(r"unit-[0-9a-f]{64}")
_TRAILING_TAG_RE = re.compile(r"(?:^|[ \t])#(?P<tag>[^\s#]+)$")
_RICH_METADATA_RE = re.compile(
    r"^\s*[-*+]\s+(?P<key>[A-Za-z0-9 _-]+)\s*:", re.IGNORECASE
)
_TASK_LABELS = frozenset({"", " ", "x", "X", "-"})
_IDENTITY_SCHEMA = "exomem.semantic-unit.identity.v1"
# This version describes retained parser facts, independently of selected vocabulary.
STRUCTURAL_FORMAT = 1

#: The one closed vocabulary for how a claim about the world turned out.
#:
#: A unit's ``verdict`` and an experiment page's ``outcome`` draw from this same
#: set, so a reader never translates between the two altitudes. It is
#: deliberately categorical: Exomem stores no numeric confidence, credence, or
#: probability, and a verdict is lifecycle state rather than a score. Refuted is
#: not superseded — a refuted claim keeps active standing and full rank, because
#: a negative result is knowledge, not replaced knowledge.
EPISTEMIC_OUTCOMES: tuple[str, ...] = (
    "abandoned",
    "confirmed",
    "inconclusive",
    "qualified",
    "refuted",
)

#: Governed rich unit-metadata keys this module parses, validates, and projects.
GOVERNED_UNIT_METADATA_KEYS: tuple[str, ...] = ("verdict", "check_by")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    registry_namespace: str | None = None
    registry_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "span": self.span.to_dict() if self.span is not None else None,
            "line": self.line,
            "raw": self.raw,
            "remediation": self.remediation,
            "severity": self.severity,
        }
        if self.registry_namespace is not None and self.registry_key is not None:
            out["registry"] = {
                "namespace": self.registry_namespace,
                "key": self.registry_key,
            }
        return out


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
    # Governed metadata, normalized off `metadata` the same way `tags` and
    # `context` already are, so no downstream consumer re-parses a raw row.
    verdict: str | None = None
    check_by: str | None = None

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
            # Emitted as null when absent, unlike `structured_filters.unit_view`
            # and the hit serializers, which omit them. The difference is
            # deliberate and follows what each shape is for: this is the
            # complete-unit projection, where every field is always present and
            # a reader indexes it positionally, so a disappearing key would be
            # a shape change. The other two are presence-sensitive — `$exists`
            # has to be able to distinguish "no verdict yet" from any value, and
            # a hit omits what it has nothing to say about.
            "verdict": self.verdict,
            "check_by": self.check_by,
        }


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

    def resolve_fragment(self, fragment: str) -> SemanticUnitResolution:
        """Resolve a `[[Page#fragment]]` fragment to one unit of this page.

        The fragment is an authored anchor (`#id` or the block-reference form
        `#^id`) or a `unit-<fingerprint>` identity, so it takes exactly the path
        an exact `unit_ref` takes. A page with no parent reference addresses
        nothing.
        """
        if not self.parent_ref:
            return SemanticUnitResolution(status="missing", unit_ref="")
        return self.resolve_unit(_anchored_unit_ref(self.parent_ref, fragment.removeprefix("^")))

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
        parent_ref, separator, fragment = requested.rpartition("#")
        if (
            separator
            and parent_ref == self.parent_ref
            and re.fullmatch(r"unit-([0-9a-f]{64})", fragment)
        ):
            return SemanticUnitResolution(
                status="stale",
                unit_ref=requested,
                expected_fingerprint=fragment.removeprefix("unit-"),
            )
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


@dataclass(frozen=True, slots=True)
class RichUnitCandidate:
    """A source heading and category syntax, without selected unit identity."""

    heading: semantic_blocks.SemanticBlockCandidate
    span: SourceSpan
    source_hash: str
    category_raw: str | None
    category_key: str | None
    category_valid: bool | None


@dataclass(frozen=True, slots=True)
class SemanticUnitCandidates:
    """Lossless parser inputs retained before rich recognition and suppression."""

    rich: tuple[RichUnitCandidate, ...]
    compact: tuple[SemanticUnit, ...]
    compact_errors: tuple[SemanticUnitDiagnostic, ...]
    note_relations: markdown_relations.MarkdownRelationCandidates


@dataclass(frozen=True, slots=True)
class StructuralOccurrence:
    """An internal source occurrence; it has no selected public unit identity."""

    key: str
    form: str
    span: SourceSpan
    source_hash: str
    content: str
    category_raw: str | None
    category_key: str | None
    kind_raw: str | None
    anchor: str | None
    metadata: Mapping[str, Any]


def occurrence_key(form: str, span: SourceSpan, source_hash: str) -> str:
    """Bind an internal occurrence to parser-owned coordinates and exact source."""
    # This closed protocol distinguishes candidate identity from public unit refs.
    payload = [STRUCTURAL_FORMAT, form, span.start_offset, span.end_offset, source_hash]
    return "occurrence:" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def structural_occurrences(candidates: SemanticUnitCandidates) -> tuple[StructuralOccurrence, ...]:
    """Project possible units once for the existing lexical, graph and vector owners."""
    rich = (
        StructuralOccurrence(
            occurrence_key("rich", item.span, item.source_hash), "rich", item.span,
            item.source_hash, item.heading.body, item.category_raw, item.category_key,
            item.heading.title, item.heading.metadata.get("id"), item.heading.metadata,
        ) for item in candidates.rich
        # The parser never emits a level-one or non-substantive rich unit.
        if item.heading.level > 1 and item.heading.substantive_body
    )
    compact = (
        StructuralOccurrence(
            occurrence_key(item.form, item.span, item.source_hash), item.form, item.span,
            item.source_hash, item.content, item.category_raw, item.category_key,
            item.kind_raw, item.anchor, item.metadata,
        ) for item in candidates.compact
    )
    return tuple(sorted((*rich, *compact), key=lambda item: (item.span.start_offset, item.form)))


@dataclass(frozen=True, slots=True)
class UnitStructure:
    """Selected summary facts and public identity; source text is absent."""

    form: str
    kind: str
    category: str
    anchor: str | None
    line: int
    end_line: int
    source_hash: str
    relations: tuple[SemanticRelation, ...] = ()
    occurrence_key: str = ""
    start_offset: int = 0
    unit_ref: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class SelectedStructure:
    """One page's selected interpretation of its stored structural candidates.

    `complete` is False when the page's selected definitions were unavailable:
    only units whose every ancestor heading is core-recognized remain, so a
    reader reports dependent coverage as incomplete, never as exact.
    """

    units: tuple[UnitStructure, ...]
    note_relations: tuple[markdown_relations.MarkdownRelation, ...]
    parent_ref: str | None = None
    complete: bool = True

    @property
    def rich_units(self) -> tuple[UnitStructure, ...]:
        return tuple(unit for unit in self.units if unit.form == "rich")

    def unit(self, key: str) -> UnitStructure | None:
        return next((unit for unit in self.units if unit.occurrence_key == key), None)


def _candidate_rich_unit(item: RichUnitCandidate) -> SemanticUnit:
    """The unit a recognized candidate would emit, for selection-free identity facts."""
    relations, _ = semantic_blocks._parse_relations(item.heading.relations, _core_relations())
    block = semantic_blocks.SemanticBlock(
        type="", title=item.heading.title, level=item.heading.level, line=item.heading.line,
        end_line=item.heading.end_line, body=item.heading.body,
        metadata=dict(item.heading.metadata), relations=relations,
    )
    unit, _ = _rich_unit(block, span=item.span, path="", line_by_number={})
    return unit


def candidate_units(candidates: SemanticUnitCandidates) -> tuple[SemanticUnit, ...]:
    """Every unit some selected interpretation could emit, unbound and kind-free.

    Rich candidates carry an empty kind: only selected recognition names it.
    """
    rich = [
        _candidate_rich_unit(item) for item in candidates.rich
        # The parser never emits a level-one or non-substantive rich unit.
        if item.heading.level > 1 and item.heading.substantive_body
    ]
    return tuple(sorted((*rich, *candidates.compact), key=lambda unit: (unit.span.start_offset, unit.form)))


def _core_relations() -> RelationRegistry:
    from . import relation_registry

    return relation_registry.core_registry()


def _identity_facts(units: list[SemanticUnit]) -> dict[str, dict[str, Any]]:
    """Fingerprints for every occurrence number a selected interpretation can bind.

    Unit fingerprints never depend on selected vocabulary; only which candidates
    emit does. An anonymous candidate preceded by m emittable candidates with an
    identical signature binds occurrence 1..m+1, so storing those fingerprints
    lets the shared binder reproduce the parser's public refs from metadata.
    """
    facts: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}
    for unit in sorted(units, key=lambda item: (item.span.start_offset, item.form)):
        signature = hashlib.sha256(
            _stable_json(_semantic_unit_signature(unit)).encode("utf-8")
        ).hexdigest()
        earlier = seen.get(signature, 0)
        seen[signature] = earlier + 1
        fingerprints = (
            [fingerprint_semantic_unit(unit)] if unit.anchor else
            [fingerprint_semantic_unit(unit, occurrence=number) for number in range(1, earlier + 2)]
        )
        facts[occurrence_key(unit.form, unit.span, unit.source_hash)] = {
            "signature": signature, "fingerprints": fingerprints,
        }
    return facts


def structural_summary(candidates: SemanticUnitCandidates) -> dict[str, Any]:
    """Serialize parser facts, without copying the parent body into metadata."""
    identity = _identity_facts(list(candidate_units(candidates)))
    # These fields are the closed structural-summary protocol, not authored vocabulary.
    return {
        "format": STRUCTURAL_FORMAT,
        "rich": [
            {
                "key": (key := occurrence_key("rich", item.span, item.source_hash)),
                "title": item.heading.title, "level": item.heading.level,
                "line": item.heading.line, "end_line": item.heading.end_line,
                "ancestor_line": item.heading.ancestor_line,
                "metadata": dict(item.heading.metadata),
                "substantive_body": item.heading.substantive_body,
                "relations": [vars(relation) for relation in item.heading.relations],
                "source_hash": item.source_hash,
                "start_offset": item.span.start_offset, "end_offset": item.span.end_offset,
                "identity": identity.get(key),
            }
            for item in candidates.rich
        ],
        "compact": [
            {"key": (key := occurrence_key(item.form, item.span, item.source_hash)),
             "category_raw": item.category_raw, "category_key": item.category_key,
             "anchor": item.anchor, "line": item.line, "end_line": item.end_line,
             "source_hash": item.source_hash,
             "start_offset": item.span.start_offset, "end_offset": item.span.end_offset,
             "identity": identity[key]}
            for item in candidates.compact
        ],
        "notes": [vars(item) for item in candidates.note_relations.candidates],
        "canonical_section_present": candidates.note_relations.canonical_section_present,
        "canonical_bullet_count": candidates.note_relations.canonical_bullet_count,
    }


def interpret_structural_summary(
    summary: Mapping[str, Any], *,
    language_registry: semantic_language_registry.LanguageRegistryView,
    relation_registry: RelationRegistry,
    project: str | None = None, page_type: str | None = None,
    parent_ref: str | None = None, path: str = "", definitions_available: bool = True,
) -> SelectedStructure:
    """Apply the selected parsers' control flow without reading Markdown bodies.

    Without the page's selected definitions the caller passes the core
    adapters and `definitions_available=False`; only core-safe units remain.
    """
    if summary.get("format") != STRUCTURAL_FORMAT:
        raise ValueError("SEMANTIC_STRUCTURE_UNAVAILABLE")
    parent_ref = _effective_parent_ref(parent_ref, path)
    headings = tuple(semantic_blocks.SemanticBlockCandidate(
        title=item["title"], level=item["level"], line=item["line"], end_line=item["end_line"],
        ancestor_line=item["ancestor_line"], metadata=item["metadata"], body="",
        substantive_body=item["substantive_body"],
        relations=tuple(semantic_blocks.SemanticRelationCandidate(**row) for row in item["relations"]),
    ) for item in summary["rich"])
    rich = semantic_blocks.interpret_semantic_blocks(
        headings, validate=False, registry=relation_registry,
        kind_resolver=lambda title: language_registry.resolve_heading(
            title, project=project, page_type=page_type,
        ).resolved,
    )
    rich_rows = {item["line"]: item for item in summary["rich"]}
    emitted: list[tuple[Mapping[str, Any], str, str, str | None, tuple[SemanticRelation, ...]]] = []
    for block in rich.blocks:
        raw, key, _ = _rich_category(block, path="", line_by_number={})
        category, _ = _selected_category(
            raw, key, form="rich", kind=block.type, explicit="category" in block.metadata,
            language=language_registry, project=project, page_type=page_type,
        )
        emitted.append((rich_rows[block.line], "rich", block.type, category, tuple(block.relations)))
    ranges = tuple((block.line, block.end_line) for block in rich.blocks)
    for item in _outside_rich_ranges(summary["compact"], ranges, line=lambda row: row["line"]):
        category, _ = _selected_category(
            item["category_raw"], item["category_key"], form="compact", kind="observation",
            explicit=True, language=language_registry, project=project, page_type=page_type,
        )
        emitted.append((item, "compact", "observation", category, ()))
    emitted.sort(key=lambda entry: (entry[0]["start_offset"], entry[1]))
    if not definitions_available:
        safe = _core_safe_keys(summary, emitted)
        emitted = [entry for entry in emitted if entry[0]["key"] in safe]
    bindings = _bind_identities(
        [
            (
                row.get("anchor") if form == "compact" else row["metadata"].get("id"),
                row["identity"]["signature"],
                _stored_fingerprint(row["identity"]["fingerprints"]),
            )
            for row, form, *_ in emitted
        ],
        parent_ref=parent_ref,
    )
    units = tuple(
        UnitStructure(
            form, kind, category,
            row.get("anchor") if form == "compact" else row["metadata"].get("id"),
            row["line"], row["end_line"], row["source_hash"], relations, row["key"],
            row["start_offset"], unit_ref, fingerprint,
        )
        for (row, form, kind, category, relations), (unit_ref, fingerprint, _) in zip(
            emitted, bindings, strict=True,
        )
    )
    notes = markdown_relations.interpret_markdown_relations(
        markdown_relations.MarkdownRelationCandidates(
            tuple(markdown_relations.MarkdownRelationCandidate(**item) for item in summary["notes"]),
            summary["canonical_section_present"], summary["canonical_bullet_count"],
        ),
        relation_types=relation_registry.keys | frozenset(relation_registry.aliases),
        # Without selected definitions only core relation meanings are known.
        retain_unknown=definitions_available,
    )
    return SelectedStructure(units, tuple(notes.relations), parent_ref, definitions_available)


def _stored_fingerprint(fingerprints: list[str]):
    def fingerprint_for(occurrence: int | None) -> str:
        index = 0 if occurrence is None else occurrence - 1
        if not 0 <= index < len(fingerprints):
            raise ValueError("SEMANTIC_STRUCTURE_UNAVAILABLE")
        return fingerprints[index]

    return fingerprint_for


def _core_safe_keys(summary: Mapping[str, Any], emitted: list[tuple]) -> frozenset[str]:
    """Core-emitted units that no extension block could suppress or re-identify.

    Extensions cannot redefine core, so only a heading that core does not
    recognize could become an extension block around a unit. Level-one headings
    never open a block in any instance. A unit sharing an anchor or an earlier
    identical signature with another candidate could bind a different public
    ref once that candidate's meaning is known, so it is withheld as well.
    """
    core = semantic_language_registry.core_registry()
    unknown_lines = {
        row["line"] for row in summary["rich"]
        if row["level"] > 1 and semantic_blocks._resolve_block_type(
            row["title"], resolver=lambda title: core.resolve_heading(title).resolved,
        )[0] is None
    }
    rows = {row["line"]: row for row in summary["rich"]}

    def enclosed_by_unknown(row: Mapping[str, Any], form: str) -> bool:
        if form == "compact":
            return any(
                heading["line"] < row["line"] <= heading["end_line"] and heading["line"] in unknown_lines
                for heading in summary["rich"]
            )
        ancestor = row["ancestor_line"]
        while ancestor is not None:
            if ancestor in unknown_lines:
                return True
            ancestor = rows[ancestor]["ancestor_line"]
        return False

    safe = {row["key"] for row, form, *_ in emitted if not enclosed_by_unknown(row, form)}
    candidates = [
        (row, "rich", row["metadata"].get("id")) for row in summary["rich"] if row.get("identity")
    ] + [(row, "compact", row.get("anchor")) for row in summary["compact"]]
    candidates.sort(key=lambda entry: (entry[0]["start_offset"], entry[1]))
    withheld: set[str] = set()
    for index, (row, _form, anchor) in enumerate(candidates):
        if row["key"] not in safe:
            continue
        others = [other for other in candidates if other[0]["key"] != row["key"]]
        if anchor and any(other_anchor == anchor and other[0]["key"] not in safe
                          for other in others for other_anchor in (other[2],)):
            withheld.add(row["key"])
        signature = row["identity"]["signature"]
        if not anchor and any(other[0]["key"] not in safe and not other[2]
                              and other[0]["identity"]["signature"] == signature
                              for other in candidates[:index]):
            withheld.add(row["key"])
    return frozenset(safe - withheld)


def core_safe_occurrences(candidates: SemanticUnitCandidates) -> frozenset[str]:
    """Occurrence keys served from a core parse while selected definitions are unavailable."""
    core = semantic_language_registry.core_registry()
    selected = interpret_structural_summary(
        structural_summary(candidates), language_registry=core,
        relation_registry=_core_relations(), definitions_available=False,
    )
    return frozenset(unit.occurrence_key for unit in selected.units)


def scan_semantic_units(
    markdown: str, *, path: str = "", include_legacy_relations: bool = True,
) -> SemanticUnitCandidates:
    """Extract structural candidates without consulting a vocabulary instance."""
    source = markdown or ""
    lines = _source_lines(source)
    line_by_number = {line.number: line for line in lines}
    rich: list[RichUnitCandidate] = []
    for heading in semantic_blocks.scan_semantic_blocks(source):
        span = _span_for_line_range(source, line_by_number, heading.line, heading.end_line)
        raw = heading.metadata.get("category")
        key, valid = None, None
        if raw is not None:
            try:
                key, valid = canonicalize_category(raw), True
            except ValueError:
                valid = False
        rich.append(RichUnitCandidate(heading, span, _source_hash(span.text), raw, key, valid))
    compact: list[SemanticUnit] = []
    errors: list[SemanticUnitDiagnostic] = []
    _parse_compact_units(lines, path=path, validate=True, units=compact, errors=errors)
    return SemanticUnitCandidates(
        tuple(rich), tuple(compact), tuple(errors),
        markdown_relations.scan_markdown_relations(source, include_legacy=include_legacy_relations),
    )


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
    language_registry: semantic_language_registry.LanguageRegistryView | None = None,
    relation_registry: RelationRegistry | None = None,
    include_legacy_relations: bool = False,
    retain_unknown_relations: bool = False,
    project: str | None = None,
    page_type: str | None = None,
    candidates: SemanticUnitCandidates | None = None,
) -> SemanticUnitDocument:
    """Parse compact observations and rich semantic blocks exactly once each."""
    source = markdown or ""
    source_path = str(path)
    effective_parent_ref = _effective_parent_ref(parent_ref, source_path)
    lines = _source_lines(source)
    line_by_number = {line.number: line for line in lines}
    if candidates is None:
        candidates = scan_semantic_units(
            source, path=source_path, include_legacy_relations=include_legacy_relations,
        )
    units: list[SemanticUnit] = []
    errors: list[SemanticUnitDiagnostic] = []
    warnings: list[SemanticUnitDiagnostic] = []
    kind_registry_findings: list[tuple[str, str, str]] = []
    if validate and language_registry is not None:
        for finding in language_registry.findings:
            namespace, key = _registry_identity(finding)
            diagnostic = _registry_diagnostic(
                finding,
                path=source_path,
                namespace=namespace,
                key=key,
            )
            (warnings if diagnostic.severity == "warning" else errors).append(diagnostic)

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
            registry_key = (
                resolution.definition.key
                if resolution.definition is not None
                else resolution.resolved or resolution.key
            )
            kind_registry_findings.extend(
                (finding["code"], "kinds", registry_key)
                for finding in resolution.findings
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

    rich_document = semantic_blocks.interpret_semantic_blocks(
        tuple(candidate.heading for candidate in candidates.rich),
        validate=validate,
        registry=relation_registry,
        kind_resolver=kind_resolver,
    )
    rich_ranges = tuple((block.line, block.end_line) for block in rich_document.blocks)
    units.extend(_outside_rich_ranges(candidates.compact, rich_ranges))
    if validate:
        errors.extend(_outside_rich_ranges(candidates.compact_errors, rich_ranges))
    note_relation_document = markdown_relations.interpret_markdown_relations(
        candidates.note_relations,
        relation_types=(
            relation_registry.keys | frozenset(relation_registry.aliases)
            if relation_registry is not None
            else None
        ),
        retain_unknown=retain_unknown_relations,
    )
    for block in rich_document.blocks:
        unit, unit_errors = _rich_unit(
            block,
            span=_span_for_line_range(source, line_by_number, block.line, block.end_line),
            path=source_path,
            line_by_number=line_by_number,
        )
        if validate:
            errors.extend(unit_errors)
        units.append(unit)

    if validate:
        errors.extend(
            _normalize_rich_diagnostics(
                rich_document.errors,
                path=source_path,
                line_by_number=line_by_number,
                severity="error",
            )
        )
        rich_warnings = _normalize_rich_diagnostics(
            [
                warning
                for warning in rich_document.warnings
                if warning.code != "duplicate_id"
            ],
            path=source_path,
            line_by_number=line_by_number,
            severity="warning",
        )
        warnings.extend(
            _attach_registry_identities(rich_warnings, kind_registry_findings)
        )

    if language_registry is not None:
        resolved_units: list[SemanticUnit] = []
        for unit in units:
            resolved_category, resolution = _selected_category(
                unit.category_raw, unit.category_key, form=unit.form, kind=unit.kind,
                explicit="category" in unit.metadata, language=language_registry,
                project=project, page_type=page_type,
            )
            resolved_units.append(replace(unit, category=resolved_category))
            if validate and resolution.status != "registry_invalid":
                for finding in resolution.findings:
                    diagnostic = _registry_diagnostic(
                        finding,
                        path=source_path,
                        unit=unit,
                        namespace="categories",
                        key=(
                            resolution.definition.key
                            if resolution.definition is not None
                            else resolution.resolved or resolution.key
                        ),
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


def _rich_unit(
    block: semantic_blocks.SemanticBlock,
    *,
    span: SourceSpan,
    path: str,
    line_by_number: dict[int, _SourceLine],
) -> tuple[SemanticUnit, list[SemanticUnitDiagnostic]]:
    """One rich unit from a recognized block; parsing and summaries share it."""
    category_raw, category_key, category_error = _rich_category(
        block, path=path, line_by_number=line_by_number,
    )
    rich_tags, tags_error = _rich_tags(block, path=path, line_by_number=line_by_number)
    rich_context, context_error = _rich_context(block, path=path, line_by_number=line_by_number)
    rich_verdict, verdict_error = _rich_verdict(block, path=path, line_by_number=line_by_number)
    rich_check_by, check_by_error = _rich_check_by(
        block, path=path, line_by_number=line_by_number,
    )
    unit = SemanticUnit(
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
        tags=rich_tags,
        context=rich_context,
        relations=tuple(block.relations),
        metadata=block.metadata,
        anchor=block.id,
        span=span,
        source_hash=_source_hash(span.text),
        title=block.title,
        level=block.level,
        body=block.body,
        verdict=rich_verdict,
        check_by=rich_check_by,
    )
    return unit, [
        error
        for error in (category_error, tags_error, context_error, verdict_error, check_by_error)
        if error is not None
    ]


def _selected_category(raw, key, *, form, kind, explicit, language, project, page_type):
    resolution = language.resolve_category(raw, project=project, page_type=page_type)
    category = resolution.resolved or key
    if form == "rich" and not explicit and resolution.status in {"unregistered", "registry_invalid"}:
        category = kind
    return category, resolution


def _registry_diagnostic(
    finding: Mapping[str, str],
    *,
    path: str,
    unit: SemanticUnit | None = None,
    namespace: str | None = None,
    key: str | None = None,
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
        registry_namespace=namespace,
        registry_key=key,
    )


def _registry_identity(finding: Mapping[str, str]) -> tuple[str, str]:
    path = str(finding.get("path", "registry"))
    parts = path.split(".")
    if len(parts) >= 2 and parts[0] in {"categories", "kinds"}:
        return parts[0], parts[1]
    return "semantic_language", path


def _attach_registry_identities(
    diagnostics: list[SemanticUnitDiagnostic],
    identities: list[tuple[str, str, str]],
) -> list[SemanticUnitDiagnostic]:
    remaining = list(identities)
    attached: list[SemanticUnitDiagnostic] = []
    for diagnostic in diagnostics:
        match_index = next(
            (
                index
                for index, (code, _namespace, _key) in enumerate(remaining)
                if code == diagnostic.code
            ),
            None,
        )
        if match_index is None:
            attached.append(diagnostic)
            continue
        _code, namespace, key = remaining.pop(match_index)
        attached.append(
            replace(
                diagnostic,
                registry_namespace=namespace,
                registry_key=key,
            )
        )
    return attached


def _effective_parent_ref(parent_ref: str | None, path: str) -> str | None:
    if parent_ref is None:
        legacy_ref = context_refs.vault_ref(path)
        return legacy_ref if legacy_ref != "exomem://vault/" else None
    parsed = memory_refs.parse_memory_ref(str(parent_ref))
    if parsed is None:
        raise ValueError("parent_ref must be a canonical exomem://memory/<uuid> reference")
    return memory_refs.memory_ref(parsed)


def _bind_identities(
    items: list[tuple[str | None, str, Any]], *, parent_ref: str | None,
) -> list[tuple[str | None, str, int | None]]:
    """The one public-identity rule for parsed documents and structural summaries.

    Each item is `(anchor, signature_key, fingerprint_for)`. Anchors bind
    directly unless another emitted unit shares them; anonymous units count
    identical signatures in source order.
    """
    anchors: dict[str, int] = {}
    for anchor, _signature, _fingerprint in items:
        if anchor:
            anchors[anchor] = anchors.get(anchor, 0) + 1
    occurrences: dict[str, int] = {}
    bound: list[tuple[str | None, str, int | None]] = []
    for anchor, signature, fingerprint_for in items:
        if anchor:
            unit_ref = (
                None
                if parent_ref is None or anchors[anchor] > 1
                else _anchored_unit_ref(parent_ref, anchor)
            )
            bound.append((unit_ref, fingerprint_for(None), None))
            continue
        occurrence = occurrences.get(signature, 0) + 1
        occurrences[signature] = occurrence
        fingerprint = fingerprint_for(occurrence)
        bound.append(
            (f"{parent_ref}#unit-{fingerprint}" if parent_ref is not None else None,
             fingerprint, occurrence)
        )
    return bound


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
    bindings = _bind_identities(
        [
            (
                unit.anchor,
                _stable_json(_semantic_unit_signature(unit)),
                lambda occurrence, unit=unit: fingerprint_semantic_unit(unit, occurrence=occurrence),
            )
            for unit in units
        ],
        parent_ref=parent_ref,
    )
    bound = [
        replace(unit, parent_ref=parent_ref, unit_ref=unit_ref, fingerprint=fingerprint,
                occurrence=occurrence)
        for unit, (unit_ref, fingerprint, occurrence) in zip(units, bindings, strict=True)
    ]

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


def is_unit_anchor_shaped(fragment: str) -> bool:
    """Whether `fragment` is written the way a unit is addressed, found or not.

    That is the block-reference form `^id`, a bare authored anchor, or a
    `unit-<fingerprint>` identity. Anything else (heading text, a phrase) does
    not claim to name a unit.
    """
    bare = fragment.removeprefix("^")
    return (
        fragment.startswith("^")
        or re.fullmatch(_ANCHOR_ID, bare) is not None
        or _UNIT_IDENTITY_RE.fullmatch(bare) is not None
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


def _outside_rich_ranges(items, ranges: tuple[tuple[int, int], ...], *, line=lambda item: item.line):
    """Keep ordered compact records only outside emitted rich ranges."""
    index = 0
    for item in items:
        while index < len(ranges) and line(item) > ranges[index][1]:
            index += 1
        if index == len(ranges) or line(item) < ranges[index][0]:
            yield item


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


def _rich_tags(
    block: semantic_blocks.SemanticBlock,
    *,
    path: str,
    line_by_number: dict[int, _SourceLine],
) -> tuple[tuple[str, ...], SemanticUnitDiagnostic | None]:
    raw = block.metadata.get("tags")
    if raw is None:
        return (), None
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        tag = unicodedata.normalize("NFKC", entry.strip()).casefold()
        if not _is_valid_tag(tag):
            return (), _invalid_rich_metadata(
                block,
                key="tags",
                path=path,
                line_by_number=line_by_number,
                message="invalid rich semantic-unit tags",
                remediation=(
                    "Use comma-separated 1-64 character tags beginning with a letter "
                    "or digit and containing only letters, digits, `_`, `-`, or `/`; "
                    "do not use `#`, empty path segments, or a trailing `/`."
                ),
            )
        if tag not in seen:
            seen.add(tag)
            normalized.append(tag)
    return tuple(normalized), None


def _rich_context(
    block: semantic_blocks.SemanticBlock,
    *,
    path: str,
    line_by_number: dict[int, _SourceLine],
) -> tuple[str | None, SemanticUnitDiagnostic | None]:
    raw = block.metadata.get("context")
    if raw is None:
        return None, None
    context = raw.strip()
    if not context or any(char in context for char in ("\r", "\n", "\v", "\f")):
        return None, _invalid_rich_metadata(
            block,
            key="context",
            path=path,
            line_by_number=line_by_number,
            message="invalid rich semantic-unit context",
            remediation="Use non-empty single-line Unicode context.",
        )
    return context, None


def _rich_verdict(
    block: semantic_blocks.SemanticBlock,
    *,
    path: str,
    line_by_number: dict[int, _SourceLine],
) -> tuple[str | None, SemanticUnitDiagnostic | None]:
    """Validate the governed categorical `verdict` row, never a number."""
    raw = block.metadata.get("verdict")
    if raw is None:
        return None, None
    verdict = unicodedata.normalize("NFKC", raw.strip()).casefold()
    if verdict not in EPISTEMIC_OUTCOMES:
        return None, _invalid_rich_metadata(
            block,
            key="verdict",
            path=path,
            line_by_number=line_by_number,
            message="invalid rich semantic-unit verdict",
            remediation=(
                "Use exactly one of "
                f"{', '.join(EPISTEMIC_OUTCOMES)}. A verdict is categorical "
                "lifecycle state; numeric confidence is not a stored field in "
                "this vault, so a score, probability, or percentage is never a "
                "valid verdict."
            ),
        )
    return verdict, None


def _rich_check_by(
    block: semantic_blocks.SemanticBlock,
    *,
    path: str,
    line_by_number: dict[int, _SourceLine],
) -> tuple[str | None, SemanticUnitDiagnostic | None]:
    """Validate the governed `check_by` row as a strict ISO calendar date."""
    raw = block.metadata.get("check_by")
    if raw is None:
        return None, None
    value = raw.strip()
    if _ISO_DATE_RE.fullmatch(value) is None:
        return None, _invalid_check_by(block, path, line_by_number)
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None, _invalid_check_by(block, path, line_by_number)
    if parsed.isoformat() != value:
        return None, _invalid_check_by(block, path, line_by_number)
    return value, None


def _invalid_check_by(
    block: semantic_blocks.SemanticBlock,
    path: str,
    line_by_number: dict[int, _SourceLine],
) -> SemanticUnitDiagnostic:
    return _invalid_rich_metadata(
        block,
        key="check_by",
        path=path,
        line_by_number=line_by_number,
        message="invalid rich semantic-unit check_by date",
        remediation=(
            "Use one exact ISO calendar date spelled YYYY-MM-DD. A check-by "
            "date names the day a claim should be revisited, so timestamps and "
            "abbreviated dates are not accepted."
        ),
    )


def _invalid_rich_metadata(
    block: semantic_blocks.SemanticBlock,
    *,
    key: str,
    path: str,
    line_by_number: dict[int, _SourceLine],
    message: str,
    remediation: str,
) -> SemanticUnitDiagnostic:
    source_line = _find_rich_metadata_line(block, key, line_by_number)
    return SemanticUnitDiagnostic(
        code=f"invalid_rich_{key}",
        message=message,
        path=path,
        span=_span_for_source_line(source_line) if source_line is not None else None,
        line=source_line.number if source_line is not None else block.line,
        raw=(source_line.text if source_line is not None else block.metadata.get(key, "")),
        remediation=remediation,
        severity="error",
    )


def _find_rich_category_line(
    block: semantic_blocks.SemanticBlock,
    line_by_number: dict[int, _SourceLine],
) -> _SourceLine | None:
    return _find_rich_metadata_line(block, "category", line_by_number)


def _find_rich_metadata_line(
    block: semantic_blocks.SemanticBlock,
    key: str,
    line_by_number: dict[int, _SourceLine],
) -> _SourceLine | None:
    for number in range(block.line + 1, block.end_line + 1):
        line = line_by_number.get(number)
        if line is None:
            continue
        match = _RICH_METADATA_RE.match(line.text)
        if match is not None and semantic_blocks.normalize_label(match.group("key")) == key:
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
    if code == "empty_rich_unit":
        return "Add substantive body content or remove the empty rich heading."
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
