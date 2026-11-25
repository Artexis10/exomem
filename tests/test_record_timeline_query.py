"""`query_records` with `timeline`: one field's spans over effective time (OpenSpec record-value-timeline 5.1-5.4).

Each case names the failure only it catches. Store collections run on the real CollectionWriter, in the JSON
encoding (one view file per row) and the typed one (a summary view). The lamps, shelves and dates are invented,
and every date lies in March 2026 or before, before any write, so each case holds whenever it runs.

The stocked lamps and their `location` spans:

- L-17: Shelf A from 2026-03-01, Shelf B from 2026-03-01T14:00:00Z, Shelf C from 2026-03-07.
- L-18: Shelf A from 2026-02-20 and Shelf D from 2026-03-03; its `status` became "lent" on 2026-02-25.
- L-19: Shelf E from 2026-03-10.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import contextmanager

import pytest
from test_collection_field_admission import GUEST
from test_governance_egress import _external, write_rule, write_scope
from test_record_timeline_schema import LAMPS, equipment_manifest

from exomem.cli_ops import OpError
from exomem.collection_store import (
    connection,
    snapshot,
    timeline_index,
    timeline_query,
    typed_storage,
)
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope, owner_principal, request_scope
from exomem.record_memory import record_memory

LAMPS_PATH = "Knowledge Base/Records/Lamps/_collection.md"
SOURCE = "Knowledge Base/Sources/inventory.md"
OWNER = owner_principal(surface="mcp")
L17 = {"column": "asset_tag", "op": "eq", "value": "L-17"}
L18 = {"column": "asset_tag", "op": "eq", "value": "L-18"}
MARCH_CHANGES = {"field": "location", "from": "2026-03-01", "to": "2026-03-31", "changes": True}
#: The queries of task 5.2, by name; the index must answer each exactly as the versions do.
QUERIES = {
    "one row": {"filters": [L17], "timeline": {"field": "location"}},
    "at an instant": {"timeline": {"field": "location", "at": "2026-03-02T00:00:00Z"}},
    "at a date": {"timeline": {"field": "location", "value": {"op": "eq", "value": "Shelf A"}, "at": "2026-03-01"}},
    "a range": {"timeline": {"field": "location", "from": "2026-03-05", "to": "2026-03-08"}},
    "changes": {"timeline": MARCH_CHANGES},
    "newest first": {"timeline": MARCH_CHANGES, "descending": True},
    "with columns": {"filters": [L18], "timeline": {"field": "location"}, "columns": ["status"]},
    "a page": {"timeline": MARCH_CHANGES, "limit": 2},
    "every span": {"timeline": {"field": "location"}},
}


def manifest(encoding):
    text = equipment_manifest(LAMPS, "Lamps")
    if encoding == "typed":
        text = text.replace("lifecycle: active\n", "lifecycle: active\nview_mode: summary\n")
    return text


@contextmanager
def stocked(tmp_path, encoding, *, text=None):
    """A writer on a new store holding the stocked lamps in the given encoding."""
    root = tmp_path / "vault"
    (root / "Knowledge Base/Sources").mkdir(parents=True)
    (root / SOURCE).write_text("---\ntype: source\nexomem_id: 0c9a3f1e-1111-4222-8333-944455556666\n---\n\n"
                               "The March stock count.\n")
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as handle, \
            library_scope():
        writer = CollectionWriter(root, handle)
        writer.create_collection(LAMPS_PATH, text or manifest(encoding), why="declare", scaffold=False)
        stored = typed_storage.collection_encoding(writer.connection, LAMPS)
        assert (stored == typed_storage.JSON_V1) == (encoding == "json-v1")
        keys = {}
        keys["L-17"] = add(writer, {"asset_tag": "L-17", "location": "Shelf A"}, "2026-03-01")
        edit(writer, keys["L-17"], {"location": "Shelf B"}, effective_at="2026-03-01T14:00:00Z")
        edit(writer, keys["L-17"], {"location": "Shelf C"}, effective_at="2026-03-07")
        keys["L-18"] = add(writer, {"asset_tag": "L-18", "location": "Shelf A", "status": "in service"}, "2026-02-20")
        edit(writer, keys["L-18"], {"status": "lent"}, effective_at="2026-02-25")
        edit(writer, keys["L-18"], {"location": "Shelf D"}, effective_at="2026-03-03")
        keys["L-19"] = add(writer, {"asset_tag": "L-19", "location": "Shelf E"}, "2026-03-10")
        writer.keys = keys
        yield writer


@pytest.fixture(params=["json-v1", "typed"])
def lamps(request, tmp_path):
    with stocked(tmp_path, request.param) as writer:
        yield writer


@pytest.fixture
def typed_lamps(tmp_path):
    """The stocked lamps in a collection that can retract a row: a typed one with a summary view."""
    with stocked(tmp_path, "typed") as writer:
        yield writer


def add(writer, item, effective_at):
    return writer.append_record(LAMPS, item=item, why="stock", effective_at=effective_at)["item_key"]


def edit(writer, key, changes, **arguments):
    container, version = writer.current_guards(LAMPS, key)
    return writer.update_record(LAMPS, item_key=key, changes=changes, expected_container_hash=container,
                                expected_item_version=version, why=arguments.pop("why", "move"), **arguments)


def query(writer, who=OWNER, **arguments):
    with request_scope(who), preview_store(writer.root, writer.handle):
        return record_memory(writer.root, "query", collection=LAMPS, **arguments)


def refused(writer, **arguments):
    with pytest.raises(OpError) as raised:
        query(writer, **arguments)
    return raised.value


def from_index(monkeypatch, writer, **arguments):
    """The reply that the span index gives, by lowering the direct bound below every selection."""
    with monkeypatch.context() as patched:
        patched.setattr(timeline_query, "DIRECT_BOUND", 0)
        return query(writer, **arguments)


def build(writer, *, batch=1):
    """Run the serving store thread's steps: seed the requested indexes, then build them batch by batch."""
    writer.seed_timeline_indexes()
    while timeline_index.due(writer.connection, LAMPS):
        writer.backfill_derived(LAMPS, limit=batch, projection=False, rollup=False, references=False)


