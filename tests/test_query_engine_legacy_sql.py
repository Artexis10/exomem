"""The engine's legacy selection must answer as the file evaluator does, including its non-SQL semantics."""

import uuid
import json

import pytest

from exomem import query_compat, query_data
from exomem import structured_collections as collections
from exomem.collection_store.reader import StoreAdapter
from exomem.query_engine import legacy, runtime, sqlite
from test_collection_store_writer import CID, create
from test_collection_store_writer import store as store
from test_collection_store_writer import KEY, OTHER, manifest_path, manifest_text

OPERATOR_CASES = [
    ("eq", 1), ("ne", 1), ("gt", 1), ("gte", 1), ("lt", 2), ("lte", 2),
    ("contains", "Éc"), ("icontains", "ÉC"), ("startswith", "Éc"),
    ("in", ["1"]), ("nin", ["1"]), ("exists", None), ("missing", None),
]


def engine_result(store, *, path="source", format="markdown-items", **arguments):
    """The engine's selection over the store, presented with the selected rows' values as the oracle presents its own."""
    with runtime.read_session(store.root, store.handle.path) as session:
        admitted = session.admit(CID)
        plan = legacy.normalize(columns_available=admitted.fields, **arguments)
        selection = sqlite.execute_legacy(admitted, plan, path=path, format=format)
        stored = {row_id: values for row_id, _, values in sqlite.scan(admitted, admitted.fields)}
    common = {"path": path, "format": format, "total_rows": selection.total_rows,
              "total_matched": selection.total_matched, "warnings": list(selection.warnings)}
    if plan.aggregate:
        aggregate = selection.aggregate
        if selection.latest_row_id is not None:
            aggregate = {**aggregate, "row": stored[selection.latest_row_id]}
        return query_data.present_aggregate(aggregate, columns=list(plan.available), **common)
    return query_data.present_rows([(row_id, stored[row_id]) for row_id in selection.row_ids], offset=plan.offset,
                                   columns=list(plan.columns), available=list(plan.available), **common)


@pytest.fixture
def legacy_rows(store):
    create(store)
    rows = [
        {"title": "<1,0 mg", "count": 1},
        {"title": "1", "count": 1},
        {"title": "Éclair", "count": 3},
        {"title": "éCOLE", "count": 4},
        {"title": "2026-01-02", "count": 5},
        {"title": "11111111-1111-4111-8111-111111111111"},
    ]
    for number, row in enumerate(rows, 1):
        store.append_record(CID, item=row, item_key=str(uuid.UUID(int=number)), why="fixture")
    return rows


@pytest.mark.parametrize("string_columns", [frozenset({"title"}), frozenset()], ids=["declared", "undeclared"])
@pytest.mark.parametrize("op,value", OPERATOR_CASES)
def test_sql_operator_answers_match_file_oracle(store, legacy_rows, op, value, string_columns):
    """Affinity, ASCII NOCASE and string membership must not replace legacy rules."""
    args = {"filters": [{"column": "title", "op": op, "value": value}], "string_columns": string_columns}
    expected = query_data.evaluate_rows(legacy_rows, path="source", format="markdown-items",
                                       columns_available=["title", "count"], **args)
    assert engine_result(store, **args).as_dict() == expected.as_dict()


def test_every_legacy_operator_has_its_parity_case():
    """Adding an operator to the dialect without exercising the engine's answer must fail."""
    assert query_compat.OPS == {op for op, _ in OPERATOR_CASES}


@pytest.mark.parametrize("aggregate", ["count", "min:count", "max:count", "sum:count", "avg:count",
                                      "latest:title", "distinct:count", "group:count", "profile"])
def test_sql_reductions_and_envelopes_match_file_oracle(store, legacy_rows, aggregate):
    """Reducers must preserve ordered Python numerics, first-occurrence and profile/card output."""
    args = {"aggregate": aggregate}
    expected = query_data.evaluate_rows(legacy_rows, path="source", format="markdown-items",
                                       columns_available=["title", "count"], **args)
    assert engine_result(store, **args).as_dict() == expected.as_dict()


