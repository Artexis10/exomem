"""Native physical queries preserve logical comparisons and disjoint continuation."""

import sqlite3
from contextlib import closing
from uuid import UUID

import pytest
from sqlalchemy.dialects.sqlite import dialect

from exomem import item_keys
from exomem.collection_store import connection, native_layout, native_metadata
from exomem.collection_store.query_indexes import build_projection_plan
from exomem.query_engine import typed_sql
from exomem.query_engine.indexes import IndexKey, IndexSpec
from exomem.query_engine.ordered_seeks import Seek
from exomem.query_engine.runtime import QueryError
from exomem.query_engine.validation import normalize_query
from exomem.structured_collections import FieldSpec

CID = "00000000-0000-4000-8000-000000000001"
AS_OF = "2024-01-01T00:00:00Z"


@pytest.fixture
def native_rows(tmp_path):
    numbers = [-(2**90), -(2**63), -1.25, -0.0, 0, 0.5, 2**63 - 1, 2**80, float(2**80), 2**80 + 1]
    source = [{"item_key": str(UUID(int=ordinal if ordinal < 20 else 2**128 - 1)),
               "bucket": "kept" if ordinal % 3 else "other", "amount": numbers[(ordinal - 1) % len(numbers)]}
              for ordinal in range(1, 21)]
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, {"amount": FieldSpec("number"), "bucket": FieldSpec("string")}, identity="uuid16")
        plan = build_projection_plan(CID, {"amount": {"type": "number"}, "bucket": {"type": "string"}},
                                     (IndexSpec("bucket_amount", (IndexKey("bucket"), IndexKey("amount", "desc"))),),
                                     generation=13, payload_layout=layout)
        compiled = plan.upsert.compile(dialect=dialect())
        for statement in plan.ddl:
            conn.execute(statement)
        conn.execute("BEGIN")
        for row_id, row in enumerate(source, 1):
            conn.execute("INSERT INTO items VALUES(?)", (row_id,))
            parameters = plan.parameters(conn, row_id, row["item_key"], 1, {name: value for name, value in row.items() if name != "item_key"})
            conn.execute(str(compiled), tuple(parameters[name] for name in compiled.positiontup))
        conn.commit()
        yield conn, plan, source


def logical(where, order):
    result = normalize_query({"version": 1, "where": where, "order_by": order, "page": {"limit": 4}},
                             collection=CID, declarations={CID: {"domain": "collections", "type": "sample", "vault": "fixture",
                             "fields": {"item_key": {"type": "string"}, "bucket": {"type": "string"}, "amount": {"type": "number"}}}})
    assert not result.findings, result.findings
    return result.query


def physical_page(conn, plan, compiled, limit=4):
    """Execute the compiled physical seeks; product session admission is a later integration boundary."""
    rows, explains = [], []
    for seek in compiled.seeks if compiled.seeks is not None else (Seek(compiled.where_sql, compiled.params, compiled.order_sql),):
        terms = ",".join(column for column, _ in compiled.order_terms)
        relation = plan.read_relation("main", index=compiled.usable_index)
        order = " ORDER BY " + seek.order_sql if seek.order_sql else ""
        sql = f"SELECT p.row_id,{terms} FROM {relation} p WHERE {seek.where_sql}{order} LIMIT ?"
        parameters = (*seek.params, limit - len(rows))
        explains.extend(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, parameters))
        rows.extend(conn.execute(sql, parameters).fetchall())
        if len(rows) == limit:
            break
    return rows, explains


def test_mixed_direction_numeric_continuations_keep_exact_order_without_duplicates(native_rows):
    """Descending native number blocks plus ascending UUID ties cannot skip or repeat tied overflow values."""
    conn, plan, source = native_rows
    query = logical({"all": [{"field": "bucket", "op": "eq", "value": "kept"},
                              {"field": "amount", "op": "between", "value": {"lower": -(2**100), "upper": 2**100}}]},
                    [{"field": "amount", "direction": "desc"}])
    expected = [ordinal for ordinal, _ in sorted(enumerate(source, 1), key=lambda pair: (-pair[1]["amount"], pair[1]["item_key"]))
                if source[ordinal - 1]["bucket"] == "kept"]
    seen, after = [], None
    for _ in range(len(source) + 1):
        compiled = typed_sql.compile_rows(query, plan, as_of=AS_OF, after=after)
        assert compiled.page_bound
        rows, explain = physical_page(conn, plan, compiled)
        assert not any("TEMP B-TREE" in line or "SCAN " in line for line in explain), explain
        seen.extend(row[0] for row in rows)
        if len(rows) < 4:
            break
        after = tuple(rows[-1][1:])
    else:
        pytest.fail("native continuation did not exhaust its finite interval")
    assert seen == expected


def test_uuid_filters_preserve_arbitrary_string_bounds_and_exact_membership(native_rows):
    """Binary identities must not normalize uppercase, compact, prefix, NUL or Unicode query literals."""
    conn, plan, source = native_rows
    literals = ["", "00000000-0000-0000-0000-000000000000", source[0]["item_key"], source[0]["item_key"] + "\0",
                "00000000-0000-0000-0000-", "00000000-0000-0000-0000-00000000000A",
                "00000000000000000000000000000001", "ffffffff-ffff-ffff-ffff-ffffffffffff", "z", "é"]
    operators = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "gt": lambda a, b: a > b,
                 "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
    for literal in literals:
        for operation, compare in operators.items():
            query = logical({"field": "item_key", "op": operation, "value": literal}, [{"field": "item_key"}])
            compiled = typed_sql.compile_rows(query, plan, as_of=AS_OF)
            rows, _ = physical_page(conn, plan, compiled, limit=len(source) + 1)
            expected = [ordinal for ordinal, row in sorted(enumerate(source, 1), key=lambda pair: pair[1]["item_key"])
                        if compare(row["item_key"].encode(), literal.encode())]
            assert [row[0] for row in rows] == expected, (operation, literal)
    query = logical({"field": "item_key", "op": "nin", "value": [source[0]["item_key"], "NOT-A-UUID"]}, [{"field": "item_key"}])
    rows, _ = physical_page(conn, plan, typed_sql.compile_rows(query, plan, as_of=AS_OF), limit=len(source) + 1)
    assert [row[0] for row in rows] == list(range(2, len(source) + 1))