def tags(writer, who=OWNER):
    """Each lamp's asset tag by the row `id` that `who` reads; a partial reader's ids differ from the owner's."""
    return {row["id"]: row["asset_tag"] for row in query(writer, who, columns=["asset_tag"])["rows"]}


def spans(writer, reply, who=OWNER):
    """(asset tag, value, effective from, effective to) of each span, in reply order."""
    named = tags(writer, who)
    return [(named[span["id"]], span.get("value"), span["effective_from"], span["effective_to"])
            for span in reply["spans"]]


# --- 5.1 the argument and its refusals ------------------------------------------------------------------------


# Each argument meets a different check of the facade first, so each is its own case.
@pytest.mark.parametrize("arguments", [
    {"sort_by": "location"}, {"aggregate": "count"}, {"date_from": "2026-03-01"}, {"view": "Shelved"},
    {"query": {"version": 1}}, {"item_key": "44444444-4444-4444-8444-444444444444"}, {"output_format": "csv"},
])
def test_a_timeline_query_refuses_an_argument_it_does_not_honour_and_names_it(lamps, arguments):
    """A sort or a date bound that a timeline silently ignored would answer a different question than was asked."""
    [name] = arguments

    error = refused(lamps, timeline={"field": "location"}, **arguments)

    assert (error.code, error.details["argument"]) == ("TIMELINE_ARGUMENT_INVALID", name)
    assert name in error.details["remediation"]


