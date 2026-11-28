"""Durable cursors for fragment links that a page edit must re-land."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from sqlite3 import Connection, OperationalError


def create_schema(conn: Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS graph_fragment_pass (
            path TEXT PRIMARY KEY, revision INTEGER NOT NULL,
            lookup_keys TEXT NOT NULL, key_offset INTEGER NOT NULL, after_path TEXT NOT NULL
        )
    """)


@dataclass(frozen=True, slots=True)
class FragmentPass:
    path: str
    revision: int
    keys: tuple[str, ...]
    key_offset: int = 0
    after: str = ""

    def batch(
        self, conn: Connection, limit: int,
    ) -> tuple[list[str], FragmentPass | None]:
        """Seek at most `limit` source paths across the target's indexed dependency names."""
        found = []
        after = self.after
        for index in range(self.key_offset, len(self.keys)):
            remaining = limit - len(found)
            paths = [path for (path,) in conn.execute(
                "SELECT DISTINCT source_path FROM graph_dependencies "
                "WHERE lookup_key = ? AND source_path > ? AND instr(raw_target, '#') > 0 "
                "ORDER BY source_path LIMIT ?",
                (self.keys[index], after, remaining + 1),
            )]
            found.extend(paths[:remaining])
            if len(paths) > remaining:
                return found, replace(self, key_offset=index, after=found[-1] if remaining else after)
            after = ""
        return found, None


def for_intents(conn: Connection, intents: Sequence[tuple[str, int]]) -> dict[str, FragmentPass]:
    """Return cursors that belong to the current receipts. A newer edit supersedes its cursor."""
    if not intents:
        return {}
    try:
        rows = conn.execute(
            "SELECT pass.path, pass.revision, pass.lookup_keys, pass.key_offset, pass.after_path "
            "FROM json_each(?) AS wanted JOIN graph_fragment_pass AS pass "
            "ON pass.path = json_extract(wanted.value, '$[0]') "
            "AND pass.revision = json_extract(wanted.value, '$[1]')",
            (json.dumps(intents),),
        ).fetchall()
    except OperationalError:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'graph_fragment_pass'"
        ).fetchone() is not None:
            raise
        return {}  # The owner upgrade has not created its derived objects yet.
    return {path: FragmentPass(path, revision, tuple(json.loads(keys)), index, after)
            for path, revision, keys, index, after in rows}


def save(conn: Connection, cursor: FragmentPass) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO graph_fragment_pass(path, revision, lookup_keys, key_offset, after_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (cursor.path, cursor.revision, json.dumps(cursor.keys), cursor.key_offset, cursor.after),
    )


def discard(conn: Connection, paths: Sequence[str]) -> None:
    try:
        conn.executemany("DELETE FROM graph_fragment_pass WHERE path = ?", ((path,) for path in paths))
    except OperationalError:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'graph_fragment_pass'"
        ).fetchone() is not None:
            raise
