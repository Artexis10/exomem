"""NEW built-in Records summary collections and the v1 view-mode boundary.

OpenSpec add-collection-query-engine S1.1. Every case drives the real
CollectionWriter on an invented store and names the defect only it catches.
Summary rows live in the external store with no per-row view file; bounded
summary pages publish later through reconcile; a populated collection never
changes view mode in place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_collection_store_legacy_import import CONTEXT, _capture, _connection, _items
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope
from test_records_bulk_upsert import EVIDENCE, _evidence

from exomem import get_page, mutation_terminal, vault
from exomem import structured_collections as collections
from exomem.collection_store import governance, legacy_import
from exomem.collection_store.reader import StoreAdapter
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import legacy, runtime
from exomem.query_engine import sqlite as legacy_sql

COPY_CID = "6f1c2d01-5c0f-4f8e-9a51-4a438d0b6b4a"
COPY_PATH = "Knowledge Base/Records/Daily/_collection.md"
MODE_CHANGE = "VIEW_MODE_CHANGE_UNSUPPORTED"
ITEMS_CAP = 100_000
PAGE_CAP, PAGE_BYTES, PAGES_TOTAL = 16, 64 * 1024, 1024 * 1024


@pytest.fixture(autouse=True)
def owner():
    """Summary collections are owner-only until field release exists; the owner drives each case."""
    with request_scope(owner_principal()):
        yield


def summary_text(text=None):
    return (text or manifest_text()).replace("lifecycle: active\n", "lifecycle: active\nview_mode: summary\n")


def create(store, *, summary=True):
    text = summary_text() if summary else manifest_text()
    receipt = store.create_collection(manifest_path(), text, why="create", scaffold=False)
    _evidence(store.root)
    return receipt


def guards(store, cid=CID):
    return store.inspect_collection(cid)["lifecycle_guards"]


def bulk(store, items, *, cid=CID, after=None):
    return store.bulk_upsert_records(
        cid, rows=[{"item": item} for item in items], why="import", source=EVIDENCE,
        expected_container_hash=after or guards(store, cid)["expected_container_hash"])


def files(store):
    """Every vault file and its bytes, fixtures included."""
    return {path.relative_to(store.root).as_posix(): path.read_bytes()
            for path in sorted(store.root.rglob("*")) if path.is_file()}


def pages(store, cid=CID):
    return [path for (path,) in store.connection.execute(
        "SELECT path FROM projection_state WHERE collection_id=? AND kind='summary' ORDER BY path", (cid,))]


def canonical(store):
    """Authority, mapping, manifest, rows, history and cursor basis of the store."""
    return {table: store.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608
            for table in ("collections", "collection_manifests", "typed_encoding_mappings", "items",
                          "version_identity", "txns", "query_projection_mappings", "query_cursor_state")}


def toggled(text):
    """The same manifest asking for the other view mode."""
    if "view_mode: summary\n" in text:
        return text.replace("view_mode: summary\n", "")
    return summary_text(text)


def populate(store, *, summary):
    create(store, summary=summary)
    first = store.append_record(CID, item={"title": "One", "count": 1}, why="observe")
    store.update_record(CID, item_key=first["item_key"], changes={"count": 2}, why="correct",
                        expected_container_hash=guards(store)["expected_container_hash"],
                        expected_item_version=_item_version(store, first["item_key"]))
    return first["item_key"]


def _item_version(store, key):
    from exomem.collection_store import tokens

    cid, version, payload = store.connection.execute(
        "SELECT collection_id,row_version,payload_hash FROM items WHERE item_key=?", (key,)).fetchone()
    return tokens.item_version(cid, key, version, payload)


def _edit_manifest_view(store, change):
    path = store.root / manifest_path()
    frontmatter, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    change(frontmatter)
    edited = ("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body).encode()
    path.write_bytes(edited)
    return edited


def _toggle_frontmatter(frontmatter):
    if frontmatter.pop("view_mode", None) is None:
        frontmatter["view_mode"] = "summary"


def _seed(store, cid, count):
    """Stand in for earlier acknowledgements: canonical rows in the collection's own encoding, no views.

    Driving 100,000 rows through 200 real bulk calls exceeds the per-test
    budget, so only the rows at the boundary go through the writer.
    """
    from exomem.collection_store import tokens, typed_storage

    conn = store.connection
    text, metadata, path = conn.execute(
        "SELECT m.manifest_text,m.governance_json,c.manifest_path FROM collection_manifests m JOIN collections c "
        "ON c.collection_id=m.collection_id AND c.manifest_version=m.manifest_version WHERE c.collection_id=?",
        (cid,)).fetchone()
    manifest = collections.parse_manifest_bytes(store.root, path, text.encode())
    typed = typed_storage.ready_layout(conn, cid)
    txn = conn.execute("SELECT MAX(txn_id) FROM txns").fetchone()[0]
    seeded = [(f"seed-{number}", {"title": f"Seed {number}"}) for number in range(count)]
    with store.handle.transaction() as tx:
        tx.executemany(
            "INSERT INTO items(collection_id,item_key,natural_key,row_version,schema_version,values_json,"
            "payload_hash,view_path,created_txn,updated_txn,governance_json,encoding) VALUES (?,?,?,1,1,?,?,?,?,?,?,?)",
            ((cid, key, collections.manifest_natural_key(manifest, values),
              None if typed else json.dumps(values), tokens.payload_hash(1, key, values, ""),
              None if typed else f"{manifest.storage.source}/{key}.md", txn, txn,
              governance.row_metadata(manifest.schema, values, metadata),
              "typed-v1" if typed else "json-v1") for key, values in seeded))
        if typed:
            ids = dict(tx.execute("SELECT item_key,row_id FROM items WHERE collection_id=?", (cid,)))
            for key, values in seeded:
                typed_storage.write_version(tx, typed, row_id=ids[key], row_version=1, values=values, body="",
                                            payload_hash=tokens.payload_hash(1, key, values, ""), txn_id=txn,
                                            schema_version=1)


def test_new_summary_collection_is_typed_and_described_by_store_bounds(store):
    """A summary collection created as JSON rows with per-row view paths, or described with a file row cap."""
    receipt = create(store)
    assert mutation_terminal.valid_record_receipt(receipt)
    assert store.connection.execute("SELECT encoding,view_mode FROM collections").fetchone() == ("typed-v1", "summary")
    store.append_record(CID, item={"title": "One", "count": 1}, why="observe")
    assert store.connection.execute("SELECT view_path FROM items").fetchall() == [(None,)]
    described = store.inspect_collection(CID)
    assert described["contract"]["view_mode"] == "summary"
    assert described["coverage"]["committed"] == 1
    capacity = described["capacity"]
    assert capacity["row_limit"] is None and capacity["bounded_by"]
    assert capacity["summary_pages"] == {"max_pages": PAGE_CAP, "max_page_bytes": PAGE_BYTES,
                                         "max_total_bytes": PAGES_TOTAL}


def test_summary_admits_rows_past_the_items_cap_that_items_refuses(store):
    """A row cap missing from items mode, or applied to summary mode, at the true 100,000 boundary."""
    create(store, summary=False)
    _seed(store, CID, ITEMS_CAP - 1)
    at_cap = store.append_record(CID, item={"title": "At the cap"}, why="observe")
    with pytest.raises(collections.CollectionError, match="COLLECTION_ITEM_LIMIT") as refused:
        store.append_record(CID, item={"title": "Past the cap"}, why="observe")
    assert refused.value.details["maximum"] == ITEMS_CAP
    assert store.connection.execute("SELECT COUNT(*) FROM items").fetchone() == (ITEMS_CAP,)
    after = store.connection.execute("SELECT generation FROM collections").fetchone()
    with pytest.raises(collections.CollectionError, match="COLLECTION_ITEM_LIMIT"):
        bulk(store, [{"title": "Past the cap in bulk"}], after=at_cap["after_container_hash"])
    assert store.connection.execute("SELECT generation FROM collections").fetchone() == after

    summary_cid = COPY_CID
    store.create_collection(COPY_PATH, summary_text(manifest_text()).replace(CID, summary_cid),
                            why="create", scaffold=False)
    _seed(store, summary_cid, ITEMS_CAP)
    past = store.append_record(summary_cid, item={"title": "Past the items cap"}, why="observe")
    assert past["outcome"] == "committed"
    assert store.connection.execute("SELECT COUNT(*) FROM items WHERE collection_id=?",
                                    (summary_cid,)).fetchone() == (ITEMS_CAP + 1,)


@pytest.mark.parametrize("release", ["uniform", "row-rule"])
def test_summary_writes_past_the_release_cache_cap_stay_incremental(store, monkeypatch, release):
    """A write past the release-cache subject cap that sheds the whole state and re-streams every row subject.

    Uniform release needs only the collection-level decision; a row-level
    rule keeps exact per-row admission over the changed rows alone.
    """
    monkeypatch.setattr(governance, "_MAX_CACHED_SUBJECTS", 16)
    if release == "row-rule":
        write_scope(store.root, paths="Records/**")
        write_rule(store.root, ceiling=0)
    create(store)
    receipt = bulk(store, [{"title": f"Row {n}"} for n in range(40)])
    streams = []
    stream = governance.iter_subjects

    def counted(conn, cid, logical_vault_id, *, identity=None, view_path=None, **options):
        if identity is None and view_path is None:
            streams.append(cid)
        return stream(conn, cid, logical_vault_id, identity=identity, view_path=view_path, **options)

    monkeypatch.setattr(governance, "iter_subjects", counted)
    for batch in range(3):
        receipt = bulk(store, [{"title": f"Batch {batch} row {n}"} for n in range(40)],
                       after=receipt["after_container_hash"])
        assert receipt["committed"]
    assert streams == []
    assert store.connection.execute("SELECT COUNT(*) FROM items").fetchone() == (160,)


def test_summary_acknowledgement_writes_no_row_files_and_publishes_bounded_labelled_pages(store, monkeypatch):
    """Per-row files or staging on the summary acknowledgement path, or an unbounded/unlabelled page set."""
    from exomem.collection_store import views

    create(store)
    touched = []
    original = views.PublicationBatch._fs
    monkeypatch.setattr(views.PublicationBatch, "_fs", lambda batch: touched.append(1) or original(batch))
    receipt = bulk(store, [{"title": f"Row {n}", "count": n} for n in range(300)])
    store.append_record(CID, item={"title": "One more"}, why="observe")
    assert touched == [] and receipt["committed"]
    with pytest.raises(collections.CollectionError) as refused:
        store.append_record(CID, item={"title": "Refused", "count": "many"}, why="observe")
    store.reconcile_views()
    declared = pages(store)
    held = refused.value.details["held"]["path"]
    owned = {path: data for path, data in files(store).items() if path not in {EVIDENCE, "Knowledge Base/log.md"}}
    assert set(owned) == {manifest_path(), held, *declared}
    assert 1 <= len(declared) <= PAGE_CAP
    assert all(len(owned[path]) <= PAGE_BYTES for path in declared)
    assert sum(len(owned[path]) for path in declared) <= PAGES_TOTAL
    page = get_page.get_page(store.root, path=declared[0])
    generation = store.connection.execute("SELECT generation FROM collections").fetchone()[0]
    labels = page.frontmatter
    assert labels["collection_id"] == CID and labels["source"] and labels["window"]
    assert labels["metric"] == "committed rows" and labels["value"] == 301
    assert labels["basis"]["generation"] == generation and labels["completeness"] == "complete at basis"
    store.append_record(CID, item={"title": "Later"}, why="observe")
    assert files(store)[declared[0]] == owned[declared[0]]
    described = store.inspect_collection(CID)
    assert described["projection"]["pending_views"] >= 1
    assert "PROJECTION_PENDING" in {diagnostic["code"] for diagnostic in described["diagnostics"]}


def test_summary_page_edit_is_held_and_rerendered_never_parsed(store):
    """An edited summary page parsed into row edits, adopted, or lost instead of held."""
    create(store)
    bulk(store, [{"title": "One", "count": 1}])
    store.reconcile_views()
    store.reconcile_views()
    page = store.root / pages(store)[0]
    published = page.read_bytes()
    edited = published.replace(b"value: 1", b"value: 2") + b"\n- title: Two\n  count: 2\n"
    page.write_bytes(edited)
    before = canonical(store)
    store.reconcile_views()
    store.reconcile_views()
    assert store.connection.execute(
        "SELECT DISTINCT code,held_bytes FROM held_candidates WHERE kind='view-correction'").fetchall() == [
        ("SUMMARY_VIEW_READ_ONLY", edited)]
    assert page.read_bytes() == published
    assert canonical(store) == before


ROUTES = ["revise-items", "revise-summary", "edit-back-items", "edit-back-summary"]


@pytest.mark.parametrize("route", ROUTES)
def test_populated_mode_change_refuses_before_any_cutover(store, route):
    """An in-place items/summary conversion of a populated collection through its own entry route."""
    action, mode = route.rsplit("-", 1)
    key = populate(store, summary=mode == "summary")
    store.reconcile_views()
    store.reconcile_views()
    before, guard, version = canonical(store), guards(store), _item_version(store, key)
    vault_before = files(store)
    current = store.connection.execute(
        "SELECT manifest_text FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1").fetchone()[0]
    if action == "revise":
        with pytest.raises(collections.CollectionError, match=MODE_CHANGE) as refused:
            store.revise_collection(CID, manifest_text=toggled(current), why="convert", **guard)
        assert "new collection" in refused.value.details["migration"].lower()
        assert files(store) == vault_before
    else:
        edited = _edit_manifest_view(store, _toggle_frontmatter)
        store.reconcile_views()
        store.reconcile_views()
        assert store.connection.execute(
            "SELECT DISTINCT code,held_bytes FROM held_candidates WHERE kind='view-correction'").fetchall() == [
            (MODE_CHANGE, edited)]
        restored = {path: data for path, data in files(store).items() if "/Held/" not in path}
        assert restored == vault_before
    assert canonical(store) == before and guards(store) == guard
    store.update_record(CID, item_key=key, changes={"count": 3}, why="still guarded",
                        expected_container_hash=guard["expected_container_hash"], expected_item_version=version)


def test_populated_file_collection_cannot_migrate_into_summary(tmp_path):
    """The migration entry point importing populated items-mode rows as a summary collection."""
    root, path = _items(tmp_path, text=summary_text(), values={"title": "One", "count": 3})
    original = {item.relative_to(root).as_posix(): item.read_bytes() for item in root.rglob("*") if item.is_file()}
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute("BEGIN")
        with pytest.raises(collections.CollectionError, match=MODE_CHANGE):
            legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone() == (0,)
        conn.execute("ROLLBACK")
    assert {item.relative_to(root).as_posix(): item.read_bytes()
            for item in root.rglob("*") if item.is_file()} == original


@pytest.mark.parametrize("outstanding", ["none", "held", "offline-edit", "pending-publication"])
def test_empty_collection_changes_mode_only_without_outstanding_state(store, outstanding):
    """An empty collection refused a guarded mode change, or allowed one over held, unclassified or unpublished state."""
    create(store, summary=outstanding == "pending-publication")
    if outstanding == "held":
        with pytest.raises(collections.CollectionError):
            store.append_record(CID, item={"title": "Refused", "count": "many"}, why="observe")
    if outstanding == "offline-edit":
        _edit_manifest_view(store, lambda frontmatter: frontmatter.update(title="Edited offline"))
    current = store.connection.execute("SELECT manifest_text FROM collection_manifests").fetchone()[0]
    if outstanding != "none":
        before = canonical(store)
        with pytest.raises(collections.CollectionError, match=MODE_CHANGE):
            store.revise_collection(CID, manifest_text=toggled(current), why="convert", **guards(store))
        assert canonical(store) == before
        return
    store.revise_collection(CID, manifest_text=summary_text(current), why="convert", **guards(store))
    store.append_record(CID, item={"title": "One"}, why="observe")
    assert store.connection.execute("SELECT view_mode FROM collections").fetchone() == ("summary",)
    assert store.connection.execute("SELECT view_path FROM items").fetchall() == [(None,)]
    assert pages(store)


def test_mode_migration_copies_into_a_new_collection_and_leaves_the_original_intact(store):
    """A copy into another mode that retargets, rewrites or disposes of the original's rows, history or files."""
    populate(store, summary=False)
    with pytest.raises(collections.CollectionError):
        store.append_record(CID, item={"title": "Refused", "count": "many"}, why="observe")
    store.reconcile_views()
    original, original_files = canonical(store), files(store)
    with store.read_collection(CID) as manifest:
        current = [record.values for record in StoreAdapter(store, manifest, None)._read(manifest).records]

    target = summary_text(manifest_text()).replace(CID, COPY_CID).replace("title: Work", "title: Daily")
    store.create_collection(COPY_PATH, target, why="new summary target", scaffold=False)
    copied = bulk(store, current, cid=COPY_CID)
    assert copied["committed"] and copied["counts"]["inserted"] == len(current)
    with store.read_collection(COPY_CID) as manifest:
        assert [record.values for record in StoreAdapter(store, manifest, None)._read(manifest).records] == current
    assert not [path for path in files(store) if path.startswith("Knowledge Base/Records/Daily/Items/")
                and not path.endswith("_summary.md")]

    failing = manifest_text().replace(CID, "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d").replace("title: Work", "title: Strict")
    failing = failing.replace("    count: {type: integer}\n", "    count: {type: integer}\n    unit: {type: string, required: true}\n")
    store.create_collection("Knowledge Base/Records/Strict/_collection.md", failing, why="strict target", scaffold=False)
    refused = bulk(store, current, cid="7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d")
    assert not refused["committed"]
    after = canonical(store)
    for table in ("items", "version_identity"):
        assert [row for row in after[table] if row in original[table]] == original[table]
    assert [row for row in after["collections"] if row[0] == CID] == [
        row for row in original["collections"] if row[0] == CID]
    assert {path: data for path, data in files(store).items() if path in original_files} == original_files


