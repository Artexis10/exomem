"""Native derived rows preserve source value distinctions through replay and backup."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

from exomem.collection_store import derived_rows, native_layout, typed_storage
from exomem.collection_store.native_operands import FileRole, register_native_operands
from exomem.collection_store.native_projections import NativeProjectionPlan
from exomem.collection_store.query_indexes import build_projection_plan
from exomem.query_engine.indexes import IndexKey, IndexSpec
from exomem.structured_collections import FieldSpec

CID = "00000000-0000-4000-8000-000000000001"
KEY = "00000000-0000-4000-8000-000000000002"


def test_native_derived_replay_keeps_numeric_subtypes_and_signed_zero_after_reopen(tmp_path):
    """Equal numeric comparisons must not discard a change in authoritative source values."""
    store_path = tmp_path / "collections.sqlite"
    fields = {"first": FieldSpec("number"), "second": FieldSpec("number"),
              "when": FieldSpec("datetime"), "kind": FieldSpec("enum", enum=(True, 1, "one"))}
    layout = native_layout.build(CID, 7, fields, identity="uuid16")
    legacy = build_projection_plan(CID, {name: {"type": field.type} for name, field in fields.items()},
                                   (IndexSpec("first", (IndexKey("first"),)),))
    projection = NativeProjectionPlan(CID, 13, legacy.scalars, legacy.indexes, layout, FileRole.DERIVED)
    target = derived_rows.Target(CID, layout, projection, "native-source-contract", ())
    store = derived_rows.Store(tmp_path, SimpleNamespace(path=store_path))
    try:
        store.conn.execute("BEGIN")
        store._install(target, state="ready", applied=0, cursor=None)
        original = {"first": 1, "second": 1.0, "when": "2024-01-01T00:00:00Z", "kind": True}
        assert derived_rows.apply(store.conn, target, [(0, KEY, original)])[0]["outcome"] == "inserted"
        swapped = {**original, "first": 1.0, "second": 1}
        assert derived_rows.apply(store.conn, target, [(0, KEY, swapped)])[0]["outcome"] == "updated"
        zeros = {**swapped, "first": -0.0, "second": 0.0}
        derived_rows.apply(store.conn, target, [(0, KEY, zeros)])
        reversed_zeros = {**zeros, "first": 0.0, "second": -0.0,
                          "when": "2024-01-02t00:00:00,1200+01:00", "kind": 1.0}
        assert derived_rows.apply(store.conn, target, [(0, KEY, reversed_zeros)])[0]["outcome"] == "updated"
        store.conn.commit()
    finally:
        store.close()

    reopened = derived_rows.Store(tmp_path, SimpleNamespace(path=store_path))
    try:
        reopened.conn.execute("BEGIN")
        assert derived_rows.apply(reopened.conn, target, [(0, KEY, reversed_zeros)])[0]["outcome"] == "unchanged"
        reopened.conn.commit()
        row_id, version = reopened.conn.execute(f"SELECT row_id,row_version FROM {target.table}").fetchone()
        assert version == 4
        actual = typed_storage.current_values(reopened.conn, target.reads("main"), row_id)
        assert typed_storage.canonical_json(actual) == typed_storage.canonical_json(reversed_zeros)
        assert reopened.conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        snapshot = derived_rows.snapshot(store_path, tmp_path / "backup.sqlite")
        assert snapshot["size_bytes"] > 0
    finally:
        reopened.close()

    with closing(sqlite3.connect(snapshot["path"])) as backup:
        register_native_operands(backup, databases={FileRole.DERIVED: "main"})
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        restored = typed_storage.current_values(backup, target.reads("main"), row_id)
        assert typed_storage.canonical_json(restored) == typed_storage.canonical_json(reversed_zeros)
