"""Native derived rows preserve source value distinctions through replay and backup."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from exomem.collection_store import derived_rows, native_layout, rollups, typed_storage
from exomem.collection_store.native_operands import FileRole, register_native_operands
from exomem.collection_store.native_projections import NativeProjectionPlan
from exomem.collection_store.query_indexes import build_projection_plan
from exomem.query_engine.indexes import IndexKey, IndexSpec
from exomem.structured_collections import FieldSpec

CID = "00000000-0000-4000-8000-000000000001"
KEY = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def indexed(tmp_path):
    fields = {"amount": FieldSpec("number"), "when": FieldSpec("datetime"),
              "kind": FieldSpec("enum", enum=(True, 1, "one"))}
    layout = native_layout.build(CID, 7, fields, identity="uuid16")
    legacy = build_projection_plan(CID, {name: {"type": field.type} for name, field in fields.items()},
                                   (IndexSpec("when", (IndexKey("when"),)), IndexSpec("kind", (IndexKey("kind"),))))
    projection = NativeProjectionPlan(CID, 13, legacy.scalars, legacy.indexes, layout, FileRole.DERIVED)
    daily = rollups.normalize(fields, {"daily": {"bucket": "day", "timestamp": "when",
        "values": {"amount": ["sum", "min", "max", "latest"]}}})[0]
    goal = derived_rows.Target(CID, layout, projection, "native-batch-contract", ((1, daily),))
    store = derived_rows.Store(tmp_path, SimpleNamespace(path=tmp_path / "collections.sqlite"))
    try:
        store.conn.execute("BEGIN")
        store._install(goal, state="ready", applied=0, cursor=None)
        store.conn.commit()
        yield store, goal
    finally:
        store.close()


def test_native_derived_duplicates_and_mixed_updates_keep_source_order_versions_and_extremes(indexed):
    """Installing later updates before upkeep must not move an indexed member out of its old bucket early."""
    store, goal = indexed
    other, third = "00000000-0000-4000-8000-000000000003", "00000000-0000-4000-8000-000000000004"
    first = {"amount": 9, "when": "2024-03-01T10:00:00Z", "kind": True}
    second = {"amount": 3, "when": "2024-03-01T11:00:00Z", "kind": "one"}
    store.conn.execute("BEGIN")
    duplicate = derived_rows.apply(store.conn, goal, [(0, KEY, first), (1, KEY, {**first, "amount": 8}),
                                                     (2, other, second)])
    assert [row["outcome"] for row in duplicate] == ["inserted", "updated", "inserted"]
    updated = [{**first, "amount": 1}, {**second, "amount": 2, "when": "2024-03-02T11:00:00Z"},
               {**first, "amount": 4, "when": "2024-03-01T12:00:00Z"}]
    mixed = derived_rows.apply(store.conn, goal, list(zip(range(3), (KEY, other, third), updated, strict=True)))
    assert mixed == [{"index": index, "item_key": key, "outcome": outcome}
                     for index, key, outcome in ((0, KEY, "updated"), (1, other, "updated"), (2, third, "inserted"))]
    store.conn.commit()
    actual = [typed_storage.current_values(store.conn, goal.reads("main"), row_id)
              for row_id, _ in store.conn.execute(f"SELECT row_id,row_version FROM {goal.table} ORDER BY row_id")]
    assert typed_storage.canonical_json(actual) == typed_storage.canonical_json(updated)
    assert store.conn.execute(f"SELECT row_version FROM {goal.table} ORDER BY row_id").fetchall() == [(3,), (2,), (1,)]
    buckets = {bucket: {"rows": rows, **{op: fields[0].result(op) for op in ("sum", "min", "max", "latest")}}
               for bucket, _, _, rows, fields, _ in rollups.bucket_states(store.conn, 1)}
    assert buckets == {"2024-03-01": {"rows": 2, "sum": 5, "min": 1, "max": 4, "latest": 4},
                       "2024-03-02": {"rows": 1, "sum": 2, "min": 2, "max": 2, "latest": 2}}


def test_native_derived_index_failure_rolls_back_and_retries_with_fresh_metadata_and_bounded_ids(indexed):
    """A failed indexed batch cannot leak metadata, and a later large batch must resolve every key in bounded reads."""
    from exomem.collection_store.native_operands import scalar_operand

    store, goal = indexed
    store.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)
    other = "00000000-0000-4000-8000-000000000003"
    store.conn.execute("BEGIN")
    with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
        derived_rows.apply(store.conn, goal, [(0, KEY, {"amount": 9, "when": "2024-01-01T00:00:00Z", "kind": True}),
            (1, other, {"amount": 2, "when": "2024-01-01T00:00:00.0000001Z", "kind": "one"})])
    store.conn.rollback()
    assert store.conn.execute(f"SELECT count(*) FROM {goal.table}").fetchone() == (0,)
    assert store.conn.execute("SELECT count(*) FROM typed_value_formats").fetchone() == (0,)
    accepted = [(index, f"00000000-0000-4000-8000-{index + 2:012x}", {
        "amount": 5, "when": "2024-02-03t01:00:00,1200+01:00", "kind": "one"}) for index in range(501)]
    accepted[0][2].pop("when")
    store.conn.execute("BEGIN")
    outcomes = derived_rows.apply(store.conn, goal, accepted)
    store.conn.commit()
    assert outcomes == [{"index": index, "item_key": key, "outcome": "inserted"} for index, key, _ in accepted]
    actual = [typed_storage.current_values(store.conn, goal.reads("main"), row_id)
              for (row_id,) in store.conn.execute(f"SELECT row_id FROM {goal.table} ORDER BY row_id")]
    assert typed_storage.canonical_json(actual) == typed_storage.canonical_json([values for _, _, values in accepted])
    assert store.conn.execute(f"SELECT count(*),min(row_version),max(row_version) FROM {goal.table}").fetchone() == (501, 1, 1)
    tag, key = goal.projection.key_columns("when")
    band = goal.projection.band_column("when")
    expected_operand = scalar_operand(accepted[1][2]["when"], "datetime")
    assert store.conn.execute(f"SELECT DISTINCT {tag},{band},{key} FROM {goal.projection.read_relation('main')} WHERE {tag}=?",
                             (expected_operand[0],)).fetchall() == [expected_operand]
    [(bucket, _, _, rows, fields, _)] = rollups.bucket_states(store.conn, 1)
    assert (bucket, rows, fields[0].result("sum"), fields[0].result("max")) == ("2024-02-03", 500, 2500, 5)
    assert store.conn.execute("SELECT flagged FROM derived_flagged WHERE rollup_id=1").fetchone() == (1,)


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

