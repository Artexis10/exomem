"""Derived epistemic graph sidecar over Exomem Markdown files.

The graph is rebuildable measurement state. Markdown remains canonical; this
module indexes files, semantic blocks, and deterministic relations into a SQLite
sidecar, then exposes read-only context and propose-only relation suggestions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import find as find_module
from . import (
    memory_refs,
    mutation_lock,
    relation_registry,
    semantic_blocks,
    semantic_index,
    semantic_language_registry,
    semantic_units,
    traversal_profiles,
)
from . import vault as vault_module
from .cli_ops import OpError
from .kbdir import kb_dirname, kb_prefix
from .markdown_relations import MarkdownRelation

SCHEMA_VERSION = 6
UNIT_SEED_MAX_BATCHES = 4
UNIT_PARENT_REF_MAX_CANDIDATES = 16
EDGE_INSPECTION_MULTIPLIER = 4
REBUILD_STABILIZATION_ATTEMPTS = 2
GRAPH_MUTATION_TIMEOUT_SECONDS = 30.0
GRAPH_COORDINATION_DIRNAME = ".graph-coordination"

RELATION_TYPES: frozenset[str] = relation_registry.core_registry().keys

@dataclass(frozen=True)
class GraphNode:
    node_key: str
    kind: str
    path: str
    anchor: str | None
    title: str | None
    text: str
    source_hash: str
    line_start: int | None = None
    line_end: int | None = None
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_key": self.node_key,
            "kind": self.kind,
            "path": self.path,
            "anchor": self.anchor,
            "title": self.title,
            "text": self.text,
            "source_hash": self.source_hash,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class GraphEdge:
    edge_key: str
    src_key: str
    dst_key: str
    relation_type: str | None
    raw_relation: str
    parent_relation: str | None
    registry_status: str
    registry_version: int
    registry_hash: str
    origin: str
    source_path: str
    source_anchor: str | None = None
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_key": self.edge_key,
            "src_key": self.src_key,
            "dst_key": self.dst_key,
            "relation_type": self.relation_type,
            "raw_relation": self.raw_relation,
            "parent_relation": self.parent_relation,
            "registry_status": self.registry_status,
            "registry_version": self.registry_version,
            "registry_hash": self.registry_hash,
            "origin": self.origin,
            "source_path": self.source_path,
            "source_anchor": self.source_anchor,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class GraphNeighbor:
    """One typed edge touching a find-lane seed, resolved to file endpoints.

    `direction` is relative to the seed: "outbound" when the seed is the edge
    source, "inbound" when the seed is the edge destination. `family` is the
    relation registry family ("" for unregistered relations).
    """

    seed_rel: str
    other_rel: str
    relation_type: str | None
    direction: str
    family: str


def graph_enabled() -> bool:
    return os.environ.get("EXOMEM_DISABLE_GRAPH_INDEX", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def sidecar_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".graph.sqlite"


def _disk_vault_freshness(vault_root: Path) -> tuple[int, int, str]:
    """Vault freshness from a direct walk, bypassing event-registry state."""
    return find_module._walk_freshness_key(
        vault_module.walk_vault_md(vault_root)
    )


class EpistemicGraphIndex:
    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.path = sidecar_path(self.vault_root)
        self.registry = relation_registry.load_registry(self.vault_root)
        self.language_registry = semantic_language_registry.load_registry(self.vault_root)
        self._mutation_coordinator = mutation_lock.VaultMutationCoordinator(
            self.vault_root / kb_dirname() / GRAPH_COORDINATION_DIRNAME,
            self.vault_root,
            timeout_seconds=GRAPH_MUTATION_TIMEOUT_SECONDS,
        )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            from . import embeddings

            embeddings._apply_sidecar_pragmas(conn)
        except Exception:  # noqa: BLE001 - sidecar pragmas are best-effort
            pass
        edge_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(graph_edges)").fetchall()
        }
        if edge_columns and "raw_relation" not in edge_columns:
            conn.execute("DROP TABLE graph_edges")
        node_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(graph_nodes)").fetchall()
        }
        required_unit_columns = {"unit_ref", "unit_category", "unit_kind"}
        if node_columns and not required_unit_columns <= node_columns:
            conn.execute("DROP TABLE graph_nodes")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_nodes (
                node_key TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
                anchor TEXT, title TEXT, text TEXT NOT NULL, source_hash TEXT NOT NULL,
                line_start INTEGER, line_end INTEGER, metadata TEXT NOT NULL,
                unit_ref TEXT, unit_category TEXT, unit_kind TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_edges (
                edge_key TEXT PRIMARY KEY, src_key TEXT NOT NULL, dst_key TEXT NOT NULL,
                relation_type TEXT, raw_relation TEXT NOT NULL, parent_relation TEXT,
                registry_status TEXT NOT NULL, registry_version INTEGER NOT NULL,
                registry_hash TEXT NOT NULL, origin TEXT NOT NULL, source_path TEXT NOT NULL,
                source_anchor TEXT, metadata TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_parent_refs (
                path TEXT PRIMARY KEY, parent_ref TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_nodes_path ON graph_nodes(path)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_unit_ref ON graph_nodes(unit_ref)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_unit_category_kind "
            "ON graph_nodes(unit_category, unit_kind, kind, path, node_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_unit_kind "
            "ON graph_nodes(unit_kind, kind, path, node_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_parent_refs_ref "
            "ON graph_parent_refs(parent_ref, path)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_key)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_source_path ON graph_edges(source_path)"
        )
        return conn

    def available(self) -> bool:
        conn = self._open_read_snapshot()
        if conn is None:
            return False
        conn.close()
        return True

    def _open_read_snapshot(self) -> sqlite3.Connection | None:
        """Open one validated read transaction without creating or migrating schema."""
        if not graph_enabled() or not self.path.exists():
            return None
        conn: sqlite3.Connection | None = None
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("BEGIN")
            # This marker validation MUST remain the first read in the transaction.
            values = dict(
                conn.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('schema_version', 'core_registry_version', 'extension_registry_hash')"
                ).fetchall()
            )
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            return None
        current = (
            values.get("schema_version") == str(SCHEMA_VERSION)
            and values.get("core_registry_version") == str(self.registry.core_version)
            and values.get("extension_registry_hash") == self.registry.extension_hash
        )
        if not current:
            conn.close()
            return None
        return conn

    def rebuild_all(self) -> dict[str, int]:
        if not graph_enabled():
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "disabled": 1}
        with self._mutation_coordinator.hold():
            return self._rebuild_all_locked()

    def _rebuild_all_locked(self) -> dict[str, int]:
        pass_started = False
        stable = False
        try:
            for _attempt in range(REBUILD_STABILIZATION_ATTEMPTS):
                before = _disk_vault_freshness(self.vault_root)
                resolver = find_module.writer_resolver_snapshot(
                    self.vault_root,
                    freshness_key=before,
                )
                pass_started = True
                report = self._rebuild_all_pass(resolver)
                if _disk_vault_freshness(self.vault_root) == before:
                    self._mark_available()
                    stable = True
                    return report
            raise RuntimeError(
                "epistemic graph rebuild did not stabilize after 2 attempts"
            )
        finally:
            if pass_started and not stable:
                self._mark_unavailable()

    def _rebuild_all_pass(
        self,
        resolver: vault_module.WikilinkResolver,
    ) -> dict[str, int]:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM graph_edges")
                conn.execute("DELETE FROM graph_nodes")
                conn.execute("DELETE FROM graph_parent_refs")
                conn.execute(
                    "DELETE FROM graph_meta WHERE key = 'schema_version'"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("core_registry_version", str(self.registry.core_version)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("extension_registry_hash", self.registry.extension_hash),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    (
                        "traversal_profile_hash",
                        traversal_profiles.load_profiles(
                            self.vault_root, registry=self.registry
                        ).content_hash,
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("indexed_scope", "kb"),
                )
                _bump_generation(conn)
            indexed = 0
            kb = self.vault_root / kb_dirname()
            if kb.is_dir():
                for md in find_module._walk_md(kb):
                    if self._index_path(conn, md, resolver=resolver):
                        indexed += 1
            with conn:
                n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
                n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            return {"indexed_files": indexed, "nodes": int(n_nodes), "edges": int(n_edges)}
        finally:
            conn.close()

    def _mark_unavailable(self) -> None:
        if not self.path.exists():
            return
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM graph_meta WHERE key = 'schema_version'"
                )
        finally:
            conn.close()

    def _mark_available(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
        finally:
            conn.close()

    def refresh_paths(self, paths: list[Path]) -> dict[str, int]:
        if not graph_enabled():
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "disabled": 1}
        with self._mutation_coordinator.hold():
            return self._refresh_paths_locked(paths)

    def _refresh_paths_locked(self, paths: list[Path]) -> dict[str, int]:
        if not self.available():
            return self._rebuild_all_locked()
        resolver = find_module.writer_resolver_snapshot(self.vault_root)
        conn = self._connect()
        indexed = 0
        try:
            for path in paths:
                if self._index_path(conn, path, resolver=resolver):
                    indexed += 1
            with conn:
                n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
                n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            return {"indexed_files": indexed, "nodes": int(n_nodes), "edges": int(n_edges)}
        finally:
            conn.close()

    def delete_paths(self, rel_paths: list[str]) -> int:
        with self._mutation_coordinator.hold():
            return self._delete_paths_locked(rel_paths)

    def _delete_paths_locked(self, rel_paths: list[str]) -> int:
        if not self.path.exists():
            return 0
        conn = self._connect()
        deleted = 0
        try:
            with conn:
                for rel in rel_paths:
                    deleted += self._delete_path(conn, _with_md(rel))
            return deleted
        finally:
            conn.close()

    def nodes(self, *, path: str | None = None) -> list[dict[str, Any]]:
        conn = self._open_read_snapshot()
        if conn is None:
            return []
        try:
            return self._nodes_from_snapshot(conn, path=path)
        finally:
            conn.close()

    def _nodes_from_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        select = (
            "SELECT node_key, kind, path, anchor, title, text, source_hash, "
            "line_start, line_end, metadata FROM graph_nodes"
        )
        if path is None:
            rows = conn.execute(select + " ORDER BY node_key").fetchall()
        else:
            rows = conn.execute(
                select + " WHERE path = ? ORDER BY node_key", (_with_md(path),)
            ).fetchall()
        return [_node_row_to_dict(r) for r in rows]

    def edges(self, *, source_path: str | None = None) -> list[dict[str, Any]]:
        conn = self._open_read_snapshot()
        if conn is None:
            return []
        try:
            return self._edges_from_snapshot(conn, source_path=source_path)
        finally:
            conn.close()

    def _edges_from_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        source_path: str | None = None,
    ) -> list[dict[str, Any]]:
        select = (
            "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, "
            "parent_relation, registry_status, registry_version, registry_hash, "
            "origin, source_path, source_anchor, metadata FROM graph_edges"
        )
        if source_path is None:
            rows = conn.execute(select + " ORDER BY edge_key").fetchall()
        else:
            rows = conn.execute(
                select + " WHERE source_path = ? ORDER BY edge_key",
                (_with_md(source_path),),
            ).fetchall()
        return [_edge_row_to_dict(r) for r in rows]

    def _index_path(
        self,
        conn: sqlite3.Connection,
        path: Path,
        *,
        resolver: vault_module.WikilinkResolver,
    ) -> bool:
        try:
            rel = path.resolve().relative_to(self.vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            return False
        if not rel.lower().endswith(".md") or vault_module.in_excluded_scan_dir(rel):
            return False
        if not path.exists():
            self._delete_path(conn, rel)
            return False
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        page = find_module._parse_page(path, path.stat().st_mtime, self.vault_root)
        if page is None:
            return False
        state = semantic_index.current_parent_index_state(
            self.vault_root,
            path,
            source=raw,
        )
        document = state.document
        file_node = _file_node(page, raw)
        unit_nodes = [
            _unit_node(page, unit, state)
            for unit in document.units
            if unit.unit_ref is not None
        ]
        edges = _edges_for_page(
            self.vault_root,
            page,
            document,
            registry=self.registry,
            source_hash=file_node.source_hash,
            parent_state=state,
            resolver=resolver,
        )
        with conn:
            conn.execute("DELETE FROM graph_edges WHERE source_path = ?", (rel,))
            conn.execute("DELETE FROM graph_nodes WHERE path = ?", (rel,))
            conn.execute("DELETE FROM graph_parent_refs WHERE path = ?", (rel,))
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                ("core_registry_version", str(self.registry.core_version)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                ("extension_registry_hash", self.registry.extension_hash),
            )
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                (
                    "traversal_profile_hash",
                    traversal_profiles.load_profiles(
                        self.vault_root, registry=self.registry
                    ).content_hash,
                ),
            )
            for node in [file_node, *unit_nodes]:
                _insert_node(conn, node)
            if document.parent_ref is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO graph_parent_refs(path, parent_ref) "
                    "VALUES (?, ?)",
                    (rel, document.parent_ref),
                )
            for edge in edges:
                _insert_edge(conn, edge)
            _bump_generation(conn)
        return True

    def _delete_path(self, conn: sqlite3.Connection, rel_path: str) -> int:
        with conn:
            conn.execute("DELETE FROM graph_edges WHERE source_path = ?", (rel_path,))
            cur = conn.execute("DELETE FROM graph_nodes WHERE path = ?", (rel_path,))
            conn.execute("DELETE FROM graph_parent_refs WHERE path = ?", (rel_path,))
            _bump_generation(conn)
        return cur.rowcount if cur.rowcount is not None else 0

    def neighbors_for(self, seeds: list[str]) -> list[GraphNeighbor]:
        """Typed edges touching `seeds` in both directions, batched over SQL.

        Semantic-block-authored relations store src/dst as the BLOCK node key,
        not the file key (`## Claim` etc. — see `_block_node`/`_edges_for_page`),
        so the seed match set is every node (file AND its semantic blocks)
        whose `path` equals the seed — one query resolves that set, since a
        block node's own `path` column already names its owning file. The two
        edge lookups (`src_key IN (...)`, `dst_key IN (...)`) then join
        `graph_nodes` on the OTHER endpoint (by `path`, not restricted to
        kind='file', so a relation touching another page's block still
        resolves to that page) — an INNER JOIN, so unresolved-placeholder
        targets (no node row at all) are excluded. Results are ordered by seed
        position then `rowid` (stable insertion/source order — edge_key is a
        content hash and is NOT a valid ordering signal), matching design D3's
        "seed order then edge insertion order" contract; family-precedence
        tiering and target dedup are the caller's job (find_candidates.py).
        Self-edges (a block's own `derived_from` edge to its owning file) drop
        out via the same-path check below.
        """
        if not seeds:
            return []
        seed_order: dict[str, int] = {}
        for i, seed in enumerate(seeds):
            rel = _with_md(seed)
            if rel not in seed_order:
                seed_order[rel] = i
        seed_paths = list(seed_order)
        conn = self._open_read_snapshot()
        if conn is None:
            return []
        try:
            path_placeholders = ",".join("?" for _ in seed_paths)
            node_rows = conn.execute(
                f"SELECT node_key, path FROM graph_nodes WHERE path IN ({path_placeholders})",
                seed_paths,
            ).fetchall()
            seed_rel_by_key: dict[str, str] = {node_key: path for node_key, path in node_rows}
            if not seed_rel_by_key:
                return []
            keys = list(seed_rel_by_key)
            key_placeholders = ",".join("?" for _ in keys)
            outbound = conn.execute(
                "SELECT e.rowid, e.src_key, e.relation_type, n.path "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.dst_key "
                f"WHERE e.src_key IN ({key_placeholders}) "
                "ORDER BY e.rowid",
                keys,
            ).fetchall()
            inbound = conn.execute(
                "SELECT e.rowid, e.dst_key, e.relation_type, n.path "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.src_key "
                f"WHERE e.dst_key IN ({key_placeholders}) "
                "ORDER BY e.rowid",
                keys,
            ).fetchall()
        finally:
            conn.close()
        rows: list[tuple[int, int, GraphNeighbor]] = []
        for direction, batch in (("outbound", outbound), ("inbound", inbound)):
            for rowid, seed_key, relation_type, other_path in batch:
                seed_rel = seed_rel_by_key.get(seed_key)
                if seed_rel is None or other_path == seed_rel:
                    continue
                definition = self.registry.definition(str(relation_type or ""))
                rows.append(
                    (
                        seed_order[seed_rel],
                        rowid,
                        GraphNeighbor(
                            seed_rel=seed_rel,
                            other_rel=other_path,
                            relation_type=relation_type,
                            direction=direction,
                            family=definition.family if definition else "",
                        ),
                    )
                )
        rows.sort(key=lambda item: (item[0], item[1]))
        return [neighbor for _order, _rowid, neighbor in rows]

    def indexed_paths(self, paths: list[str]) -> set[str]:
        """Subset of `paths` (vault-relative, .md-suffixed) with a FILE node in
        the sidecar. `rebuild_all` indexes only the KB tree, so a seed outside
        it (reachable under `scope="vault"`) is never in this set — the
        find-lane hybrid branch uses that to run legacy wikilink expansion for
        seeds the sidecar never covered, instead of silently dropping them."""
        if not paths:
            return set()
        rels = [_with_md(p) for p in paths]
        conn = self._open_read_snapshot()
        if conn is None:
            return set()
        try:
            placeholders = ",".join("?" for _ in rels)
            rows = conn.execute(
                f"SELECT DISTINCT path FROM graph_nodes WHERE path IN ({placeholders}) "
                "AND kind = 'file'",
                rels,
            ).fetchall()
        finally:
            conn.close()
        return {row[0] for row in rows}


def graph_context(
    vault_root: Path,
    *,
    path: str | None = None,
    query: str | None = None,
    unit_ref: str | None = None,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    depth: int = 1,
    relation_types: list[str] | None = None,
    node_types: list[str] | None = None,
    max_nodes: int = 40,
    max_edges: int = 80,
    traversal_profile: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, read-only graph neighborhood for a path or query."""
    idx = EpistemicGraphIndex(vault_root)
    profile_registry = traversal_profiles.load_profiles(vault_root, registry=idx.registry)
    profile = profile_registry.resolve(traversal_profile)
    depth = min(max(0, int(depth)), profile.max_depth, traversal_profiles.MAX_DEPTH)
    max_nodes = min(max(1, int(max_nodes)), profile.max_nodes, traversal_profiles.MAX_NODES)
    max_edges = min(max(0, int(max_edges)), profile.max_edges, traversal_profiles.MAX_EDGES)
    allowed = {
        definition.key
        for definition in (*idx.registry.core.values(), *idx.registry.extensions.values())
        if traversal_profiles.relation_allowed(profile, definition)
    }
    narrowed = traversal_profiles.narrow_relations(profile, relation_types, idx.registry)
    if narrowed is not None:
        allowed &= set(narrowed)
    conn = idx._open_read_snapshot()
    if conn is None:
        return {
            "available": False,
            "reason": "graph sidecar unavailable",
            "seeds": [],
            "nodes": [],
            "edges": [],
            "truncation": [],
        }
    try:
        drift_counts: dict[str, int] = {}
        freshness_cache: dict[tuple[str, str, str, int], bool] = {}

        def _current_record(record: dict[str, Any], *, parent_path: str) -> bool:
            metadata = record.get("metadata") or {}
            if metadata.get("record_type") != "semantic_unit":
                return True
            try:
                stamp = (
                    parent_path,
                    str(metadata["parent_generation"]),
                    str(metadata["parent_source_hash"]),
                    int(metadata["parser_version"]),
                )
            except (KeyError, TypeError, ValueError):
                drift_counts["invalid_generation_stamp"] = (
                    drift_counts.get("invalid_generation_stamp", 0) + 1
                )
                return False
            accepted = freshness_cache.get(stamp)
            if accepted is None:
                freshness = semantic_index.validate_parent_record(
                    vault_root,
                    parent_path=stamp[0],
                    parent_generation_value=stamp[1],
                    parent_source_hash=stamp[2],
                    parser_version=stamp[3],
                )
                accepted = freshness.current
                freshness_cache[stamp] = accepted
                if not accepted:
                    drift_counts[freshness.code] = drift_counts.get(freshness.code, 0) + 1
            return accepted

        category_filter = _resolved_unit_filters(
            idx.language_registry, categories, namespace="category"
        )
        kind_filter = _resolved_unit_filters(
            idx.language_registry, kinds, namespace="kind"
        )
        unit_status: str | None = None
        unit_filter_status: str | None = None
        seed_cap_hit = False
        unit_work_exhausted = False
        unit_parent_work_exhausted = False
        seed_node_overrides: dict[str, dict[str, Any]] = {}
        seeds: list[dict[str, Any]]
        if unit_ref is not None:
            indexed = _seed_nodes(
                conn,
                path=None,
                query=None,
                unit_ref=unit_ref,
                limit=UNIT_PARENT_REF_MAX_CANDIDATES,
            )
            current = [
                seed
                for seed in indexed
                if _current_record(seed, parent_path=str(seed.get("path") or ""))
            ]
            (
                resolved_status,
                current_parent_paths,
                canonical_seeds,
                parent_drift_counts,
                unit_parent_work_exhausted,
            ) = _current_unit_status(conn, vault_root, unit_ref)
            for code, count in parent_drift_counts.items():
                drift_counts[code] = max(drift_counts.get(code, 0), count)
            current = [
                seed for seed in current if str(seed.get("path") or "") in current_parent_paths
            ]
            collision_candidate = (
                resolved_status == "found"
                and bool(indexed)
                and bool(canonical_seeds)
                and not any(
                    str(seed.get("path") or "") in current_parent_paths
                    for seed in indexed
                )
            )
            recovery_seeds = (
                [
                    seed
                    for seed in canonical_seeds
                    if _current_unit_seed_has_graph_proof(conn, seed)
                ]
                if collision_candidate
                else []
            )
            collision_recovery = bool(recovery_seeds)
            if resolved_status == "ambiguous":
                unit_status = "ambiguous"
                seeds = []
            elif unit_parent_work_exhausted:
                unit_status = "stale"
                seeds = []
            elif resolved_status == "found" and (current or collision_recovery):
                unit_status = "found"
                if collision_recovery:
                    drift_counts["current_graph_row_overwritten"] = 1
                seeds = _filter_unit_nodes(
                    current or recovery_seeds,
                    categories=category_filter,
                    kinds=kind_filter,
                )
                if collision_recovery:
                    seed_node_overrides.update(
                        (str(seed["node_key"]), seed) for seed in seeds
                    )
                if category_filter is not None or kind_filter is not None:
                    unit_filter_status = "matched" if seeds else "excluded"
            elif indexed:
                unit_status = "stale"
                seeds = []
            else:
                if resolved_status in {"found", "stale"}:
                    unit_status = "stale"
                    drift_counts["missing_graph_row"] = 1
                else:
                    unit_status = resolved_status
                seeds = []
        elif category_filter is not None or kind_filter is not None:
            seeds, seed_cap_hit, unit_work_exhausted = _bounded_current_unit_seeds(
                conn,
                path=path,
                query=query,
                categories=category_filter,
                kinds=kind_filter,
                max_nodes=max_nodes,
                current_record=_current_record,
            )
        else:
            seeds = [
                seed
                for seed in _seed_nodes(conn, path=path, query=query)
                if _current_record(seed, parent_path=str(seed.get("path") or ""))
            ]
        if not seeds:
            empty: dict[str, Any] = {
                "available": True,
                "reason": None,
                "seeds": [],
                "nodes": [],
                "edges": [],
                "truncation": _unit_seed_truncation(
                    max_nodes=max_nodes,
                    unit_work_exhausted=unit_work_exhausted,
                    unit_parent_work_exhausted=unit_parent_work_exhausted,
                ),
            }
            if unit_status is not None:
                empty["unit_status"] = unit_status
            if unit_filter_status is not None:
                empty["unit_filter_status"] = unit_filter_status
            if drift_counts:
                empty["warnings"] = [_drift_warning(drift_counts)]
            return empty
        type_filter = set(node_types or [])
        seen_nodes: set[str] = {s["node_key"] for s in seeds}
        seen_edges: dict[str, dict[str, Any]] = {}
        edge_cap_hit = False
        edge_inspection_cap_hit = False
        edge_inspection_budget = _edge_inspection_budget(
            max_nodes=max_nodes, max_edges=max_edges
        )
        inspected_edges = 0
        placeholder_nodes: dict[str, dict[str, Any]] = {}
        node_cap_hit = False
        excluded_profile = 0
        excluded_scope = 0
        unknown: dict[tuple[str, str, str], dict[str, Any]] = {}
        frontier = set(seen_nodes)
        for _ in range(max(0, depth)):
            if not frontier:
                break
            rows, inspection_overflow = _neighbor_edges(
                conn,
                frontier,
                set(),
                limit=max(0, edge_inspection_budget - inspected_edges),
            )
            inspected_edges += len(rows)
            edge_inspection_cap_hit = edge_inspection_cap_hit or inspection_overflow
            rows.sort(key=lambda edge: _edge_priority(edge, profile, idx.registry))
            next_frontier: set[str] = set()
            for edge in rows:
                if not _current_record(
                    edge, parent_path=str(edge.get("source_path") or "")
                ):
                    continue
                status = edge.get("registry_status")
                if status == "unregistered":
                    key = (
                        str(edge.get("source_path")),
                        str(edge.get("source_anchor")),
                        str(edge.get("raw_relation")),
                    )
                    unknown.setdefault(
                        key,
                        {
                            "raw_relation": edge.get("raw_relation"),
                            "source_path": edge.get("source_path"),
                            "source_anchor": edge.get("source_anchor"),
                        },
                    )
                    continue
                if status == "scope_violation":
                    excluded_scope += 1
                    continue
                if edge.get("relation_type") not in allowed:
                    excluded_profile += 1
                    continue
                if profile.direction == "outgoing" and edge["src_key"] not in frontier:
                    continue
                if profile.direction == "incoming" and edge["dst_key"] not in frontier:
                    continue
                if edge["edge_key"] not in seen_edges:
                    if len(seen_edges) >= max_edges:
                        edge_cap_hit = True
                        break
                    seen_edges[edge["edge_key"]] = edge
                for key in (edge["src_key"], edge["dst_key"]):
                    if key not in seen_nodes:
                        node = _node_by_key(conn, key)
                        if node is None:
                            node = _placeholder_node(key)
                        elif not _current_record(
                            node, parent_path=str(node.get("path") or "")
                        ):
                            continue
                        if type_filter and node["kind"] not in type_filter:
                            continue
                        if len(seen_nodes) >= max_nodes:
                            node_cap_hit = True
                            continue
                        seen_nodes.add(key)
                        if node["kind"] == "unresolved":
                            placeholder_nodes[key] = node
                        else:
                            next_frontier.add(key)
            if edge_cap_hit or edge_inspection_cap_hit:
                break
            frontier = next_frontier
        nodes = [
            node
            for node in _nodes_by_keys(conn, seen_nodes)
            if _current_record(node, parent_path=str(node.get("path") or ""))
        ]
        present_node_keys = {str(node["node_key"]) for node in nodes}
        nodes.extend(
            seed_node_overrides[key]
            for key in sorted(seed_node_overrides)
            if key in seen_nodes and key not in present_node_keys
        )
        nodes += [
            placeholder_nodes[key] for key in sorted(placeholder_nodes)
        ]
        edges = list(seen_edges.values())
        truncation: list[str] = []
        if seed_cap_hit:
            truncation.append(f"seed nodes capped at {max_nodes}")
        if unit_work_exhausted:
            truncation.append(_unit_seed_work_truncation(max_nodes))
        if unit_parent_work_exhausted:
            truncation.append(_unit_parent_work_truncation())
        if len(nodes) > max_nodes:
            truncation.append(
                f"nodes capped at {max_nodes} ({len(nodes) - max_nodes} more not shown)"
            )
            nodes = nodes[:max_nodes]
        elif node_cap_hit:
            truncation.append(f"nodes capped at {max_nodes}")
        if edge_cap_hit:
            truncation.append(f"edges capped at {max_edges}")
        if edge_inspection_cap_hit:
            truncation.append(
                f"edge inspection capped at {edge_inspection_budget} records"
            )
        warnings: list[dict[str, Any]] = []
        if unknown:
            warnings.append(
                {
                    "code": "unregistered_relations",
                    "count": len(unknown),
                    "examples": list(unknown.values())[:5],
                }
            )
        if excluded_scope:
            warnings.append({"code": "scope_violations", "count": excluded_scope})
        if drift_counts:
            warnings.append(_drift_warning(drift_counts))
        result: dict[str, Any] = {
            "available": True,
            "reason": None,
            "seeds": seeds,
            "nodes": nodes,
            "edges": edges,
            "truncation": truncation,
            "profile": profile.as_dict(),
            "registry": {
                "core_version": idx.registry.core_version,
                "extension_hash": idx.registry.extension_hash,
                "profile_hash": profile_registry.content_hash,
            },
            "included_relation_families": sorted(profile.families),
            "excluded": {
                "profile": excluded_profile,
                "scope_violation": excluded_scope,
                "unregistered": len(unknown),
            },
            "warnings": warnings,
        }
        if unit_status is not None:
            result["unit_status"] = unit_status
        if unit_filter_status is not None:
            result["unit_filter_status"] = unit_filter_status
        return result
    finally:
        conn.close()


def suggest_relations(
    vault_root: Path,
    *,
    path: str | None = None,
    draft_title: str | None = None,
    draft_body: str | None = None,
    include_model_suggestions: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Return proposed relation candidates without mutating files or sidecars."""
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    if path:
        rel = _with_md(path)
        page = find_module._CACHE.get(Path(vault_root) / rel, Path(vault_root))
        if page is not None:
            candidates.extend(_wikilink_candidates(vault_root, page.body, rel))
            candidates.extend(_frontmatter_source_candidates(page))
            candidates.extend(_shared_source_candidates(vault_root, rel))
            candidates.extend(_embedding_proximity_candidates(vault_root, page))
    elif draft_body:
        candidates.extend(
            _draft_wikilink_candidates(vault_root, draft_body, draft_title=draft_title)
        )
    if include_model_suggestions:
        warnings.append("model-backed graph relation suggestions unavailable")
    return {
        "candidates": _dedupe_candidates(candidates)[: max(0, limit)],
        "warnings": warnings,
        "model_suggestions_available": False,
        "mutated": False,
    }


def _bump_generation(conn: sqlite3.Connection) -> None:
    """Monotonically advance the in-band content generation counter.

    Called inside each sidecar write transaction (index, delete, rebuild) so the
    freshness token below changes iff graph content changed — never on a WAL
    checkpoint, which moves the file mtime without touching content.

    Self-initializing upsert (no separate "seed the row" step): a fresh sidecar
    has no `generation` row yet, so the first bump inserts '1'; every
    subsequent bump increments in place. This keeps row initialization scoped
    to genuine write paths. Trusted readers use `_open_read_snapshot()` instead
    of the schema-creating `_connect()` writer helper.
    """
    conn.execute(
        "INSERT INTO graph_meta(key, value) VALUES ('generation', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
    )


def cache_token(vault_root: Path) -> tuple | None:
    """`(schema_version, extension_registry_hash, generation)` or None.

    None whenever the sidecar is unavailable (disabled, missing, or
    schema/registry drift), which the find freshness key maps to a stable
    absent-sentinel so typed-mode and fallback-mode entries never collide.
    """
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return None
    try:
        values = dict(
            conn.execute(
                "SELECT key, value FROM graph_meta WHERE key IN "
                "('schema_version', 'extension_registry_hash', 'generation')"
            ).fetchall()
        )
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return (
        values.get("schema_version"),
        values.get("extension_registry_hash"),
        values.get("generation"),
    )


def upsert_after_write(vault_root: Path, written_paths: list[Path]) -> None:
    if not graph_enabled():
        return
    try:
        EpistemicGraphIndex(vault_root).refresh_paths(written_paths)
    except OpError:
        raise
    except Exception:  # noqa: BLE001 - writer hooks must not break Markdown writes
        return


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    if not graph_enabled():
        return
    try:
        EpistemicGraphIndex(vault_root).delete_paths(removed_rel_paths)
    except OpError:
        raise
    except Exception:  # noqa: BLE001 - writer hooks must not break Markdown writes
        return


def graph_drift(vault_root: Path) -> list[dict[str, Any]]:
    if not graph_enabled():
        return []
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return [
            {
                "path": kb_prefix(),
                "reason": (
                    "graph sidecar missing, schema-mismatched, or "
                    "relation-registry hash drift"
                ),
            }
        ]
    try:
        by_path = {
            node["path"]: node
            for node in idx._nodes_from_snapshot(conn)
            if node["kind"] == "file"
        }
    finally:
        conn.close()
    drift: list[dict[str, Any]] = []
    kb = vault_root / kb_dirname()
    if not kb.is_dir():
        return drift
    disk_paths: set[str] = set()
    for md in find_module._walk_md(kb):
        try:
            rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
            raw = md.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        disk_paths.add(rel)
        expected_hash = vault_module.content_hash(raw)
        node = by_path.get(rel)
        if node is None:
            drift.append({"path": rel, "reason": "missing graph row"})
        elif node.get("source_hash") != expected_hash:
            drift.append({"path": rel, "reason": "stale graph row"})
    for rel in sorted(set(by_path) - disk_paths):
        drift.append({"path": rel, "reason": "graph row for missing file"})
    return drift


def _file_node(page, raw_text: str) -> GraphNode:
    return GraphNode(
        node_key=_file_key(page.rel_path),
        kind="file",
        path=page.rel_path,
        anchor="page",
        title=page.title,
        text=page.title or page.rel_path,
        source_hash=vault_module.content_hash(raw_text),
        metadata={
            "page_type": page.page_type,
            "status": page.status,
            "scope": page.scope,
            "origin": "file",
        },
    )


def _block_key(page, unit: semantic_units.SemanticUnit) -> str:
    block_id = unit.anchor or f"line-{unit.line}"
    key_material = "\n".join(
        [page.rel_path, unit.kind, block_id, unit.title or "", unit.body or ""]
    )
    return f"block:{_hash(key_material)}"


def _block_anchor(unit: semantic_units.SemanticUnit) -> str:
    return (
        unit.anchor
        or semantic_blocks.normalize_label(unit.title or "")
        or f"line-{unit.line}"
    )


def _block_node(page, unit: semantic_units.SemanticUnit, raw_text: str) -> GraphNode:
    return GraphNode(
        node_key=_block_key(page, unit),
        kind=unit.kind,
        path=page.rel_path,
        anchor=_block_anchor(unit),
        title=unit.title,
        text=unit.body or unit.title or "",
        source_hash=vault_module.content_hash(raw_text),
        line_start=unit.line,
        line_end=unit.end_line,
        metadata={**unit.metadata, "origin": "semantic_block", "level": unit.level},
    )


def _compact_unit_key(unit: semantic_units.SemanticUnit) -> str:
    if unit.unit_ref is None:
        raise ValueError("compact semantic-unit graph nodes require an addressable unit_ref")
    return "unit:" + hashlib.sha256(unit.unit_ref.encode("utf-8")).hexdigest()


def _unit_key(page, unit: semantic_units.SemanticUnit) -> str:
    return _block_key(page, unit) if unit.form == "rich" else _compact_unit_key(unit)


def _unit_generation_metadata(
    unit: semantic_units.SemanticUnit,
    state: semantic_index.SemanticParentIndexState,
) -> dict[str, Any]:
    return {
        "record_type": "semantic_unit",
        "unit_ref": unit.unit_ref,
        "form": unit.form,
        "category_raw": unit.category_raw,
        "category_key": unit.category_key,
        "category": unit.category,
        "kind": unit.kind,
        "tags": list(unit.tags),
        "context": unit.context,
        "parent_generation": state.parent_generation,
        "parent_source_hash": state.parent_source_hash,
        "parser_version": state.parser_version,
    }


def _unit_node(
    page,
    unit: semantic_units.SemanticUnit,
    state: semantic_index.SemanticParentIndexState,
) -> GraphNode:
    generation = _unit_generation_metadata(unit, state)
    if unit.form == "rich":
        legacy = _block_node(page, unit, "")
        return GraphNode(
            node_key=legacy.node_key,
            kind=legacy.kind,
            path=legacy.path,
            anchor=legacy.anchor,
            title=legacy.title,
            text=legacy.text,
            source_hash=state.parent_source_hash,
            line_start=legacy.line_start,
            line_end=legacy.line_end,
            metadata={**(legacy.metadata or {}), **generation},
        )
    return GraphNode(
        node_key=_compact_unit_key(unit),
        kind=unit.kind,
        path=page.rel_path,
        anchor=unit.anchor,
        title=None,
        text=unit.content,
        source_hash=state.parent_source_hash,
        line_start=unit.line,
        line_end=unit.end_line,
        metadata={
            "origin": "compact_observation",
            "tags": list(unit.tags),
            "context": unit.context,
            **generation,
        },
    )


def _edges_for_page(
    vault_root: Path,
    page,
    document: semantic_units.SemanticUnitDocument,
    *,
    registry: relation_registry.RelationRegistry | None = None,
    source_hash: str | None = None,
    parent_state: semantic_index.SemanticParentIndexState | None = None,
    resolver: vault_module.WikilinkResolver | None = None,
) -> list[GraphEdge]:
    registry = registry or relation_registry.load_registry(vault_root)
    source_hash = source_hash or vault_module.content_hash(page.body)
    project = _page_project(page.frontmatter)

    def page_edge(*args, **kwargs) -> GraphEdge:
        return _edge(
            *args,
            **kwargs,
            registry=registry,
            project=project,
            page_type=page.page_type,
            source_hash=source_hash,
        )

    rel = page.rel_path
    file_key = _file_key(rel)
    if resolver is None:
        resolver = find_module.shared_resolver(vault_root)
    edges: list[GraphEdge] = []
    for unit in document.units:
        if unit.unit_ref is None or unit.form == "rich":
            continue
        generation = (
            _unit_generation_metadata(unit, parent_state)
            if parent_state is not None
            else {}
        )
        edges.append(
            page_edge(
                _compact_unit_key(unit),
                file_key,
                "derived_from",
                "semantic_unit",
                source_path=rel,
                source_anchor=unit.anchor or f"line-{unit.line}",
                metadata=generation,
            )
        )
    for unit in document.rich_units:
        block_key = _block_key(page, unit)
        block_anchor = _block_anchor(unit)
        generation = (
            _unit_generation_metadata(unit, parent_state)
            if parent_state is not None
            else {}
        )
        edges.append(
            page_edge(
                block_key,
                file_key,
                "derived_from",
                "semantic_block",
                source_path=rel,
                source_anchor=block_anchor,
                metadata={"block_kind": unit.kind, **generation},
            )
        )
        for relation in unit.relations:
            target = relation.target
            if target.startswith("[[") and target.endswith("]]"):
                target = target[2:-2]
            target = target.split("|", 1)[0].split("#", 1)[0].strip()
            try:
                canonical, warning = vault_module.normalize_wikilink(
                    target, vault_root, resolver=resolver, strict=False
                )
            except Exception:  # noqa: BLE001 - malformed links are ignored
                continue
            if not canonical:
                continue
            edges.append(
                page_edge(
                    block_key,
                    _file_key(_with_md(canonical)),
                    relation.kind,
                    "semantic_relation",
                    source_path=rel,
                    source_anchor=block_anchor,
                    raw_relation=relation.raw.split(":", 1)[0].strip(),
                    source_kind=unit.kind,
                    target_kind=_target_kind(vault_root, canonical),
                    metadata={
                        "block_kind": unit.kind,
                        "line": relation.line,
                        "raw": relation.raw,
                        "target_resolution": "unresolved" if warning else "resolved",
                        **generation,
                    },
                )
            )
    for target in _frontmatter_links(page.frontmatter.get("sources")):
        edges.append(
            page_edge(
                file_key,
                _file_key(_with_md(target)),
                "derived_from",
                "frontmatter",
                source_path=rel,
                source_anchor="sources",
            )
        )
    for field in ("evidence", "evidences", "evidence_paths"):
        for target in _frontmatter_links(page.frontmatter.get(field)):
            edges.append(
                page_edge(
                    file_key,
                    _file_key(_with_md(target)),
                    "evidenced_by",
                    "frontmatter",
                    source_path=rel,
                    source_anchor=field,
                )
            )
    for target in _frontmatter_links(page.frontmatter.get("supersedes")):
        edges.append(
            page_edge(
                file_key,
                _file_key(_with_md(target)),
                "supersedes",
                "frontmatter",
                source_path=rel,
                source_anchor="supersedes",
            )
        )
    for target in _frontmatter_links(page.frontmatter.get("superseded_by")):
        edges.append(
            page_edge(
                _file_key(_with_md(target)),
                file_key,
                "supersedes",
                "frontmatter",
                source_path=rel,
                source_anchor="superseded_by",
            )
        )
    for target in _frontmatter_links(page.frontmatter.get("related")):
        edges.append(
            page_edge(
                file_key,
                _file_key(_with_md(target)),
                "links_to",
                "frontmatter",
                source_path=rel,
                source_anchor="related",
            )
        )
    relation_edges, canonical_lines = _relation_line_edges(
        vault_root,
        list(document.note_relations),
        rel,
        file_key,
        resolver=resolver,
        registry=registry,
        project=project,
        page_type=page.page_type,
        source_hash=source_hash,
    )
    for target in _body_wikilink_paths(
        vault_root, page.body, skip_lines=canonical_lines, resolver=resolver
    ):
        edges.append(
            page_edge(
                file_key, _file_key(_with_md(target)), "links_to", "wikilink", source_path=rel
            )
        )
    edges.extend(relation_edges)
    return _dedupe_edges(edges)


def _relation_line_edges(
    vault_root: Path,
    relations: list[MarkdownRelation],
    rel_path: str,
    file_key: str,
    *,
    resolver: vault_module.WikilinkResolver,
    registry: relation_registry.RelationRegistry,
    project: str | None = None,
    page_type: str | None = None,
    source_hash: str = "",
) -> tuple[list[GraphEdge], set[int]]:
    edges: list[GraphEdge] = []
    canonical_lines: set[int] = set()
    for relation in relations:
        try:
            canonical, warning = vault_module.normalize_wikilink(
                relation.target, vault_root, resolver=resolver, strict=False
            )
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        if not canonical:
            continue
        target_path = _with_md(canonical)
        if relation.canonical:
            canonical_lines.add(relation.line)
        edges.append(
            _edge(
                file_key,
                _file_key(target_path),
                relation.kind,
                "markdown_relation" if relation.canonical else "semantic_relation",
                source_path=rel_path,
                source_anchor=f"line-{relation.line}",
                raw_relation=relation.kind,
                registry=registry,
                project=project,
                page_type=page_type,
                source_kind="file",
                target_kind=_target_kind(vault_root, canonical),
                source_hash=source_hash,
                metadata={
                    "line": relation.raw,
                    "canonical": relation.canonical,
                    "target_resolution": "unresolved" if warning else "resolved",
                },
            )
        )
    return edges, canonical_lines


def _body_wikilink_paths(
    vault_root: Path,
    body: str,
    *,
    skip_lines: set[int],
    resolver: vault_module.WikilinkResolver,
) -> list[str]:
    """Resolve body links while omitting canonical relation bullets themselves."""
    out: list[str] = []
    seen: set[str] = set()
    for match in vault_module.find_body_wikilinks(body):
        line = body.count("\n", 0, match.start()) + 1
        if line in skip_lines:
            continue
        target = match.group(0)[2:-2].split("|", 1)[0].strip()
        if not target or target.endswith("/"):
            continue
        try:
            canonical, warning = vault_module.normalize_wikilink(
                target, vault_root, resolver=resolver, strict=False
            )
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        target_path = _with_md(canonical)
        if warning or not target_path.startswith(kb_prefix()) or target_path in seen:
            continue
        seen.add(target_path)
        out.append(target_path)
    return out


def _frontmatter_links(value: Any) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        out.extend(_links_from_string(value))
    elif isinstance(value, list):
        for item in value:
            out.extend(_frontmatter_links(item))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_frontmatter_links(item))
    return out


def _links_from_string(value: str) -> list[str]:
    matches = re.findall(r"\[\[([^\]|\n]+)(?:\|[^\]\n]+)?\]\]", value)
    if matches:
        return [m.split("#", 1)[0].strip() for m in matches if m.strip()]
    stripped = value.strip()
    return [stripped] if stripped else []


def _with_md(path: str) -> str:
    cleaned = str(path).strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2]
    cleaned = cleaned.split("|", 1)[0].split("#", 1)[0].strip().strip("/")
    if not cleaned:
        return cleaned
    if not cleaned.startswith(kb_prefix()) and "/" in cleaned:
        cleaned = kb_prefix() + cleaned.removeprefix(kb_dirname() + "/")
    return cleaned if cleaned.lower().endswith(".md") else cleaned + ".md"


def _page_project(frontmatter: dict[str, Any]) -> str | None:
    value = frontmatter.get("project")
    if value not in (None, ""):
        return str(value)
    projects = frontmatter.get("projects")
    if isinstance(projects, list) and len(projects) == 1:
        return str(projects[0])
    return None


def _target_kind(vault_root: Path, target: str) -> str:
    return "file" if (Path(vault_root) / _with_md(target)).exists() else "unresolved"


def _file_key(rel_path: str) -> str:
    return f"file:{_with_md(rel_path)}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _edge(
    src_key: str,
    dst_key: str,
    relation_type: str,
    origin: str,
    *,
    source_path: str,
    source_anchor: str | None = None,
    metadata: dict[str, Any] | None = None,
    raw_relation: str | None = None,
    registry: relation_registry.RelationRegistry | None = None,
    project: str | None = None,
    page_type: str | None = None,
    source_kind: str | None = None,
    target_kind: str | None = None,
    source_hash: str = "",
) -> GraphEdge:
    registry = registry or relation_registry.core_registry()
    raw_relation = raw_relation or relation_type
    resolution = registry.resolve(
        raw_relation,
        project=project,
        page_type=page_type,
        source_kind=source_kind,
        target_kind=target_kind,
        origin="semantic_relation" if origin == "markdown_relation" else origin,
    )
    canonical = resolution.canonical
    key_material = "\n".join(
        [src_key, dst_key, raw_relation, origin, source_path, source_anchor or ""]
    )
    edge_key = f"edge:{_hash(key_material)}"
    return GraphEdge(
        edge_key,
        src_key,
        dst_key,
        canonical,
        raw_relation,
        resolution.parent,
        resolution.status,
        registry.core_version,
        registry.extension_hash,
        origin,
        source_path,
        source_anchor,
        {
            **(metadata or {}),
            "source_hash": source_hash,
            "replacement": resolution.replacement,
            "registry_findings": list(resolution.findings),
        },
    )


def _insert_node(conn: sqlite3.Connection, node: GraphNode) -> None:
    metadata = node.metadata or {}
    is_unit = metadata.get("record_type") == "semantic_unit"
    conn.execute(
        "INSERT OR REPLACE INTO graph_nodes "
        "(node_key, kind, path, anchor, title, text, source_hash, line_start, "
        "line_end, metadata, unit_ref, unit_category, unit_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node.node_key,
            node.kind,
            node.path,
            node.anchor,
            node.title,
            node.text,
            node.source_hash,
            node.line_start,
            node.line_end,
            json.dumps(metadata, sort_keys=True),
            metadata.get("unit_ref") if is_unit else None,
            metadata.get("category") if is_unit else None,
            metadata.get("kind") if is_unit else None,
        ),
    )


def _insert_edge(conn: sqlite3.Connection, edge: GraphEdge) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO graph_edges "
        "(edge_key, src_key, dst_key, relation_type, raw_relation, parent_relation, "
        "registry_status, registry_version, registry_hash, origin, source_path, "
        "source_anchor, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge.edge_key,
            edge.src_key,
            edge.dst_key,
            edge.relation_type,
            edge.raw_relation,
            edge.parent_relation,
            edge.registry_status,
            edge.registry_version,
            edge.registry_hash,
            edge.origin,
            edge.source_path,
            edge.source_anchor,
            json.dumps(edge.metadata or {}, sort_keys=True),
        ),
    )


def _node_row_to_dict(row) -> dict[str, Any]:
    return {
        "node_key": row[0],
        "kind": row[1],
        "path": row[2],
        "anchor": row[3],
        "title": row[4],
        "text": row[5],
        "source_hash": row[6],
        "line_start": row[7],
        "line_end": row[8],
        "metadata": _json(row[9]),
    }


def _edge_row_to_dict(row) -> dict[str, Any]:
    return {
        "edge_key": row[0],
        "src_key": row[1],
        "dst_key": row[2],
        "relation_type": row[3],
        "raw_relation": row[4],
        "parent_relation": row[5],
        "registry_status": row[6],
        "registry_version": row[7],
        "registry_hash": row[8],
        "origin": row[9],
        "source_path": row[10],
        "source_anchor": row[11],
        "metadata": _json(row[12]),
    }


def _json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _seed_nodes(
    conn: sqlite3.Connection,
    *,
    path: str | None,
    query: str | None,
    unit_ref: str | None = None,
    categories: set[str] | None = None,
    kinds: set[str] | None = None,
    limit: int | None = None,
):
    select = (
        "SELECT node_key, kind, path, anchor, title, text, source_hash, "
        "line_start, line_end, metadata FROM graph_nodes"
    )
    unit_controls = unit_ref is not None or bool(categories) or bool(kinds)
    if not unit_controls:
        if path:
            rows = conn.execute(
                select + " WHERE path = ? ORDER BY kind, node_key", (_with_md(path),)
            ).fetchall()
            return [_node_row_to_dict(row) for row in rows]
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                select + " WHERE title LIKE ? OR text LIKE ? ORDER BY kind, path LIMIT 5",
                (like, like),
            ).fetchall()
            return [_node_row_to_dict(r) for r in rows]
        return []

    candidates, _has_more = _query_unit_seed_batch(
        conn,
        path=path,
        query=query,
        unit_ref=unit_ref,
        categories=categories,
        kinds=kinds,
        limit=limit or 2,
    )
    return candidates


def _query_unit_seed_batch(
    conn: sqlite3.Connection,
    *,
    path: str | None,
    query: str | None,
    unit_ref: str | None,
    categories: set[str] | None,
    kinds: set[str] | None,
    limit: int,
    after: tuple[str, str, str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    select = (
        "SELECT node_key, kind, path, anchor, title, text, source_hash, "
        "line_start, line_end, metadata FROM graph_nodes"
    )
    clauses = ["unit_ref IS NOT NULL"]
    params: list[Any] = []
    if unit_ref is not None:
        clauses.append("unit_ref = ?")
        params.append(unit_ref)
    if path:
        clauses.append("path = ?")
        params.append(_with_md(path))
    if query:
        clauses.append("(title LIKE ? OR text LIKE ?)")
        like = f"%{query}%"
        params.extend((like, like))
    if categories:
        values = sorted(categories)
        clauses.append(f"unit_category IN ({','.join('?' for _ in values)})")
        params.extend(values)
    if kinds:
        values = sorted(kinds)
        clauses.append(f"unit_kind IN ({','.join('?' for _ in values)})")
        params.extend(values)
    if after is not None:
        after_kind, after_path, after_key = after
        clauses.append(
            "(kind > ? OR (kind = ? AND path > ?) OR "
            "(kind = ? AND path = ? AND node_key > ?))"
        )
        params.extend(
            (after_kind, after_kind, after_path, after_kind, after_path, after_key)
        )
    rows = conn.execute(
        select
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY kind, path, node_key LIMIT ?",
        (*params, max(1, int(limit)) + 1),
    ).fetchall()
    has_more = len(rows) > limit
    return [_node_row_to_dict(row) for row in rows[:limit]], has_more


def _bounded_current_unit_seeds(
    conn: sqlite3.Connection,
    *,
    path: str | None,
    query: str | None,
    categories: set[str] | None,
    kinds: set[str] | None,
    max_nodes: int,
    current_record,
) -> tuple[list[dict[str, Any]], bool, bool]:
    work_budget = max_nodes * UNIT_SEED_MAX_BATCHES
    checked = 0
    seeds: list[dict[str, Any]] = []
    after: tuple[str, str, str] | None = None
    has_more = False
    while checked < work_budget and len(seeds) < max_nodes:
        batch_limit = min(max_nodes, work_budget - checked)
        batch, has_more = _query_unit_seed_batch(
            conn,
            path=path,
            query=query,
            unit_ref=None,
            categories=categories,
            kinds=kinds,
            limit=batch_limit,
            after=after,
        )
        if not batch:
            has_more = False
            break
        for index, seed in enumerate(batch):
            checked += 1
            if current_record(seed, parent_path=str(seed.get("path") or "")):
                seeds.append(seed)
                if len(seeds) >= max_nodes:
                    capped = has_more or index < len(batch) - 1
                    return seeds, capped, False
        last = batch[-1]
        after = (str(last["kind"]), str(last["path"]), str(last["node_key"]))
        if not has_more:
            break
    work_exhausted = has_more and checked >= work_budget
    return seeds, False, work_exhausted


def _unit_seed_work_truncation(max_nodes: int) -> str:
    work_budget = max_nodes * UNIT_SEED_MAX_BATCHES
    return (
        f"unit seed freshness work capped at {work_budget}; "
        "additional matching rows were not checked"
    )


def _unit_parent_work_truncation() -> str:
    return (
        "unit parent-ref validation work capped at "
        f"{UNIT_PARENT_REF_MAX_CANDIDATES}; "
        "additional indexed parents were not checked"
    )


def _unit_seed_truncation(
    *,
    max_nodes: int,
    unit_work_exhausted: bool,
    unit_parent_work_exhausted: bool,
) -> list[str]:
    truncation: list[str] = []
    if unit_work_exhausted:
        truncation.append(_unit_seed_work_truncation(max_nodes))
    if unit_parent_work_exhausted:
        truncation.append(_unit_parent_work_truncation())
    return truncation


def _filter_unit_nodes(
    nodes: list[dict[str, Any]],
    *,
    categories: set[str] | None,
    kinds: set[str] | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for node in nodes:
        metadata = node.get("metadata") or {}
        if metadata.get("record_type") != "semantic_unit":
            continue
        if categories and metadata.get("category") not in categories:
            continue
        if kinds and metadata.get("kind") not in kinds:
            continue
        filtered.append(node)
    return filtered


def _resolved_unit_filters(
    registry: semantic_language_registry.SemanticLanguageRegistry,
    values: list[str] | None,
    *,
    namespace: str,
) -> set[str] | None:
    if not values:
        return None
    resolver = registry.resolve_category if namespace == "category" else registry.resolve_kind
    resolved: set[str] = set()
    for value in values:
        resolution = resolver(value)
        resolved.add(resolution.resolved or resolution.key)
    return resolved


def _current_unit_status(
    conn: sqlite3.Connection, vault_root: Path, unit_ref: str
) -> tuple[str, list[str], list[dict[str, Any]], dict[str, int], bool]:
    parent_ref, separator, _fragment = str(unit_ref or "").rpartition("#")
    if not separator or not parent_ref:
        return "missing", [], [], {}, False
    paths, seeds, drift_counts, work_exhausted = _current_unit_parent_paths(
        conn,
        vault_root,
        parent_ref=parent_ref,
        unit_ref=unit_ref,
    )
    if work_exhausted:
        drift_counts["parent_ref_validation_work_exhausted"] = 1
        return "stale", paths, seeds, drift_counts, True
    if len(paths) > 1:
        return "ambiguous", paths, seeds, drift_counts, False
    if not paths:
        return "missing", [], [], drift_counts, False
    return "found", paths, seeds, drift_counts, False


def _current_unit_parent_paths(
    conn: sqlite3.Connection,
    vault_root: Path,
    *,
    parent_ref: str,
    unit_ref: str,
) -> tuple[list[str], list[dict[str, Any]], dict[str, int], bool]:
    rows = conn.execute(
        "SELECT path FROM graph_parent_refs WHERE parent_ref = ? "
        "ORDER BY path LIMIT ?",
        (parent_ref, UNIT_PARENT_REF_MAX_CANDIDATES + 1),
    ).fetchall()
    current_paths: list[str] = []
    current_seeds: list[dict[str, Any]] = []
    drift_counts: dict[str, int] = {}
    for row in rows[:UNIT_PARENT_REF_MAX_CANDIDATES]:
        rel = str(row[0])
        path = vault_root / rel
        try:
            source = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            drift_counts["missing_parent"] = drift_counts.get("missing_parent", 0) + 1
            continue
        except (OSError, UnicodeError):
            drift_counts["parent_unavailable"] = (
                drift_counts.get("parent_unavailable", 0) + 1
            )
            continue
        if memory_refs.ref_from_markdown(source) != parent_ref:
            drift_counts["parent_ref_mismatch"] = (
                drift_counts.get("parent_ref_mismatch", 0) + 1
            )
            continue
        try:
            state = semantic_index.current_parent_index_state(
                vault_root, path, source=source
            )
        except (TypeError, ValueError):
            drift_counts["invalid_current_parent"] = (
                drift_counts.get("invalid_current_parent", 0) + 1
            )
            continue
        resolution = state.document.resolve_unit(unit_ref)
        if resolution.status != "found" or resolution.unit is None:
            drift_counts["missing_current_unit"] = (
                drift_counts.get("missing_current_unit", 0) + 1
            )
            continue
        page = find_module._parse_page(
            path,
            0.0,
            vault_root,
            content=source.encode("utf-8"),
        )
        if page is None:
            drift_counts["invalid_current_parent"] = (
                drift_counts.get("invalid_current_parent", 0) + 1
            )
            continue
        current_paths.append(rel)
        current_seeds.append(_unit_node(page, resolution.unit, state).as_dict())
        if len(current_paths) == 2:
            return current_paths, current_seeds, drift_counts, False
    return (
        current_paths,
        current_seeds,
        drift_counts,
        len(rows) > UNIT_PARENT_REF_MAX_CANDIDATES,
    )


def _current_unit_seed_has_graph_proof(
    conn: sqlite3.Connection, seed: dict[str, Any]
) -> bool:
    metadata = seed.get("metadata")
    if not isinstance(metadata, dict):
        return False
    node_key = str(seed.get("node_key") or "")
    parent_path = str(seed.get("path") or "")
    if not node_key or not parent_path:
        return False
    origin = "semantic_block" if metadata.get("form") == "rich" else "semantic_unit"
    rows = conn.execute(
        "SELECT metadata FROM graph_edges "
        "WHERE src_key = ? AND dst_key = ? AND relation_type = 'derived_from' "
        "AND origin = ? AND source_path = ? ORDER BY edge_key LIMIT 2",
        (node_key, _file_key(parent_path), origin, parent_path),
    ).fetchall()
    generation_fields = (
        "record_type",
        "unit_ref",
        "parent_generation",
        "parent_source_hash",
        "parser_version",
    )
    return any(
        all(_json(row[0]).get(field) == metadata.get(field) for field in generation_fields)
        for row in rows
    )


def indexed_unit_parent_path_resolution(
    vault_root: Path, unit_ref: str
) -> tuple[list[str], bool]:
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return [], False
    try:
        _status, paths, _seeds, _drift_counts, work_exhausted = _current_unit_status(
            conn, vault_root, unit_ref
        )
        return paths, work_exhausted
    finally:
        conn.close()


def indexed_unit_parent_paths(vault_root: Path, unit_ref: str) -> list[str]:
    paths, _work_exhausted = indexed_unit_parent_path_resolution(vault_root, unit_ref)
    return paths


def _drift_warning(drift_counts: dict[str, int]) -> dict[str, Any]:
    return {
        "code": "semantic_unit_index_drift",
        "count": sum(drift_counts.values()),
        "reasons": dict(sorted(drift_counts.items())),
    }


def _neighbor_edges(
    conn: sqlite3.Connection,
    frontier: set[str],
    relation_filter: set[str],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    if not frontier:
        return [], False
    select = (
        "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, "
        "parent_relation, registry_status, registry_version, registry_hash, "
        "origin, source_path, source_anchor, metadata FROM graph_edges"
    )
    keys = sorted(frontier)
    placeholders = ",".join("?" for _ in keys)
    where = f" WHERE (src_key IN ({placeholders}) OR dst_key IN ({placeholders}))"
    params: list[Any] = [*keys, *keys]
    if relation_filter:
        relations = sorted(relation_filter)
        relation_placeholders = ",".join("?" for _ in relations)
        where += f" AND relation_type IN ({relation_placeholders})"
        params.extend(relations)
    rows = conn.execute(
        select + where + " ORDER BY edge_key LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    overflow = len(rows) > limit
    return [_edge_row_to_dict(row) for row in rows[:limit]], overflow


def _edge_inspection_budget(*, max_nodes: int, max_edges: int) -> int:
    """Bound raw adjacency work while leaving room for filtered/stale edges."""
    return max(1, (max_nodes + max_edges) * EDGE_INSPECTION_MULTIPLIER)


def _edge_priority(
    edge: dict[str, Any],
    profile: traversal_profiles.TraversalProfile,
    registry: relation_registry.RelationRegistry,
) -> tuple[int, str]:
    definition = registry.definition(str(edge.get("relation_type") or ""))
    candidates = [str(edge.get("relation_type") or "")]
    if definition:
        candidates.append(definition.family)
        if definition.parent:
            candidates.append(definition.parent)
    positions = [profile.priority.index(item) for item in candidates if item in profile.priority]
    return (min(positions) if positions else len(profile.priority), str(edge.get("edge_key")))


def _node_by_key(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT node_key, kind, path, anchor, title, text, source_hash, line_start, "
        "line_end, metadata FROM graph_nodes WHERE node_key = ?",
        (key,),
    ).fetchone()
    return _node_row_to_dict(row) if row else None


def _placeholder_node(key: str) -> dict[str, Any]:
    path = key.removeprefix("file:") if key.startswith("file:") else key
    title = Path(path).stem.replace("-", " ").replace("_", " ").strip() or path
    return {
        "node_key": key,
        "kind": "unresolved",
        "path": path,
        "anchor": None,
        "title": title,
        "text": "",
        "source_hash": "",
        "line_start": None,
        "line_end": None,
        "metadata": {"placeholder": True, "resolution": "unresolved"},
    }


def _nodes_by_keys(conn: sqlite3.Connection, keys: set[str]) -> list[dict[str, Any]]:
    nodes = [_node_by_key(conn, key) for key in sorted(keys)]
    return [n for n in nodes if n is not None]


def _wikilink_candidates(vault_root: Path, body: str, rel_path: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for match in vault_module.find_body_wikilinks(body):
        target = match.group(1).strip()
        try:
            canonical, warning = vault_module.normalize_wikilink(target, vault_root, strict=False)
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        if warning:
            continue
        candidates.append(
            {
                "from": rel_path,
                "to": _with_md(canonical),
                "relation_type": "links_to",
                "method": "wikilink",
                "evidence": {"source_path": rel_path, "target": target},
            }
        )
    return candidates


def _frontmatter_source_candidates(page) -> list[dict[str, Any]]:
    return [
        {
            "from": page.rel_path,
            "to": _with_md(target),
            "relation_type": "derived_from",
            "method": "frontmatter_sources",
            "evidence": {"source_path": page.rel_path, "field": "sources"},
        }
        for target in _frontmatter_links(page.frontmatter.get("sources"))
    ]


def _shared_source_candidates(vault_root: Path, rel_path: str) -> list[dict[str, Any]]:
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return []
    try:
        src_key = _file_key(rel_path)
        rows = conn.execute(
            "SELECT e2.src_key, e1.dst_key FROM graph_edges e1 "
            "JOIN graph_edges e2 ON e1.dst_key = e2.dst_key "
            "WHERE e1.src_key = ? AND e1.relation_type = 'derived_from' "
            "AND e2.relation_type = 'derived_from' AND e2.src_key != ? "
            "ORDER BY e2.src_key LIMIT 10",
            (src_key, src_key),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for other_key, shared_key in rows:
        out.append(
            {
                "from": rel_path,
                "to": other_key.removeprefix("file:"),
                "relation_type": "relates_to",
                "method": "shared_sources",
                "evidence": {"shared_source": shared_key.removeprefix("file:")},
            }
        )
    return out


def _embedding_proximity_candidates(vault_root: Path, page) -> list[dict[str, Any]]:
    """Optional embedding-proximity suggestions; empty when embeddings are off."""
    try:
        from . import corpus_aware

        scores = corpus_aware._best_cosine_per_file(
            vault_root, title=page.title, body=page.body, k=10
        )
    except Exception:  # noqa: BLE001 - writer hooks must not break Markdown writes
        return []
    out: list[dict[str, Any]] = []
    self_path = page.rel_path
    for target, score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        target_path = _with_md(target)
        if target_path == self_path:
            continue
        out.append(
            {
                "from": self_path,
                "to": target_path,
                "relation_type": "relates_to",
                "method": "embedding_proximity",
                "evidence": {"cosine": round(float(score), 4)},
            }
        )
    return out


def _draft_wikilink_candidates(
    vault_root: Path, body: str, *, draft_title: str | None
) -> list[dict[str, Any]]:
    pseudo = f"draft:{draft_title or 'untitled'}"
    candidates: list[dict[str, Any]] = []
    for match in vault_module.find_body_wikilinks(body):
        target = match.group(1).strip()
        try:
            canonical, warning = vault_module.normalize_wikilink(target, vault_root, strict=False)
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        if warning:
            continue
        candidates.append(
            {
                "from": pseudo,
                "to": _with_md(canonical),
                "relation_type": "links_to",
                "method": "wikilink",
                "evidence": {"target": target},
            }
        )
    return candidates


def _dedupe_edges(edges: list[GraphEdge]) -> list[GraphEdge]:
    out: list[GraphEdge] = []
    seen: set[str] = set()
    for edge in edges:
        if edge.edge_key in seen:
            continue
        seen.add(edge.edge_key)
        out.append(edge)
    return out


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for c in candidates:
        key = (c.get("from", ""), c.get("to", ""), c.get("relation_type", ""), c.get("method", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
