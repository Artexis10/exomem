"""Finding and editing a Records row by its id on a store collection (OpenSpec harden-records-import-and-row-access)."""

from __future__ import annotations

import json

import pytest
from test_collection_store_importer import CID as WORKOUTS
from test_collection_store_importer import ndjson, run, setup, start
from test_collection_store_writer import CID, KEY, OTHER, create
from test_collection_store_writer import store as store
from test_governance_egress import write_scope

from exomem import page_ids, query_data, record_governance
from exomem.cli_ops import OpError
from exomem.collection_store import typed_storage
from exomem.collection_store.preview import bind_row, preview_store
from exomem.governance import policy
from exomem.governance.principal import (
    ClientBinding,
    OriginSessionBinding,
    RequestPrincipal,
    owner_principal,
    request_scope,
)
from exomem.record_memory import record_memory

ROWS = 1_100
MAPPING = {
    "fields": {"exercise_id": "id", "kind": "kind"},
    "on_invalid": "skip",
    "coverage": {name: {"classification": None} for name in ("id", "kind")},
}


def test_edit_by_row_id_binds_one_row_of_a_large_collection_without_paging_it(store, monkeypatch):
    """Finding a row by paging the collection costs the whole collection and fails at the response cap."""
    setup(store, ndjson({"id": f"r{index:04}", "kind": "run"} for index in range(ROWS)))
    start(store, mapping=MAPPING)
    run(store)
    paged = []
    query = record_governance.query_collection
    monkeypatch.setattr(record_governance, "query_collection", lambda *a, **k: paged.append(1) or query(*a, **k))
    with request_scope(owner_principal()), preview_store(store.root, store.handle):
        found = record_memory(
            store.root, "query", collection=WORKOUTS, columns=["exercise_id"], limit=1,
            filters=[{"column": "exercise_id", "op": "eq", "value": "r0777"}],
        )
        row_id, paged[:] = found["rows"][0]["id"], []

        record_memory(store.root, "update", collection=WORKOUTS, item_id=row_id,
                      changes={"kind": "tempo"}, why="correct the kind")
        assert paged == []
        with pytest.raises(OpError) as stale:
            record_memory(store.root, "update", collection=WORKOUTS, item_id=row_id,
                          changes={"kind": "long"}, why="stale id")

    stored = {values["exercise_id"]: values["kind"] for _, values in typed_storage.collection_values(store.connection, WORKOUTS)}
    assert len(stored) == ROWS and stored["r0777"] == "tempo" and stored["r0776"] == "run"
    assert stale.value.code == "STALE_ITEM"
    assert page_ids.is_page_id(stale.value.details["current_id"]) and stale.value.details["current_id"] != row_id


def test_a_query_page_that_fits_as_rows_but_not_as_rendered_shrinks_and_continues(store):
    """The cap counts the rendered page, with its envelope and source list: rows alone under it still overflowed it."""
    setup(store, ndjson({"id": f"r{index:04}", "kind": "run"} for index in range(ROWS)))
    start(store, mapping=MAPPING)
    run(store)
    seen, pages, token = [], 0, None
    with request_scope(owner_principal()), preview_store(store.root, store.handle):
        while True:
            page = record_memory(store.root, "query", collection=WORKOUTS, columns=["exercise_id", "item_version"],
                                 limit=query_data.HARD_ROW_CAP, continuation=token)
            assert len(page["rendered"].encode()) <= query_data.MAX_RESPONSE_BYTES
            seen += [row["exercise_id"] for row in page["rows"]]
            pages, token = pages + 1, page["continuation"]
            if token is None:
                break
    assert pages > 1
    assert sorted(seen) == [f"r{index:04}" for index in range(ROWS)]


