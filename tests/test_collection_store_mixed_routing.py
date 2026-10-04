"""A trusted mixed binding keeps file and store collection authority separate."""

import json

import pytest
from test_collection_store_writer import CID, KEY, OTHER, create, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope
from test_incident_records_routing import _routed_state

from exomem import (
    collection_claims,
    due_state,
    record_formats,
    records,
    records_disposition,
    working_set_state,
)
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import authority
from exomem.collection_store.connection import CollectionStoreError
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import request_scope
from exomem.plan_memory import plan_memory
from exomem.record_memory import record_memory


@pytest.fixture
def mixed(store, monkeypatch):
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    for profile, cid in (("records", KEY), ("planning", OTHER)):
        path = manifest_path(profile).replace("Work", "Legacy")
        records.create_collection(store.root, path, manifest_text(profile).replace(CID, cid),
                                  why="legacy fixture", scaffold=True)
    create(store)
    store.append_record(CID, item={"title": "Canonical"}, item_key=KEY, why="fixture")
    sid = store.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    marker = {
        "version": 1, "mode": "store", "default_authority": "file", "store_id": sid,
        "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": manifest_path(),
                         "authority": "store", "store_id": sid}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
    }
    path = authority.marker_path(store.root)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(marker))
    return store


def test_mixed_public_facades_read_write_and_inspect_each_authority(mixed):
    with preview_store(mixed.root, mixed.handle):
        for call, cid, key_name, append in (
            (record_memory, KEY, "item_key", "append"),
            (plan_memory, OTHER, "plan_id", "add"),
            (record_memory, CID, "item_key", "append"),
        ):
            added = call(mixed.root, append, collection=cid, item={"title": "One"},
                         **{key_name: OTHER}, why="capture")
            changed = call(mixed.root, "update", collection=cid, **{key_name: OTHER},
                           changes={"title": "Two"}, why="correct",
                           expected_container_hash=added["after_container_hash"],
                           expected_item_version=added["after_item_hash"])
            assert changed["outcome"] == "committed"
            result = call(mixed.root, "query", collection=cid)
            assert "Two" in [row["title"] for row in result["rows"]]
            inspected = call(mixed.root, "inspect", collection=cid)
            assert inspected["contract"]["collection_id"] == cid
            assert inspected["audit"]["status"] == "ok", (cid, inspected["audit"])
        inventory = record_memory(mixed.root, "inspect")
        assert {row["collection_id"] for row in inventory["collections"]} == {KEY, CID}
        assert {row["collection_id"]: row["committed"] for row in inventory["collections"]} == {KEY: 1, CID: 2}
        claims = due_state._recompute_claims(mixed.root)
        assert {path: len(row["items"]) for path, row in claims.items()} == {
            manifest_path().replace("Work", "Legacy"): 1, manifest_path(): 2,
        }
    assert mixed.connection.execute("SELECT collection_id FROM collections").fetchall() == [(CID,)]
    assert "Two" in (mixed.root / manifest_path().replace("Work", "Legacy")).parent.joinpath(
        "Items", f"{OTHER}.md").read_text()


def test_mixed_discovery_and_adapters_ignore_store_manifest_projection(mixed):
    projection = mixed.root / manifest_path()
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text("broken projected manifest")
    with preview_store(mixed.root, mixed.handle):
        manifests, errors = collections.discover_collections_with_errors(mixed.root)
        assert errors == ()
        assert {manifest.collection_id for manifest in manifests} == {KEY, OTHER, CID}
        counts = {manifest.collection_id: len(record_formats.load_adapter(mixed.root, manifest).read().records)
                  for manifest in manifests}
    assert counts == {KEY: 0, OTHER: 0, CID: 1}


def test_mixed_missing_store_collection_never_reads_valid_projection(mixed):
    marker = authority.parse_marker(mixed.root, authority.read_marker(mixed.root))
    absent = "33333333-3333-4333-8333-333333333333"
    path = manifest_path().replace("Work", "Missing")
    marker["collections"].append({"collection_id": absent, "manifest_path": path,
                                  "authority": "store", "store_id": marker["store_id"]})
    authority.marker_path(mixed.root).write_text(json.dumps(marker))
    projection = mixed.root / path
    projection.parent.mkdir(parents=True)
    projection.write_text(manifest_text().replace(CID, absent))
    with preview_store(mixed.root, mixed.handle):
        assert record_memory(mixed.root, "query", collection=KEY, columns=["title"])["rows"] == []
        assert collections.resolve_collection(mixed.root, KEY).collection_id == KEY
        for selector in (absent, path):
            with pytest.raises(OpError):
                record_memory(mixed.root, "query", collection=selector)


def test_mixed_foreign_store_identity_refuses_c_without_displacing_file_a(mixed):
    marker = authority.parse_marker(mixed.root, authority.read_marker(mixed.root))
    marker["store_id"] = "33333333-3333-4333-8333-333333333333"
    marker["collections"][0]["store_id"] = marker["store_id"]
    authority.marker_path(mixed.root).write_text(json.dumps(marker))
    projection = mixed.root / manifest_path()
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text(manifest_text())
    with preview_store(mixed.root, mixed.handle):
        assert record_memory(mixed.root, "query", collection=KEY, columns=["title"])["rows"] == []
        with pytest.raises(CollectionStoreError, match="COLLECTION_STORE_MARKER_CONFLICT"):
            record_memory(mixed.root, "query", collection=manifest_path())


