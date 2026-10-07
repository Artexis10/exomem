"""Usage counts for vocabulary registries, read from maintained projections.

Counts come only from projections the indexer already keeps current: the
published graph snapshot (entity types, relations, semantic categories) and
the lexical catalogue (source kinds, domains). Nothing here parses Markdown.

A projection that is absent, warming, stale or refused makes the registry's
counts `unavailable` with a reason; a zero is only ever a counted zero. Under a
governed policy the counts are a whole-vault aggregate and are served to the
owner only (`egress.owner_only_aggregate`), decided before anything is read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .registry import Snapshot

#: Graph edge origins an author wrote, as opposed to structure the indexer adds.
_AUTHORED_EDGE_ORIGINS = ("semantic_relation", "markdown_relation", "frontmatter")


@dataclass(frozen=True, slots=True)
class Usage:
    """Counts per canonical key, or why there are none."""

    available: bool
    counts: Mapping[str, int] = field(default_factory=dict)
    reason: str | None = None
    source: str = ""

    def count(self, key: str) -> int | None:
        return self.counts.get(key, 0) if self.available else None


def _unavailable(reason: str, source: str) -> Usage:
    return Usage(available=False, reason=reason, source=source)


def restricted(vault_root: Path) -> str | None:
    """`audience_restricted` when the caller may not see whole-vault counts."""
    from ..governance import egress

    refusal = egress.owner_only_aggregate(vault_root)
    return None if refusal is None else str(refusal.get("reason") or "audience_restricted")


def _graph_rows(vault_root: Path, sql: str) -> list[tuple[Any, ...]] | str:
    """Rows from one current graph snapshot, or the reason there is none."""
    from ..epistemic_graph import EpistemicGraphIndex

    refusal: list[str] = []
    try:
        connection = EpistemicGraphIndex(vault_root)._open_read_snapshot(refusal_out=refusal)
    except Exception:  # noqa: BLE001 — a projection fault is reported, never raised
        return "graph_unavailable"
    if connection is None:
        return refusal[0] if refusal else "graph_unavailable"
    try:
        return [tuple(row) for row in connection.execute(sql).fetchall()]
    except Exception:  # noqa: BLE001
        return "graph_unavailable"
    finally:
        connection.close()


def _canonical_counts(
    rows: list[tuple[Any, ...]], snapshot: Snapshot, resolve: Callable[[str], str | None]
) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for value, count in rows:
        if not isinstance(value, str) or not value:
            continue
        key = resolve(value)
        if key is not None and key in snapshot.entries:
            counts[key] = counts.get(key, 0) + int(count)
    return MappingProxyType(counts)


def _guarded(vault_root: Path, source: str, read: Callable[[], Usage]) -> Usage:
    reason = restricted(vault_root)
    if reason is not None:
        return _unavailable(reason, source)
    return read()


def entity_types(vault_root: Path, snapshot: Snapshot) -> Usage:
    """Entity pages per registered type, from the graph's file nodes."""

    def read() -> Usage:
        rows = _graph_rows(
            vault_root,
            "SELECT json_extract(metadata, '$.entity_type'), COUNT(*) FROM graph_nodes "
            "WHERE kind = 'file' AND page_type = 'entity' GROUP BY 1",
        )
        if isinstance(rows, str):
            return _unavailable(rows, "graph entity pages")
        resolve = snapshot.typed.resolve
        return Usage(
            True,
            _canonical_counts(rows, snapshot, lambda value: getattr(resolve(value), "id", None)),
            source="graph entity pages",
        )

    return _guarded(vault_root, "graph entity pages", read)


def relations(vault_root: Path, snapshot: Snapshot) -> Usage:
    """Authored edges per canonical relation, from the graph's edges."""

    def read() -> Usage:
        origins = ", ".join(f"'{origin}'" for origin in _AUTHORED_EDGE_ORIGINS)
        rows = _graph_rows(
            vault_root,
            "SELECT relation_type, COUNT(*) FROM graph_edges "
            f"WHERE relation_type IS NOT NULL AND origin IN ({origins}) GROUP BY 1",
        )
        if isinstance(rows, str):
            return _unavailable(rows, "graph authored edges")
        return Usage(
            True,
            _canonical_counts(rows, snapshot, lambda value: value),
            source="graph authored edges",
        )

    return _guarded(vault_root, "graph authored edges", read)


def categories(vault_root: Path, snapshot: Snapshot) -> Usage:
    """Semantic units per category, from the graph's unit nodes."""

    def read() -> Usage:
        rows = _graph_rows(
            vault_root,
            "SELECT unit_category, COUNT(*) FROM graph_nodes "
            "WHERE unit_category IS NOT NULL GROUP BY 1",
        )
        if isinstance(rows, str):
            return _unavailable(rows, "graph semantic units")

        def resolve(value: str) -> str | None:
            return snapshot.typed.resolve_category(value).resolved

        return Usage(
            True, _canonical_counts(rows, snapshot, resolve), source="graph semantic units"
        )

    return _guarded(vault_root, "graph semantic units", read)


def catalogue_axis(vault_root: Path, snapshot: Snapshot, *, column: str) -> Usage:
    """Knowledge Base pages per source kind or domain, from the lexical catalogue."""

    def read() -> Usage:
        from .. import lexstore

        rows = lexstore.get_store(Path(vault_root)).page_axis_counts(column)
        if rows is None:
            return _unavailable("catalogue_unavailable", "catalogue Knowledge Base pages")
        aliases = (
            snapshot.typed.kind_aliases
            if column == "source_kind"
            else snapshot.typed.domain_aliases
        )
        return Usage(
            True,
            _canonical_counts(
                list(rows.items()), snapshot, lambda value: aliases.get(value, value)
            ),
            source="catalogue Knowledge Base pages",
        )

    return _guarded(vault_root, "catalogue Knowledge Base pages", read)
