"""Canonical typed-v1 storage keeps values, subtypes, hashes and history exact.

OpenSpec add-collection-query-engine Q7.1. Each test names the defect only it
catches. The typed module is imported lazily so that, before the encoding
exists, every case fails on its own missing behaviour.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import subprocess
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from test_collection_store_schema import _populated
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_records_bulk_upsert import EVIDENCE, _evidence

from exomem import query_data
from exomem.collection_store import connection, schema, tokens, views
from exomem.collection_store.reader import StoreAdapter
from exomem.query_engine import legacy, runtime
from exomem.query_engine import sqlite as legacy_sql
from exomem.query_engine.scalars import scalar_key
from exomem.query_engine.validation import normalize_query

AS_OF = "2026-10-04T00:00:00+00:00"
EXTRA_FIELDS = (
    "    amount: {type: number, sortable: true}\n"
    "    flag: {type: boolean}\n"
    "    tags: {type: array, items: {type: string}}\n"
    "    meta: {type: object}\n"
)
COLUMNS = ["title", "count", "amount", "flag", "tags", "meta"]


def typed():
    from exomem.collection_store import typed_storage

    return typed_storage


def create(store):
    text = manifest_text().replace("    count: {type: integer}\n", "    count: {type: integer}\n" + EXTRA_FIELDS)
    store.create_collection(manifest_path(), text, why="fixture", scaffold=False)
    _evidence(store.root)
    return text


def migrate(store, *, limit=500):
    for _ in range(64):
        state = store.migrate_typed_encoding(CID, limit=limit)
        if state != "building":
            return state
    raise AssertionError("typed migration did not finish")


def guard(store):
    return store.inspect_collection(CID)["lifecycle_guards"]["expected_container_hash"]


def bulk(store, *items):
    result = store.bulk_upsert_records(CID, rows=[{"item": item} for item in items], why="import",
                                       expected_container_hash=guard(store), source=EVIDENCE)
    return [row["outcome"] for row in result["rows"]]


def row_ids(store):
    return dict(store.connection.execute("SELECT item_key,row_id FROM items WHERE collection_id=?", (CID,)))


def exact(value):
    """A comparison form that distinguishes 1 from 1.0 and -0.0 from 0.0."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def typed_page(store, *, select, order_by=None):
    request = {"version": 1, "select": select, "order_by": order_by or [{"field": "amount"}],
               "page": {"limit": 100}}
    fields = {"item_key": {"type": "string"}, "title": {"type": "string"}, "count": {"type": "integer"},
              "amount": {"type": "number"}, "flag": {"type": "boolean"},
              "tags": {"type": "array"}, "meta": {"type": "object"}}
    normalized = normalize_query(request, collection=CID, declarations={CID: {
        "domain": "collections", "type": "records", "vault": "fixture", "fields": fields}})
    assert not normalized.findings, normalized.findings
    from exomem.query_engine.typed_rows import execute_rows

    with runtime.read_session(store.root, store.handle.path) as session:
        return execute_rows(session.admit_query(normalized.query, as_of=AS_OF)).rows


def legacy_read(store, **arguments):
    with runtime.read_session(store.root, store.handle.path) as session:
        admitted = session.admit(CID)
        plan = legacy.normalize(columns_available=admitted.fields, **arguments)
        return legacy_sql.execute_legacy(admitted, plan, path="source", format="markdown-items").as_dict()


def history(store, row_id):
    return store.connection.execute(
        "SELECT row_version,encoding,payload_hash,txn_id,schema_version FROM version_identity "
        "WHERE row_id=? ORDER BY row_version", (row_id,)).fetchall()


def frozen_history(conn):
    """Every immutable byte a forward migration must leave untouched."""
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()  # noqa: S608 - fixed names
            for table in ("item_versions", "item_sources", "txns", "audit_effects", "version_identity")}


COUNTEREXAMPLES = {
    # An encoder that collapses int and float stores 1.0 as 1 and skips the version.
    "int-to-float": (1, 1.0, "updated"),
    # An encoder whose float hash input is not the original float mints a spurious version.
    "float-unchanged": (1.0, 1.0, "unchanged"),
    # Python and SQL equality treat -0.0 and 0.0 as equal; only the hash input separates them.
    "signed-zero": (-0.0, 0.0, "updated"),
    # Rounding an integer through REAL makes 2**53 + 1 equal to its nearest double.
    "beyond-binary64": (9007199254740993, 9007199254740992.0, "updated"),
    # Binding 2**63 as a SQLite INTEGER overflows; it needs exact decimal text.
    "int64-overflow": (2**63 - 1, 2**63, "updated"),
}