@pytest.mark.parametrize(("timeline", "argument"), [
    ({"field": "location", "at": "2026-03-01", "from": "2026-02-01"}, "timeline.at"),
    ({"field": "location", "changes": True}, "timeline.changes"),
    ({"field": "location", "from": "2026-03-08", "to": "2026-03-01"}, "timeline.to"),
    ({"field": "location", "at": "1 March"}, "timeline.at"),
    ({"field": "location", "when": "2026-03-01"}, "timeline.when"),
    ({"field": "location", "value": {"op": "near", "value": "Shelf A"}}, "timeline.value.op"),
    ({"field": "location", "value": {"op": "eq", "value": 3}}, "timeline.value.value"),
    ({"field": "location", "value": {"op": "eq", "value": None}}, "timeline.value.value"),
])
def test_a_malformed_timeline_is_refused_naming_its_part(lamps, timeline, argument):
    """A window or a value that cannot mean what was sent would answer as an empty timeline, which reads as a fact."""
    error = refused(lamps, timeline=timeline)

    assert (error.code, error.details["argument"]) == ("TIMELINE_ARGUMENT_INVALID", argument)
    assert error.details["remediation"]


def test_a_field_that_is_not_a_top_level_scalar_is_refused(tmp_path):
    """An array or a nested path has no single value at a time, so its spans would mean nothing."""
    root = tmp_path / "vault"
    root.mkdir()
    text = equipment_manifest(LAMPS, "Lamps").replace(
        "    status: {type: string}\n", "    status: {type: string}\n    labels: {type: array, items: {type: string}}\n")
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as handle, \
            library_scope():
        writer = CollectionWriter(root, handle)
        writer.create_collection(LAMPS_PATH, text, why="declare", scaffold=False)

        for field in ("labels", "location.room"):
            error = refused(writer, timeline={"field": field})
            assert (error.code, error.details["argument"]) == ("TIMELINE_FIELD_NOT_SCALAR", "timeline.field")


def test_an_undeclared_field_is_refused_with_the_fields_it_can_name(lamps):
    error = refused(lamps, timeline={"field": "no_such_field"})

    assert error.code == "QUERY_FIELD_UNKNOWN"
    assert error.details["allowed"] == ["asset_tag", "location", "status"]


# --- 5.2 answers from the versions ----------------------------------------------------------------------------


def test_one_rows_timeline_lists_every_span_with_its_times(lamps):
    reply = query(lamps, **QUERIES["one row"])

    assert reply["timeline"] == {"field": "location", "window": None, "source": "versions"}
    assert (reply["returned"], reply["total_matched"], reply["truncated"], reply["continuation"]) == (3, 3, False, None)
    first, moved, last = reply["spans"]
    assert spans(lamps, reply) == [("L-17", "Shelf A", "2026-03-01", "2026-03-01T14:00:00Z"),
                                   ("L-17", "Shelf B", "2026-03-01T14:00:00Z", "2026-03-07"),
                                   ("L-17", "Shelf C", "2026-03-07", None)]
    assert first["first"] is True and "previous" not in first
    assert (moved["previous"], last["previous"]) == ("Shelf A", "Shelf B")
    assert {span["effective_basis"] for span in reply["spans"]} == {"stated"}
    # The write time is the commit of the write that recorded the span's start.
    written = [row[0] for row in lamps.connection.execute(
        "SELECT t.committed_at FROM txns t JOIN audit_effects e ON e.txn_id=t.txn_id JOIN item_rows i "
        "ON i.row_id=e.row_id WHERE i.item_key=? ORDER BY t.txn_id", (lamps.keys["L-17"],))]
    assert [span["written_at"] for span in reply["spans"]] == written
    # An owner reads the whole row, so each span names its write and the reason given for it.
    assert [span["why"] for span in reply["spans"]] == ["stock", "move", "move"]
    assert all(span["transition_id"] and span["sources"] == [] for span in reply["spans"])


def test_an_instant_returns_the_spans_that_contain_it(lamps):
    reply = query(lamps, **QUERIES["at an instant"])

    assert spans(lamps, reply) == [("L-18", "Shelf A", "2026-02-20", "2026-03-03"),
                                   ("L-17", "Shelf B", "2026-03-01T14:00:00Z", "2026-03-07")]
    assert reply["timeline"]["window"] == {"from": "2026-03-02T00:00:00Z", "to": "2026-03-02T00:00:00.000001Z"}


