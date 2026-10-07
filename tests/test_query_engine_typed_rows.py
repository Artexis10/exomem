"""Canonical typed pages admit release and cost before evaluating values."""

import json
from dataclasses import replace
from io import BytesIO

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime
from exomem.query_engine.validation import normalize_query

AS_OF = "2026-10-04T00:00:00+00:00"


def setup_rows(store, rows, fields=None):
    text = manifest_text().replace("type: integer}", "type: integer, sortable: true}")
    text = text.replace("type: string, required: true}", "type: string, required: true, filterable: true}")
    if fields:
        text = text.removesuffix("---\n") + "".join(
            f"    {json.dumps(name)}: {json.dumps(spec)}\n" for name, spec in fields.items()
        ) + "---\n"
    store.create_collection(manifest_path(), text, why="fixture", scaffold=False)
    for key, values in rows:
        store.append_record(CID, item=values, item_key=key, why="fixture")


def query(*, fields=None, **request):
    result = normalize_query({"version": 1, **request}, collection=CID, declarations={CID: {
        "domain": "collections", "type": "records", "vault": "fixture",
        "fields": {"item_key": {"type": "string"}, "title": {"type": "string"}, "count": {"type": "integer"},
                   **(fields or {})},
    }})
    assert not result.findings, result.findings
    return result.query


def page(session, logical):
    from exomem.query_engine.typed_rows import execute_rows
    return execute_rows(session.admit_query(logical, as_of=AS_OF))


def test_indexed_page_fits_where_legacy_full_count_exceeds_visit_cap(store):
    """A page of one must not count an otherwise large, uniformly released source."""
    setup_rows(store, [(KEY, {"title": "One", "count": 1}),
                       (OTHER, {"title": "Two", "count": 2}),
                       ("33333333-3333-4333-8333-333333333333", {"title": "Three", "count": 3})])
    limits = runtime.QueryLimits(max_row_visits=2)
    with runtime.read_session(store.root, store.handle.path, limits=limits) as session:
        with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
            session.admit(CID)
    statements = []
    with runtime.read_session(store.root, store.handle.path, limits=limits) as session:
        session.connection.set_trace_callback(statements.append)
        result = page(session, query(select=["title"], order_by=[{"field": "count"}], page={"limit": 1}))
        assert result.rows == [{"title": "One"}]
        assert result.has_more and result.total is None
        assert result.last_order_key[-1] == KEY
    assert not any("count(*)" in sql.lower() for sql in statements)


