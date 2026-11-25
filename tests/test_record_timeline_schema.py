"""Store schema 21 keeps timeline facts inside verified history (OpenSpec record-value-timeline 3.1, 3.2).

Each case names the failure only it catches. Stores are written by the maintained writer; the collections,
keys and values are invented.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from test_typed_history_generations import before_timeline_facts

from exomem.collection_store import chain, connection, history, schema, typed_storage
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
        before_timeline_facts(earlier)
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


@pytest.fixture
def timeline_store(tmp_path):
    """Lamp L-17 at schema 21: a stated append, a late move, a missed move, a correction of the late move and an
    edit that states no time, so it records no fact and carries no marker.

    Returns the store path and each write's transition id, by name.
    """
    root, path = tmp_path / "vault", tmp_path / "collections.sqlite"
    (root / "Knowledge Base").mkdir(parents=True)
    with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
        writer = CollectionWriter(root, handle)
        writer.create_collection("Knowledge Base/Records/Lamps/_collection.md", equipment_manifest(LAMPS, "Lamps"),
                                 why="declare", scaffold=False)
        key = writer.append_record(LAMPS, item={"asset_tag": "L-17", "location": "Shelf A"}, why="capture",
                                   effective_at="2026-03-01")["item_key"]
        writes = {"move": edit(writer, LAMPS, key, {"location": "Shelf B"}, effective_at="2026-03-07"),
                  "missed": edit(writer, LAMPS, key, {"location": "Repair Bench"}, effective_at="2026-03-03")}
        writes["fix"] = edit(writer, LAMPS, key, {"location": "Shelf C"}, corrects=writes["move"]["audit_correlation"])
        writes["lent"] = edit(writer, LAMPS, key, {"status": "lent"})
    return path, {name: reply["audit_correlation"] for name, reply in writes.items()}


@pytest.mark.parametrize(("table", "statement", "write"), [
    ("version_effective", "UPDATE version_effective SET effective_at='2026-03-08' WHERE effective_at='2026-03-07'",
     "move"),
    ("timeline_points", "INSERT INTO timeline_points(row_id,field_path,txn_id,row_version,placement,effective_at,"
     "value_json,corrects_txn) SELECT e.row_id,'status',e.txn_id,e.version_after,'past','2026-03-02','\"lent\"',NULL "
     "FROM audit_effects e JOIN txns t ON t.txn_id=e.txn_id WHERE t.transition_id=:lent", "lent"),
    ("timeline_points", "DELETE FROM timeline_points WHERE placement='past'", "missed"),
    ("txns", "UPDATE txns SET timeline_version=NULL WHERE transition_id=:fix", "fix"),
], ids=["changed effective time", "added point on an unmarked write", "removed point", "cleared marker"])
def test_a_timeline_fact_changed_outside_the_writer_fails_verification_at_its_write(timeline_store, table,
                                                                                    statement, write):
    """A fact outside the event hash can be rewritten unseen, so history no longer proves what was believed."""
    path, transitions = timeline_store
    with closing(sqlite3.connect(path)) as raw:
        for (trigger,) in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)):
            raw.execute(f'DROP TRIGGER "{trigger}"')
        raw.execute(statement, transitions)
        raw.commit()
        [(commit_seq,)] = raw.execute("SELECT commit_seq FROM txns WHERE transition_id=?", (transitions[write],))

    with closing(connection.open_reader(path)) as reader, pytest.raises(chain.StoreChainError) as broken:
        history.verify(reader, LAMPS)

    assert broken.value.commit_seq == commit_seq


def test_a_sound_store_with_timeline_facts_verifies_after_a_cold_reopen(timeline_store):
    """A member rebuilt from the tables in another order or encoding than the writer hashed breaks every reopen."""
    path, _ = timeline_store

    with closing(connection.open_reader(path)) as reader:
        history.verify(reader, LAMPS)
        assert reader.execute("SELECT COUNT(*) FROM version_effective").fetchone() == (3,)
        assert reader.execute("SELECT placement, corrects_txn IS NOT NULL FROM timeline_points "
                              "ORDER BY txn_id").fetchall() == [("past", 0), ("current", 1)]