def test_a_date_returns_every_span_that_overlaps_its_utc_day(lamps):
    """A date read as its first instant would drop the Shelf B span that began that afternoon."""
    held_shelf_a = query(lamps, **QUERIES["at a date"])
    one_lamp = query(lamps, filters=[L17], timeline={"field": "location", "at": "2026-03-01"})

    assert spans(lamps, held_shelf_a) == [("L-18", "Shelf A", "2026-02-20", "2026-03-03"),
                                          ("L-17", "Shelf A", "2026-03-01", "2026-03-01T14:00:00Z")]
    assert [span[1] for span in spans(lamps, one_lamp)] == ["Shelf A", "Shelf B"]
    assert one_lamp["timeline"]["window"] == {"from": "2026-03-01T00:00:00Z", "to": "2026-03-02T00:00:00Z"}


def test_a_range_returns_the_spans_that_overlap_it_through_its_last_day(lamps):
    reply = query(lamps, **QUERIES["a range"])

    assert spans(lamps, reply) == [("L-17", "Shelf B", "2026-03-01T14:00:00Z", "2026-03-07"),
                                   ("L-18", "Shelf D", "2026-03-03", None),
                                   ("L-17", "Shelf C", "2026-03-07", None)]
    assert reply["timeline"]["window"] == {"from": "2026-03-05T00:00:00Z", "to": "2026-03-09T00:00:00Z"}


def test_changes_list_the_spans_that_started_in_the_window_with_the_value_before(lamps):
    reply = query(lamps, **QUERIES["changes"])

    assert [(tag, value) for tag, value, _, _ in spans(lamps, reply)] == [
        ("L-17", "Shelf A"), ("L-17", "Shelf B"), ("L-18", "Shelf D"), ("L-17", "Shelf C"), ("L-19", "Shelf E")]
    assert [span.get("previous", "first" if span.get("first") else None) for span in reply["spans"]] == [
        "first", "Shelf A", "Shelf A", "Shelf B", "first"]


def test_descending_lists_the_newest_span_first(lamps):
    oldest_first = spans(lamps, query(lamps, **QUERIES["changes"]))

    assert spans(lamps, query(lamps, **QUERIES["newest first"])) == oldest_first[::-1]


def test_columns_add_each_rows_current_values(lamps):
    """A column is the row's value now, not at the span: L-18 was "in service" when it reached Shelf A."""
    reply = query(lamps, **QUERIES["with columns"])

    assert [(span["value"], span["columns"]) for span in reply["spans"]] == [
        ("Shelf A", {"status": "lent"}), ("Shelf D", {"status": "lent"})]


def test_a_continuation_resumes_the_same_spans_and_is_refused_after_a_write(lamps):
    whole = query(lamps, **QUERIES["changes"])["spans"]
    pages = [query(lamps, **QUERIES["a page"])]
    while pages[-1]["continuation"] is not None:
        pages.append(query(lamps, **QUERIES["a page"], continuation=pages[-1]["continuation"]))

    assert [page["returned"] for page in pages] == [2, 2, 1]
    assert [span for page in pages for span in page["spans"]] == whole
    assert {page["total_matched"] for page in pages} == {5}
    edit(lamps, lamps.keys["L-19"], {"status": "lent"})
    assert refused(lamps, **QUERIES["a page"], continuation=pages[0]["continuation"]).code == "STALE_RECORD_SNAPSHOT"


