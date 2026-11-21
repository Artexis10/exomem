"""A store Records query answers by selecting rows, as the whole read answers, within its own budget.

The selecting route reads only the values its filters, sort and aggregate decide on, and rebuilds
only the rows it returns. Each case here names the failure it catches: a routed answer that differs
from the whole read's, a query that reads every row to return a page, a query that times out while
another in-process thread is busy, and a withheld row or a restricted caller reaching the route.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from contextlib import contextmanager

import pytest
from test_collection_store_summary import _seed, bulk, summary_text
from test_collection_store_writer import CID, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope
from test_records_bulk_upsert import _evidence

from exomem import call_spans, record_governance
from exomem import structured_collections as collections
from exomem.collection_store import reader, typed_storage
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime

FIELDS = "    count: {type: integer}\n    date: {type: date}\n    note: {type: string}\n"

QUERIES = [
    {"filters": [{"column": "title", "op": "startswith", "value": "Row 1"}], "sort_by": "count",
     "descending": True, "limit": 4},
    {"sort_by": "title", "limit": 3},
    {"filters": [{"column": "count", "op": "gte", "value": 3}], "columns": ["title", "count"], "limit": 5},
    {"date_from": "2026-01-03", "date_to": "2026-01-05", "limit": 50},
    {"filters": [{"column": "count", "op": "missing"}]},
    {"aggregate": "count", "filters": [{"column": "count", "op": "lt", "value": 2}]},
    {"aggregate": "sum:count"},
    {"aggregate": "avg:count"},
    {"aggregate": "min:count"},
    {"aggregate": "latest:title"},
    {"aggregate": "distinct:count"},
    {"aggregate": "group:count"},
]


def rows(count, *, note_at=None, width=0):
    """Rows with ties, a missing value, dates and a field only one late row holds."""
    found = []
    for number in range(count):
        row = {"title": f"Row {number:02d}" + "x" * width, "date": f"2026-01-{number % 7 + 1:02d}"}
        if number % 9:
            row["count"] = number % 5
        if number == note_at:
            row["note"] = "late"
        found.append(row)
    return found


def make(store, *, summary, items, fields=FIELDS):
    text = manifest_text().replace("    count: {type: integer}\n", fields)
    store.create_collection(manifest_path(), summary_text(text) if summary else text, why="create",
                            scaffold=False)
    _evidence(store.root)
    for start in range(0, len(items), 100):
        bulk(store, items[start:start + 100])
    store.reconcile_views()


@contextmanager
def selections():
    """Count the routed selections a query makes."""
    made = []
    select = reader._Selecting.select

    def counted(self, query, **options):
        selection = select(self, query, **options)
        made.append(selection)
        return selection

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reader._Selecting, "select", counted)
        yield made


@contextmanager
def whole_read_only():
    """The route this change replaces: every query reads and rebuilds every row."""
    @contextmanager
    def declined(self):
        yield None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reader.StoreAdapter, "selecting", declined)
        yield


def query(store, who=None, **arguments):
    with request_scope(who or owner_principal(surface="mcp")), preview_store(store.root, store.handle):
        return record_governance.query_collection(store.root, CID, **arguments)


def answer(result):
    """Everything a caller receives, less the rendering's own clock reading."""
    rendered = json.loads(result.rendered)
    rendered.pop("generated_at")
    return {**dataclasses.asdict(result), "rendered": rendered}


@pytest.mark.parametrize("summary", [True, False], ids=["summary-typed", "items-json"])
def test_a_selected_answer_is_the_whole_read_answer(store, summary):
    """A routed filter, sort, page, date range, default column or aggregate differs from the whole read's."""
    make(store, summary=summary, items=rows(40, note_at=33))
    for arguments in QUERIES:
        with whole_read_only():
            expected = query(store, **arguments)
        with selections() as made:
            actual = query(store, **arguments)
        assert made, arguments
        assert answer(actual) == answer(expected), arguments
    with selections() as made:
        first = query(store, sort_by="title", limit=3)
        second = query(store, sort_by="title", limit=3, continuation=first.continuation)
    with whole_read_only():
        expected = query(store, sort_by="title", limit=3, continuation=first.continuation)
    assert len(made) == 2 and answer(second) == answer(expected)
    assert "note" in first.columns  # only row 33 holds it