def test_mixed_hidden_scalar_and_json_traps_never_reach_evaluation(store):
    """A withheld invalid scalar/JSON row must be absent before SQLite or Python evaluates it."""
    setup_rows(store, [(KEY, {"title": "Visible", "count": 2}), (OTHER, {"title": "Secret", "count": 1})])
    write_scope(store.root, paths=f"Records/Work/Items/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    from exomem.collection_store.index_migrations import ready_plan
    plan = ready_plan(store.connection, CID)
    store.connection.execute("PRAGMA ignore_check_constraints=ON")
    store.connection.execute("UPDATE items SET values_json='not json' WHERE item_key=?", (OTHER,))
    # Valid key JSON but an invalid UTF-8 text key explodes in the residual UDF.
    scalar = next(n for n, scalar in enumerate(plan.scalars) if scalar.field == "title")
    keys = json.loads(plan.encode({"title": "Secret", "count": 1}))
    keys[scalar][1] = "ff"
    store.connection.execute(f"UPDATE {plan.table_name} SET keys_json=? WHERE item_key=?", (json.dumps(keys), OTHER))
    seen, statements = [], []
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        session.project_with(lambda row: seen.append(row) or row, fields={"title"})
        session.connection.set_trace_callback(statements.append)
        result = page(session, query(select=["title"], where={"field": "title", "op": "contains", "value": "Visible"},
                                     order_by=[{"field": "count", "nulls": "first"}]))
        assert result.rows == [{"title": "Visible"}] and not result.has_more
    assert seen == [{"title": "Visible"}]
    copy = next(sql for sql in statements if sql.startswith("INSERT INTO temp.cq_"))
    assert "CROSS JOIN main." in copy and "p.row_id=a.row_id" in copy
    with pytest.raises(UnicodeDecodeError):
        from exomem.query_engine.typed_sql import text_predicate
        text_predicate("contains", "ff", "Visible")


def test_typed_handles_do_not_survive_principal_session_or_policy_changes(store):
    """A prior owner's valid typed plan cannot authorize another request."""
    from exomem.query_engine.typed_rows import execute_rows
    setup_rows(store, [(KEY, {"title": "One", "count": 1})])
    with request_scope(owner_principal()), runtime.read_session(store.root, store.handle.path) as session:
        admitted = session.admit_query(query(), as_of=AS_OF)
        assert execute_rows(admitted).rows == [{"title": "One", "count": 1}]
        with request_scope(_external()), pytest.raises(runtime.QueryError, match="QUERY_CANCELLED"):
            execute_rows(admitted)
        with pytest.raises(runtime.QueryError):
            execute_rows(replace(admitted, fields=("secret",)))
    with pytest.raises(runtime.QueryError):
        execute_rows(admitted)
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        statements = []
        session.connection.set_trace_callback(statements.append)
        with pytest.raises(runtime.QueryError, match="COLLECTION_NOT_FOUND"):
            session.admit_query(query(), as_of=AS_OF)
    assert not any("query_projection_mappings" in sql for sql in statements)


def test_residual_cost_refuses_before_any_value_function(store, monkeypatch):
    """A LIMIT cannot hide the full residual pass from quota admission."""
    from exomem.query_engine import typed_sql
    setup_rows(store, [(KEY, {"title": "One"}), (OTHER, {"title": "Two"})])

    def trap(*_):
        pytest.fail("evaluated a value before admitting the residual pass")

    monkeypatch.setattr(typed_sql, "text_predicate", trap)
    with runtime.read_session(store.root, store.handle.path, limits=runtime.QueryLimits(max_row_visits=3)) as session:
        session.project_with(trap, fields={"title"})
        with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
            page(session, query(where={"field": "title", "op": "contains", "value": "One"}, page={"limit": 1}))


def test_selected_values_preserve_missing_null_and_tie_order(store):
    """Sort keys remain internal and missing values must not become present null."""
    third, fourth = "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
    setup_rows(store, [(KEY, {"title": "Null", "count": None}), (OTHER, {"title": "Missing"}),
                       (third, {"title": "Tied first", "count": 1}), (fourth, {"title": "Tied second", "count": 1})])
    with runtime.read_session(store.root, store.handle.path) as session:
        result = page(session, query(select=["count"], order_by=[{"field": "count"}]))
        assert result.rows == [{"count": 1}, {"count": 1}, {"count": None}, {}]
        assert not result.has_more
        result = page(session, query(select=["item_key"], order_by=[{"field": "count", "nulls": "first"}]))
        assert result.rows == [{"item_key": OTHER}, {"item_key": KEY}, {"item_key": third}, {"item_key": fourth}]


def test_ir_must_rebind_to_current_declared_fields_and_type(store):
    """A hand-built or formerly valid IR is not field/schema authorization."""
    setup_rows(store, [(KEY, {"title": "One", "count": 1})])
    logical = query(select=["count"])
    from exomem.query_engine import ir
    forged = replace(logical, select=ir.Project((replace(logical.select.fields[0], value_type="string"),)))
    with runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError, match="QUERY_VALUE_INVALID"):
            session.admit_query(forged, as_of=AS_OF)
        with pytest.raises(runtime.QueryError, match="QUERY_VALUE_INVALID"):
            session.admit_query(replace(logical, source=replace(logical.source, type_id="other")), as_of=AS_OF)
        for changes in ({"mode": "preview"}, {"execution_profile": "analytics"}, {"joins": (object(),)},
                        {"aggregate": object()}, {"text": object()}, {"graph": object()}):
            with pytest.raises(runtime.QueryError, match="QUERY_UNSUPPORTED"):
                session.admit_query(replace(logical, **changes), as_of=AS_OF)
        with pytest.raises(runtime.QueryError, match="authenticated"):
            session.admit_query(replace(logical, page=ir.Page(after="raw")), as_of=AS_OF)


