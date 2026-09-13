#!/usr/bin/env python
"""Read-only graph catch-up proof for the mixed-load benchmark."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote


class _SnapshotUnavailable(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _SnapshotUnavailable("snapshot_unsafe")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise _SnapshotUnavailable("graph_unavailable")
    _identity(path)
    return sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)


def _digest(membership: dict[str, str]) -> str:
    rows = "".join(f"{path}\0{source_hash}\n" for path, source_hash in sorted(membership.items()))
    return hashlib.sha256(rows.encode("utf-8")).hexdigest()


def _unsafe_markdown_symlink(vault_root: Path) -> bool:
    """Reject a Markdown symlink rather than silently letting the walker skip it."""
    from exomem import vault as vault_module

    root = Path(vault_root)
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        try:
            relative = current.relative_to(root).as_posix()
        except ValueError:
            return True
        if relative != "." and vault_module.in_excluded_scan_dir(relative):
            dirnames.clear()
            continue
        for name in tuple(dirnames):
            candidate = current / name
            try:
                child_relative = candidate.relative_to(root).as_posix()
                if vault_module.in_excluded_scan_dir(child_relative):
                    dirnames.remove(name)
                    continue
                if stat.S_ISLNK(candidate.lstat().st_mode):
                    return True
            except OSError:
                return True
        for name in filenames:
            if not name.lower().endswith(".md"):
                continue
            candidate = current / name
            try:
                if stat.S_ISLNK(candidate.lstat().st_mode):
                    return True
            except OSError:
                return True
    return False


def _source_snapshot(vault_root: Path) -> tuple[dict[str, str], tuple[Any, ...]]:
    """Capture the current graph-input files through the vault's guarded read seam."""
    from exomem import find as find_module
    from exomem import recall_policy
    from exomem import vault as vault_module

    root = Path(vault_root)
    if _unsafe_markdown_symlink(root):
        raise _SnapshotUnavailable("symlink_source")
    membership: dict[str, str] = {}
    guards: list[Any] = []
    kb = root / vault_module.kb_dirname()
    try:
        candidates = find_module._walk_md(kb)
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            if not recall_policy.is_recall_candidate(root, path):
                continue
            text, guard = vault_module.read_guarded_text(root, path)
            guard.recheck(root)
            membership[relative] = vault_module.content_hash(text)
            guards.append(guard)
    except (OSError, UnicodeError, ValueError, vault_module.PathGuardError) as error:
        raise _SnapshotUnavailable("source_snapshot_unavailable") from error
    return membership, tuple(guards)


def _queue_counts(vault_root: Path) -> tuple[dict[str, int], int | None]:
    """Read deferred work directly so observation can never initialize its schema."""
    from exomem import deferred_index

    path = deferred_index.store_path(vault_root)
    if not path.exists():
        return {"graph": 0, "full": 0, "semantic": 0}, None
    try:
        before = _identity(path)
        wal_path = path.with_name(f"{path.name}-wal")
        with closing(_readonly_connection(path)) as conn:
            conn.execute("BEGIN")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            # SQLite may create an empty WAL as this connection establishes its
            # first read snapshot.  Capture the WAL revision after that point;
            # a later writer changes it without relying on the main-db mtime.
            wal_snapshot = _optional_identity(wal_path)
            required = {"graph_upserts", "full_upserts", "semantic_upserts", "maintenance_state"}
            if not required <= tables:
                raise _SnapshotUnavailable("queue_schema_unavailable")
            counts = {
                "graph": int(conn.execute("SELECT COUNT(*) FROM graph_upserts").fetchone()[0]),
                "full": int(conn.execute("SELECT COUNT(*) FROM full_upserts").fetchone()[0]),
                "semantic": int(conn.execute("SELECT COUNT(*) FROM semantic_upserts").fetchone()[0]),
            }
            row = conn.execute(
                "SELECT value FROM maintenance_state WHERE key = 'graph_full_rebuild_generation'"
            ).fetchone()
            if before != _identity(path) or wal_snapshot != _optional_identity(wal_path):
                raise _SnapshotUnavailable("queue_snapshot_changed")
    except sqlite3.Error as error:
        raise _SnapshotUnavailable("queue_schema_unavailable") from error
    if row is None:
        return counts, None
    try:
        return counts, int(row[0])
    except (TypeError, ValueError) as error:
        raise _SnapshotUnavailable("queue_schema_unavailable") from error


def _optional_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        return _identity(path)
    except FileNotFoundError:
        return None