@pytest.mark.parametrize("string_columns,equal", [
    (frozenset({"title"}), ["1"]),  # a declared string compares as its whole value
    (frozenset(), ["<1,0 mg", "1"]),  # an undeclared column keeps the tolerant numeric reading
], ids=["declared", "undeclared"])
def test_unicode_numeric_and_membership_have_independent_expected_answers(store, legacy_rows, string_columns, equal):
    """A shared primitive changed wrongly must not make both parity arms pass."""
    expected = {"eq": equal, "in": ["1"], "startswith": ["Éclair", "éCOLE"]}
    for op, value in (("eq", 1), ("in", ["1"]), ("startswith", "Éc")):
        result = engine_result(store, string_columns=string_columns,
                               filters=[{"column": "title", "op": op, "value": value}])
        assert [row["title"] for row in result.rows] == expected[op]


@pytest.mark.parametrize("descending", [False, True])
def test_sort_ties_paging_and_missing_values_keep_stable_input_order(store, legacy_rows, descending):
    """SQL reversing the tie-breaker would skip or reorder tied page results."""
    args = {"sort_by": "count", "descending": descending, "offset": 1, "limit": 3,
            "columns": ["title", "count"]}
    oracle = query_data.evaluate_rows(legacy_rows, path="source", format="markdown-items",
                                     columns_available=["title", "count"], **args)
    assert engine_result(store, **args).as_dict() == oracle.as_dict()


def test_nested_null_array_and_inclusive_dates_keep_legacy_semantics(store):
    """JSON1 null/absence, array paths and native comparisons differ from this dialect."""
    text = manifest_text().replace("    count: {type: integer}",
        "    count: {type: integer}\n    payload: {type: object}\n    date: {type: date}")
    store.create_collection(manifest_path(), text, why="fixture", scaffold=False)
    rows = [{"title": "A", "payload": {"values": [None, "<2,5 mg"]}, "date": "2026-01-01"},
            {"title": "B", "payload": {"values": ["", "3"]}, "date": "2026-01-02"},
            {"title": "C", "payload": {}, "date": "2026-01-03"}]
    for number, row in enumerate(rows, 1):
        store.append_record(CID, item=row, item_key=str(uuid.UUID(int=number)), why="fixture")
    args = {"filters": [{"column": "payload.values.0", "op": "missing"}],
            "columns": ["title", "payload.values.1", "absent"],
            "date_from": "2026-01-01", "date_to": "2026-01-02"}
    fields = ["title", "count", "payload", "date"]
    expected = query_data.evaluate_rows(rows, path="source", format="markdown-items",
                                       columns_available=fields, **args)
    actual = engine_result(store, **args)
    assert actual.as_dict() == expected.as_dict()
    assert actual.rows == [{"title": "A", "payload.values.1": "<2,5 mg", "absent": None},
                           {"title": "B", "payload.values.1": "3", "absent": None}]


def test_sum_preserves_python_compensated_reduction_not_sqlite_sum(store):
    """A naïve SQLite SUM loses the small middle value after cancellation."""
    create(store)
    for number, count in enumerate((10**16, 1, -(10**16)), 1):
        store.append_record(CID, item={"title": str(number), "count": count},
                            item_key=str(uuid.UUID(int=number)), why="fixture")
    result = engine_result(store, aggregate="sum:count")
    assert result.aggregate == {"sum": sum(float(n) for n in (10**16, 1, -(10**16))), "n": 3}