def test_byte_cap_stops_after_last_emitted_row_and_single_row_fails(store):
    """Byte truncation must offer progress; an oversized first row must fail."""
    from exomem.query_engine.typed_rows import execute_rows
    setup_rows(store, [(KEY, {"title": "First"}), (OTHER, {"title": "Second"})])
    with runtime.read_session(store.root, store.handle.path) as session:
        result = execute_rows(session.admit_query(query(select=["title"]), as_of=AS_OF), max_response_bytes=25)
        assert result.rows == [{"title": "First"}] and result.has_more
        assert result.last_order_key == (KEY,)
    with runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError, match="QUERY_RESULT_TOO_LARGE"):
            execute_rows(session.admit_query(query(select=["title"]), as_of=AS_OF), max_response_bytes=10)


def test_normalized_membership_and_four_sort_keys_rebind_without_changing_meaning(store):
    """IR tuples and the appended fifth identity key must survive schema rebinding."""
    setup_rows(store, [(KEY, {"title": "One", "count": 1}), (OTHER, {"title": "Two", "count": 2})])
    with runtime.read_session(store.root, store.handle.path) as session:
        result = page(session, query(select=["title"], where={"field": "count", "op": "in", "value": [1]}))
        assert result.rows == [{"title": "One"}]
        result = page(session, query(select=["title"], order_by=[{"field": "count"}, {"field": "title"},
                                                                {"field": "count"}, {"field": "title"}]))
        assert result.rows == [{"title": "One"}, {"title": "Two"}]


def test_relative_date_membership_rebinds_and_matches_frozen_instant(store):
    """Rebinding must serialize the normalized RelativeDate inside an IN tuple."""
    fields = {"at": {"type": "datetime", "filterable": True}}
    setup_rows(store, [(KEY, {"title": "Now", "at": AS_OF}),
                       (OTHER, {"title": "Before", "at": "2026-10-03T00:00:00+00:00"})], fields)
    logical = query(fields=fields, select=["title"], where={
        "field": "at", "op": "in", "value": [{"relative_to": "as_of", "amount": 0, "unit": "day"}],
    })
    with runtime.read_session(store.root, store.handle.path) as session:
        assert page(session, logical).rows == [{"title": "Now"}]


@pytest.mark.parametrize("unused_count", [9, 40])
def test_large_unselected_fields_do_not_consume_selected_value_budget(store, unused_count):
    """A small selected row must survive canonical JSON larger than the decode cap."""
    fields = {f"unused_{n}": {"type": "string"} for n in range(unused_count)}
    setup_rows(store, [(KEY, {"title": "Tiny", **{name: "x" * 30_000 for name in fields}})], fields)
    stored_bytes = store.connection.execute("SELECT length(CAST(values_json AS BLOB)) FROM items").fetchone()[0]
    assert stored_bytes > 256 * 1024
    with runtime.read_session(store.root, store.handle.path) as session:
        from exomem.query_engine.typed_rows import execute_rows

        result = execute_rows(session.admit_query(query(fields=fields, select=["title"]), as_of=AS_OF),
                              max_response_bytes=len(b'[{"title": "Tiny"}]'))
        assert result.rows == [{"title": "Tiny"}] and not result.has_more


def test_large_row_projection_preserves_exact_json_and_literal_field_names(store):
    """Projection must not round numbers or interpret logical names as JSON paths."""
    fields = {'a".b': {"type": "integer"}, "slash\\dot.": {"type": "number"},
              "negative_zero": {"type": "number"}, "nested": {"type": "object"},
              "absent": {"type": "string"}, "null": {"type": "string"},
              **{f"unused_{n}": {"type": "string"} for n in range(9)}}
    expected = {'a".b': 2**200, "slash\\dot.": 1.0, "negative_zero": -0.0,
                "nested": {"nul\0tail": [2**200, 1, 1.0, -0.0, True, None, "\0"]}, "null": None}
    setup_rows(store, [(KEY, {"title": "Exact", **expected,
                             **{f"unused_{n}": "x" * 30_000 for n in range(9)}})], fields)
    with runtime.read_session(store.root, store.handle.path) as session:
        result = page(session, query(fields=fields, select=[*expected, "absent"]))
        # JSON equality would equate 1/1.0 and both signs of zero.
        assert json.dumps(result.rows, sort_keys=True) == json.dumps([expected], sort_keys=True)