def test_rows_past_one_packed_result_read_in_smaller_statements(store):
    """A chunk whose packed result passes the length limit must split, not fail or drop rows."""
    make(store, summary=True, items=rows(300, width=6000))
    for arguments in ({"sort_by": "title", "descending": True, "limit": 2}, {"aggregate": "count"}):
        with whole_read_only():
            expected = query(store, **arguments)
        with selections() as made:
            actual = query(store, **arguments)
        assert made and answer(actual) == answer(expected)


def test_a_filtered_page_of_five_thousand_rows_rebuilds_only_its_rows(store):
    """A filtered, sorted page that rebuilds every row breaks the Records latency budget."""
    make(store, summary=True, items=[])
    _seed(store, CID, 5000)
    rebuilt = []
    span = call_spans.span

    def spy(name, attributes=None):
        if name in {"store.materialize", "store.authorized_rows"}:
            rebuilt.append((name, (attributes or {}).get("items")))
        return span(name, attributes)

    arguments = {"filters": [{"column": "title", "op": "startswith", "value": "Seed 4"}],
                 "sort_by": "title", "descending": True, "limit": 10}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reader.call_spans, "span", spy)
        result = query(store, columns=["title"], **arguments)
        assert rebuilt == [("store.materialize", 10)]
        rebuilt.clear()
        # Default columns come from the 500 leading rows, which are rebuilt with the page.
        inferred = query(store, **arguments)
    assert result.total_matched == inferred.total_matched == 1111  # Seed 4, 40-49, 400-499, 4000-4999
    assert [row["title"] for row in result.rows][:2] == ["Seed 4999", "Seed 4998"]
    assert sorted(rebuilt) == [("store.materialize", 10), ("store.materialize", 500)]


@pytest.mark.parametrize("field, value", [("note", None), ("tags", ["blue"])], ids=["null-value", "array"])
def test_default_columns_rebuild_the_leading_rows_in_a_few_reads(store, field, value):
    """Rebuilding leading rows one read at a time costs a statement per row, seconds beside busy threads.

    A null value that projection drops, or an array field that the field-tree walk decodes row by
    row, made every leading row look like it might add a column.
    """
    fields = FIELDS + "    tags: {type: array, items: {type: string}}\n"
    make(store, summary=True, items=[{"title": f"R{n:04d}", "count": n, field: value} for n in range(400)],
         fields=fields)
    statements = []
    init = reader._Selecting.__init__

    def traced(self, adapter, manifest, snapshot, admitted):
        init(self, adapter, manifest, snapshot, admitted)
        admitted.session.connection.set_trace_callback(lambda _sql: statements.append(1))

    arguments = {"filters": [{"column": "count", "op": "gte", "value": 0}], "limit": 5}
    with whole_read_only():
        expected = query(store, **arguments)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reader._Selecting, "__init__", traced)
        actual = query(store, **arguments)
    assert answer(actual) == answer(expected)
    assert len(statements) < 100  # one or more per leading row before: 400 rows took over 400


@pytest.mark.query_deadline
def test_time_spent_waiting_is_not_the_query_s_cost(store, monkeypatch):
    """A deadline that counts time spent waiting for the GIL behind busy threads refused a 154-row page."""
    make(store, summary=True, items=rows(154))
    # Each clock reading on this thread jumps half a second and spends no CPU, as a wait does. The
    # wall backstop is raised so that only the CPU budget decides.
    caller, jumped, monotonic = threading.get_ident(), [0.0], time.monotonic

    def waited():
        if threading.get_ident() == caller:
            jumped[0] += 0.5
        return monotonic() + jumped[0]

    monkeypatch.setattr(runtime, "WALL_BACKSTOP_MS", 10**9, raising=False)
    monkeypatch.setattr(time, "monotonic", waited)
    result = query(store, filters=[{"column": "title", "op": "startswith", "value": "Row 1"}],
                   sort_by="count", limit=5)
    assert result.total_matched == 64  # Row 1, Row 10-19, Row 100-153
    assert result.returned == 5


