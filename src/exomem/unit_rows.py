"""The one builder of a page's unit rows, derived from page bytes.

OpenSpec make-vault-store-canonical D10, task 5.1. A page's registry-independent structural
candidates become one `UnitRow` each, keyed by the page path and the occurrence key. The lexical
`semantic_units` rows and the graph's candidate nodes are projections of those rows, so each rule
that decides a field lives here or in the projection that names it: `lexical_columns` below,
`epistemic_graph._candidate_node`. The existing page summary supplies positions and identity facts.

A row holds no bound public `unit_ref`. Which candidates emit, and so which anonymous unit binds
occurrence 1 or 2, depends on the selected registries, so a reader binds the reference when it
selects a page's units. The row's own facts derive from the bytes alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .semantic_index import SemanticParentIndexState
    from .semantic_units import SemanticUnit


@dataclass(frozen=True, slots=True)
class UnitRow:
    """One structural occurrence of a page, with every fact that its projections read.

    The page's own flags (`in_kb`, `in_vault`, `updated`) are not here: they come with the page's
    row. The attribute names that the graph shares with `SemanticUnit` (`form`, `anchor`, `title`,
    `level`, `line`, `tags`, `context`, `metadata`) mean the same there, so one graph rule reads
    either.
    """

    path: str
    occurrence_key: str
    ordinal: int
    form: str
    line: int
    end_line: int
    unit_source_hash: str
    parent_ref: str | None
    parent_generation: str
    parent_source_hash: str
    parser_version: int
    anchor: str | None
    title: str | None
    level: int | None
    category_raw: str
    category_key: str
    category: str
    kind_key: str
    content: str
    tags: tuple[str, ...]
    context: str | None
    metadata: Mapping[str, Any]
    verdict: str | None
    check_by_day: str | None
    check_by_json: str | None
    tags_canonical_json: str | None

    @property
    def tags_json(self) -> str:
        return json.dumps(list(self.tags), ensure_ascii=False, separators=(",", ":"))

    @property
    def row_keys(self) -> tuple[str, ...]:
        """The governed metadata keys that the author wrote, for `unit_row_keys`."""
        from . import semantic_units

        return tuple(
            key for key in semantic_units.GOVERNED_UNIT_METADATA_KEYS if key in self.metadata
        )


def build(
    rel: str, state: SemanticParentIndexState, *,
    candidates: Sequence[SemanticUnit] | None = None,
) -> tuple[UnitRow, ...]:
    """The unit rows of one page's parent state, in source order.

    The state must hold structural candidates.
    """
    from . import lexstore, semantic_language_registry, semantic_units

    rows = []
    if candidates is None:
        candidates = semantic_units.candidate_units(state.candidates)
    for ordinal, unit in enumerate(candidates):
        key = semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash)
        verdict, check_by_day, check_by_json, tags_canonical_json = (
            lexstore.unit_eligibility_columns(unit)
        )
        rows.append(UnitRow(
            path=rel, occurrence_key=key, ordinal=ordinal, form=unit.form,
            line=unit.line, end_line=unit.end_line,
            unit_source_hash=unit.source_hash, parent_ref=state.parent_ref,
            parent_generation=state.parent_generation, parent_source_hash=state.parent_source_hash,
            parser_version=state.parser_version, anchor=unit.anchor, title=unit.title,
            level=unit.level,
            category_raw=unit.category_raw, category_key=unit.category_key,
            category=semantic_language_registry.normalize_label(unit.category_raw),
            kind_key=unit.kind_key, content=unit.content, tags=tuple(unit.tags),
            context=unit.context,
            metadata=dict(unit.metadata), verdict=verdict, check_by_day=check_by_day,
            check_by_json=check_by_json, tags_canonical_json=tags_canonical_json,
        ))
    return tuple(rows)


def lexical_columns(row: UnitRow, page: Mapping[str, Any]) -> dict[str, Any]:
    """The `semantic_units` row of ``row``, keyed by column.

    ``page`` carries `updated`, `in_kb` and `in_vault`. The lexical row keeps the occurrence key in
    `unit_ref` and never a fingerprint: a public reference is bound when a reader selects a page's
    units.
    """
    return {
        "record_type": "structural_occurrence",
        "unit_ref": row.occurrence_key,
        "parent_path": row.path,
        "parent_ref": row.parent_ref,
        "parent_generation": row.parent_generation,
        "parent_source_hash": row.parent_source_hash,
        "parser_version": row.parser_version,
        "form": row.form,
        "category_raw": row.category_raw,
        "category_key": row.category_key,
        "category": row.category,
        "kind": row.kind_key,
        "content": row.content,
        "tags_json": row.tags_json,
        "context": row.context,
        "unit_source_hash": row.unit_source_hash,
        "anchor": row.anchor,
        "line": row.line,
        "end_line": row.end_line,
        "fingerprint": None,
        "source_order": row.ordinal,
        "updated": page["updated"],
        "in_kb": page["in_kb"],
        "in_vault": page["in_vault"],
        "verdict": row.verdict,
        "check_by_day": row.check_by_day,
        "check_by_json": row.check_by_json,
        "tags_canonical_json": row.tags_canonical_json,
    }