def test_contradictory_native_ranges_exhaust_before_entering_an_excluded_tail(native_rows):
    """Continuation bounds must intersect both range endpoints, including intervals collapsed by UUID cuts."""
    conn, plan, _ = native_rows
    query = logical({"all": [{"field": "item_key", "op": "gte", "value": "00000000-0000-0000-0000-00000000000"},
                              {"field": "item_key", "op": "lt", "value": "00000000-0000-0000-0000-00000000000"}]},
                    [{"field": "item_key"}])
    compiled = typed_sql.compile_rows(query, plan, as_of=AS_OF)
    assert compiled.page_bound and compiled.seeks == ()
    assert physical_page(conn, plan, compiled)[0] == []


@pytest.fixture
def ordered_rows(tmp_path):
    """Twelve rows keyed by `ordered-v1` keys of a date, inserted out of order, in a native table with the ordered codec."""
    keys = [item_keys.spell(item_keys.ordered_key([("date", f"2024-03-{day:02d}")], lambda: "")) for day in range(1, 13)]
    with closing(connection._connect(str(tmp_path / "ordered.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, {"bucket": FieldSpec("string")}, identity="ordered")
        plan = build_projection_plan(CID, {"bucket": {"type": "string"}}, (), generation=13, payload_layout=layout)
        compiled = plan.upsert.compile(dialect=dialect())
        for statement in plan.ddl:
            conn.execute(statement)
        conn.execute("BEGIN")
        for row_id in (5, 1, 12, 3, 9, 2, 11, 4, 8, 6, 10, 7):
            conn.execute("INSERT INTO items VALUES(?)", (row_id,))
            parameters = plan.parameters(conn, row_id, keys[row_id - 1], 1, {"bucket": "kept"})
            conn.execute(str(compiled), tuple(parameters[name] for name in compiled.positiontup))
        conn.commit()
        yield conn, plan, keys


def _ordered_rows_where(conn, plan, where):
    query = logical(where, [{"field": "item_key"}])
    return [row[0] for row in physical_page(conn, plan, typed_sql.compile_rows(query, plan, as_of=AS_OF), limit=20)[0]]


def test_ordered_key_filters_match_nothing_for_an_unspelled_equality_and_refuse_an_unspelled_range_bound(ordered_rows):
    """Defect: an item_key literal that no ordered key spells is matched against raw bytes, or refused where a
    UUID collection answers an empty page, or a range bound that is no key's spelling cuts the range anywhere."""
    conn, plan, keys = ordered_rows
    unspelled = ["", "ZZ", keys[4].upper(), keys[4] + "=", "0", "not a key"]
    for literal in unspelled:
        assert _ordered_rows_where(conn, plan, {"field": "item_key", "op": "eq", "value": literal}) == [], literal
    assert _ordered_rows_where(conn, plan, {"field": "item_key", "op": "in", "value": ["ZZ", keys[1], keys[7]]}) == [2, 8]
    for operation in ("gt", "gte", "lt", "lte"):
        with pytest.raises(QueryError) as refused:
            _ordered_rows_where(conn, plan, {"field": "item_key", "op": operation, "value": "ZZ"})
        assert refused.value.code == "QUERY_VALUE_INVALID", operation
    with pytest.raises(QueryError) as refused:
        _ordered_rows_where(conn, plan, {"field": "item_key", "op": "between", "value": {"lower": keys[1], "upper": "ZZ"}})
    assert refused.value.code == "QUERY_VALUE_INVALID"


def test_ordered_key_ranges_compare_stored_bytes_so_they_follow_the_natural_order(ordered_rows):
    conn, plan, keys = ordered_rows
    assert _ordered_rows_where(conn, plan, {"field": "item_key", "op": "eq", "value": keys[4]}) == [5]
    assert _ordered_rows_where(conn, plan, {"all": [{"field": "item_key", "op": "gte", "value": keys[4]},
                                                    {"field": "item_key", "op": "lt", "value": keys[8]}]}) == [5, 6, 7, 8]
    assert _ordered_rows_where(conn, plan, {"field": "item_key", "op": "gt", "value": keys[9]}) == [11, 12]
    assert _ordered_rows_where(conn, plan, {"field": "item_key", "op": "lte", "value": keys[1]}) == [1, 2]


@pytest.mark.parametrize("stored", [b"", bytes(321)], ids=["empty", "over 320 bytes"])
def test_an_ordered_table_refuses_a_key_outside_one_to_320_bytes(ordered_rows, stored):
    """Defect: a key that its public spelling cannot carry, or that bloats every index ending in it, is stored."""
    conn, plan, keys = ordered_rows
    compiled = plan.upsert.compile(dialect=dialect())
    parameters = {**plan.parameters(conn, 99, keys[0], 1, {"bucket": "kept"}), "item_key": stored}
    conn.execute("INSERT INTO items VALUES(99)")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(str(compiled), tuple(parameters[name] for name in compiled.positiontup))
