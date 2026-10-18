"""A trusted mixed binding keeps file and store collection authority separate."""

import json

import pytest
from test_collection_store_service import service as service
from test_collection_store_writer import CID, KEY, OTHER, create, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope
from test_incident_records_routing import _routed_state

from exomem import (
    collection_claims,
    due_state,
    plan_progress,
    record_formats,
    record_governance,
    records,
    records_disposition,
    working_set_state,
)
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import authority
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope
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
        "version": 2, "mode": "store", "default_authority": "file", "store_id": sid,
        "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": manifest_path(),
                         "authority": "store", "store_id": sid,
                         "source_path": "Knowledge Base/Records/Work/Items", "layout": "markdown-items"}],
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


def test_unknown_hint_entry_keeps_a_real_file_collection_readable(mixed):
    marker = authority.parse_marker(mixed.root, authority.read_marker(mixed.root))
    absent = "33333333-3333-4333-8333-333333333333"
    path = manifest_path().replace("Work", "Missing")
    marker["collections"].append(authority.marker_entry(
        absent, path, marker["store_id"], "records", "Knowledge Base/Records/Missing/Items", "markdown-items"))
    authority.marker_path(mixed.root).write_text(json.dumps(marker))
    projection = mixed.root / path
    projection.parent.mkdir(parents=True)
    projection.write_text(manifest_text().replace(CID, absent))
    (projection.parent / "Items").mkdir()
    with preview_store(mixed.root, mixed.handle):
        assert record_memory(mixed.root, "query", collection=KEY, columns=["title"])["rows"] == []
        assert collections.resolve_collection(mixed.root, KEY).collection_id == KEY
        for selector in (absent, path):
            result = record_memory(mixed.root, "query", collection=selector, columns=["title"])
            assert result["rows"] == []
            resolved = collections.resolve_collection(mixed.root, selector)
            assert resolved.collection_id == absent and resolved.path == path
        assert record_memory(mixed.root, "query", collection=CID)["rows"][0]["title"] == "Canonical"


def test_foreign_hint_identity_cannot_override_the_canonical_collection(mixed):
    marker = authority.parse_marker(mixed.root, authority.read_marker(mixed.root))
    marker["store_id"] = "33333333-3333-4333-8333-333333333333"
    marker["collections"][0]["store_id"] = marker["store_id"]
    authority.marker_path(mixed.root).write_text(json.dumps(marker))
    projection = mixed.root / manifest_path()
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text(manifest_text())
    with preview_store(mixed.root, mixed.handle):
        assert record_memory(mixed.root, "query", collection=KEY, columns=["title"])["rows"] == []
        assert record_memory(mixed.root, "query", collection=manifest_path())["rows"][0]["title"] == "Canonical"


@pytest.mark.parametrize("unreadable", [False, True])
def test_unknown_store_ownership_withholds_the_whole_inventory_but_not_notes(mixed, monkeypatch, tmp_path, unreadable):
    """Unknown collection ownership cannot release a partial inventory or ordinary Records files."""
    from exomem.get_page import get_page

    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"not a database")
    if unreadable:
        broken.chmod(0)
    monkeypatch.setattr("exomem.collection_store.connection.store_path", lambda root: broken)
    with pytest.raises(OpError, match="^COLLECTION_STORE_UNAVAILABLE:"):
        record_governance.inventory_collections(mixed.root)
    note = mixed.root / "Knowledge Base/Notes/Independent.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: Independent\n---\nIndependent knowledge\n")
    assert "Independent knowledge" in get_page(mixed.root, path=note.relative_to(mixed.root).as_posix()).body


def test_canonical_inventory_ignores_a_hints_foreign_identity_and_profile(service):
    from exomem.collection_store import runtime

    root = service.root
    marker = json.loads(authority.read_marker(root))
    marker["collections"][0].update(collection_id="88888888-8888-4888-8888-888888888888", semantic_profile="planning")
    authority.marker_path(root).write_text(json.dumps(marker))

    def inspect():
        inventory = record_governance.inventory_collections(root)
        assert {row["collection_id"] for row in inventory["collections"]} == {KEY, CID}
        assert inventory["unreadable_manifests"] == []
        assert {row["collection_id"] for row in record_governance.inventory_collections(
            root, semantic_profile="planning")["collections"]} == {OTHER}

    runtime._SERVERS[root].call(inspect, read_only=True)


def test_canonical_profile_controls_sweeps_despite_edited_hint_and_view(service):
    from exomem.collection_store import runtime

    root = service.root
    marker = json.loads(authority.read_marker(root))
    marker["collections"][0]["semantic_profile"] = "planning"
    authority.marker_path(root).write_text(json.dumps(marker))
    view = root / manifest_path()
    view.parent.mkdir(parents=True, exist_ok=True)
    view.write_text(manifest_text("planning"))

    def sweep():
        manifests, errors = collections.discover_collections_with_errors(root)
        assert errors == ()
        canonical = next(manifest for manifest in manifests if manifest.collection_id == CID)
        assert canonical.semantic_profile == "records"
        assert plan_progress.review(root)["collections_unavailable"] == 0
        assert {row["collection_id"] for row in record_governance.inventory_collections(
            root)["collections"]} == {KEY, CID}

    runtime._SERVERS[root].call(sweep, read_only=True)


