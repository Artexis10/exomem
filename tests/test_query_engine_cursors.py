"""Authenticated continuation must survive page closure, not hidden writes."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import cursors, runtime, validation

AS_OF = "2026-10-04T00:00:00Z"
THIRD = "33333333-3333-4333-8333-333333333333"


def setup(store, *, payload=False, extra_fields=()):
    text = manifest_text().replace("count: {type: integer}", "count: {type: number, sortable: true}")
    if payload:
        text = text.replace("\n---", "\n    payload: {type: string}\n---")
    for name in extra_fields:
        text = text.replace("\n---", f"\n    {name}: {{type: string}}\n---")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    return {"domain": "collections", "type": "records", "vault": "fixture",
            "fields": {"title": {"type": "string"}, "count": {"type": "number"},
                       "item_key": {"type": "string"}, **({"payload": {"type": "string"}} if payload else {}),
                       **{name: {"type": "string"} for name in extra_fields}}}


def query(declaration, **request):
    result = validation.normalize_query({"version": 1, "select": ["count"],
                                         "order_by": [{"field": "count"}], "page": {"limit": 1}, **request},
                                        declarations={CID: declaration}, collection=CID)
    assert not result.findings, result.findings
    return result.query


def page(store, intent):
    with runtime.read_session(store.root, store.handle.path) as session:
        return cursors.execute_page(session, intent, as_of=AS_OF)


def after(intent, first):
    assert first["has_more"] and first["next_cursor"]
    return replace(intent, page=replace(intent.page, after=first["next_cursor"]))


def test_cursor_resumes_ties_and_nulls_after_snapshot_closes(store):
    declaration = setup(store)
    for key, title, count in [(KEY, "One", 1), (OTHER, "Two", 1), (THIRD, "Three", None)]:
        store.append_record(CID, item={"title": title, "count": count}, item_key=key, why="observe")
    intent = query(declaration)
    first = page(store, intent)
    assert first["schema_version"] == 1
    second = page(store, after(intent, first))
    third = page(store, after(intent, second))
    assert [p["rows"] for p in [first, second, third]] == [[{"count": 1}], [{"count": 1}], [{"count": None}]]
    assert not third["has_more"] and third["next_cursor"] is None


def test_unselected_correction_preserves_cursor_but_relevant_change_is_stale(store):
    declaration = setup(store)
    store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    added = store.append_record(CID, item={"title": "Two", "count": 2}, item_key=OTHER, why="observe")
    intent = query(declaration)
    continuation = after(intent, page(store, intent))
    corrected = store.update_record(CID, item_key=OTHER, changes={"title": "Renamed"}, why="correct",
                                    expected_item_version=added["after_item_hash"], expected_container_hash=added["after_container_hash"])
    assert page(store, continuation)["rows"] == [{"count": 2}]
    store.update_record(CID, item_key=OTHER, changes={"count": 3}, why="correct",
                        expected_item_version=corrected["after_item_hash"], expected_container_hash=corrected["after_container_hash"])
    with pytest.raises(runtime.QueryError, match="QUERY_CURSOR_STALE"):
        page(store, continuation)


def test_hidden_sibling_changes_leave_mixed_continuation_valid(store):
    declaration = setup(store)
    store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    store.append_record(CID, item={"title": "Three", "count": 3}, item_key=THIRD, why="observe")
    hidden = store.append_record(CID, item={"title": "Hidden", "count": 2}, item_key=OTHER, why="observe")
    write_scope(store.root, paths=f"Records/Work/Items/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    intent = query(declaration)
    with request_scope(_external()):
        continuation = after(intent, page(store, intent))
    with request_scope(owner_principal()):
        store.update_record(CID, item_key=OTHER, changes={"count": 100}, why="correct",
                            expected_item_version=hidden["after_item_hash"], expected_container_hash=hidden["after_container_hash"])
    with request_scope(_external()):
        assert page(store, continuation)["rows"] == [{"count": 3}]


def test_mixed_cursor_basis_does_not_decode_large_unselected_fields(store):
    names = tuple(f"unused{n}" for n in range(9))
    declaration = setup(store, extra_fields=names)
    for key, count in [(KEY, 1), (OTHER, 2), (THIRD, 3)]:
        store.append_record(CID, item={"title": str(count), "count": count,
                                      **{name: "x" * 30_000 for name in names}},
                            item_key=key, why="observe")
    write_scope(store.root, paths=f"Records/Work/Items/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    intent = query(declaration)
    with request_scope(_external()):
        continuation = after(intent, page(store, intent))
        assert page(store, continuation)["rows"] == [{"count": 3}]


def test_cursor_boundary_respects_an_explicit_leading_identity_sort(store):
    declaration = setup(store)
    for key, count in [(KEY, 9), (OTHER, 4), (THIRD, 1)]:
        store.append_record(CID, item={"title": str(count), "count": count}, item_key=key, why="observe")
    intent = query(declaration, select=["item_key", "count"],
                   order_by=[{"field": "item_key", "direction": "desc"}, {"field": "count"}])
    first = page(store, intent)
    second = page(store, after(intent, first))
    third = page(store, after(intent, second))
    assert [result["rows"][0]["item_key"] for result in [first, second, third]] == sorted([KEY, OTHER, THIRD], reverse=True)
    assert not third["has_more"]


def test_relative_date_page_keeps_frozen_time_after_the_clock_advances(store, monkeypatch):
    from test_query_engine_typed_rows import query as typed_query
    from test_query_engine_typed_rows import setup_rows

    fields = {"at": {"type": "datetime", "filterable": True}}
    setup_rows(store, [(KEY, {"title": "First", "at": AS_OF}),
                       (OTHER, {"title": "Second", "at": AS_OF})], fields)
    intent = typed_query(fields=fields, select=["title"], page={"limit": 1}, where={
        "field": "at", "op": "eq", "value": {"relative_to": "as_of", "amount": 0, "unit": "day"},
    })
    first = page(store, intent)
    later = cursors.dt.datetime.fromisoformat("2026-10-05T00:00:00+00:00")
    clock = SimpleNamespace(datetime=SimpleNamespace(now=lambda _: later), UTC=cursors.dt.UTC)
    monkeypatch.setattr(cursors, "dt", clock)
    with runtime.read_session(store.root, store.handle.path) as session:
        second = cursors.execute_page(session, after(intent, first))
    assert second["rows"] == [{"title": "Second"}]
    assert second["as_of"] == first["as_of"] and not second["has_more"]


def test_tampered_or_differently_bound_cursor_cannot_read(store):
    declaration = setup(store)
    store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="observe")
    store.append_record(CID, item={"title": "Two", "count": 2}, item_key=OTHER, why="observe")
    intent = query(declaration)
    continuation = after(intent, page(store, intent))
    token = continuation.page.after
    tampered = replace(continuation, page=replace(continuation.page, after=token[:40] + ("A" if token[40] != "A" else "B") + token[41:]))
    with pytest.raises(runtime.QueryError, match="QUERY_CURSOR_INVALID"):
        page(store, tampered)
    with request_scope(_external()), pytest.raises(runtime.QueryError, match="QUERY_CURSOR_INVALID"):
        page(store, continuation)
    changed = query(declaration, select=["title"], page={"limit": 1, "after": token})
    with pytest.raises(runtime.QueryError, match="QUERY_CURSOR_INVALID"):
        page(store, changed)


def test_byte_truncated_page_resumes_first_unemitted_row(store):
    declaration = setup(store, payload=True)
    for key, count in [(KEY, 1), (OTHER, 2)]:
        store.append_record(CID, item={"title": str(count), "count": count, "payload": "x" * 30_000},
                            item_key=key, why="observe")
    intent = query(declaration, select=["count", "payload"], page={"limit": 10})
    first = page(store, intent)
    assert first["truncation_reason"] == "bytes" and len(json.dumps(first).encode()) <= 64 * 1024
    second = page(store, after(intent, first))
    assert first["rows"][0]["count"] == 1 and second["rows"][0]["count"] == 2
    assert not second["has_more"]