def test_mixed_hidden_store_projection_does_not_consume_discovery_budget(mixed):
    projection = mixed.root / manifest_path()
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text(manifest_text())
    write_scope(mixed.root, paths="Records/Work/**")
    write_rule(mixed.root, ceiling=0)
    with preview_store(mixed.root, mixed.handle), request_scope(_external()):
        manifests = collections.discover_collections(mixed.root, max_candidates=2,
                                                     max_raw_candidates=2)
        assert {manifest.collection_id for manifest in manifests} == {KEY, OTHER}
        with pytest.raises(OpError):
            record_memory(mixed.root, "query", collection=manifest_path())


def test_mixed_discovery_keeps_duplicate_identity_refusal(mixed):
    duplicate = mixed.root / manifest_path().replace("Work", "Duplicate")
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(manifest_text())
    with preview_store(mixed.root, mixed.handle):
        with pytest.raises(collections.CollectionError, match="AMBIGUOUS_COLLECTION"):
            collections.discover_collections(mixed.root)


def test_mixed_marker_does_not_bind_a_store_or_route_ordinary_create_to_it(mixed):
    assert collections.load_manifest(mixed.root, manifest_path().replace("Work", "Legacy")).collection_id == KEY
    new_path = manifest_path().replace("Work", "New")
    new_id = "33333333-3333-4333-8333-333333333333"
    with preview_store(mixed.root, mixed.handle):
        result = record_memory(mixed.root, "create", manifest_path=new_path,
                               manifest_text=manifest_text().replace(CID, new_id),
                               why="file collection", scaffold=False)
    assert result["outcome"] == "committed"
    assert mixed.connection.execute("SELECT collection_id FROM collections").fetchall() == [(CID,)]
    assert collections.load_manifest(mixed.root, new_path).collection_id == new_id


def test_unadmitted_store_registry_cannot_override_file_collection(mixed):
    cid = "33333333-3333-4333-8333-333333333333"
    path = manifest_path().replace("Work", "Unadmitted")
    text = manifest_text().replace(CID, cid)
    mixed.create_collection(path, text, why="unadmitted registry", scaffold=False)
    mixed.append_record(cid, item={"title": "Store"}, item_key=KEY, why="unadmitted row")
    records.create_collection(mixed.root, path, text, why="file authority", scaffold=True)
    added = record_memory(mixed.root, "append", collection=cid,
                          item={"title": "File"}, item_key=KEY, why="file row")
    item_path = next(path for path in added["affected_paths"] if "/Items/" in path)
    with preview_store(mixed.root, mixed.handle):
        assert record_memory(mixed.root, "query", collection=cid)["rows"][0]["title"] == "File"
        assert working_set_state._profile_data(mixed.root, item_path)[0]["title"] == "File"


def test_mixed_inventory_retains_unreadable_files_and_combined_budget(mixed):
    broken = mixed.root / manifest_path().replace("Work", "Broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("not a manifest")
    with preview_store(mixed.root, mixed.handle):
        manifests, errors = collections.discover_collections_with_errors(mixed.root)
        assert len(manifests) == 3
        assert [error.path for error in errors] == [broken.relative_to(mixed.root).as_posix()]
        with pytest.raises(collections.CollectionError, match="COLLECTION_DISCOVERY_LIMIT"):
            collections.discover_collections(mixed.root, max_candidates=3)


def test_mixed_hidden_file_item_never_surfaces_as_a_recurrence(mixed):
    path = manifest_path().replace("Work", "Legacy")
    title = "Widget panel froze on load"
    record_memory(mixed.root, "append", collection=path,
                  item={"title": title}, item_key=OTHER, why="capture")
    due_state.save(mixed.root, {"version": due_state.SCHEMA_VERSION, "categories": {},
                               "claims": due_state._recompute_claims(mixed.root)})
    assert due_state.visible_claim_items(mixed.root, path)[0]["key"] == OTHER
    state = _routed_state(mixed.root, title=title)
    write_scope(mixed.root, paths="Records/Legacy/Items/**")
    write_rule(mixed.root, ceiling=0)
    with preview_store(mixed.root, mixed.handle), request_scope(_external()):
        result = records_disposition.disposition(
            mixed.root, {"strength": "strong", "collection": path}, state, level="balanced",
        )
        assert result is not None and result["disposition"] == "ask"
        assert OTHER not in json.dumps(result)
        assert due_state.visible_claim_items(mixed.root, path) == []


def test_mixed_file_routing_keeps_owner_natural_key_evidence(mixed):
    path = manifest_path().replace("Work", "Legacy")
    manifest = mixed.root / path
    manifest.write_text(manifest.read_text().replace(
        "lifecycle: active\n", "lifecycle: active\nclaims:\n  terms: [widget, panel, froze]\n",
    ))
    title = "Widget panel froze on load"
    record_memory(mixed.root, "append", collection=path,
                  item={"title": title}, item_key=OTHER, why="capture")
    due_state.save(mixed.root, {"version": due_state.SCHEMA_VERSION, "categories": {},
                               "claims": due_state._recompute_claims(mixed.root)})
    baseline = collection_claims.route([title], due_state.routing_targets(mixed.root))
    assert baseline is not None and baseline["strength"] == "strong"
    with preview_store(mixed.root, mixed.handle):
        targets = due_state.routing_targets(mixed.root)
        routed = collection_claims.route([title], targets)
        assert routed == baseline
        target = next(target for target in targets if target.collection == path)
        assert collection_claims.normalize_text(title) in target.natural_key_values