def test_oversized_selected_row_preserves_progress_then_refuses(store):
    """A decode-cap refusal after a small row must retain the last emitted identity."""
    fields = {f"large_{n}": {"type": "string"} for n in range(9)}
    setup_rows(store, [(KEY, {"title": "First"}),
                       (OTHER, {"title": "Large", **{name: "x" * 30_000 for name in fields}})], fields)
    with runtime.read_session(store.root, store.handle.path) as session:
        result = page(session, query(fields=fields))
        assert result.rows == [{"title": "First"}] and result.has_more
        assert result.last_order_key == (KEY,)
    with runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError, match="QUERY_RESULT_TOO_LARGE"):
            page(session, query(fields=fields, where={"field": "item_key", "op": "gt", "value": KEY}))


def test_stream_projection_counts_nested_json_bytes_and_preserves_nul_keys():
    """NUL keys must stay distinct, and nested/escaped JSON must fit its exact byte cap."""
    from exomem.query_engine.selected_values import read_selected_values

    expected = {"nul\0tail": [1, 1.0, -0.0, {"é": 'a\n\\"'}], "null": None}
    encoded = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    raw = json.dumps({**expected, "nul": "unselected", "unused": "x" * 30_000}).encode()
    args = dict(fields=[*expected, "missing"], max_bytes=len(encoded.encode()), check=lambda: None)
    result = read_selected_values(BytesIO(raw), **args)
    assert json.dumps(result, ensure_ascii=False, separators=(",", ":")) == encoded
    with pytest.raises(runtime.QueryError, match="QUERY_RESULT_TOO_LARGE"):
        read_selected_values(BytesIO(raw), **{**args, "max_bytes": args["max_bytes"] - 1})


def test_stream_projection_cancels_between_reads_of_unselected_values():
    """Skipping a large unselected value must still honor cancellation before EOF."""
    from exomem.query_engine.selected_values import read_selected_values

    raw = json.dumps({"unused": "x" * 30_000, "selected": "yes"}).encode()
    source = BytesIO(raw)

    def check():
        if source.tell():
            raise runtime.QueryError("QUERY_CANCELLED")

    with pytest.raises(runtime.QueryError, match="QUERY_CANCELLED"):
        read_selected_values(source, ["selected"], max_bytes=100, check=check)
    assert 0 < source.tell() < len(raw)


@pytest.mark.parametrize("shape", ["aligned", "unaligned", "mixed"])
def test_execution_uses_proved_indexes_and_never_an_external_sorter(store, shape):
    """Forced main/private indexes must bound actual ORDER BY work, not just claim it."""
    setup_rows(store, [(KEY, {"title": "One", "count": 1}), (OTHER, {"title": "Hidden", "count": 2})])
    if shape == "mixed":
        write_scope(store.root, paths=f"Records/Work/Items/{OTHER}.md")
        write_rule(store.root, ceiling=0)
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        statements = []
        session.connection.set_trace_callback(statements.append)
        result = page(session, query(select=["title"], order_by=[{
            "field": "count", "nulls": "last" if shape == "aligned" else "first",
        }]))
        assert result.rows[0] == {"title": "One"}
        sql = next(sql for sql in statements if sql.startswith("SELECT p.row_id,"))
        steps = [row[3] for row in session.connection.execute("EXPLAIN QUERY PLAN " + sql)]
        assert any("USING INDEX" in step for step in steps), steps
        assert not any("TEMP B-TREE" in step for step in steps), steps
        if shape == "mixed":
            copy = next(sql for sql in statements if sql.startswith("INSERT INTO temp.cq_"))
            steps = [row[3] for row in session.connection.execute("EXPLAIN QUERY PLAN " + copy)]
            assert any("SEARCH p USING INTEGER PRIMARY KEY" in step for step in steps), steps
            assert not any("SCAN p" in step for step in steps), steps


def test_private_projection_obeys_temp_quota_before_decoding(store):
    """The bounded sorter must consume accounted TEMP pages and fail before row decoding."""
    setup_rows(store, [(KEY, {"title": "One", "count": 1})])

    def trap(_):
        pytest.fail("decoded before the TEMP quota refusal")

    with pytest.raises(runtime.QueryError, match="QUERY_COST_LIMIT"):
        with runtime.read_session(store.root, store.handle.path,
                                  limits=runtime.QueryLimits(max_temp_bytes=8192)) as session:
            session.project_with(trap, fields={"title", "count"})
            page(session, query(order_by=[{"field": "count", "nulls": "first"}]))
    with runtime.read_session(store.root, store.handle.path) as session:
        assert page(session, query(select=["title"])).rows == [{"title": "One"}]
