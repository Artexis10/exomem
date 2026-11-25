"""`query_records` with `timeline`: one field's spans over effective time (OpenSpec record-value-timeline 5.1).

Each case names the failure only it catches. Store collections run on the real CollectionWriter, in the JSON
encoding (one view file per row) and the typed one (a summary view). The lamps, shelves and dates are invented,
and every date lies in March 2026 or before, before any write, so each case holds whenever it runs.

The stocked lamps and their `location` spans:

- L-17: Shelf A from 2026-03-01, Shelf B from 2026-03-01T14:00:00Z, Shelf C from 2026-03-07.
- L-18: Shelf A from 2026-02-20 and Shelf D from 2026-03-03; its `status` became "lent" on 2026-02-25.
- L-19: Shelf E from 2026-03-10.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from test_record_timeline_schema import LAMPS, equipment_manifest

from exomem.cli_ops import OpError
from exomem.collection_store import (
    connection,
    typed_storage,
)
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope, owner_principal, request_scope
from exomem.record_memory import record_memory

LAMPS_PATH = "Knowledge Base/Records/Lamps/_collection.md"
SOURCE = "Knowledge Base/Sources/inventory.md"
OWNER = owner_principal(surface="mcp")


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
