"""The proof that runs before store mode activates (OpenSpec make-vault-store-canonical D8, task 1.4).

Two questions, each answered from the files and nothing else. Do the rows that migration copied in equal what the
owners derive from the files now? Does projecting the stored pages reproduce each file's bytes?
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from .. import epistemic_graph, lexstore, memory_refs, vault
from ..governance import page_admission
from . import projection

#: How many differing keys a refusal names per table.
KEYS_REPORTED = 5

# The owners name the tables that describe a sidecar's own history, and the table that only its row presence derives.
_BUILD_STAMP_TABLES = frozenset((*lexstore.BUILD_STAMP_TABLES, *epistemic_graph.BUILD_STAMP_TABLES))
_KEY_ONLY_TABLES = frozenset(lexstore.KEY_ONLY_TABLES)


@dataclass(frozen=True, slots=True)
class Difference:
    """One table whose copied rows differ from the derived rows: how many rows, and the first few keys."""

    table: str
    rows: int
    keys: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.table}: {self.rows} row(s) differ, e.g. {', '.join(self.keys)}"


def derive_expected(vault_root: Path, path: Path) -> None:
    """Derive the lexical, graph, reference and inbound-link rows from the files with the owners' own builders into a new database."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        lexstore.create_catalogue_schema(conn)
        epistemic_graph.create_row_schema(conn, publication=False)
        memory_refs.create_identity_schema(conn)
        vault.create_inbound_schema(conn)
        page_admission.create_schema(conn)
        lexstore.derive_catalogue_rows(vault_root, conn)
        epistemic_graph.derive_row_tables(vault_root, conn)
        memory_refs.derive_identities(vault_root, conn)
        vault.derive_inbound_rows(vault_root, conn)
        page_admission.derive_files(vault_root, conn)
        conn.execute("COMMIT")
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class Comparison:
    """The tables a comparison covered, and those among them that differ."""

    tables: tuple[str, ...]
    differences: tuple[Difference, ...]


def compare_tables(
    staged: sqlite3.Connection, expected_path: Path, *, only: Collection[str] | None = None
) -> Comparison:
    """Compare every derived table of ``staged`` with the one in ``expected_path``, whole table by natural key.

    A table that differs is named with the number of differing rows and up to ``KEYS_REPORTED`` of its keys.
    Row ids are surrogates and never compared: a full-text row is read through the key of the row it indexes.
    ``only`` limits the comparison to those tables, for a database that holds just some of them.
    """
    staged.execute("ATTACH DATABASE ? AS expected", (str(expected_path),))
    try:
        compared, found = [], []
        for table, kind in staged.execute(
            "SELECT name, type FROM pragma_table_list WHERE schema = 'expected' "
            "AND type IN ('table', 'virtual') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall():
            if table in _BUILD_STAMP_TABLES or (only is not None and table not in only):
                continue
            compared.append(table)
            difference = _table_difference(staged, table, indexed=kind == "virtual")
            if difference is not None:
                found.append(difference)
        return Comparison(tuple(compared), tuple(found))
    finally:
        staged.execute("DETACH DATABASE expected")


def _table_difference(
    staged: sqlite3.Connection, table: str, *, indexed: bool
) -> Difference | None:
    view = _derived_view(staged, table, indexed=indexed)
    key = " || '|' || ".join(f"COALESCE(CAST({column} AS TEXT), '')" for column in view.key)
    differing = (
        f"{view.current} EXCEPT {view.expected}",
        f"{view.expected} EXCEPT {view.current}",
    )
    rows = sum(staged.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0] for sql in differing)
    if not rows:
        return None
    keys = staged.execute(
        f"SELECT DISTINCT k FROM (SELECT {key} AS k FROM ({differing[0]}) UNION ALL SELECT {key} AS k FROM ({differing[1]})) "
        f"ORDER BY k LIMIT {KEYS_REPORTED}"
    ).fetchall()
    return Difference(table, rows, tuple(str(row[0]) for row in keys))


@dataclass(frozen=True, slots=True)
class _View:
    """The canonical key and the selections that put both sides in the same column order."""

    key: tuple[str, ...]
    current: str
    expected: str


def _derived_view(conn: sqlite3.Connection, table: str, *, indexed: bool) -> _View:
    columns = _columns(conn, "expected", table)
    if not indexed:
        key = _natural_key(conn, table, columns)
        compared = key if table in _KEY_ONLY_TABLES else columns
        current = set(_columns(conn, "main", table))
        # An older sidecar has no value for a later optional field. Project a
        # NULL explicitly instead of SQLite interpreting a missing name as text.
        values = ", ".join(f't."{c}"' if c in current else f'NULL AS "{c}"' for c in compared)
        expected = ", ".join(f't."{c}"' for c in compared)
        return _View(
            tuple(f'"{c}"' for c in key),
            f'SELECT {values} FROM main."{table}" AS t',
            f'SELECT {expected} FROM expected."{table}" AS t',
        )
    parent = lexstore.FULLTEXT_PARENT_TABLES[table]
    parent_columns = _columns(conn, "expected", parent)
    key = _natural_key(conn, parent, parent_columns)
    # A row left over from a deleted parent has no key, so it differs from the derived rows by its text.
    values = ", ".join([*(f'p."{c}" AS "{c}"' for c in key), *(f'i."{c}"' for c in columns)])
    source = f'{{schema}}."{table}" AS i LEFT JOIN {{schema}}."{parent}" AS p ON p.rowid = i.rowid'
    return _View(
        tuple(f'"{c}"' for c in key),
        f"SELECT {values} FROM {source.format(schema='main')}",
        f"SELECT {values} FROM {source.format(schema='expected')}",
    )


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _natural_key(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[str]:
    """The columns of the table's declared primary key, else of its first unique index, else all of them."""
    unique = [
        (origin, name)
        for _, name, unique_flag, origin, _ in conn.execute(f'PRAGMA expected.index_list("{table}")')
        if unique_flag
    ]
    for wanted in ("pk", "u"):
        for origin, name in unique:
            if origin == wanted:
                return [row[2] for row in conn.execute(f'PRAGMA expected.index_info("{name}")')]
    return columns


def projection_differences(staged: sqlite3.Connection, scratch_root: Path) -> list[str]:
    """Project every stored page into ``scratch_root`` and name each file whose bytes differ from its stored bytes.

    It writes through the one projection writer, so the check covers the code that later puts pages on disk.
    """
    differing = []
    # One page body at a time: the cursor streams the rows, so memory holds a page and not the vault.
    for rel, body, sha256 in staged.execute(
        "SELECT path, body, sha256 FROM store_pages ORDER BY path"
    ):
        written = projection.write_file(scratch_root, rel, body, durable=False).read_bytes()
        if hashlib.sha256(written).hexdigest() != sha256 or written != body:
            differing.append(rel)
    return differing
