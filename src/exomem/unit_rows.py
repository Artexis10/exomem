"""The one unit row set that a vault store holds, derived from page bytes.

OpenSpec make-vault-store-canonical D10, task 5.1. A page's registry-independent structural
candidates become one `UnitRow` each, keyed by the page path and the occurrence key. The lexical
`semantic_units` rows and the graph's candidate nodes are projections of that set, so each rule that
decides a field lives here or in the projection that names it: `lexical_columns` below,
`epistemic_graph._candidate_node`.

A row holds the identity facts of its unit and no bound public `unit_ref`. Which candidates emit,
and so which anonymous unit binds occurrence 1 or 2, depends on the selected registries, and an
overlay or a vocabulary pack change would leave a stored reference stale. The facts derive from the
bytes alone.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .semantic_index import SemanticParentIndexState

TABLE = "unit_rows"

_CREATE = (
    "CREATE TABLE IF NOT EXISTS unit_rows("
    # `path` and `occurrence_key` name the unit: the internal key of one parser occurrence.
    " path TEXT NOT NULL,"
    " occurrence_key TEXT NOT NULL,"
    " ordinal INTEGER NOT NULL,"
    " form TEXT NOT NULL,"
    # The parser's code-point offsets into the body after the frontmatter.
    " start_offset INTEGER NOT NULL,"
    " end_offset INTEGER NOT NULL,"
    " line INTEGER NOT NULL,"
    " end_line INTEGER NOT NULL,"
    " node_key TEXT NOT NULL,"
    " unit_source_hash TEXT NOT NULL,"
    " parent_ref TEXT,"
    " parent_generation TEXT NOT NULL,"
    " parent_source_hash TEXT NOT NULL,"
    " parser_version INTEGER NOT NULL,"
    # The authored anchor only: a rich unit's `id`, a compact unit's `^anchor`.
    " anchor TEXT,"
    " title TEXT,"
    " level INTEGER,"
    " category_raw TEXT NOT NULL,"
    " category_key TEXT NOT NULL,"
    " category TEXT NOT NULL,"
    " kind_key TEXT NOT NULL,"
    " content TEXT NOT NULL,"
    " tags_json TEXT NOT NULL,"
    " context TEXT,"
    " metadata_json TEXT NOT NULL,"
    " verdict TEXT,"
    " check_by_day TEXT,"
    " check_by_json TEXT,"
    " tags_canonical_json TEXT,"
    # Identity facts, not a bound `unit_ref`: the selection depends on the registry, so a stored
    # reference goes stale after an overlay or vocabulary pack change. The facts derive from the
    # bytes.
    " identity_signature TEXT NOT NULL,"
    " identity_fingerprints_json TEXT NOT NULL,"
    " in_kb INTEGER NOT NULL,"
    " in_vault INTEGER NOT NULL,"
    " updated TEXT NOT NULL,"
    " PRIMARY KEY(path, occurrence_key))"
)


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
    start_offset: int
    end_offset: int
    line: int
    end_line: int
    node_key: str
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
    identity_signature: str
    identity_fingerprints: tuple[str, ...]

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

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> UnitRow:
        """The row that `record` (a `unit_rows` row keyed by column) stores."""
        return cls(
            path=record["path"], occurrence_key=record["occurrence_key"],
            ordinal=record["ordinal"], form=record["form"],
            start_offset=record["start_offset"], end_offset=record["end_offset"],
            line=record["line"], end_line=record["end_line"], node_key=record["node_key"],
            unit_source_hash=record["unit_source_hash"], parent_ref=record["parent_ref"],
            parent_generation=record["parent_generation"],
            parent_source_hash=record["parent_source_hash"],
            parser_version=record["parser_version"], anchor=record["anchor"],
            title=record["title"], level=record["level"], category_raw=record["category_raw"],
            category_key=record["category_key"], category=record["category"],
            kind_key=record["kind_key"], content=record["content"],
            tags=tuple(json.loads(record["tags_json"])), context=record["context"],
            metadata=json.loads(record["metadata_json"]), verdict=record["verdict"],
            check_by_day=record["check_by_day"], check_by_json=record["check_by_json"],
            tags_canonical_json=record["tags_canonical_json"],
            identity_signature=record["identity_signature"],
            identity_fingerprints=tuple(json.loads(record["identity_fingerprints_json"])),
        )


def build(
    rel: str,
    state: SemanticParentIndexState,
    *,
    units: Sequence[Any] | None = None,
    identity: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[UnitRow, ...]:
    """The unit rows of one page's parent state, in source order.

    ``units`` is `semantic_units.candidate_units(state.candidates)` and ``identity`` is
    `semantic_units.identity_facts` of them; a caller that already has either passes it. The state
    must hold structural candidates.
    """
    from . import epistemic_graph, lexstore, semantic_language_registry, semantic_units

    candidates = semantic_units.candidate_units(state.candidates) if units is None else units
    facts = semantic_units.identity_facts(list(candidates)) if identity is None else identity
    rows = []
    for ordinal, unit in enumerate(candidates):
        key = semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash)
        verdict, check_by_day, check_by_json, tags_canonical_json = (
            lexstore.unit_eligibility_columns(unit)
        )
        rows.append(UnitRow(
            path=rel, occurrence_key=key, ordinal=ordinal, form=unit.form,
            start_offset=unit.span.start_offset, end_offset=unit.span.end_offset,
            line=unit.line, end_line=unit.end_line,
            node_key=epistemic_graph.unit_node_key(rel, key, unit.form),
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
            identity_signature=facts[key]["signature"],
            identity_fingerprints=tuple(facts[key]["fingerprints"]),
        ))
    return tuple(rows)


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the table that the store holds. It has no trigger and no index beyond its key."""
    conn.execute(_CREATE)