def test_two_moves_stated_on_one_date_both_stay_and_keep_their_order(lamps):
    """A zero-length span that merged away would hide a move; an order by date alone could split a page twice."""
    key = add(lamps, {"asset_tag": "L-20", "location": "Shelf A"}, "2026-02-01")
    edit(lamps, key, {"location": "Shelf B"}, effective_at="2026-03-01")
    edit(lamps, key, {"location": "Shelf C"}, effective_at="2026-03-01")
    one_lamp = [{"column": "asset_tag", "op": "eq", "value": "L-20"}]

    every = query(lamps, filters=one_lamp, timeline={"field": "location"})
    first = query(lamps, filters=one_lamp, timeline=MARCH_CHANGES, limit=1)
    second = query(lamps, filters=one_lamp, timeline=MARCH_CHANGES, limit=1, continuation=first["continuation"])

    assert spans(lamps, every)[1:] == [("L-20", "Shelf B", "2026-03-01", "2026-03-01"),
                                       ("L-20", "Shelf C", "2026-03-01", None)]
    assert [span["value"] for span in (*first["spans"], *second["spans"])] == ["Shelf B", "Shelf C"]
    assert second["continuation"] is None


def test_a_retracted_row_has_no_spans(typed_lamps):
    """A retracted lamp still on Shelf A in a timeline would contradict every other query, which treats it as absent."""
    container, version = typed_lamps.current_guards(LAMPS, typed_lamps.keys["L-18"])
    typed_lamps.retract_record(LAMPS, item_key=typed_lamps.keys["L-18"], expected_container_hash=container,
                               expected_item_version=version, why="sold")

    reply = query(typed_lamps, **QUERIES["at a date"])

    assert spans(typed_lamps, reply) == [("L-17", "Shelf A", "2026-03-01", "2026-03-01T14:00:00Z")]
    assert reply["total_matched"] == 1


# --- 5.3 governance -------------------------------------------------------------------------------------------


@pytest.fixture(params=["json-v1", "typed"])
def classified_lamps(request, tmp_path):
    """The stocked lamps with `location` classified, so a guest reads each lamp's tag and status but not where it is."""
    text = manifest(request.param).replace("    location: {type: string}\n",
                                           "    location: {type: string, classification: location}\n")
    with stocked(tmp_path, request.param, text=text) as writer:
        yield writer


def without(span, *names):
    return {name: value for name, value in span.items() if name not in {"id", *names}}


def test_a_withheld_field_is_refused_as_an_undeclared_one(classified_lamps):
    """A refusal that differed would tell a guest the collection holds a location it may not read."""
    withheld = refused(classified_lamps, who=GUEST, timeline={"field": "location"})
    undeclared = refused(classified_lamps, who=GUEST, timeline={"field": "no_such_field"})

    assert withheld.code == "QUERY_FIELD_UNKNOWN"
    assert (withheld.code, str(withheld), withheld.details) == (undeclared.code, str(undeclared), undeclared.details)
    assert "location" not in str(withheld.details)


def test_a_field_reader_gets_the_values_and_times_but_not_the_writes(classified_lamps):
    """A transition id, a reason or a source page names a write that also changed the withheld location."""
    writes = ("transition_id", "why", "sources")
    owner = query(classified_lamps, timeline={"field": "status"})
    guest = query(classified_lamps, who=GUEST, timeline={"field": "status"})

    assert all(set(writes) <= span.keys() for span in owner["spans"])
    assert [without(span) for span in guest["spans"]] == [without(span, *writes) for span in owner["spans"]]
    assert spans(classified_lamps, guest, GUEST) == spans(classified_lamps, owner)


def withhold(writer, key):
    """Withhold one lamp's row from external callers: a scope on its ref, at ceiling 0."""
    write_scope(writer.root, paths="Unrelated/**")
    scope = writer.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{LAMPS}/{key}]\n")
    write_rule(writer.root, ceiling=0)


