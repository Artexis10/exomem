"""Forward installation and retired layouts cannot reinterpret retained history."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import text
from test_collection_store_typed_storage import bulk, create, migrate, row_ids
from test_collection_store_writer import CID
from test_collection_store_writer import store as store

from exomem.collection_store import (
    connection,
    edit_back,
    governance,
    history,
    index_migrations,
    schema,
    snapshot,
    source_dependencies,
    tables,
    typed_storage,
    views,
    writer,
)
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope


def before_retractable_rows(patch):
    """Let the maintained writer seed a store from before schema 19, whose ``items`` is the one table of rows.

    The writer names ``item_rows``. The seeding session renames the table to it, and every file the session
    leaves, the store itself and each snapshot of it, gets its old name back. The upgrade then opens the
    shape a release before schema 19 wrote.
    """
    ensure_schema, close = schema.ensure_schema, connection.WriterConnection.close
    backup_fill, collection_fill = snapshot._backup_fill, snapshot._collection_fill

    def renamed(raw):
        return raw.execute("SELECT 1 FROM sqlite_master WHERE name='item_rows'").fetchone() is not None

    def rename_to_the_writers_name(raw):
        raw.execute("ALTER TABLE items RENAME TO item_rows")
        columns = ", ".join(row[1] for row in raw.execute("PRAGMA table_info(item_rows)"))
        raw.execute("ALTER TABLE item_rows ADD COLUMN retracted_txn INTEGER")
        raw.execute(f"CREATE VIEW items AS SELECT {columns} FROM item_rows WHERE retracted_txn IS NULL")

    def give_the_old_name_back(raw):
        if renamed(raw):
            raw.execute("DROP VIEW items")
            raw.execute("ALTER TABLE item_rows DROP COLUMN retracted_txn")
            raw.execute("ALTER TABLE item_rows RENAME TO items")

    def seeding_schema(conn):
        version = ensure_schema(conn)
        raw = conn.connection.driver_connection
        if version < 19 and not renamed(raw):
            rename_to_the_writers_name(raw)
        return version

    def closing_store(self):
        if not self._closed:
            give_the_old_name_back(self.connection)
        close(self)

    def copying(fill_for):
        def filled(*args, **kwargs):
            fill = fill_for(*args, **kwargs)

            def copy(destination, *checks):
                fill(destination, *checks)
                give_the_old_name_back(destination)

            return copy

        return filled

    patch.setattr(schema, "ensure_schema", seeding_schema)
    patch.setattr(connection.WriterConnection, "close", closing_store)
    patch.setattr(snapshot, "_backup_fill", copying(backup_fill))
    patch.setattr(snapshot, "_collection_fill", copying(collection_fill))


def before_source_dependencies(patch):
    """Make the maintained writer seed v1 events, as every release before schema 18 wrote them."""
    before_retractable_rows(patch)  # a store from before schema 18 is also from before schema 19
    # A store before schema 18 has no event_version column, no version-Source association table and no timeline marker.
    columns = tuple(column.name for column in tables.txns.c if column.name not in ("event_version", "timeline_version"))
    patch.setattr(tables, "INSERT_TXN", text(
        f"INSERT INTO txns({','.join(columns)}) VALUES({','.join(':' + name for name in columns)})"))
    current_txn = writer.CollectionWriter._txn

    def historical_txn(self, *args, **kwargs):
        kwargs.pop("_source_inputs", None)
        return current_txn(self, *args, **kwargs)

    patch.setattr(writer.CollectionWriter, "_txn", historical_txn)

    def historical_dependencies(current, prepared, _arguments):
        identity = views.store_identity(current.connection)
        return source_dependencies.DependencyPlan("historical-seed", identity["store_id"], identity["instance_id"],
            prepared.manifest.collection_id, (), edit_back.NativeInputs((), True))

    patch.setattr(source_dependencies, "_bulk_plan", historical_dependencies)


def previous_release(patch):
    """Seed the historical schema-10 identity and payload wire through the maintained writer."""
    patch.setattr(schema, "SCHEMA_VERSION", 10)
    before_source_dependencies(patch)

    def historical_watermark(conn, cid, **_options):
        # This producer predates the canonical verifier; only the current reader verifies after upgrade.
        return conn.execute("SELECT verified_through_txn FROM collections WHERE collection_id=?", (cid,)).fetchone()[0]

    patch.setattr(history, "verify", historical_watermark)
    current_subjects = governance.iter_subjects

    def historical_subjects(conn, cid, logical_vault_id, **options):
        # These hold-free historical seeds have no schema-12 quarantine column.
        assert conn.execute("SELECT 1 FROM held_candidates WHERE collection_id=? LIMIT 1", (cid,)).fetchone() is None
        options["include_held"] = False
        return current_subjects(conn, cid, logical_vault_id, **options)

    patch.setattr(governance, "iter_subjects", historical_subjects)
    # The old writer has no native conversion diagnostics or schema-11 column.
    patch.setattr(typed_storage, "migration_diagnostic", lambda *_: None)
    trigger = typed_storage._identity_trigger
    patch.setattr(typed_storage, "_identity_trigger",
                  lambda layout, generation_bound=False: trigger(layout, generation_bound=False))
    # Schema 10 has six identity columns; this is its fixed historical INSERT.
    # Schema 10 has no item key scheme column; its collections take the column default on upgrade.
    patch.setattr(writer, "INSERT_COLLECTION", writer.INSERT_COLLECTION.replace(", item_key_scheme)", ")")
                  .replace(", :item_key_scheme)", ")"))
    current_manifest = writer.CollectionWriter._collection_manifest
    patch.setattr(writer.CollectionWriter, "_collection_manifest",
                  lambda self, row: current_manifest(self, {"item_key_scheme": "hashed-v1", **dict(row)}))
    patch.setattr(tables, "INSERT_IDENTITY", text(
        "INSERT INTO version_identity(row_id,row_version,encoding,payload_hash,txn_id,schema_version) "
        "VALUES(:row_id,:row_version,:encoding,:payload_hash,:txn_id,:schema_version)"))
    # Schema 10 has no typed-v2, so a new summary collection published typed-v1.
    batch = typed_storage.migrate_batch
    patch.setattr(typed_storage, "migrate_batch",
                  lambda conn, cid, fields, *, limit=128, encoding=None: batch(conn, cid, fields, limit=limit))


@pytest.fixture
def historical_store(tmp_path, monkeypatch):
    """Seed the shipped schema-10 identity wire through the maintained writer."""
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    path = tmp_path / "historical.sqlite"
    with monkeypatch.context() as old:
        previous_release(old)
        with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
            writer = CollectionWriter(root, handle)
            create(writer)
            bulk(writer, {"title": "Entry", "amount": 1})
            assert migrate(writer) == "ready"
            bulk(writer, {"title": "Entry", "amount": -0.0})
            row_id = next(iter(row_ids(writer).values()))
            layout = typed_storage.require_layout(handle.connection, CID)
            before = {name: handle.connection.execute(f"SELECT * FROM {name}").fetchall()
                      for name in ("item_versions", "version_identity", "item_sources", "txns", layout.version_table)}
    return path, root, row_id, layout, before


def test_schema10_upgrade_binds_exact_history_and_preserves_source_spine(historical_store):
    """The new column must bind existing typed payloads without minting or rewriting versions."""
    path, root, row_id, layout, before = historical_store
    with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
        conn = handle.connection
        assert schema.schema_version(conn) == schema.SCHEMA_VERSION
        for name, rows in before.items():
            after = conn.execute(f"SELECT * FROM {name}").fetchall()
            if name == "version_identity":
                assert [row[:6] for row in after] == rows
            elif name == "txns":
                # Migration 18 adds the event version, 1 for every earlier event; no earlier event has a timeline marker.
                assert [row[:-2] for row in after] == rows
                assert all(row[-2:] == (1, None) for row in after)
            else:
                assert after == rows
        assert conn.execute("SELECT encoding,payload_generation FROM version_identity ORDER BY row_version").fetchall() == [
            ("json-v1", None), ("typed-v1", layout.generation)]
        writer = CollectionWriter(root, handle)
        assert bulk(writer, {"title": "Entry", "amount": 2**64}) == ["updated"]
    with closing(connection.open_reader(path)) as reader:
        values = [typed_storage.version_values(reader, row_id, version)[0] for version in (1, 2, 3)]
        assert json.dumps(values, sort_keys=True) == json.dumps([
            {"title": "Entry", "amount": 1}, {"title": "Entry", "amount": -0.0},
            {"title": "Entry", "amount": 2**64}], sort_keys=True)
        assert reader.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("damage", ["descriptor", "missing-payload", "wrong-payload", "ambiguous"])
def test_unproved_schema10_binding_rolls_back_the_installation(historical_store, damage):
    """A missing, ambiguous or false old binding must leave schema 10 and its history unchanged."""
    path, _, row_id, layout, _ = historical_store
    with sqlite3.connect(path) as raw:
        if damage == "descriptor":
            raw.execute("UPDATE typed_encoding_mappings SET layout_hash=?", ("0" * 64,))
        elif damage == "ambiguous":
            raw.execute("DROP INDEX typed_encoding_one_ready")
            raw.execute("INSERT INTO typed_encoding_mappings SELECT collection_id,generation+1,state,layout_json,"
                        "layout_hash,last_row_id FROM typed_encoding_mappings")
        else:
            for suffix in ("append_only_delete", "append_only_update"):
                raw.execute(f"DROP TRIGGER {layout.version_table}_{suffix}")
            if damage == "missing-payload":
                raw.execute(f"DELETE FROM {layout.version_table} WHERE row_id=?", (row_id,))
            else:
                raw.execute(f"UPDATE {layout.version_table} SET body='Altered' WHERE row_id=?", (row_id,))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(typed_storage.TypedStorageError):
        connection.open_writer(path, lease_check=lambda: True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as raw:
        assert schema.schema_version(raw) == 10
        assert len(raw.execute("PRAGMA table_info(version_identity)").fetchall()) == 6


def test_retired_layout_still_decodes_its_versions_after_new_generation_and_reopen(store):
    """A new ready layout must neither decode old bytes nor require a payload in every history table."""
    create(store)
    assert migrate(store) == "ready"
    bulk(store, {"title": "Entry", "amount": -0.0})
    conn = store.connection
    row_id = next(iter(row_ids(store).values()))
    first = typed_storage.require_layout(conn, CID)
    second = typed_storage.Layout(CID, first.generation + 1, tuple(reversed(first.fields)))
    with store.handle.transaction() as tx:
        writer = index_migrations.AccountedWriter(tx, store._execute)
        typed_storage._create_tables(writer, second)
        typed_storage._upsert_current(writer, second, row_id, 1, typed_storage.encode_row(
            second, typed_storage.current_values(tx, first, row_id)))
        tx.execute("UPDATE typed_encoding_mappings SET state='retired' WHERE collection_id=?", (CID,))
        tx.execute("INSERT INTO typed_encoding_mappings(collection_id,generation,state,layout_json,layout_hash) "
                   "VALUES(?,?,'ready',?,?)", (CID, second.generation, second.encoded,
                                             hashlib.sha256(second.encoded.encode()).hexdigest()))
        tx.execute(typed_storage._identity_trigger(second))
    assert bulk(store, {"title": "Entry", "amount": 2**64}) == ["updated"]
    path = store.handle.path
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as reopened:
        assert typed_storage.version_values(reopened.connection, row_id, 1) == ({"amount": -0.0, "title": "Entry"}, "")
        assert typed_storage.version_values(reopened.connection, row_id, 2) == ({"amount": 2**64, "title": "Entry"}, "")
        assert reopened.connection.execute("SELECT payload_generation FROM version_identity ORDER BY row_version").fetchall() == [
            (first.generation,), (second.generation,)]
        with pytest.raises(sqlite3.IntegrityError, match="ready typed generation"), reopened.transaction():
            _, _, _, insert_version = typed_storage._declarations(first)
            encoded = typed_storage.encode_row(first, {"title": "Stale", "amount": 1})
            reopened.execute(insert_version, {"row_id": row_id, "row_version": 3, "body": "",
                **dict(zip((*first.value_columns, "r"), encoded, strict=True))})
        # Restored guards must keep both retained descriptors and the owning collection stable.
        for statement, parameters in (
            ("UPDATE typed_encoding_mappings SET layout_json='{}'", ()),
            ("DELETE FROM typed_encoding_mappings", ()),
            ("INSERT OR REPLACE INTO typed_encoding_mappings SELECT * FROM typed_encoding_mappings", ()),
            ("UPDATE typed_encoding_mappings SET state='ready' WHERE generation=?", (first.generation,)),
            ("UPDATE item_rows SET collection_id=?", ("00000000-0000-4000-8000-000000000001",)),
        ):
            with pytest.raises(sqlite3.IntegrityError), reopened.transaction() as tx:
                tx.execute(statement, parameters)


def test_generation_switch_refuses_a_payload_staged_before_retirement(store):
    """Even one transaction cannot attach payloads in two generations to one version identity."""
    create(store)
    assert migrate(store) == "ready"
    bulk(store, {"title": "Entry", "amount": 1})
    row_id = next(iter(row_ids(store).values()))
    first = typed_storage.require_layout(store.connection, CID)
    second = typed_storage.Layout(CID, first.generation + 1, first.fields)
    current = store.connection.execute("SELECT payload_hash,updated_txn,schema_version FROM items WHERE row_id=?", (row_id,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="generation-bound typed payload"), store.handle.transaction() as tx:
        writer = index_migrations.AccountedWriter(tx, store._execute)
        # This payload is legal while its generation is ready, before the switch.
        _, _, _, insert_version = typed_storage._declarations(first)
        encoded = typed_storage.encode_row(first, {"title": "Entry", "amount": 1})
        writer.execute(insert_version, {"row_id": row_id, "row_version": 2, "body": "",
            **dict(zip((*first.value_columns, "r"), encoded, strict=True))})
        typed_storage._create_tables(writer, second)
        tx.execute("UPDATE typed_encoding_mappings SET state='retired' WHERE collection_id=?", (CID,))
        tx.execute("INSERT INTO typed_encoding_mappings(collection_id,generation,state,layout_json,layout_hash) "
                   "VALUES(?,?,'ready',?,?)", (CID, second.generation, second.encoded,
                                             hashlib.sha256(second.encoded.encode()).hexdigest()))
        tx.execute(typed_storage._identity_trigger(second))
        typed_storage.write_version(writer, second, row_id=row_id, row_version=2,
            values={"title": "Entry", "amount": 1}, body="", payload_hash=current[0], txn_id=current[1], schema_version=current[2])
    assert typed_storage.require_layout(store.connection, CID) == first
    assert store.connection.execute("SELECT count(*) FROM version_identity").fetchone() == (1,)
    assert store.connection.execute(f"SELECT count(*) FROM {first.version_table}").fetchone() == (1,)