def test_a_withheld_row_id_is_refused_like_an_absent_one(store, tmp_path, monkeypatch):
    """A stale or current id of a row the caller cannot read must not tell it from a row that never existed."""
    create(store)
    store.append_record(CID, item={"title": "Allowed"}, item_key=KEY, why="capture")
    hidden = store.append_record(CID, item={"title": "Protected"}, item_key=OTHER, why="capture")
    with request_scope(owner_principal()), preview_store(store.root, store.handle):
        ids = {row["title"]: row["id"] for row in record_memory(store.root, "query", collection=CID)["rows"]}
        store.update_record(CID, item_key=OTHER, changes={"title": "Protected again"},
                            expected_container_hash=hidden["after_container_hash"],
                            expected_item_version=hidden["after_item_hash"], why="correct")
    write_scope(store.root, paths=f"Records/**/{OTHER}.md")
    scope_id = next(iter(policy.load(store.root).scopes))
    config = tmp_path / "host-boundary.json"
    config.write_text(json.dumps({"version": 1, "default_denied_scope_ids": [scope_id], "clients": [{
        "issuer": "urn:fixture:issuer", "client_id": "configured-client", "denied_scope_ids": [scope_id]}]}))
    monkeypatch.setenv("EXOMEM_CONNECTOR_BOUNDARY_CONFIG", str(config))
    limited_owner = RequestPrincipal(
        "owner", surface="mcp", issuer_family="mcp-oauth:fixture", remote_owner=True,
        client_binding=ClientBinding("urn:fixture:issuer", "configured-client"),
        origin_session=OriginSessionBinding("fixture-session", "fixture-generation", "fixture-audience"))
    absent = page_ids.part_id("44444444-4444-4444-8444-444444444444", "x")

    def refusal(item_id):
        with request_scope(limited_owner), preview_store(store.root, store.handle), pytest.raises(OpError) as error:
            record_memory(store.root, "update", collection=CID, item_id=item_id, changes={"title": "x"}, why="try")
        return error.value.code, error.value.details, str(error.value).replace(item_id, "<id>")

    # The hidden row's old id is stale, and a stale id answers with the row's current id: only for a readable row.
    assert refusal(ids["Protected"]) == refusal(absent) == ("ITEM_NOT_FOUND", {}, refusal(absent)[2])
    with request_scope(limited_owner), preview_store(store.root, store.handle):
        query = record_memory(store.root, "query", collection=CID, columns=["title", "item_version"])
        bound = bind_row(store.root, CID, ids["Allowed"])
    assert [row["title"] for row in query["rows"]] == ["Allowed"]
    assert bound == {"item_key": KEY, "expected_item_version": query["rows"][0]["item_version"],
                     "expected_container_hash": query["snapshot"]}


def test_a_record_id_is_refused_as_one_only_for_a_row_the_caller_may_read(store, tmp_path, monkeypatch):
    """`record_id` names a row but no version of it: a reader is told which id edits, anyone else hears no row."""
    create(store)
    store.append_record(CID, item={"title": "Allowed"}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Protected"}, item_key=OTHER, why="capture")
    write_scope(store.root, paths=f"Records/**/{OTHER}.md")
    scope_id = next(iter(policy.load(store.root).scopes))
    config = tmp_path / "host-boundary.json"
    config.write_text(json.dumps({"version": 1, "default_denied_scope_ids": [scope_id], "clients": [{
        "issuer": "urn:fixture:issuer", "client_id": "configured-client", "denied_scope_ids": [scope_id]}]}))
    monkeypatch.setenv("EXOMEM_CONNECTOR_BOUNDARY_CONFIG", str(config))
    limited_owner = RequestPrincipal(
        "owner", surface="mcp", issuer_family="mcp-oauth:fixture", remote_owner=True,
        client_binding=ClientBinding("urn:fixture:issuer", "configured-client"),
        origin_session=OriginSessionBinding("fixture-session", "fixture-generation", "fixture-audience"))
    nobody = "44444444-4444-4444-8444-444444444444"

    def refusal(record_id):
        with request_scope(limited_owner), preview_store(store.root, store.handle), pytest.raises(OpError) as error:
            record_memory(store.root, "update", collection=CID, item_id=record_id, changes={"title": "x"}, why="try")
        return error.value.code, error.value.details, str(error.value).replace(record_id, "<id>")

    code, _details, message = refusal(KEY)
    assert code == "ITEM_ID_IS_KEY"
    assert "record_id" in message and "`id`" in message
    assert refusal(OTHER) == refusal(nobody)
    assert refusal(OTHER)[0] == "ITEM_NOT_FOUND"