def test_a_withheld_row_leaves_no_span_total_or_continuation_trace(lamps):
    """A span, a count or a stale continuation of a withheld lamp would show that the lamp exists."""
    hidden = lamps.keys["L-18"]
    withhold(lamps, hidden)
    stranger = _external()
    owner = query(lamps, **QUERIES["every span"])
    others = [span for span, (tag, *_) in zip(owner["spans"], spans(lamps, owner), strict=True) if tag != "L-18"]

    seen = query(lamps, who=stranger, **QUERIES["every span"])
    seen_tags = [tag for tag, *_ in spans(lamps, seen, stranger)]
    one_lamp = query(lamps, who=stranger, filters=[L18], timeline={"field": "location"})
    no_lamp = query(lamps, who=stranger, filters=[{"column": "asset_tag", "op": "eq", "value": "L-99"}],
                    timeline={"field": "location"})
    first = query(lamps, who=stranger, **QUERIES["a page"])
    edit(lamps, hidden, {"location": "Shelf M"}, effective_at="2026-03-15")
    second = query(lamps, who=stranger, **QUERIES["a page"], continuation=first["continuation"])

    assert "L-18" not in seen_tags
    assert ([without(span) for span in seen["spans"]], seen["total_matched"]) == (
        [without(span) for span in others], len(others))
    assert (one_lamp["spans"], one_lamp["total_matched"]) == ([], 0)
    assert one_lamp == no_lamp
    assert (first["total_matched"], second["total_matched"]) == (4, 4)
    assert [without(span) for span in (*first["spans"], *second["spans"])] == [
        without(span) for span in others if span["effective_from"] >= "2026-03-01"]


# --- 5.4 the span index ---------------------------------------------------------------------------------------


def test_the_index_answers_every_query_as_the_versions_do(lamps, monkeypatch):
    direct = {name: query(lamps, **arguments) for name, arguments in QUERIES.items()}

    warming = pytest.raises(OpError, from_index, monkeypatch, lamps, **QUERIES["one row"]).value
    build(lamps)

    assert warming.code == "TIMELINE_WARMING"
    assert warming.details["retry_after_ms"] > 0 and warming.details["progress"] == {"percent_built": 0}
    for name, arguments in QUERIES.items():
        indexed = from_index(monkeypatch, lamps, **arguments)
        assert indexed["timeline"]["source"] == "index", name
        assert {**indexed, "timeline": direct[name]["timeline"]} == direct[name], name


def test_a_large_selection_before_the_build_is_refused_never_answered_short(lamps, monkeypatch):
    """A warming index answered as it stands would return a partial list that reads as the whole timeline."""
    from_index_refusal = pytest.raises(OpError, from_index, monkeypatch, lamps, **QUERIES["every span"]).value
    lamps.seed_timeline_indexes()
    lamps.backfill_derived(LAMPS, limit=1, projection=False, rollup=False, references=False)

    building = pytest.raises(OpError, from_index, monkeypatch, lamps, **QUERIES["every span"]).value

    assert from_index_refusal.code == building.code == "TIMELINE_WARMING"
    assert 0 < building.details["progress"]["percent_built"] < 100
    assert "retry_after_ms" in building.details["remediation"]


def test_writes_keep_a_built_index_current(typed_lamps, monkeypatch):
    """Each kind of write changes spans differently; an index that missed one would answer from a stale history."""
    keys = typed_lamps.keys
    pytest.raises(OpError, from_index, monkeypatch, typed_lamps, **QUERIES["every span"])
    build(typed_lamps)

    def agree():
        direct = query(typed_lamps, **QUERIES["every span"])
        indexed = from_index(monkeypatch, typed_lamps, **QUERIES["every span"])
        assert {**indexed, "timeline": direct["timeline"]} == direct
        return spans(typed_lamps, direct)

    edit(typed_lamps, keys["L-19"], {"location": "Shelf F"}, effective_at="2026-03-12")
    assert ("L-19", "Shelf F", "2026-03-12", None) in agree()
    edit(typed_lamps, keys["L-18"], {"location": "Repair Bench"}, effective_at="2026-02-27")  # a past point
    assert ("L-18", "Repair Bench", "2026-02-27", "2026-03-03") in agree()
    move = edit(typed_lamps, keys["L-17"], {"location": "Shelf G"}, effective_at="2026-03-08")["audit_correlation"]
    edit(typed_lamps, keys["L-17"], {"location": "Shelf H"}, corrects=move, why="it was Shelf H")
    assert ("L-17", "Shelf H", "2026-03-08", None) in agree()
    container = typed_lamps.current_guards(LAMPS, keys["L-19"])[0]
    counted = typed_lamps.bulk_upsert_records(
        LAMPS, rows=[{"item": {"asset_tag": "L-19", "location": "Shelf J"}},
                     {"item": {"asset_tag": "L-21", "location": "Shelf K"}}],
        why="count", expected_container_hash=container, source=SOURCE, effective_at="2026-03-20")
    assert counted["committed"] is True
    assert {("L-19", "Shelf J", "2026-03-20", None), ("L-21", "Shelf K", "2026-03-20", None)} <= set(agree())
    assert [span["sources"] for span in query(typed_lamps, **QUERIES["every span"])["spans"]
            if span["value"] in {"Shelf J", "Shelf K"}] == [[SOURCE], [SOURCE]]
    container, version = typed_lamps.current_guards(LAMPS, keys["L-18"])
    typed_lamps.retract_record(LAMPS, item_key=keys["L-18"], expected_container_hash=container,
                               expected_item_version=version, why="sold")
    assert "L-18" not in {tag for tag, *_ in agree()}
    add(typed_lamps, {"asset_tag": "L-18", "location": "Shelf L"}, "2026-03-21")  # brought back
    assert ("L-18", "Shelf L", "2026-03-21", None) in agree()