def test_summary_rows_live_only_in_the_external_store_while_items_stays_default(store):
    """Summary row values leaking into vault files, or an omitted view_mode no longer meaning items."""
    create(store)
    bulk(store, [{"title": "zq-marker-7", "count": 7}])
    store.reconcile_views()
    assert Path(store.root) not in Path(store.handle.path).parents
    assert not any(b"zq-marker-7" in data for data in files(store).values())

    items_text = manifest_text().replace(CID, COPY_CID).replace("title: Work", "title: Daily")
    assert collections.parse_manifest_bytes(store.root, COPY_PATH, items_text.encode()).view_mode == "items"
    store.create_collection(COPY_PATH, items_text, why="items default", scaffold=False)
    receipt = store.append_record(COPY_CID, item={"title": "zq-items-8"}, why="observe")
    assert b"zq-items-8" in (store.root / receipt["affected_paths"][0]).read_bytes()
    assert store.connection.execute("SELECT view_mode FROM collections WHERE collection_id=?",
                                    (COPY_CID,)).fetchone() == ("items",)


def test_summary_store_reads_are_owner_only_before_field_governance(store):
    """A non-owner reading summary rows before field release exists, or the owner failing on path-less rows."""
    create(store)
    bulk(store, [{"title": "One", "count": 1}, {"title": "Two", "count": 2}])
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.inspect_collection(CID)
        with runtime.read_session(store.root, store.handle.path) as session:
            with pytest.raises(runtime.QueryError) as refused:
                session.admit(CID)
        assert refused.value.code == "COLLECTION_NOT_FOUND"
    with request_scope(owner_principal()):
        with store.read_collection(CID) as manifest:
            titles = [record.values["title"] for record in StoreAdapter(store, manifest, None)._read(manifest).records]
        assert sorted(titles) == ["One", "Two"]
        with runtime.read_session(store.root, store.handle.path) as session:
            admitted = session.admit(CID)
            plan = legacy.normalize(columns_available=admitted.fields, columns=["title"], sort_by="title")
            result = legacy_sql.execute_legacy(admitted, plan, path="source", format="markdown-items").as_dict()
        assert [row["title"] for row in result["rows"]] == ["One", "Two"]
