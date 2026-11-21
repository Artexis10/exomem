"""Canonical scalar projections use only generated SQL names and index paths."""

import json
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError

import pytest

from exomem.collection_store.query_indexes import ProjectionPlan, build_projection_plan
from exomem.query_engine.indexes import IndexDeclarationError, IndexKey, IndexSpec
from exomem.query_engine.scalars import scalar_key

CID = "2db90f18-70df-4e41-986e-2d7d7db1caca"


@pytest.fixture
def projection_db(tmp_path):
    with closing(sqlite3.connect(tmp_path / "projection.sqlite", isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY,values_json TEXT NOT NULL) STRICT")
        yield conn


def install(conn, plan):
    assert conn.in_transaction
    for sql in plan.ddl:
        conn.execute(sql)


def test_hostile_labels_are_data_and_large_integer_source_bytes_are_unchanged(projection_db):
    """A declaration cannot inject DDL or narrow the canonical integer payload."""
    field = "value'); DROP TABLE items;--"
    name = 'index"; DROP TABLE items;--'
    fields = {field: {"type": "number"}, "unindexed": {"type": "object"}}
    plan = build_projection_plan(CID, fields, (IndexSpec(name, (IndexKey(field),)),))
    assert all(field not in sql and name not in sql for sql in (*plan.ddl, plan.upsert_sql))
    install(projection_db, plan)
    value = {field: 2**2000 + 1, "unindexed": {"source": "unchanged"}}
    canonical = json.dumps(value, separators=(",", ":"))
    projection_db.execute("INSERT INTO items VALUES(?,?)", (1, canonical))
    payload = plan.encode(value)
    projection_db.execute(plan.upsert_sql, (1, "first", 1, payload))
    assert json.loads(payload) == [list(scalar_key(value[field], "number"))]
    assert "unindexed" not in payload and "source" not in payload
    assert projection_db.execute("SELECT values_json FROM items WHERE row_id=1").fetchone()[0] == canonical
    assert projection_db.execute(f"SELECT k0_tag,k0_key FROM {plan.table_name}").fetchone() == scalar_key(value[field], "number")
    # SQLite's generated-column metadata identifies VIRTUAL, not stored copies.
    assert [column[6] for column in projection_db.execute(f"PRAGMA table_xinfo({plan.table_name})")][-2:] == [2, 2]


def test_projection_plan_freezes_paths_and_indexes_without_dotted_path_inference(projection_db):
    """A logical label can be literal or explicitly mapped, never executable JSON path."""
    fields = {"literal.dot": {"type": "integer"}, "nested.value": {"type": "integer"},
              "array.value": {"type": "string"}}
    paths = {"nested.value": ("nested", "value"), "array.value": ("array", 0, "value")}
    specs = (IndexSpec("sample", tuple(IndexKey(name) for name in fields)),)
    plan = build_projection_plan(CID, fields, specs, scalar_paths=paths)
    install(projection_db, plan)
    fields["literal.dot"]["type"] = "string"
    paths["nested.value"] = ("changed",)
    values = {"literal.dot": 2, "literal": {"dot": 99}, "nested": {"value": None},
              "array": [{"value": "kept"}, {"value": "not indexed"}]}
    assert json.loads(plan.encode(values)) == [list(scalar_key(2, "integer")), [1, ""], list(scalar_key("kept", "string"))]
    assert json.loads(plan.encode({})) == [[0, ""], [0, ""], [0, ""]]
    with pytest.raises(FrozenInstanceError):
        plan.generation = 2


@pytest.mark.parametrize("bad", ["uuid", "generation", "array", "path"])
def test_projection_refuses_unbound_or_nonscalar_templates(bad):
    """Only a canonical UUID and declared scalar mapping can produce templates."""
    fields = {"value": {"type": "array" if bad == "array" else "number"}}
    with pytest.raises(IndexDeclarationError):
        build_projection_plan("x'); DROP TABLE items;--" if bad == "uuid" else CID,
                              fields, (IndexSpec("sample", (IndexKey("value"),)),),
                              generation="1); DROP TABLE items;--" if bad == "generation" else 1,
                              scalar_paths={"value": ("array", -1)} if bad == "path" else None)
    if bad == "generation":
        with pytest.raises(IndexDeclarationError):
            ProjectionPlan(CID, "1); DROP TABLE items;--", (), ())


def test_projection_maintenance_and_ddl_share_the_callers_transaction(projection_db):
    """A failed canonical write must roll back its derived key update or installation."""
    plan = build_projection_plan(CID, {"value": {"type": "integer"}},
                                 (IndexSpec("sample", (IndexKey("value"),)),))
    install(projection_db, plan)
    projection_db.execute("INSERT INTO items VALUES(1,'{\"value\":1}')")
    projection_db.execute(plan.upsert_sql, (1, "first", 1, plan.encode({"value": 1})))
    projection_db.execute("COMMIT")
    projection_db.execute("BEGIN")
    projection_db.execute("UPDATE items SET values_json='{\"value\":2}' WHERE row_id=1")
    projection_db.execute(plan.upsert_sql, (1, "first", 2, plan.encode({"value": 2})))
    replacement = build_projection_plan(CID, {"value": {"type": "integer"}}, plan.indexes, generation=2)
    install(projection_db, replacement)
    projection_db.execute("ROLLBACK")
    assert projection_db.execute("SELECT values_json FROM items").fetchone() == ('{"value":1}',)
    assert projection_db.execute(f"SELECT row_version,k0_key FROM {plan.table_name}").fetchone() == (1, scalar_key(1, "integer")[1])
    assert projection_db.execute("SELECT name FROM sqlite_schema WHERE name=?", (replacement.table_name,)).fetchone() is None


def test_composite_range_and_keyset_use_declared_index_at_100k_rows(projection_db):
    """An indexed range plus deterministic continuation needs no collection scan or sorter."""
    plan = build_projection_plan(CID, {"bucket": {"type": "string"}, "amount": {"type": "integer"}},
                                 (IndexSpec("bucket_amount", (IndexKey("bucket"), IndexKey("amount", "desc"))),))
    install(projection_db, plan)
    for n in range(100_000):
        values = {"bucket": "even" if n % 2 == 0 else "odd", "amount": n % 1000}
        projection_db.execute("INSERT INTO items VALUES(?,?)", (n, json.dumps(values)))
        projection_db.execute(plan.upsert_sql, (n, f"{n:06d}", 1, plan.encode(values)))
    tag, bucket = scalar_key("even", "string")
    number_tag, low = scalar_key(100, "integer")
    high = scalar_key(105, "integer")[1]
    sql = (f"SELECT row_id,item_key,k1_key FROM {plan.table_name} "
           "WHERE k0_rank=0 AND k0_tag=? AND k0_key=? AND k1_rank=0 AND k1_tag=? AND k1_key>=? AND k1_key<? "
           "ORDER BY k1_key DESC,item_key ASC LIMIT 7")
    arguments = (tag, bucket, number_tag, low, high)
    explain = [row[3] for row in projection_db.execute("EXPLAIN QUERY PLAN " + sql, arguments)]
    assert any(f"SEARCH {plan.table_name} USING INDEX {plan.index_names[0]}" in step for step in explain), explain
    assert not any("SCAN " in step or "TEMP B-TREE" in step for step in explain), explain
    first = list(projection_db.execute(sql, arguments))
    assert [row[0] for row in first] == [104 + 1000 * n for n in range(7)]
    continuation = sql.replace("ORDER BY", "AND (k1_key<? OR (k1_key=? AND item_key>?)) ORDER BY")
    after = (*arguments, first[-1][2], first[-1][2], first[-1][1])
    next_plan = [row[3] for row in projection_db.execute("EXPLAIN QUERY PLAN " + continuation, after)]
    assert not any("SCAN " in step or "TEMP B-TREE" in step for step in next_plan), next_plan
    assert [row[0] for row in projection_db.execute(continuation, after)] == [104 + 1000 * n for n in range(7, 14)]
    # The public typed compiler must produce the same indexed interval, not
    # merely rely on these hand-written constraints knowing the physical keys.
    from exomem.query_engine.typed_sql import compile_rows
    from exomem.query_engine.validation import normalize_query

    fields = {"item_key": {"type": "string"}, "bucket": {"type": "string"}, "amount": {"type": "integer"}}
    logical = normalize_query({"version": 1, "where": {"all": [
        {"field": "bucket", "op": "eq", "value": "even"},
        {"field": "amount", "op": "between", "value": {"lower": 100, "upper": 105, "include_upper": False}},
    ]}, "order_by": [{"field": "amount", "direction": "desc"}]}, declarations={
        CID: {"domain": "collections", "type": "sample", "vault": "fixture", "fields": fields}}, collection=CID)
    assert not logical.findings
    compiled = compile_rows(logical.query, plan, as_of="2026-01-01T00:00:00Z")
    assert compiled.page_bound and compiled.usable_index == plan.index_names[0]
    compiled_sql = f"SELECT row_id FROM {plan.table_name} p WHERE {compiled.where_sql} ORDER BY {compiled.order_sql} LIMIT 7"
    compiled_plan = [row[3] for row in projection_db.execute("EXPLAIN QUERY PLAN " + compiled_sql, compiled.params)]
    assert any("SEARCH " in step and plan.index_names[0] in step for step in compiled_plan), compiled_plan
    assert not any("SCAN " in step or "TEMP B-TREE" in step for step in compiled_plan), compiled_plan
    assert [row[0] for row in projection_db.execute(compiled_sql, compiled.params)] == [104 + 1000 * n for n in range(7)]
