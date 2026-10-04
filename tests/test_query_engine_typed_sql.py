"""Typed row SQL must preserve logical semantics and honest index proofs."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from exomem.collection_store.query_indexes import build_projection_plan
from exomem.query_engine.indexes import IndexKey, IndexSpec
from exomem.query_engine.runtime import QueryError
from exomem.query_engine.typed_sql import compile_rows, text_predicate
from exomem.query_engine.validation import normalize_query

CID = "2db90f18-70df-4e41-986e-2d7d7db1caca"
AS_OF = "2024-03-31T00:30:00+01:00"


def query(fields, **request):
    declarations = {CID: {"domain": "collections", "type": "sample", "vault": "fixture",
                          "fields": {"item_key": {"type": "string"}, **fields}}}
    result = normalize_query({"version": 1, **request}, declarations=declarations, collection=CID)
    assert not result.findings, result.findings
    return result.query


def run(conn, plan, compiled):
    sql = f"SELECT row_id FROM {plan.table_name} AS p WHERE {compiled.where_sql} ORDER BY {compiled.order_sql}"
    return [row[0] for row in conn.execute(sql, compiled.params)]


@pytest.fixture
def db():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("BEGIN")
        conn.create_function("exomem_typed_text", 3, text_predicate, deterministic=True)
        yield conn


def install(conn, fields, rows, indexes=None):
    plan = build_projection_plan(CID, fields, indexes or (IndexSpec("value", (IndexKey("value"),)),))
    for statement in plan.ddl:
        conn.execute(statement)
    conn.executemany(plan.upsert_sql, ((n, f"{n:06d}", 1, plan.encode(row)) for n, row in enumerate(rows)))
    return plan


def test_default_nulls_last_order_uses_declared_index_without_sort():
    """A tag/key index alone cannot provide the public nulls-last ordering."""
    plan = build_projection_plan(CID, {"value": {"type": "integer"}},
                                 (IndexSpec("value", (IndexKey("value"),)),))
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("BEGIN")
        for statement in plan.ddl:
            conn.execute(statement)
        rows = [{"value": None}, {}, {"value": 2}, {"value": 1}]
        conn.executemany(plan.upsert_sql, ((n, str(n), 1, plan.encode(row)) for n, row in enumerate(rows)))
        compiled = compile_rows(query({"value": {"type": "integer"}}, order_by=[{"field": "value"}]), plan, as_of=AS_OF)
        sql = f"SELECT row_id FROM {plan.table_name} AS p ORDER BY {compiled.order_sql}"
        assert [row[0] for row in conn.execute(sql)] == [3, 2, 0, 1]
        explain = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert not any("TEMP B-TREE" in step for step in explain), explain
        assert compiled.usable_index == plan.index_names[0] and compiled.page_bound


@pytest.mark.parametrize("predicate,expected", [
    ({"op": "eq", "value": 2**53 + 1}, [3]),
    ({"op": "ne", "value": 2**53 + 1}, [2, 4]),
    ({"op": "gt", "value": 2**53}, [3, 4]),
    ({"op": "lte", "value": 2**53 + 1}, [2, 3]),
    ({"op": "in", "value": [2**53, 2**200]}, [2, 4]),
    ({"op": "nin", "value": [2**53, 2**200]}, [3]),
    ({"op": "in", "value": []}, []),
    ({"op": "nin", "value": []}, [2, 3, 4]),
    ({"op": "between", "value": {"lower": 2**53, "upper": 2**200, "include_lower": False, "include_upper": False}}, [3]),
    ({"op": "is_null"}, [0]),
    ({"op": "is_missing"}, [1]),
    ({"op": "is_not_null"}, [2, 3, 4]),
])
def test_exact_comparison_and_presence_are_two_valued(db, predicate, expected):
    """No affinity rounding, coercion, or SQL UNKNOWN may alter a typed filter."""
    fields = {"value": {"type": "number"}}
    rows = [{"value": None}, {}, {"value": float(2**53)}, {"value": 2**53 + 1}, {"value": 2**200}]
    plan = install(db, fields, rows)
    compiled = compile_rows(query(fields, where={"field": "value", **predicate}), plan, as_of=AS_OF)
    assert run(db, plan, compiled) == expected


def test_boolean_complement_differs_from_explicit_nonnull_ne(db):
    """NOT is ordinary complement; ne deliberately excludes null and missing."""
    rows = [{"value": None}, {}, {"value": 3}, {"value": 4}]
    fields = {"value": {"type": "integer"}}
    plan = install(db, fields, rows)
    equal = {"field": "value", "op": "eq", "value": 3}
    complemented = compile_rows(query(fields, where={"not": equal}), plan, as_of=AS_OF)
    unequal = compile_rows(query(fields, where={**equal, "op": "ne"}), plan, as_of=AS_OF)
    assert run(db, plan, complemented) == [n for n, row in enumerate(rows) if row.get("value") != 3]
    assert run(db, plan, unequal) == [3]
    assert not complemented.page_bound and not unequal.page_bound
    disjunction = compile_rows(query(fields, where={"any": [equal, {"field": "value", "op": "is_missing"}]}), plan, as_of=AS_OF)
    assert run(db, plan, disjunction) == [1, 2] and not disjunction.page_bound


def test_string_residual_and_compatibility_presence_preserve_unicode(db):
    """SQLite NOCASE and LIKE differ from the existing Unicode string contract."""
    fields = {"value": {"type": "string"}}
    plan = install(db, fields, [{}, {"value": None}, {"value": ""}, {"value": "ÅBC\0"}, {"value": "no"}])
    for op, value in [("icontains", "åb"), ("startswith", "å"), ("contains", "BC\0")]:
        compiled = compile_rows(query(fields, where={"field": "value", "op": op, "value": value}), plan, as_of=AS_OF)
        assert run(db, plan, compiled) == [3]
        assert compiled.residual and not compiled.page_bound
    for op, expected in [("exists", [3, 4]), ("missing", [0, 1, 2])]:
        compiled = compile_rows(query(fields, where={"field": "value", "op": op}), plan, as_of=AS_OF)
        assert run(db, plan, compiled) == expected


def test_relative_calendar_uses_one_utc_instant_and_clamps_month_end(db):
    """March 31 minus a month is leap-day, not 30 days or host-local time."""
    fields = {"value": {"type": "datetime"}}
    plan = install(db, fields, [{"value": "2024-02-29T23:30:00Z"}, {"value": "2024-03-01T00:30:00Z"}])
    relative = {"relative_to": "as_of", "unit": "month", "amount": -1}
    compiled = compile_rows(query(fields, where={"field": "value", "op": "eq", "value": relative}), plan, as_of=AS_OF)
    assert run(db, plan, compiled) == [0]
    assert compiled.as_of == "2024-03-30T23:30:00+00:00"


def test_projection_labels_and_literals_never_enter_executable_sql(db):
    """Declared hostile logical paths remain data even in projection/filter/order."""
    label = "value'); DROP TABLE items; --"
    literal = "'); SELECT secret; --"
    fields = {label: {"type": "string"}, "unindexed": {"type": "object"}}
    plan = install(db, fields, [{label: literal}], (IndexSpec(label, (IndexKey(label),)),))
    compiled = compile_rows(query(fields, select=[label, "unindexed"], where={"field": label, "op": "eq", "value": literal},
                                  order_by=[{"field": label}]), plan, as_of=AS_OF)
    assert run(db, plan, compiled) == [0]
    assert label not in compiled.where_sql + compiled.order_sql and literal not in compiled.where_sql
    assert compiled.projection_paths == (label, "unindexed")
    assert set(compiled.dependency_paths) == {label, "unindexed", "item_key"}


def test_index_proof_rejects_other_key_filters_and_opposite_null_order(db):
    """An ORDER BY index is not a bounded selective scan through unrelated filters."""
    fields = {"value": {"type": "integer"}, "other": {"type": "integer"}}
    indexes = (IndexSpec("value", (IndexKey("value"),)), IndexSpec("other", (IndexKey("other"),)))
    plan = install(db, fields, [{"value": 2, "other": 1}, {}], indexes)
    compiled = compile_rows(query(fields, order_by=[{"field": "value"}], where={"field": "other", "op": "eq", "value": 1}), plan, as_of=AS_OF)
    assert compiled.usable_index == plan.index_names[0] and not compiled.page_bound
    reverse = compile_rows(query(fields, order_by=[{"field": "value", "nulls": "first"}]), plan, as_of=AS_OF)
    assert run(db, plan, reverse) == [1, 0]
    assert reverse.usable_index is None and not reverse.page_bound


def test_continuations_are_exact_and_never_implicitly_authorized(db):
    """Ties, nulls and mixed descending keys must not skip or duplicate rows."""
    fields = {"value": {"type": "integer"}}
    plan = install(db, fields, [{"value": 1}, {"value": 1}, {"value": 2}, {"value": None}, {}])
    logical = query(fields, order_by=[{"field": "value"}])
    compiled = compile_rows(logical, plan, as_of=AS_OF)
    sql = f"SELECT {','.join(column for column, _ in compiled.order_terms)} FROM {plan.table_name} p ORDER BY {compiled.order_sql} LIMIT 1"
    after = db.execute(sql).fetchone()
    next_page = compile_rows(logical, plan, as_of=AS_OF, after=after)
    assert run(db, plan, next_page) == [1, 2, 3, 4] and next_page.page_bound
    reverse = query(fields, order_by=[{"field": "value", "direction": "desc"}])
    descending = compile_rows(reverse, plan, as_of=AS_OF, after=after)
    assert run(db, plan, descending) == [1, 3, 4] and not descending.page_bound
    with pytest.raises(QueryError, match="authenticated"):
        compile_rows(replace(logical, page=replace(logical.page, after="opaque")), plan, as_of=AS_OF)


@pytest.mark.parametrize("operator", ["gte", "eq", "between"])
def test_filtered_deep_pages_seek_the_boundary_without_visiting_skipped_rows(operator):
    """Fixed rank/type prefixes must not make continuation a linear residual."""
    fields = {"value": {"type": "integer"}}
    work = []
    for size in (1024, 8192):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("BEGIN")
            plan = install(conn, fields, (
                {"value": 7 if operator == "eq" else number} for number in range(size)
            ))
            boundary_id = size // 2 if operator == "between" else size - 9
            value = (7 if operator == "eq" else
                     {"lower": 250, "upper": boundary_id + 4} if operator == "between" else 250)
            logical = query(fields, where={"field": "value", "op": operator,
                                          "value": value},
                            order_by=[{"field": "value"}])
            first = compile_rows(logical, plan, as_of=AS_OF)
            terms = ",".join(column for column, _ in first.order_terms)
            boundary = conn.execute(f"SELECT {terms} FROM {plan.table_name} p WHERE row_id=?",
                                    (boundary_id,)).fetchone()
            compiled = compile_rows(logical, plan, as_of=AS_OF, after=boundary)
            assert compiled.page_bound
            instructions = []
            conn.set_progress_handler(lambda instructions=instructions: instructions.append(None) or 0, 1)
            sql = (f"SELECT row_id FROM {plan.table_name} p INDEXED BY {compiled.usable_index} "
                   f"WHERE {compiled.where_sql} ORDER BY {compiled.order_sql} LIMIT 7")
            stop = boundary_id + (5 if operator == "between" else 8)
            assert [row[0] for row in conn.execute(sql, compiled.params)] == list(range(boundary_id + 1, stop))
            conn.set_progress_handler(None, 0)
            work.append(len(instructions))
    assert work[1] <= work[0] * 2, work


def test_nonmatching_boundary_keeps_its_full_order_and_scan_cost(db):
    """A null boundary must not be discarded as a query-fixed present value."""
    fields = {"value": {"type": "integer"}}
    plan = install(db, fields, [{"value": None}, {"value": 1}, {"value": 2}])
    logical = query(fields, where={"field": "value", "op": "gte", "value": 1},
                    order_by=[{"field": "value"}])
    first = compile_rows(logical, plan, as_of=AS_OF)
    boundary = db.execute(f"SELECT {','.join(column for column, _ in first.order_terms)} "
                          f"FROM {plan.table_name} p WHERE row_id=0").fetchone()
    compiled = compile_rows(logical, plan, as_of=AS_OF, after=boundary)
    assert run(db, plan, compiled) == []
    assert not compiled.page_bound


def test_unsupported_operators_and_unindexed_filters_never_fall_back(db):
    """Q3 nodes and unavailable scalar keys cannot silently become collection scans."""
    fields = {"value": {"type": "integer"}, "other": {"type": "integer"}}
    plan = install(db, fields, [])
    logical = query(fields)
    for changes in [{"aggregate": object()}, {"having": object()}, {"joins": (object(),)}, {"text": object()}, {"graph": object()}]:
        with pytest.raises(QueryError) as failure:
            compile_rows(replace(logical, **changes), plan, as_of=AS_OF)
        assert failure.value.code == "QUERY_UNSUPPORTED"
    with pytest.raises(QueryError, match="scalar projection"):
        compile_rows(query(fields, where={"field": "other", "op": "eq", "value": 1}), plan, as_of=AS_OF)