def test_malformed_hint_with_unknown_canonical_ownership_withholds_whole_sweeps(mixed):
    authority.marker_path(mixed.root).write_text("{")
    for sweep in (collections.discover_collections_with_errors, plan_progress.review):
        with pytest.raises(OpError, match="^COLLECTION_STORE_UNAVAILABLE:"):
            sweep(mixed.root)


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


def test_bound_canonical_rows_override_a_conflicting_file_collection(mixed):
    cid = "33333333-3333-4333-8333-333333333333"
    path = manifest_path().replace("Work", "Bound")
    text = manifest_text().replace(CID, cid)
    with preview_store(mixed.root, mixed.handle):
        records.create_collection(mixed.root, path, text, why="file fixture", scaffold=True)
        added = record_memory(mixed.root, "append", collection=cid,
                              item={"title": "File"}, item_key=KEY, why="file row")
    item_path = next(path for path in added["affected_paths"] if "/Items/" in path)
    mixed.create_collection(path, text, why="canonical registry", scaffold=False)
    mixed.append_record(cid, item={"title": "Store"}, item_key=KEY, why="canonical row")
    with preview_store(mixed.root, mixed.handle):
        assert record_memory(mixed.root, "query", collection=cid)["rows"][0]["title"] == "Store"
        assert working_set_state._profile_data(mixed.root, item_path)[0]["title"] == "Store"
    assert "File" in (mixed.root / item_path).read_text()


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


def test_mixed_hidden_file_item_never_surfaces_as_a_recurrence(service):
    mixed = service
    path = manifest_path().replace("Work", "Legacy")
    title = "Widget panel froze on load"
    record_memory(mixed.root, "append", collection=path,
                  item={"title": title}, item_key=OTHER, why="capture")
    from exomem.collection_store import runtime

    def refresh():
        claims = due_state._recompute_claims(mixed.root)
        assert claims[path]["items"][0]["key"] == OTHER
        due_state.save(mixed.root, {"version": due_state.SCHEMA_VERSION, "categories": {}, "claims": claims})
        assert due_state.load(mixed.root)["claims"][path]["items"] == claims[path]["items"]
        assert due_state.visible_claim_items(mixed.root, path)[0]["key"] == OTHER

    with request_scope(owner_principal()):
        runtime._SERVERS[mixed.root].call(refresh)
    state = _routed_state(mixed.root, title=title)
    write_scope(mixed.root, paths="Records/Legacy/Items/**")
    write_rule(mixed.root, ceiling=0)
    def bound():
        result = records_disposition.disposition(
            mixed.root, {"strength": "strong", "collection": path}, state, level="balanced",
        )
        assert result is not None and result["disposition"] == "ask"
        assert OTHER not in json.dumps(result)
        assert due_state.visible_claim_items(mixed.root, path) == []

    with request_scope(_external()):
        runtime._SERVERS[mixed.root].call(bound, read_only=True)


def test_mixed_file_routing_keeps_owner_natural_key_evidence(service):
    mixed = service
    path = manifest_path().replace("Work", "Legacy")
    manifest = mixed.root / path
    manifest.write_text(manifest.read_text().replace(
        "lifecycle: active\n", "lifecycle: active\nclaims:\n  terms: [widget, panel, froze]\n",
    ))
    title = "Widget panel froze on load"
    record_memory(mixed.root, "append", collection=path,
                  item={"title": title}, item_key=OTHER, why="capture")
    def bound():
        due_state.save(mixed.root, {"version": due_state.SCHEMA_VERSION, "categories": {},
                                   "claims": due_state._recompute_claims(mixed.root)})
        targets = due_state.routing_targets(mixed.root)
        routed = collection_claims.route([title], targets)
        assert routed is not None and routed["strength"] == "strong"
        assert routed["collection"] == path
        target = next(target for target in targets if target.collection == path)
        assert collection_claims.normalize_text(title) in target.natural_key_values

    from exomem.collection_store import runtime

    runtime._SERVERS[mixed.root].call(bound)


@pytest.mark.parametrize("identity", ["unissued-owner", "remote-tenant"])
def test_unknown_store_inventory_is_unavailable_to_unissued_or_remote_principals(mixed, monkeypatch, tmp_path, identity):
    """An owner label or RAW tenant exemption that exposes otherwise withheld C inventory metadata."""
    from exomem.governance.principal import RequestPrincipal, resolve_rest_principal

    broken = tmp_path / "unavailable.sqlite"
    broken.write_bytes(b"not a database")
    monkeypatch.setattr("exomem.collection_store.connection.store_path", lambda root: broken)
    who = (RequestPrincipal("owner", surface="mcp") if identity == "unissued-owner"
           else resolve_rest_principal("tenant:unrelated"))
    with request_scope(who), pytest.raises(OpError) as refused:
        record_governance.inventory_collections(mixed.root)
    assert refused.value.code == "COLLECTION_STORE_UNAVAILABLE"
    assert CID not in str(refused.value) and manifest_path() not in str(refused.value)