def record(row: UnitRow, page: Mapping[str, Any]) -> dict[str, Any]:
    """The `unit_rows` row of ``row``, keyed by column.

    ``page`` is the page's own row (`pages` columns).
    """
    return {
        "path": row.path, "occurrence_key": row.occurrence_key, "ordinal": row.ordinal,
        "form": row.form,
        "start_offset": row.start_offset, "end_offset": row.end_offset, "line": row.line,
        "end_line": row.end_line, "node_key": row.node_key,
        "unit_source_hash": row.unit_source_hash,
        "parent_ref": row.parent_ref, "parent_generation": row.parent_generation,
        "parent_source_hash": row.parent_source_hash, "parser_version": row.parser_version,
        "anchor": row.anchor, "title": row.title, "level": row.level,
        "category_raw": row.category_raw,
        "category_key": row.category_key, "category": row.category, "kind_key": row.kind_key,
        "content": row.content, "tags_json": row.tags_json, "context": row.context,
        "metadata_json": json.dumps(dict(row.metadata), ensure_ascii=False, sort_keys=True),
        "verdict": row.verdict, "check_by_day": row.check_by_day,
        "check_by_json": row.check_by_json, "tags_canonical_json": row.tags_canonical_json,
        "identity_signature": row.identity_signature,
        "identity_fingerprints_json": json.dumps(
            list(row.identity_fingerprints), separators=(",", ":")
        ),
        "in_kb": page["in_kb"], "in_vault": page["in_vault"], "updated": page["updated"],
    }


def write(conn: sqlite3.Connection, rows: Sequence[UnitRow], page: Mapping[str, Any]) -> None:
    """Insert one page's unit rows (the caller owns the transaction and the page's old rows)."""
    for row in rows:
        values = record(row, page)
        conn.execute(
            f"INSERT INTO {TABLE}({', '.join(values)}) VALUES({', '.join('?' * len(values))})",
            tuple(values.values()),
        )


def delete(conn: sqlite3.Connection, path: str) -> None:
    """Remove every unit row of one page."""
    conn.execute(f"DELETE FROM {TABLE} WHERE path = ?", (path,))


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