def test_matching_withheld_sibling_is_absent_before_value_evaluation(store):
    """A secret matching row must not affect the filter, aggregate or projector, nor be decoded."""
    from exomem.governance.principal import owner_principal, request_scope
    from test_governance_egress import _external, write_rule, write_scope

    create(store)
    store.append_record(CID, item={"title": "Visible", "count": 2}, item_key=KEY, why="fixture")
    write_scope(store.root, paths=f"Records/**/{OTHER}.md")
    write_rule(store.root, ceiling=0)

    def project(value):
        # The engine reads only the fields its decisions need, so the projector sees only those.
        assert value.get("count") != 999 and value.get("title") != "Secret"
        return value

    def query():
        with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
            session.project_with(project, fields={"title", "count"})
            admitted = session.admit(CID)
            plan = legacy.normalize(columns_available=admitted.fields,
                                    filters=[{"column": "count", "op": "gt", "value": 1}],
                                    aggregate="sum:count")
            selection = sqlite.execute_legacy(admitted, plan, path="source", format="markdown-items")
            return selection.total_rows, selection.total_matched, selection.aggregate
    before = query()
    with request_scope(owner_principal()):
        store.append_record(CID, item={"title": "Secret", "count": 999}, item_key=OTHER, why="fixture")
    assert query() == before == (1, 1, {"sum": 2.0, "n": 1})
    # Invalid JSON is a real decode trap: reading the withheld row at all would raise.
    store.connection.execute("UPDATE items SET values_json='not json' WHERE item_key=?", (OTHER,))
    assert query() == before


def test_scan_reads_in_row_order_on_the_collection_row_index_without_a_sorter(store, legacy_rows):
    """A chunk that needs a sorter spends memory per chunk; one off the row index reads rows at random."""
    statements = []
    with runtime.read_session(store.root, store.handle.path) as session:
        admitted = session.admit(CID)
        session.connection.set_trace_callback(statements.append)
        list(sqlite.scan(admitted, ("count",)))
        session.connection.set_trace_callback(None)
        scans = [sql for sql in statements if "ORDER BY i.row_id" in sql]
        assert scans
        for sql in scans:
            plan = " ".join(str(row[-1]) for row in session.connection.execute("EXPLAIN QUERY PLAN " + sql))
            assert "items_by_collection_row" in plan
            assert "TEMP B-TREE" not in plan


def test_projection_precedes_filter(store, legacy_rows):
    """Values removed by trusted projection cannot match."""
    with runtime.read_session(store.root, store.handle.path) as session:
        session.project_with(lambda row: {"count": row.get("count")}, fields={"title"})
        admitted = session.admit(CID)
        plan = legacy.normalize(columns_available=admitted.fields,
                                filters=[{"column": "title", "op": "exists"}])
        result = sqlite.execute_legacy(admitted, plan, path="source", format="markdown-items")
        assert result.total_matched == 0


def test_bare_ir_cannot_be_executed_and_callers_cannot_mutate_frozen_filters():
    """Normalization is not authority, and caller mutation cannot change an admitted request."""
    filters = [{"column": "count", "op": "in", "value": [1]}]
    plan = legacy.normalize(columns_available=["count"], filters=filters)
    filters[0]["value"].append(999)
    assert json.loads(plan.filters_json)[0]["value"] == [1]
    with pytest.raises(runtime.QueryError, match="QUERY_UNSUPPORTED"):
        sqlite.execute_legacy(object(), plan, path="source", format="markdown-items")