@pytest.mark.parametrize(("before", "after", "outcome"), COUNTEREXAMPLES.values(), ids=COUNTEREXAMPLES)
def test_numeric_counterexamples_keep_value_subtype_and_hash(store, before, after, outcome):
    """Typed current, history and hash input must equal json-v1 for each named counterexample."""
    create(store)
    assert migrate(store) == "ready"
    assert bulk(store, {"title": "N", "amount": before}) == ["inserted"]
    assert bulk(store, {"title": "N", "amount": after}) == [outcome]
    key, row_id = next(iter(row_ids(store).items()))
    current = before if outcome == "unchanged" else after
    assert exact(typed_page(store, select=["amount"])) == exact([{"amount": current}])
    assert exact(legacy_read(store)["rows"]) == exact([{"title": "N", "amount": current}])
    versions = [before] if outcome == "unchanged" else [before, after]
    assert [(version, encoding) for version, encoding, *_ in history(store, row_id)] == [
        (number, "typed-v1") for number in range(1, len(versions) + 1)]
    for (version, _, payload, _, schema_version), value in zip(history(store, row_id), versions, strict=True):
        expected = {"title": "N", "amount": value}
        values, body = typed().version_values(store.connection, row_id, version)
        assert exact(values) == exact(expected) and body == ""
        assert payload == tokens.payload_hash(schema_version, key, expected, "")
    layout = typed().ready_layout(store.connection, CID)
    ordinal = layout.fields.index("amount")
    stored = store.connection.execute(
        f"SELECT typeof(v{ordinal}),v{ordinal},typeof(k{ordinal}),k{ordinal} "  # noqa: S608 - internal ordinals
        f"FROM {layout.current_table} WHERE row_id=?", (row_id,)).fetchone()
    if type(current) is float:
        assert stored == ("blob", struct.pack(">d", current), "real", current)
    elif -(2**63) <= current < 2**63:
        assert stored == ("integer", current, "null", None)
    else:
        assert stored == ("text", str(current), "text", scalar_key(current, "number")[1])
    assert store.connection.execute("SELECT values_json,encoding FROM items WHERE row_id=?",
                                    (row_id,)).fetchone() == (None, "typed-v1")


def test_large_integers_order_and_reduce_exactly(store):
    """Out-of-range integers must not narrow through REAL when typed rows sort or reduce."""
    create(store)
    assert migrate(store) == "ready"
    amounts = [2**63, 2**63 - 1, float(2**63), -(2**63) - 1, 2**64 + 1, 0.5]
    items = [{"title": f"T{index}", "amount": amount} for index, amount in enumerate(amounts)]
    assert bulk(store, *items) == ["inserted"] * len(items)
    stored_keys = [key for (key,) in store.connection.execute(
        "SELECT item_key FROM items WHERE collection_id=? ORDER BY row_id", (CID,))]
    by_key = dict(zip(stored_keys, items, strict=True))
    # item_key is the declared final tie-breaker between 2**63 and float(2**63).
    expected = sorted(stored_keys, key=lambda key: (Decimal(by_key[key]["amount"]), key))
    assert [row["title"] for row in typed_page(store, select=["title"])] == [by_key[key]["title"] for key in expected]
    for aggregate in ("min:amount", "max:amount", "sum:amount"):
        oracle = query_data.evaluate_rows(items, path="source", format="markdown-items",
                                          columns_available=COLUMNS, aggregate=aggregate)
        assert exact(legacy_read(store, aggregate=aggregate)["aggregate"]) == exact(oracle.as_dict()["aggregate"])