def test_a_field_removed_or_retyped_loses_its_index(lamps, monkeypatch):
    """An index kept across a type change would compare values under the old type's keys."""
    encoding = "json-v1" if typed_storage.collection_encoding(lamps.connection, LAMPS) == typed_storage.JSON_V1 \
        else "typed"
    declared = manifest(encoding).replace(
        "    status: {type: string}\n", "    status: {type: string}\n    notes: {type: string}\n    bay: {type: string}\n")
    revise(lamps, declared)
    for field in ("notes", "bay"):
        pytest.raises(OpError, from_index, monkeypatch, lamps, timeline={"field": field})
    build(lamps)
    built = lamps.connection.execute("SELECT count(*) FROM field_spans").fetchone()[0]

    revise(lamps, declared.replace("    bay: {type: string}\n", "").replace(
        "    notes: {type: string}\n", "    notes: {type: integer}\n"))

    assert built > 0
    assert lamps.connection.execute("SELECT field_path,ready FROM field_span_state").fetchall() == [("notes", 0)]
    assert lamps.connection.execute("SELECT count(*) FROM field_spans").fetchone() == (0,)
    assert pytest.raises(OpError, from_index, monkeypatch, lamps, timeline={"field": "notes"}).value.code == \
        "TIMELINE_WARMING"


def revise(writer, text):
    with request_scope(OWNER), preview_store(writer.root, writer.handle):
        guards = record_memory(writer.root, "inspect", collection=LAMPS)["lifecycle_guards"]
    writer.revise_collection(LAMPS, manifest_text=text, **guards, why="revise")


def test_a_restored_copy_warms_rather_than_read_a_copied_index(lamps, monkeypatch, tmp_path):
    pytest.raises(OpError, from_index, monkeypatch, lamps, **QUERIES["every span"])
    build(lamps)
    stage = tmp_path / "stage"
    stage.mkdir()
    with snapshot._staged_copy(snapshot._collection_fill(lamps.handle.path), directory=stage,
                               deadline=time.monotonic() + 10, attaches_source=True) as artifact:
        shutil.copy(artifact.path, tmp_path / "restored.sqlite")
    copy = sqlite3.connect(tmp_path / "restored.sqlite")
    try:
        assert copy.execute("SELECT (SELECT count(*) FROM field_spans),(SELECT count(*) FROM field_span_state)"
                            ).fetchone() == (0, 0)
    finally:
        copy.close()

    with connection.open_writer(tmp_path / "restored.sqlite", lease_check=lambda: True) as handle:
        restored = CollectionWriter(lamps.root, handle)
        assert pytest.raises(OpError, from_index, monkeypatch, restored, **QUERIES["every span"]).value.code == \
            "TIMELINE_WARMING"