def test_a_stopped_query_refuses_alike_on_both_routes(store, monkeypatch):
    """A caller that branches on the refusal must not see a different one when routing declines."""
    make(store, summary=True, items=rows(20))
    monkeypatch.setitem(runtime.PROFILES, "records", {**runtime.PROFILES["records"], "timeout_ms": 0})
    with pytest.raises(collections.CollectionError) as routed:
        query(store, sort_by="title", limit=3)
    with whole_read_only(), pytest.raises(collections.CollectionError) as whole:
        query(store, sort_by="title", limit=3)
    assert (routed.value.code, routed.value.reason, routed.value.details) == \
        (whole.value.code, whole.value.reason, whole.value.details)
    assert routed.value.code == "QUERY_TIMEOUT" and "CPU budget" in routed.value.reason


def test_converted_summary_rows_keep_their_item_key_order(store):
    """Rows a conversion left with their old view paths must not reorder an unsorted page or its offsets."""
    make(store, summary=True, items=rows(12))
    keys = [key for (key,) in store.connection.execute(
        "SELECT item_key FROM items WHERE collection_id=? ORDER BY item_key", (CID,))]
    # A conversion keeps each row's item file path, which need not follow item key order.
    with store.handle.transaction() as tx:
        tx.executemany("UPDATE item_rows SET view_path=? WHERE item_key=?",
                       [(f"Knowledge Base/Records/Work/Items/{n:02d}.md", key) for n, key in enumerate(reversed(keys))])
    with store.read_collection(CID) as manifest:
        whole = [record.identity.key for record in reader.StoreAdapter(store, manifest, None).read().records]
    with whole_read_only():
        expected = query(store, limit=5)
    with selections() as made:
        actual = query(store, limit=5)
    assert whole == keys
    assert made and answer(actual) == answer(expected)


def test_a_withheld_row_and_a_restricted_caller_keep_the_whole_read(store):
    """A route that released a withheld row's values, or served a caller who sees some fields."""
    make(store, summary=False, items=rows(12))
    store.append_record(CID, item={"title": "Secret", "count": 4}, item_key=OTHER, why="observe")
    write_scope(store.root, paths=f"Records/**/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    arguments = {"filters": [{"column": "count", "op": "gte", "value": 1}], "sort_by": "title"}
    with selections() as made:
        restricted = query(store, _external(), **arguments)
    assert not made
    assert restricted.total_matched == 8 and "Secret" not in {row["title"] for row in restricted.rows}
    with whole_read_only():
        expected = query(store, **arguments)
    with selections() as made:
        owner = query(store, **arguments)
    assert made and answer(owner) == answer(expected)
    assert owner.total_matched == 9 and "Secret" in {row["title"] for row in owner.rows}


@pytest.mark.query_deadline
def test_a_whole_summary_read_outside_a_query_answers_under_the_shipped_profile(store, monkeypatch):
    """Only a Records query bounds the decode: an export, alias or audit read of 5,000 rows refused."""
    make(store, summary=True, items=[])
    _seed(store, CID, 5000)
    decode = typed_storage._current

    def slow(*args, **kwargs):  # 30 ms of CPU per 500-row batch: 300 ms, past the 200 ms profile
        start = time.thread_time()
        while time.thread_time() - start < 0.03:
            pass
        return decode(*args, **kwargs)

    monkeypatch.setattr(typed_storage, "_current", slow)
    with store.read_collection(CID) as manifest:
        assert len(reader.StoreAdapter(store, manifest, None).read().records) == 5000


def test_a_whole_summary_read_stops_decoding_at_its_deadline(store, monkeypatch):
    """A read that routing declines must not decode every row past its budget and hold the store."""
    make(store, summary=True, items=rows(1500))
    monkeypatch.setitem(runtime.PROFILES, "records", {**runtime.PROFILES["records"], "timeout_ms": 300})
    decode = typed_storage._current

    def slow(*args, **kwargs):  # 200 ms of CPU per 500-row batch
        start = time.thread_time()
        while time.thread_time() - start < 0.2:
            pass
        return decode(*args, **kwargs)

    monkeypatch.setattr(typed_storage, "_current", slow)
    with whole_read_only(), pytest.raises(collections.CollectionError) as failure:
        query(store, sort_by="title", limit=3)
    assert failure.value.code == "QUERY_TIMEOUT"