@pytest.mark.parametrize("profile", ["planning", "declared"])
def test_schema_and_defaults_match_canonical_adapter_for_other_types(store, profile):
    """SQL must use each source's declared fields, not a Records-only schema."""
    builtin = "planning" if profile == "planning" else "records"
    text = manifest_text(builtin)
    if profile == "declared":
        text = text.replace("    count: {type: integer}", "    payload: {type: object}")
    path = manifest_path(builtin)
    store.create_collection(path, text, why="fixture", scaffold=False)
    for number, title in enumerate(("One", "Two"), 1):
        item = {"title": title}
        if profile == "declared":
            item["payload"] = {"ready": number == 2, "value": f"{number},5"}
        store.append_record(CID, item=item, item_key=str(uuid.UUID(int=number)), why="fixture")
    manifest = collections.parse_manifest_bytes(store.root, path, text.encode())
    rows = [dict(record.values) for record in StoreAdapter(store, manifest, None).read().records]
    if profile == "declared":
        # Authoring is still unavailable: install one invented canonical registry
        # fixture to exercise the existing non-built-in read contract only.
        declaration = json.loads(store.connection.execute(
            "SELECT declaration_json FROM collection_type_versions WHERE name='records'"
        ).fetchone()[0])
        declaration.update(name="sample-records", item_type="sample-record")
        declaration.pop("wire")
        store.connection.execute("INSERT INTO collection_types VALUES('sample-records',1,0)")
        store.connection.execute("INSERT INTO collection_type_versions VALUES('sample-records',1,?,'fixture','additive',1)",
                                 (json.dumps(declaration),))
        store.connection.execute("UPDATE collections SET type_name='sample-records' WHERE collection_id=?", (CID,))
    queries = ({"filters": [{"column": "kind", "op": "exists"}], "aggregate": "group:kind"},
               {"columns": ["title", "status", "horizon"], "sort_by": "title", "descending": True})
    if profile == "declared":
        queries = ({"filters": [{"column": "payload.ready", "op": "eq", "value": True}]},
                   {"aggregate": "sum:payload.value"})
    for arguments in queries:
        expected = query_data.evaluate_rows(rows, path=path, format="markdown-items",
                                           columns_available=list(manifest.schema.fields), **arguments)
        actual = engine_result(store, path=path, **arguments)
        assert actual.as_dict() == expected.as_dict()
    if profile == "declared":
        assert actual.aggregate == {"sum": 4.0, "n": 2}
    else:
        assert [row["title"] for row in actual.rows] == ["Two", "One"]
        assert all("status" in row and "horizon" in row for row in actual.rows)


@pytest.mark.parametrize("insertion", ["oldest-first", "newest-first"])
def test_log_notes_and_first_occurrence_follow_declared_input_order(store, insertion):
    """Admission's physical order cannot change log pages or first-tie reducers."""
    text = (manifest_text()
            .replace("strategy: markdown-items\n  source: Items", "strategy: markdown-log\n  source: Log.md")
            .replace("  format_version: 1\n", "  format_version: 1\n"
                     "  section: {level: 2, title: Entries}\n"
                     "  item_heading:\n    level: 3\n"
                     "    fields: [{name: title, type: string}]\n"
                     '    separator: " · "\n'
                     '    note: {field: note, open: " (", close: ")"}\n'
                     '  child_rows:\n    prefix: "- "\n    delimiter: "|"\n'
                     "    fields: [field, value]\n    container_field: details\n"
                     f"  insertion: {insertion}\n")
            .replace("    count: {type: integer}", "    details: {type: array, items: {type: object}}"))
    store.create_collection(manifest_path(), text, why="fixture", scaffold=False)
    for title, key in (("One", OTHER), ("Two", KEY)):
        store.append_record(CID, item={"title": title, "details": [], "note": "Same note"},
                            item_key=key, why="fixture")
    manifest = collections.parse_manifest_bytes(store.root, manifest_path(), text.encode())
    rows = [dict(record.values) for record in StoreAdapter(store, manifest, None).read().records]
    expected_titles = ["Two", "One"] if insertion == "newest-first" else ["One", "Two"]
    assert [row["title"] for row in rows] == expected_titles
    for arguments in ({"sort_by": "note", "limit": 1}, {"aggregate": "latest:note"}):
        expected = query_data.evaluate_rows(rows, path="source", format="markdown-log",
                                           columns_available=["title", "details", "note"], **arguments)
        actual = engine_result(store, format="markdown-log", **arguments)
        assert actual.as_dict() == expected.as_dict()
    assert actual.aggregate["row"]["title"] == expected_titles[0]