def _expected_link_proof(
    expected_link: tuple[str, str] | None, expected_relation: str | None
) -> dict[str, str | bool | None]:
    source = target = None
    if isinstance(expected_link, tuple) and len(expected_link) == 2:
        source, target = expected_link
    return {
        "source": source if isinstance(source, str) else None,
        "target": target if isinstance(target, str) else None,
        "relation": expected_relation if isinstance(expected_relation, str) else None,
        "present": None,
    }


def _graph_snapshot(
    vault_root: Path,
    expected_link: tuple[str, str] | None,
    expected_relation: str | None,
    absent_link: tuple[str, str] | None = None,
) -> dict[str, Any]:
    from exomem import epistemic_graph

    path = epistemic_graph.sidecar_path(vault_root)
    before = _identity(path)
    try:
        with closing(_readonly_connection(path)) as conn:
            conn.execute("BEGIN")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"graph_nodes", "graph_edges", "graph_meta"} <= tables:
                raise _SnapshotUnavailable("graph_schema_unavailable")
            node_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(graph_nodes)")}
            edge_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(graph_edges)")}
            if not {"node_key", "kind", "path", "source_hash"} <= node_columns or not {
                "src_key",
                "dst_key",
                "source_path",
            } <= edge_columns:
                raise _SnapshotUnavailable("graph_schema_unavailable")
            schema = conn.execute(
                "SELECT value FROM graph_meta WHERE key = 'schema_version'"
            ).fetchone()
            if schema is None or str(schema[0]) != str(epistemic_graph.SCHEMA_VERSION):
                raise _SnapshotUnavailable("graph_schema_unavailable")
            rows = conn.execute(
                "SELECT path, source_hash FROM graph_nodes WHERE kind = 'file'"
            ).fetchall()
            membership = {str(row[0]): str(row[1]) for row in rows}
            if len(membership) != len(rows) or any(len(value) != 64 for value in membership.values()):
                raise _SnapshotUnavailable("graph_schema_unavailable")
            link_proof = _expected_link_proof(expected_link, expected_relation)
            if expected_link is not None:
                if (
                    not isinstance(expected_link, tuple)
                    or len(expected_link) != 2
                    or not all(isinstance(path, str) for path in expected_link)
                    or (
                        expected_relation is not None
                        and (not isinstance(expected_relation, str) or not expected_relation)
                    )
                ):
                    raise _SnapshotUnavailable("expected_link_invalid")
                source, target = expected_link
                sql = (
                    "SELECT 1 FROM graph_edges AS edge "
                    "JOIN graph_nodes AS destination ON destination.node_key = edge.dst_key "
                    "WHERE edge.source_path = ? AND destination.kind = 'file' "
                    "AND destination.path = ?"
                )
                params: list[str] = [source.replace("\\", "/"), target.replace("\\", "/")]
                if expected_relation is not None:
                    sql += " AND edge.relation_type = ?"
                    params.append(expected_relation)
                link_proof["present"] = conn.execute(sql + " LIMIT 1", params).fetchone() is not None
            elif expected_relation is not None:
                raise _SnapshotUnavailable("expected_link_invalid")
            absent_proof = _expected_link_proof(absent_link, expected_relation)
            if absent_link is not None:
                if expected_link is None:
                    raise _SnapshotUnavailable("expected_link_invalid")
                old_params = [value.replace("\\", "/") for value in absent_link]
                if expected_relation is not None:
                    old_params.append(expected_relation)
                absent_proof["present"] = conn.execute(sql + " LIMIT 1", old_params).fetchone() is not None
    except sqlite3.Error as error:
        raise _SnapshotUnavailable("graph_schema_unavailable") from error
    if before != _identity(path):
        raise _SnapshotUnavailable("graph_snapshot_changed")
    return {"membership": membership, "expected_link": link_proof, "absent_link": absent_proof}


def _epoch_proof(vault_root: Path) -> dict[str, Any]:
    from exomem import graph_sync

    epoch = graph_sync.classify_epoch(vault_root)
    floor = epoch.floor
    checkpoint = epoch.checkpoint
    acknowledgement = epoch.acknowledgement
    matching = (
        checkpoint is not None
        and acknowledgement == graph_sync.GraphBuildOutcome.covering(checkpoint)
    )
    return {
        "kind": epoch.kind,
        "floor_generation": floor.generation if floor is not None else None,
        "checkpoint_generation": checkpoint.generation if checkpoint is not None else None,
        "checkpoint_sha256": checkpoint.checkpoint_sha256 if checkpoint is not None else None,
        "acknowledgement_generation": acknowledgement.generation if acknowledgement is not None else None,
        "acknowledgement_sha256": (
            acknowledgement.checkpoint_sha256 if acknowledgement is not None else None
        ),
        "matching_acknowledgement": matching,
    }


