"""Durable cursors for graph dependencies that a page edit must re-land."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from sqlite3 import Connection, OperationalError


def create_schema(conn: Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS graph_dependency_pass (
            path TEXT PRIMARY KEY, revision INTEGER NOT NULL,
            lookup_keys TEXT NOT NULL, fragments_only INTEGER NOT NULL,
            key_offset INTEGER NOT NULL, after_path TEXT NOT NULL
        )
    """)


@dataclass(frozen=True, slots=True)
class DependencyPass:
    path: str
    revision: int
    keys: tuple[str, ...]
    fragments_only: bool
    key_offset: int = 0
    after: str = ""

    def batch(
        self, conn: Connection, limit: int, *, covered: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[list[str], DependencyPass | None]:
        """Seek at most `limit` source paths across the target's indexed dependency names."""
        found: list[str] = []
        found_paths: set[str] = set()
        after = self.after
        filter_fragment = " AND instr(raw_target, '#') > 0" if self.fragments_only else ""
        for index in range(self.key_offset, len(self.keys)):
            rows = conn.execute(
                "SELECT DISTINCT source_path FROM graph_dependencies "
                f"WHERE lookup_key = ? AND source_path > ?{filter_fragment} "
                "ORDER BY source_path LIMIT ?",
                (self.keys[index], after, limit + len(covered) + 1),
            )
            for (path,) in rows:
                if path in covered or path in found_paths:
                    after = path
                    continue
                if len(found) == limit:
                    return found, replace(self, key_offset=index, after=after)
                found.append(path)
                found_paths.add(path)
                after = path
            after = ""
        return found, None


def for_intents(conn: Connection, intents: Sequence[tuple[str, int]]) -> dict[str, DependencyPass]:
    """Return cursors that belong to the current receipts. A newer edit supersedes its cursor."""
    cursors = for_paths(conn, [path for path, _ in intents])
    return {path: cursors[path] for path, revision in intents
            if path in cursors and cursors[path].revision == revision}


def for_paths(conn: Connection, paths: Sequence[str]) -> dict[str, DependencyPass]:
    if not paths:
        return {}
    try:
        rows = conn.execute(
            "SELECT path, revision, lookup_keys, fragments_only, key_offset, after_path "
            "FROM graph_dependency_pass WHERE path IN (SELECT value FROM json_each(?))",
            (json.dumps(paths),),
        ).fetchall()
    except OperationalError:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'graph_dependency_pass'"
        ).fetchone() is not None:
            raise
        return {}  # The owner upgrade has not created its derived objects yet.
    return {path: DependencyPass(path, revision, tuple(json.loads(keys)), bool(fragments), index, after)
            for path, revision, keys, fragments, index, after in rows}


def save(conn: Connection, cursor: DependencyPass) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO graph_dependency_pass"
        "(path, revision, lookup_keys, fragments_only, key_offset, after_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cursor.path, cursor.revision, json.dumps(cursor.keys), cursor.fragments_only,
         cursor.key_offset, cursor.after),
    )


def discard(conn: Connection, paths: Sequence[str]) -> None:
    try:
        conn.executemany("DELETE FROM graph_dependency_pass WHERE path = ?", ((path,) for path in paths))
    except OperationalError:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'graph_dependency_pass'"
        ).fetchone() is not None:
            raise