def test_forward_migration_keeps_json_history_and_spines_typed_versions(store):
    """A migrated item keeps its JSON versions/sources and its next version is typed on the same spine."""
    create(store)
    first = store.append_record(CID, item={"title": "One", "amount": 1}, why="observe", sources=(EVIDENCE,),
                                body="Body\n")
    key = first["item_key"]
    second = store.update_record(CID, item_key=key, changes={"amount": 1.0}, why="correct",
                                 expected_container_hash=first["after_container_hash"],
                                 expected_item_version=first["after_item_hash"], sources=(EVIDENCE,))
    before = frozen_history(store.connection)
    assert [row[1] for row in before["version_identity"]] == [1, 2]
    assert migrate(store) == "ready"
    assert frozen_history(store.connection) == before
    third = store.update_record(CID, item_key=key, changes={"amount": -0.0}, why="correct",
                                expected_container_hash=second["after_container_hash"],
                                expected_item_version=second["after_item_hash"], sources=(EVIDENCE,))
    row_id = row_ids(store)[key]
    txn = store.connection.execute("SELECT max(txn_id) FROM txns").fetchone()[0]
    payload = store.connection.execute("SELECT payload_hash FROM items WHERE row_id=?", (row_id,)).fetchone()[0]
    assert third["after_item_hash"] == tokens.item_version(CID, key, 3, payload)
    assert history(store, row_id) == [*(row[1:] for row in before["version_identity"]),
                                      (3, "typed-v1", payload, txn, 1)]
    assert store.connection.execute("SELECT row_version,ordinal,source_ref FROM item_sources WHERE row_id=? "
                                    "ORDER BY row_version", (row_id,)).fetchall() == [
        (1, 0, EVIDENCE), (2, 0, EVIDENCE), (3, 0, EVIDENCE)]
    decoded = [typed().version_values(store.connection, row_id, version) for version in (1, 2, 3)]
    assert exact(decoded) == exact([({"amount": 1, "title": "One"}, "Body\n"),
                                    ({"amount": 1.0, "title": "One"}, "Body\n"),
                                    ({"amount": -0.0, "title": "One"}, "Body\n")])
    assert store.connection.execute("SELECT count(*) FROM item_versions WHERE row_id=?", (row_id,)).fetchone() == (2,)


