"""Chunked multi-row INSERT for batch writes.

Python's sqlite3 gives up the GIL at every step, and ``executemany`` takes one step per parameter
set. In a process with other busy threads each step then waits for the GIL, so a batch of N rows
waits N times for each table it writes. One ``INSERT ... VALUES`` statement per chunk takes one step.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


def insert_rows(execute, connection: sqlite3.Connection, table: str, rows: Sequence[Mapping[str, Any]],
                *, upsert_key: str | None = None) -> None:
    """Insert ``rows``, which share one key set, in chunks that stay under SQLite's variable limit.

    ``execute`` is the writer's accounting ``(sql, parameters)`` call. ``table`` and the row keys are
    schema or layout identifiers, never authored text. With ``upsert_key`` a row that meets an existing
    ``upsert_key`` replaces every other column, as the single-row upsert statements do.
    """
    if not rows:
        return
    columns = tuple(rows[0])
    head = f"INSERT INTO {table} ({','.join(columns)}) VALUES "  # noqa: S608
    tail = "" if upsert_key is None else (
        f" ON CONFLICT({upsert_key}) DO UPDATE SET "
        + ",".join(f"{name}=excluded.{name}" for name in columns if name != upsert_key))
    marks = "(" + ",".join("?" * len(columns)) + ")"
    chunk = max(1, connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) // len(columns))
    for start in range(0, len(rows), chunk):
        part = rows[start:start + chunk]
        execute(head + ",".join([marks] * len(part)) + tail, [row[name] for row in part for name in columns])