def inspect_graph(
    vault: Path,
    *,
    expected_link: tuple[str, str] | None = None,
    expected_relation: str | None = None,
    absent_link: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Return a bounded, read-only proof that the graph caught up with one vault."""
    started = time.perf_counter()
    empty_membership = {"source_count": 0, "graph_count": 0, "source_digest": None, "graph_digest": None}
    proof: dict[str, Any] = {
        "ready": False,
        "reason": None,
        "epoch": {
            "kind": None,
            "floor_generation": None,
            "checkpoint_generation": None,
            "checkpoint_sha256": None,
            "acknowledgement_generation": None,
            "acknowledgement_sha256": None,
            "matching_acknowledgement": False,
        },
        "membership": empty_membership,
        "queues": {"graph": None, "full": None, "semantic": None},
        "full_rebuild_generation": None,
        "expected_link": _expected_link_proof(expected_link, expected_relation),
        "absent_link": _expected_link_proof(absent_link, expected_relation),
        "proof_elapsed_ms": 0.0,
    }
    root = Path(vault)
    try:
        before_epoch = _epoch_proof(root)
        proof["epoch"] = before_epoch
        if (
            before_epoch["kind"] != "coherent"
            or before_epoch["floor_generation"] is None
            or before_epoch["checkpoint_generation"] is None
            or before_epoch["acknowledgement_generation"] is None
            or not before_epoch["matching_acknowledgement"]
        ):
            proof["reason"] = "epoch_not_coherent"
            return proof

        queues, full_rebuild_generation = _queue_counts(root)
        proof["queues"] = queues
        proof["full_rebuild_generation"] = full_rebuild_generation
        if queues["graph"]:
            proof["reason"] = "pending_graph_work"
            return proof
        if full_rebuild_generation is not None:
            proof["reason"] = "pending_graph_full_rebuild"
            return proof

        before_sources, before_guards = _source_snapshot(root)
        snapshot = _graph_snapshot(root, expected_link, expected_relation, absent_link)
        proof["expected_link"] = snapshot["expected_link"]
        proof["absent_link"] = snapshot["absent_link"]
        for guard in before_guards:
            guard.recheck(root)
        graph_membership = snapshot["membership"]
        after_sources, after_guards = _source_snapshot(root)
        proof["membership"] = {
            "source_count": len(after_sources),
            "graph_count": len(graph_membership),
            "source_digest": _digest(after_sources),
            "graph_digest": _digest(graph_membership),
        }
        if before_sources != after_sources:
            proof["reason"] = "membership_changed"
            return proof
        if not before_sources:
            proof["reason"] = "empty_graph_membership"
            return proof
        missing = set(graph_membership) - set(before_sources)
        if missing:
            proof["reason"] = "file_missing"
            return proof
        if set(before_sources) != set(graph_membership):
            proof["reason"] = "membership_mismatch"
            return proof
        if before_sources != graph_membership:
            proof["reason"] = "file_hash_stale"
            return proof
        if expected_link is not None and not snapshot["expected_link"]["present"]:
            proof["reason"] = "expected_link_missing"
            return proof
        if absent_link is not None and snapshot["absent_link"]["present"]:
            proof["reason"] = "old_link_still_present"
            return proof

        after_epoch = _epoch_proof(root)
        if after_epoch != before_epoch:
            proof["epoch"] = after_epoch
            proof["reason"] = "epoch_changed"
            return proof
        final_queues, final_full_rebuild_generation = _queue_counts(root)
        proof["queues"] = final_queues
        proof["full_rebuild_generation"] = final_full_rebuild_generation
        if final_queues["graph"]:
            proof["reason"] = "pending_graph_work"
            return proof
        if final_full_rebuild_generation is not None:
            proof["reason"] = "pending_graph_full_rebuild"
            return proof
        for guard in (*before_guards, *after_guards):
            guard.recheck(root)
        proof["ready"] = True
    except _SnapshotUnavailable as error:
        proof["reason"] = error.reason
    except (OSError, RuntimeError, sqlite3.Error, TypeError, UnicodeError, ValueError):
        # The benchmark retains an unusable proof as evidence instead of raising.
        proof["reason"] = "proof_unavailable"
    finally:
        proof["proof_elapsed_ms"] = (time.perf_counter() - started) * 1000.0
    return proof