def test_typed_history_and_spine_abort_update_and_delete(store):
    """Typed history must be immutable in the database, not only in the writer."""
    create(store)
    assert migrate(store) == "ready"
    bulk(store, {"title": "One", "amount": 2**63})
    layout = typed().ready_layout(store.connection, CID)
    before = frozen_history(store.connection), store.connection.execute(
        f"SELECT * FROM {layout.version_table}").fetchall()  # noqa: S608 - internal name
    for table in ("version_identity", layout.version_table):
        for statement in (f"UPDATE {table} SET row_version=row_version", f"DELETE FROM {table}",
                          f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
            # A replacing insert may meet the attach-to-identity guard first; both refuse.
            with pytest.raises(sqlite3.IntegrityError, match="append-only|existing version identity"), \
                    store.handle.transaction() as tx:
                tx.execute(statement)
    assert (frozen_history(store.connection), store.connection.execute(
        f"SELECT * FROM {layout.version_table}").fetchall()) == before  # noqa: S608


def test_spine_refuses_orphan_sources_identities_and_payloads(store):
    """No source, identity or typed payload may exist without its own partner on the spine."""
    create(store)
    bulk(store, {"title": "One", "amount": 1})
    assert migrate(store) == "ready"
    row_id = next(iter(row_ids(store).values()))
    layout = typed().ready_layout(store.connection, CID)
    txn = store.connection.execute("SELECT max(txn_id) FROM txns").fetchone()[0]

    def history_and_typed():
        return frozen_history(store.connection), store.connection.execute(
            f"SELECT * FROM {layout.version_table}").fetchall()  # noqa: S608 - internal name

    before = history_and_typed()
    attempts = [
        ("INSERT INTO item_sources VALUES(?,?,?,?)", (row_id, 9, 0, EVIDENCE), "FOREIGN KEY"),
        ("INSERT INTO version_identity VALUES(?,?,?,?,?,?)", (row_id, 9, "typed-v1", "0" * 64, txn, 1), "payload"),
        ("INSERT INTO version_identity VALUES(?,?,?,?,?,?)", (row_id, 9, "json-v1", "0" * 64, txn, 1), "payload"),
        ("INSERT INTO item_versions VALUES(?,?,?,?,?,?)", (row_id, 9, "{}", "", "0" * 64, txn), "typed"),
    ]
    columns = ",".join(["row_id", "row_version", "body", *layout.value_columns, "r"])
    placeholders = ",".join("?" for _ in range(3 + len(layout.value_columns) + 1))
    payload = typed().encode_row(layout, {"title": "Ghost"})
    insert_typed = f"INSERT INTO {layout.version_table}({columns}) VALUES({placeholders})"
    # Version 1 is the migrated JSON version: a typed payload must not join its identity.
    attempts += [(insert_typed, (row_id, 1, "", *payload), "existing version identity"),
                 (insert_typed, (row_id, 9, "", *payload), "FOREIGN KEY")]
    for statement, parameters, message in attempts:
        with pytest.raises(sqlite3.IntegrityError, match=message), store.handle.transaction() as tx:
            tx.execute(statement, parameters)
    assert history_and_typed() == before


def test_encoding_discriminator_is_checked_and_view_path_may_be_null(store):
    """One collection has one encoding authority; summary rows may have no physical view path."""
    create(store)
    bulk(store, {"title": "One", "amount": 1})
    conn = store.connection
    for statement, message in (
        ("UPDATE items SET values_json=NULL", "CHECK"),
        ("UPDATE items SET encoding='typed-v1',values_json=NULL", "encoding"),
        ("UPDATE items SET encoding='bson'", "encoding"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match=message), store.handle.transaction() as tx:
            tx.execute(statement)
    with pytest.raises(RuntimeError, match="rolled back"), store.handle.transaction() as tx:
        tx.execute("UPDATE items SET view_path=NULL")
        raise RuntimeError("rolled back")
    assert migrate(store) == "ready"
    with pytest.raises(sqlite3.IntegrityError, match="encoding"), store.handle.transaction() as tx:
        tx.execute("UPDATE collections SET encoding='json-v1'")
    assert conn.execute("SELECT encoding FROM collections").fetchone() == ("typed-v1",)


def test_typed_reads_match_json_reads(store):
    """Presence, null, bodies, hashes, rendered views and both query paths must survive cutover."""
    text = create(store)
    bulk(store,
         {"title": "Full", "count": 3, "amount": 2.5, "flag": True, "tags": ["x", "é"], "meta": {"k": [1, 2.5, None]}},
         {"title": "Null", "count": None, "amount": None, "flag": False, "tags": []},
         {"title": "Missing"},
         {"title": "Zero", "amount": -0.0, "meta": {}},
         {"title": "Huge", "amount": -(2**70), "count": 2**64})
    store.append_record(CID, item={"title": "Bodied", "amount": 7}, why="observe", body="Authored\n")
    manifest = store._projection_manifest(CID)
    identity = views.store_identity(store.connection)

    def reads():
        conn = store.connection
        rows = conn.execute("SELECT row_id,item_key,row_version,payload_hash,body FROM items "
                            "WHERE collection_id=? ORDER BY row_id", (CID,)).fetchall()
        return {
            "rows": rows,
            "values": [exact(typed().item_values(conn, row_id)) for row_id, *_ in rows],
            "views": [views.render_view(conn, identity, {"kind": "item", "row_id": row_id}, manifest)
                      for row_id, *_ in rows],
            "legacy": exact(legacy_read(store)),
            "filtered": exact(legacy_read(store, filters=[{"column": "amount", "op": "gt", "value": 0}])),
            "typed": exact(typed_page(store, select=COLUMNS)),
            "inspection": exact(store.inspect_collection(CID)["lifecycle_guards"]),
            # Released rows read through the adapter, not the release cache.
            "adapter": exact([(record.identity.key, record.values, record.body)
                              for record in StoreAdapter(store, manifest, None).read().records]),
        }

    before = reads()
    stored_json = [raw for (raw,) in store.connection.execute(
        "SELECT values_json FROM items WHERE collection_id=? ORDER BY row_id", (CID,))]
    assert before["values"] == [exact(json.loads(raw)) for raw in stored_json]
    assert migrate(store) == "ready"
    assert reads() == before
    assert store.connection.execute("SELECT count(*) FROM items WHERE values_json IS NOT NULL").fetchone() == (0,)
    # A governed revise revalidates every typed row through the same decode.
    store.revise_collection(CID, manifest_text=text.replace("title: Work", "title: Revised"),
                            expected_manifest_hash=tokens.manifest_hash(text),
                            expected_container_hash=guard(store), why="revise")
    assert reads()["values"] == before["values"]


def test_wide_typed_collection_reads_under_the_sqlite_function_argument_floor(store, monkeypatch):
    """SQLite before 3.48 caps function arguments at 127; field count must not become SQL function arity."""
    extra = "".join(f"    f{index:02d}: {{type: string}}\n" for index in range(45))
    text = manifest_text().replace("    count: {type: integer}\n", "    count: {type: integer}\n" + extra)
    store.create_collection(manifest_path(), text, why="fixture", scaffold=False)
    _evidence(store.root)
    bulk(store, {"title": "A", "f00": "x", "f44": "y"})
    assert migrate(store) == "ready"
    open_connection = connection._connect

    def capped(*args, **kwargs):
        conn = open_connection(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG, 127)
        return conn

    monkeypatch.setattr(connection, "_connect", capped)
    assert legacy_read(store)["rows"] == [{"title": "A", "f00": "x", "f44": "y"}]


_OLDER_READER = """
import json, sys
from pathlib import Path
from exomem.collection_store import connection
from exomem.query_engine import runtime
path = Path(sys.argv[1])
results = {}
for name, attempt in (
    ("reader", lambda: connection.open_reader(path).close()),
    ("writer", lambda: connection.open_writer(path, lease_check=lambda: True).close()),
):
    try:
        attempt()
        results[name] = "opened"
    except connection.CollectionStoreError as error:
        results[name] = error.code
try:
    with runtime.read_session(Path(sys.argv[2]), path):
        results["query"] = "opened"
except runtime.QueryError as error:
    results["query"] = error.code
print(json.dumps(results))
"""


def test_older_readers_refuse_a_typed_store_before_access(store, monkeypatch):
    """A release without typed-v1 must refuse the store before reading or writing it."""
    create(store)
    bulk(store, {"title": "One", "amount": 2**63})
    assert migrate(store) == "ready"
    store.handle.close()
    path = store.handle.path

    def state():
        # Opening a WAL database may create an empty -wal file; no frame may be written.
        wal = Path(f"{path}-wal")
        return hashlib.sha256(path.read_bytes()).hexdigest(), wal.stat().st_size if wal.exists() else 0

    before = state()
    older = os.environ.get("EXOMEM_TEST_OLDER_READER_PYTHON")
    if older:
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        completed = subprocess.run([older, "-c", _OLDER_READER, str(path), str(store.root)], env=env,
                                   capture_output=True, text=True, timeout=120, check=True)
        assert json.loads(completed.stdout.splitlines()[-1]) == {
            "reader": "COLLECTION_STORE_SCHEMA_NEWER", "writer": "COLLECTION_STORE_SCHEMA_NEWER",
            "query": "QUERY_UNAVAILABLE"}
    # The release immediately before typed-v1, simulated in process.
    monkeypatch.setattr(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION - 1)
    with pytest.raises(connection.CollectionStoreError, match="SCHEMA_NEWER"):
        connection.open_reader(path)
    with pytest.raises(connection.CollectionStoreError, match="SCHEMA_NEWER"):
        connection.open_writer(path, lease_check=lambda: True)
    with pytest.raises(runtime.QueryError, match="QUERY_UNAVAILABLE"):
        with runtime.read_session(store.root, path):
            pass
    assert state() == before
    monkeypatch.undo()
    with closing(connection.open_reader(path)) as reader:
        assert reader.execute("SELECT encoding FROM collections").fetchone() == ("typed-v1",)


def test_unproved_typed_encoding_never_becomes_authoritative(store, monkeypatch):
    """A lossy encoder must fail its parity proof and leave the JSON mapping as the one authority."""
    create(store)
    bulk(store, {"title": "One", "amount": 1}, {"title": "Two", "amount": 2**63})
    conn = store.connection
    before = conn.execute("SELECT * FROM items ORDER BY row_id").fetchall(), frozen_history(conn)
    tables = set(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    module = typed()
    decode = module.decode_value

    def lossy(tag, value, key):
        decoded = decode(tag, value, key)
        return float(decoded) if type(decoded) is int else decoded

    monkeypatch.setattr(module, "decode_value", lossy)
    assert migrate(store) == "failed"
    assert (conn.execute("SELECT * FROM items ORDER BY row_id").fetchall(), frozen_history(conn)) == before
    assert set(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == tables
    assert conn.execute("SELECT encoding FROM collections").fetchone() == ("json-v1",)
    assert conn.execute("SELECT state FROM typed_encoding_mappings").fetchall() == [("failed",)]
    assert exact(legacy_read(store)["rows"]) == exact([{"title": "One", "amount": 1}, {"title": "Two", "amount": 2**63}])
    monkeypatch.undo()
    assert migrate(store) == "ready"
    assert exact(legacy_read(store)["rows"]) == exact([{"title": "One", "amount": 1}, {"title": "Two", "amount": 2**63}])


def test_interrupted_migration_resumes_and_catches_up_concurrent_writes(store, monkeypatch):
    """Writes between batches and a crashed batch must neither be lost nor publish a partial mapping."""
    create(store)
    first = store.append_record(CID, item={"title": "One", "amount": 1}, why="observe")
    bulk(store, {"title": "Two", "amount": 2}, {"title": "Three", "amount": 3})
    conn = store.connection
    assert store.migrate_typed_encoding(CID, limit=1) == "building"
    assert conn.execute("SELECT encoding FROM collections").fetchone() == ("json-v1",)
    store.update_record(CID, item_key=first["item_key"], changes={"amount": 1.5}, why="correct",
                        expected_container_hash=guard(store), expected_item_version=first["after_item_hash"])
    bulk(store, {"title": "Four", "amount": 2**64})
    expected = conn.execute("SELECT row_id,values_json,payload_hash,row_version FROM items ORDER BY row_id").fetchall()
    progress = conn.execute("SELECT last_row_id FROM typed_encoding_mappings").fetchone()
    module = typed()

    def crash(*_args, **_kwargs):
        raise OSError("simulated crash inside a migration batch")

    monkeypatch.setattr(module, "encode_row", crash)
    with pytest.raises(OSError, match="simulated crash"):
        store.migrate_typed_encoding(CID, limit=1)
    assert conn.execute("SELECT last_row_id,state FROM typed_encoding_mappings").fetchone() == (*progress, "building")
    assert conn.execute("SELECT encoding FROM collections").fetchone() == ("json-v1",)
    monkeypatch.undo()
    assert migrate(store, limit=1) == "ready"
    assert [(row_id, exact(typed().item_values(conn, row_id)), payload, version)
            for row_id, _, payload, version in expected] == [
        (row_id, exact(json.loads(raw)), payload, version) for row_id, raw, payload, version in expected]


def test_existing_json_store_gains_the_spine_without_rewriting_history(tmp_path, monkeypatch):
    """Upgrading a schema-4 store must backfill identities and keep every history byte."""
    path = tmp_path / "state" / "collections.sqlite"
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 4)
    writer = connection.open_writer(path, lease_check=lambda: True)
    try:
        row_id = _populated(writer)["row_id"]
        old = {table: writer.connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
               for table in ("item_versions", "item_sources", "txns", "audit_effects")}
        old_items = writer.connection.execute("SELECT * FROM items").fetchall()
    finally:
        writer.close()
    monkeypatch.undo()
    with connection.open_writer(path, lease_check=lambda: True) as upgraded:
        conn = upgraded.connection
        assert {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in old} == old  # noqa: S608
        assert [row[:-1] for row in conn.execute("SELECT * FROM items")] == old_items
        assert conn.execute("SELECT encoding FROM items").fetchall() == [("json-v1",)]
        assert conn.execute("SELECT * FROM version_identity").fetchall() == [
            (row_id, 1, "json-v1", "a" * 64, 1, 1)]
        assert conn.execute("SELECT \"table\",\"from\" FROM pragma_foreign_key_list('item_sources')").fetchall() == [
            ("version_identity", "row_id"), ("version_identity", "row_version")]
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), upgraded.transaction() as tx:
            tx.execute("INSERT INTO item_sources VALUES(?,?,?,?)", (row_id, 2, 0, "Knowledge Base/Sources/x.md"))
