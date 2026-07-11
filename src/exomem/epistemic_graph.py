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
from . import relation_registry, semantic_blocks, traversal_profiles
from . import vault as vault_module
from .kbdir import kb_dirname, kb_prefix
from .markdown_relations import MarkdownRelation, parse_markdown_relations

SCHEMA_VERSION = 2

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


class EpistemicGraphIndex:
    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.path = sidecar_path(self.vault_root)
        self.registry = relation_registry.load_registry(self.vault_root)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            from . import embeddings

            embeddings._apply_sidecar_pragmas(conn)
        except Exception:  # noqa: BLE001 - sidecar pragmas are best-effort
            pass
        columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_edges)").fetchall()}
        if columns and "raw_relation" not in columns:
            conn.execute("DROP TABLE graph_edges")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_nodes (
                node_key TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
                anchor TEXT, title TEXT, text TEXT NOT NULL, source_hash TEXT NOT NULL,
                line_start INTEGER, line_end INTEGER, metadata TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS graph_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO graph_meta(key, value) VALUES ('generation', '0')"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_nodes_path ON graph_nodes(path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_key)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_source_path ON graph_edges(source_path)"
        )
        return conn

    def available(self) -> bool:
        if not graph_enabled() or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(self.path)
            try:
                values = dict(
                    conn.execute(
                        "SELECT key, value FROM graph_meta WHERE key IN "
                        "('schema_version', 'core_registry_version', 'extension_registry_hash')"
                    ).fetchall()
                )
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return (
            values.get("schema_version") == str(SCHEMA_VERSION)
            and values.get("core_registry_version") == str(self.registry.core_version)
            and values.get("extension_registry_hash") == self.registry.extension_hash
        )

    def rebuild_all(self) -> dict[str, int]:
        if not graph_enabled():
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "disabled": 1}
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM graph_edges")
                conn.execute("DELETE FROM graph_nodes")
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
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
                    if self._index_path(conn, md):
                        indexed += 1
            with conn:
                n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
                n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            return {"indexed_files": indexed, "nodes": int(n_nodes), "edges": int(n_edges)}
        finally:
            conn.close()

    def refresh_paths(self, paths: list[Path]) -> dict[str, int]:
        if not graph_enabled():
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "disabled": 1}
        if self.path.exists() and not self.available():
            return self.rebuild_all()
        conn = self._connect()
        indexed = 0
        try:
            for path in paths:
                if self._index_path(conn, path):
                    indexed += 1
            with conn:
                n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
                n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            return {"indexed_files": indexed, "nodes": int(n_nodes), "edges": int(n_edges)}
        finally:
            conn.close()

    def delete_paths(self, rel_paths: list[str]) -> int:
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
        if not self.path.exists():
            return []
        conn = self._connect()
        try:
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
        finally:
            conn.close()
        return [_node_row_to_dict(r) for r in rows]

    def edges(self, *, source_path: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        conn = self._connect()
        try:
            select = (
                "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, "
                "parent_relation, registry_status, registry_version, registry_hash, "
                "origin, source_path, source_anchor, metadata FROM graph_edges"
            )
            if source_path is None:
                rows = conn.execute(select + " ORDER BY edge_key").fetchall()
            else:
                rows = conn.execute(
                    select + " WHERE source_path = ? ORDER BY edge_key", (_with_md(source_path),)
                ).fetchall()
        finally:
            conn.close()
        return [_edge_row_to_dict(r) for r in rows]

    def _index_path(self, conn: sqlite3.Connection, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            return False
        if not rel.lower().endswith(".md") or vault_module.in_excluded_scan_dir(rel):
            return False
        self._delete_path(conn, rel)
        if not path.exists():
            return False
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        page = find_module._parse_page(path, path.stat().st_mtime, self.vault_root)
        if page is None:
            return False
        document = semantic_blocks.parse_semantic_blocks(
            page.body, validate=False, registry=self.registry
        )
        blocks = tuple(document.blocks)
        file_node = _file_node(page, raw)
        block_nodes = [_block_node(page, block, raw) for block in blocks]
        edges = _edges_for_page(
            self.vault_root,
            page,
            blocks,
            registry=self.registry,
            source_hash=file_node.source_hash,
        )
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
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
            for node in [file_node, *block_nodes]:
                _insert_node(conn, node)
            for edge in edges:
                _insert_edge(conn, edge)
            _bump_generation(conn)
        return True

    def _delete_path(self, conn: sqlite3.Connection, rel_path: str) -> int:
        with conn:
            conn.execute("DELETE FROM graph_edges WHERE source_path = ?", (rel_path,))
            cur = conn.execute("DELETE FROM graph_nodes WHERE path = ?", (rel_path,))
            _bump_generation(conn)
        return cur.rowcount if cur.rowcount is not None else 0

    def neighbors_for(self, seeds: list[str]) -> list[GraphNeighbor]:
        """Typed edges touching `seeds` in both directions, batched over SQL.

        Two indexed lookups (`src_key IN (...)`, `dst_key IN (...)`) joined to
        `graph_nodes` on the OTHER endpoint so unresolved-placeholder targets
        (no node row) are excluded. Each result names the seed it touched, the
        neighbour file, the relation type/direction, and the registry family
        (looked up from `self.registry`, never re-parsed from YAML). Results are
        ordered by seed position then edge key, so callers can group by family
        precedence deterministically. Self-edges (block→file plumbing) drop out.
        """
        if not seeds or not self.available():
            return []
        seed_order: dict[str, int] = {}
        seed_rel_by_key: dict[str, str] = {}
        for i, seed in enumerate(seeds):
            key = _file_key(seed)
            if key not in seed_order:
                seed_order[key] = i
                seed_rel_by_key[key] = _with_md(seed)
        keys = list(seed_order)
        placeholders = ",".join("?" for _ in keys)
        conn = self._connect()
        try:
            outbound = conn.execute(
                "SELECT e.edge_key, e.src_key, e.relation_type, n.path "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.dst_key "
                f"WHERE e.src_key IN ({placeholders}) AND n.kind = 'file' "
                "ORDER BY e.edge_key",
                keys,
            ).fetchall()
            inbound = conn.execute(
                "SELECT e.edge_key, e.dst_key, e.relation_type, n.path "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.src_key "
                f"WHERE e.dst_key IN ({placeholders}) AND n.kind = 'file' "
                "ORDER BY e.edge_key",
                keys,
            ).fetchall()
        finally:
            conn.close()
        rows: list[tuple[int, str, GraphNeighbor]] = []
        for direction, batch in (("outbound", outbound), ("inbound", inbound)):
            for edge_key, seed_key, relation_type, other_path in batch:
                seed_rel = seed_rel_by_key.get(seed_key)
                if seed_rel is None or other_path == seed_rel:
                    continue
                definition = self.registry.definition(str(relation_type or ""))
                rows.append(
                    (
                        seed_order[seed_key],
                        edge_key,
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
        return [neighbor for _order, _edge_key, neighbor in rows]


def graph_context(
    vault_root: Path,
    *,
    path: str | None = None,
    query: str | None = None,
    depth: int = 1,
    relation_types: list[str] | None = None,
    node_types: list[str] | None = None,
    max_nodes: int = 40,
    max_edges: int = 80,
    traversal_profile: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, read-only graph neighborhood for a path or query."""
    idx = EpistemicGraphIndex(vault_root)
    if not idx.available():
        return {
            "available": False,
            "reason": "graph sidecar unavailable",
            "seeds": [],
            "nodes": [],
            "edges": [],
            "truncation": [],
        }
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
    conn = idx._connect()
    try:
        seeds = _seed_nodes(conn, path=path, query=query)
        if not seeds:
            return {
                "available": True,
                "reason": None,
                "seeds": [],
                "nodes": [],
                "edges": [],
                "truncation": [],
            }
        type_filter = set(node_types or [])
        seen_nodes: set[str] = {s["node_key"] for s in seeds}
        seen_edges: dict[str, dict[str, Any]] = {}
        placeholder_nodes: dict[str, dict[str, Any]] = {}
        node_cap_hit = False
        excluded_profile = 0
        excluded_scope = 0
        unknown: dict[tuple[str, str, str], dict[str, Any]] = {}
        frontier = set(seen_nodes)
        for _ in range(max(0, depth)):
            if not frontier:
                break
            rows = _neighbor_edges(conn, frontier, set())
            rows.sort(key=lambda edge: _edge_priority(edge, profile, idx.registry))
            next_frontier: set[str] = set()
            for edge in rows:
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
                if len(seen_edges) < max_edges:
                    seen_edges.setdefault(edge["edge_key"], edge)
                for key in (edge["src_key"], edge["dst_key"]):
                    if key not in seen_nodes:
                        node = _node_by_key(conn, key)
                        if node is None:
                            node = _placeholder_node(key)
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
            frontier = next_frontier
        nodes = _nodes_by_keys(conn, seen_nodes) + [
            placeholder_nodes[key] for key in sorted(placeholder_nodes)
        ]
        edges = list(seen_edges.values())
        truncation: list[str] = []
        if len(nodes) > max_nodes:
            truncation.append(
                f"nodes capped at {max_nodes} ({len(nodes) - max_nodes} more not shown)"
            )
            nodes = nodes[:max_nodes]
        elif node_cap_hit:
            truncation.append(f"nodes capped at {max_nodes}")
        if len(seen_edges) >= max_edges:
            truncation.append(f"edges capped at {max_edges}")
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
        return {
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
    """
    conn.execute(
        "UPDATE graph_meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'"
    )


def cache_token(vault_root: Path) -> tuple | None:
    """`(schema_version, extension_registry_hash, generation)` or None.

    None whenever the sidecar is unavailable (disabled, missing, or
    schema/registry drift), which the find freshness key maps to a stable
    absent-sentinel so typed-mode and fallback-mode entries never collide.
    """
    idx = EpistemicGraphIndex(vault_root)
    if not idx.available():
        return None
    try:
        conn = sqlite3.connect(idx.path)
        try:
            values = dict(
                conn.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('schema_version', 'extension_registry_hash', 'generation')"
                ).fetchall()
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return None
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
    except Exception:  # noqa: BLE001 - writer hooks must not break Markdown writes
        return


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    if not graph_enabled():
        return
    try:
        EpistemicGraphIndex(vault_root).delete_paths(removed_rel_paths)
    except Exception:  # noqa: BLE001 - writer hooks must not break Markdown writes
        return


def graph_drift(vault_root: Path) -> list[dict[str, Any]]:
    if not graph_enabled():
        return []
    idx = EpistemicGraphIndex(vault_root)
    if not idx.path.exists() or not idx.available():
        return [
            {
                "path": kb_prefix(),
                "reason": (
                    "graph sidecar missing, schema-mismatched, or "
                    "relation-registry hash drift"
                ),
            }
        ]
    by_path = {n["path"]: n for n in idx.nodes() if n["kind"] == "file"}
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


def _block_key(page, block: semantic_blocks.SemanticBlock) -> str:
    block_id = block.id or f"line-{block.line}"
    key_material = "\n".join([page.rel_path, block.type, block_id, block.title, block.body])
    return f"block:{_hash(key_material)}"


def _block_anchor(block: semantic_blocks.SemanticBlock) -> str:
    return block.id or semantic_blocks.normalize_label(block.title) or f"line-{block.line}"


def _block_node(page, block: semantic_blocks.SemanticBlock, raw_text: str) -> GraphNode:
    return GraphNode(
        node_key=_block_key(page, block),
        kind=block.type,
        path=page.rel_path,
        anchor=_block_anchor(block),
        title=block.title,
        text=block.body or block.title,
        source_hash=vault_module.content_hash(raw_text),
        line_start=block.line,
        line_end=block.end_line,
        metadata={**block.metadata, "origin": "semantic_block", "level": block.level},
    )


def _edges_for_page(
    vault_root: Path,
    page,
    blocks: tuple[semantic_blocks.SemanticBlock, ...],
    *,
    registry: relation_registry.RelationRegistry | None = None,
    source_hash: str | None = None,
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
    resolver = find_module.shared_resolver(vault_root)
    edges: list[GraphEdge] = []
    for block in blocks:
        block_key = _block_key(page, block)
        block_anchor = _block_anchor(block)
        edges.append(
            page_edge(
                block_key,
                file_key,
                "derived_from",
                "semantic_block",
                source_path=rel,
                source_anchor=block_anchor,
                metadata={"block_kind": block.type},
            )
        )
        for relation in block.relations:
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
                    source_kind=block.type,
                    target_kind=_target_kind(vault_root, canonical),
                    metadata={
                        "block_kind": block.type,
                        "line": relation.line,
                        "raw": relation.raw,
                        "target_resolution": "unresolved" if warning else "resolved",
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
    relation_doc = parse_markdown_relations(
        page.body,
        include_legacy=True,
        relation_types=registry.keys | frozenset(registry.aliases),
        retain_unknown=True,
    )
    relation_edges, canonical_lines = _relation_line_edges(
        vault_root,
        relation_doc.relations,
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
    conn.execute(
        "INSERT OR REPLACE INTO graph_nodes "
        "(node_key, kind, path, anchor, title, text, source_hash, line_start, "
        "line_end, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            json.dumps(node.metadata or {}, sort_keys=True),
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


def _seed_nodes(conn: sqlite3.Connection, *, path: str | None, query: str | None):
    select = (
        "SELECT node_key, kind, path, anchor, title, text, source_hash, "
        "line_start, line_end, metadata FROM graph_nodes"
    )
    if path:
        rows = conn.execute(
            select + " WHERE path = ? ORDER BY kind, node_key", (_with_md(path),)
        ).fetchall()
        return [_node_row_to_dict(row) for row in rows]
    if query:
        like = f"%{query}%"
        rows = conn.execute(
            select + " WHERE title LIKE ? OR text LIKE ? ORDER BY kind, path LIMIT 5", (like, like)
        ).fetchall()
        return [_node_row_to_dict(r) for r in rows]
    return []


def _neighbor_edges(
    conn: sqlite3.Connection, frontier: set[str], relation_filter: set[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    select = (
        "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, "
        "parent_relation, registry_status, registry_version, registry_hash, "
        "origin, source_path, source_anchor, metadata FROM graph_edges"
    )
    for key in sorted(frontier):
        rows = conn.execute(
            select + " WHERE src_key = ? OR dst_key = ? ORDER BY edge_key", (key, key)
        ).fetchall()
        for row in rows:
            edge = _edge_row_to_dict(row)
            if relation_filter and edge["relation_type"] not in relation_filter:
                continue
            out.append(edge)
    return out


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
    if not idx.available():
        return []
    conn = idx._connect()
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
