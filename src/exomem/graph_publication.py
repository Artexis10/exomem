"""Keep changed rows' predecessors for independent complete-graph readers."""

from __future__ import annotations

import sqlite3

# These five row tables are fixed by the maintained graph schema, not vocabulary.
ROW_TABLES = (
    "graph_nodes",
    "graph_edges",
    "graph_parent_refs",
    "graph_dependencies",
    "graph_dependency_coverage",
)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return list(conn.execute(f"PRAGMA main.table_info({_quote(table)})"))


def _primary_key(info: list[tuple]) -> list[str]:
    return [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]


def _same_key(keys: list[str], left: str, right: str) -> str:
    return " AND ".join(f"{left}.{_quote(key)} IS {right}.{_quote(key)}" for key in keys)


def _saved_table(table: str) -> str:
    return "graph_saved_" + table.removeprefix("graph_")


def _unrecorded(saved: str, primary: list[str], alias: str) -> str:
    return f"NOT EXISTS (SELECT 1 FROM main.{saved} AS s WHERE {_same_key(primary, 's', alias)})"


def install(conn: sqlite3.Connection) -> None:
    """Install row capture after the graph schema, without owning a transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS main.graph_publication_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    for table in ROW_TABLES:
        info = _columns(conn, table)
        names = [row[1] for row in info]
        primary = _primary_key(info)
        saved = _quote(_saved_table(table))
        definitions = ", ".join(f"{_quote(row[1])} {row[2]}" for row in info)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS main.{saved} "
            "(published_present INTEGER NOT NULL, published_rowid INTEGER, "
            f"{definitions}, PRIMARY KEY ({', '.join(_quote(key) for key in primary)}))"
        )

        old_fields = ", ".join(f"OLD.{_quote(name)}" for name in names)
        original = (
            f"INSERT INTO {saved} SELECT 1, OLD.rowid, {old_fields} "
            f"WHERE {_unrecorded(saved, primary, 'OLD')}; "
        )
        current_fields = ", ".join(f"m.{_quote(name)}" for name in names)
        missing_fields = ", ".join(
            f"NEW.{_quote(name)}" if name in primary else "NULL" for name in names
        )
        restore_absent = (
            f"INSERT INTO {saved} SELECT 0, NULL, {missing_fields} "
            f"WHERE {_unrecorded(saved, primary, 'NEW')}; "
        )
        # Capture a replacement's target before SQLite deletes it, even without recursive triggers.
        capture_new = (
            f"INSERT INTO {saved} SELECT 1, m.rowid, {current_fields} "
            f"FROM main.{_quote(table)} AS m WHERE {_same_key(primary, 'm', 'NEW')} "
            f"AND {_unrecorded(saved, primary, 'NEW')}; "
        ) + restore_absent
        retire_absent = (
            f"DELETE FROM {saved} WHERE published_present = 0 AND "
            + " AND ".join(f"{_quote(key)} IS OLD.{_quote(key)}" for key in primary)
            + f" AND NOT EXISTS (SELECT 1 FROM main.{_quote(table)} AS m WHERE "
            + _same_key(primary, "m", "OLD")
            + "); "
        )
        # Guarded SELECTs retain the first image even when outer REPLACE overrides conflict policy.
        for event, body in (
            ("INSERT", capture_new),
            ("DELETE", original),
            ("UPDATE", original + capture_new),
        ):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(_saved_table(table) + '_before_' + event)} "
                f"BEFORE {event} ON {_quote(table)} "
                "WHEN EXISTS (SELECT 1 FROM main.graph_publication_meta) "
                f"BEGIN {body} END"
            )
        # Recursive REPLACE can prune a born row's marker between BEFORE and AFTER INSERT.
        for event, body in (
            ("INSERT", restore_absent),
            ("DELETE", retire_absent),
            ("UPDATE", retire_absent + restore_absent),
        ):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(_saved_table(table) + '_after_' + event)} "
                f"AFTER {event} ON {_quote(table)} "
                "WHEN EXISTS (SELECT 1 FROM main.graph_publication_meta) "
                f"BEGIN {body} END"
            )


def publish(conn: sqlite3.Connection) -> None:
    """Publish main rows only after the caller proves a complete graph.

    The caller must include its complete-publication proof and acknowledgement
    in this same transaction. Partial repair must never call this function.
    This function neither proves completeness nor commits the transaction.
    """
    withdraw(conn)
    conn.execute(
        "INSERT INTO main.graph_publication_meta(key, value) SELECT key, value FROM main.graph_meta"
    )


def withdraw(conn: sqlite3.Connection) -> None:
    """Remove invalid publication proof and saved rows in the caller's transaction."""
    conn.execute("DELETE FROM main.graph_publication_meta")
    for table in ROW_TABLES:
        conn.execute(f"DELETE FROM main.{_quote(_saved_table(table))}")


def open_view(conn: sqlite3.Connection) -> dict[str, str] | None:
    """Read publication metadata first, then install connection-local row views.

    The caller owns the independent connection, read transaction and deadline.
    It must validate the returned proof before accepting rows; these views add
    no completeness, authorization, resolver or checkpoint proof.
    """
    metadata = dict(conn.execute("SELECT key, value FROM main.graph_publication_meta"))
    if not metadata:
        return None
    for table in ROW_TABLES:
        info = _columns(conn, table)
        primary = _primary_key(info)
        saved = _quote(_saved_table(table))
        current_fields = ", ".join(f"m.{_quote(row[1])}" for row in info)
        saved_fields = ", ".join(_quote(row[1]) for row in info)
        conn.execute(
            f"CREATE TEMP VIEW {_quote(table)} AS "
            f"SELECT m.rowid AS rowid, {current_fields} FROM main.{_quote(table)} AS m "
            f"WHERE NOT EXISTS (SELECT 1 FROM main.{saved} AS s "
            f"WHERE {_same_key(primary, 's', 'm')}) UNION ALL "
            f"SELECT published_rowid AS rowid, {saved_fields} "
            f"FROM main.{saved} WHERE published_present = 1"
        )
    return metadata
