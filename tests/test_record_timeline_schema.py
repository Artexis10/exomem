"""Store schema 21 keeps timeline facts inside verified history (OpenSpec record-value-timeline 3.1, 3.2).

Each case names the failure only it catches. Stores are written by the maintained writer; the collections,
keys and values are invented.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from exomem.collection_store import connection, history, schema, typed_storage
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope

LAMPS = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
SPARES = "7a2d3b9f-4c5e-4f60-9bac-1d2e3f4a5b6c"


def equipment_manifest(cid, title):
    return f"""---
type: collection
exomem_id: {cid}
title: {title}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [asset_tag]
  fields:
    asset_tag: {{type: string, required: true}}
    location: {{type: string}}
    status: {{type: string}}
---
"""


def edit(writer, cid, key, changes, **arguments):
    container, version = writer.current_guards(cid, key)
    return writer.update_record(cid, item_key=key, changes=changes, expected_container_hash=container,
                                expected_item_version=version, why="move", **arguments)


def make_typed(writer, cid):
    for _ in range(16):
        if writer.migrate_typed_encoding(cid) == "ready":
            return
    raise AssertionError("typed cutover did not finish")


def every_version(raw):
    return {(row_id, version): typed_storage.version_values(raw, row_id, version)
            for row_id, version in raw.execute("SELECT row_id, row_version FROM version_identity ORDER BY 1, 2")}


def table_rows(raw, columns_of):
    """Every row of every table, read in the columns the table had before the migration."""
    return {name: raw.execute(f"SELECT {', '.join(columns)} FROM {name} ORDER BY 1, 2").fetchall()  # noqa: S608
            for name, columns in columns_of.items()}


@pytest.fixture
def schema_20_store(tmp_path, monkeypatch):
    """A JSON collection and a typed one, each with an edited row, written by the maintained writer at schema 20."""
    root, path = tmp_path / "vault", tmp_path / "collections.sqlite"
    (root / "Knowledge Base").mkdir(parents=True)
    with monkeypatch.context() as earlier:
        earlier.setattr(schema, "SCHEMA_VERSION", 20)
        with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
            writer = CollectionWriter(root, handle)
            for cid, title in ((LAMPS, "Lamps"), (SPARES, "Spares")):
                writer.create_collection(f"Knowledge Base/Records/{title}/_collection.md",
                                         equipment_manifest(cid, title), why="declare", scaffold=False)
                key = writer.append_record(cid, item={"asset_tag": "L-17", "location": "Shelf A"}, why="capture")["item_key"]
                edit(writer, cid, key, {"location": "Shelf B"})
            make_typed(writer, SPARES)
            edit(writer, SPARES, key, {"status": "lent"})
    return root, path


def test_migration_keeps_every_row_version_and_proof(schema_20_store):
    """A migration that rewrites `txns` or loses a typed table's row breaks every later proof of history."""
    root, path = schema_20_store
    with closing(sqlite3.connect(path)) as raw:
        assert schema.schema_version(raw) == 20
        assert typed_storage.collection_encoding(raw, SPARES) != typed_storage.JSON_V1
        columns_of = {
            name: [column[1] for column in raw.execute(f"PRAGMA table_info({name})")]  # noqa: S608
            for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if name not in ("store_meta", "alembic_version")
        }
        before, versions = table_rows(raw, columns_of), every_version(raw)
    assert any(name not in schema.TABLES for name in columns_of)  # the typed collection's own tables

    with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
        conn = handle.connection
        assert schema.schema_version(conn) == schema.SCHEMA_VERSION == schema.TIMELINE_SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert table_rows(conn, columns_of) == before
        assert every_version(conn) == versions
        assert conn.execute("SELECT COUNT(*) FROM txns WHERE timeline_version IS NOT NULL").fetchone() == (0,)
        for cid in (LAMPS, SPARES):
            history.verify(conn, cid)
        guarded = {table for (table,) in conn.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger' "
                                                      "AND name LIKE '%append_only_update'")}
        assert {"version_effective", "timeline_points"} <= guarded
