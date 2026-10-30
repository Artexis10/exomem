"""Structured-files maintenance for store collections: filename recipes move views, never rows.

Each test starts from the state a migrated vault has: items whose views carry UUID filenames,
then a manifest revision that adds a title recipe, so inspection reports filename drift.
"""

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import mutation_terminal
from exomem import structured_collections as collections
from exomem.collection_store import tokens

ITEMS = "Knowledge Base/Planning/Work/Items"
THIRD = "33333333-3333-4333-8333-333333333333"
RECIPE = "item_filename:\n  version: 1\n  fields: [title]\nstorage:\n"


def _drifted(store, items):
    """A Planning collection whose items were captured before it named views by title."""
    created = store.create_collection(manifest_path("planning"), manifest_text("planning"), why="create",
                                      scaffold=False)
    receipt = created
    for key, title, body in items:
        receipt = store.append_record(CID, item={"title": title}, item_key=key, body=body, why="capture")
    text = manifest_text("planning")
    store.revise_collection(CID, manifest_text=text.replace("storage:\n", RECIPE),
                            expected_manifest_hash=tokens.manifest_hash(text),
                            expected_container_hash=receipt["after_container_hash"], why="name views by title")
    store.reconcile_views()
    return store


def _views(store):
    return dict(store.connection.execute("SELECT item_key, view_path FROM items ORDER BY item_key").fetchall())


def _apply(store, plan, why="readable filenames"):
    return store.apply_filenames(CID, plan_id=plan["plan_id"], source_snapshot=plan["source_snapshot"], why=why)


def _findings(store):
    """The filename states inspection reports, which a complete apply must clear."""
    presentation = store.inspect_collection(CID).get("presentation", {"items": []})
    return {item["state"] for item in presentation["items"]} & {"filename_drift", "filename_collision"}


def test_apply_moves_uuid_views_to_titles_and_keeps_each_row(store):
    _drifted(store, [(KEY, "Ship the importer", "First.\n"), (OTHER, "Write the runbook", "")])
    before = store.connection.execute(
        "SELECT item_key, row_version, payload_hash FROM items ORDER BY item_key").fetchall()
    assert _findings(store) == {"filename_drift"}

    plan = store.preview_filenames(CID)
    assert plan["blockers"] == [] and plan["inbound_rewrites"] == []
    assert [(move["from"], move["to"]) for move in plan["moves"]] == [
        (f"{ITEMS}/{KEY}.md", f"{ITEMS}/Ship the importer.md"),
        (f"{ITEMS}/{OTHER}.md", f"{ITEMS}/Write the runbook.md"),
    ]
    assert (store.root / ITEMS / f"{KEY}.md").exists(), "preview writes nothing"

    receipt = _apply(store, plan)
    store.reconcile_views()

    assert receipt["outcome"] == "committed" and mutation_terminal.valid_structured_files_receipt(receipt)
    assert [entry["after_path"] for entry in receipt["inverse"]] == [move["to"] for move in plan["moves"]]
    assert sorted(path.name for path in (store.root / ITEMS).iterdir()) == [
        "Ship the importer.md", "Write the runbook.md"]
    assert "First." in (store.root / ITEMS / "Ship the importer.md").read_text()
    # A move changes no row: same identity, version and payload, so no history or guard is lost.
    assert store.connection.execute(
        "SELECT item_key, row_version, payload_hash FROM items ORDER BY item_key").fetchall() == before
    assert _findings(store) == set()
    # The old path retires: it is neither tracked nor held as a deleted view.
    assert store.connection.execute(
        "SELECT COUNT(*) FROM projection_state WHERE path LIKE ?", (f"{ITEMS}/%-%-%.md",)).fetchone() == (0,)
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone() == (0,)
    moves = store.connection.execute(
        "SELECT operation, actor FROM txns WHERE operation='view_move'").fetchall()
    assert len(moves) == 2 and all(actor != "owner:view-edit" for _, actor in moves)


def test_a_body_link_to_a_moved_item_follows_it(store):
    link = f"See [[{ITEMS}/{OTHER}|the runbook]] first.\n"
    _drifted(store, [(KEY, "Ship the importer", link), (OTHER, "Write the runbook", "")])

    plan = store.preview_filenames(CID)
    assert [(entry["item_key"], entry["links"]) for entry in plan["inbound_rewrites"]] == [(KEY, 1)]
    _apply(store, plan)
    store.reconcile_views()

    body, version = store.connection.execute(
        "SELECT body, row_version FROM items WHERE item_key=?", (KEY,)).fetchone()
    assert body == f"See [[{ITEMS}/Write the runbook|the runbook]] first.\n" and version == 2
    assert f"[[{ITEMS}/Write the runbook|the runbook]]" in (store.root / ITEMS / "Ship the importer.md").read_text()
    assert (store.root / ITEMS / "Write the runbook.md").exists()


def test_a_link_from_a_note_outside_the_collection_blocks_the_plan(store):
    _drifted(store, [(KEY, "Ship the importer", "")])
    note = store.root / "Knowledge Base/Notes/plan.md"
    note.parent.mkdir(parents=True)
    note.write_text(f"---\ntype: insight\nstatus: active\n---\n\nTracks [[{ITEMS}/{KEY}]].\n")

    plan = store.preview_filenames(CID)
    assert [blocker["code"] for blocker in plan["blockers"]] == ["IMMUTABLE_INBOUND_LINK"]
    with pytest.raises(collections.CollectionError, match="BLOCKED_STRUCTURED_FILE_PLAN"):
        _apply(store, plan)
    assert _views(store) == {KEY: f"{ITEMS}/{KEY}.md"}
    assert f"[[{ITEMS}/{KEY}]]" in note.read_text()


def test_a_change_after_preview_makes_apply_stale(store):
    _drifted(store, [(KEY, "Ship the importer", "")])
    plan = store.preview_filenames(CID)
    current = store.connection.execute("SELECT generation, audit_head FROM collections").fetchone()
    item = store.connection.execute("SELECT row_version, payload_hash FROM items").fetchone()
    store.update_record(CID, item_key=KEY, changes={}, body="Changed.\n", why="edit",
                        expected_container_hash=tokens.container_hash(CID, *current),
                        expected_item_version=tokens.item_version(CID, KEY, *item))

    with pytest.raises(collections.CollectionError, match="STALE_STRUCTURED_FILE_PLAN"):
        _apply(store, plan)
    assert _views(store) == {KEY: f"{ITEMS}/{KEY}.md"}


def test_applying_the_same_plan_again_replays_without_moving_twice(store):
    _drifted(store, [(KEY, "Ship the importer", ""), (OTHER, "Write the runbook", "")])
    plan = store.preview_filenames(CID)
    first = _apply(store, plan)
    count = store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()

    replay = _apply(store, plan)

    assert replay["outcome"] == "replayed" and mutation_terminal.valid_structured_files_receipt(replay)
    assert replay["inverse"] == first["inverse"]
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone() == count
    with pytest.raises(collections.CollectionError, match="IDEMPOTENCY_KEY_REUSED"):
        _apply(store, plan, why="a different reason")


def test_titles_that_render_one_filename_leave_no_collision_behind(store):
    _drifted(store, [(KEY, "Q3: plan", ""), (OTHER, "Q3 plan", ""), (THIRD, "Unrelated", "")])
    assert _findings(store) == {"filename_drift", "filename_collision"}

    plan = store.preview_filenames(CID)
    assert [collision["item_keys"] for collision in plan["collisions"]] == [[KEY, OTHER]]
    _apply(store, plan)
    store.reconcile_views()

    views = _views(store)
    assert views[KEY] == f"{ITEMS}/Q3 plan.md"
    assert views[OTHER].startswith(f"{ITEMS}/Q3 plan — ") and views[OTHER] != views[KEY]
    assert _findings(store) == set()
    assert store.preview_filenames(CID)["moves"] == []


def test_imported_history_links_follow_the_moved_view(tmp_path, store):
    from test_collection_store_history import _import_history

    _import_history(tmp_path, store)
    text, digest = store.connection.execute(
        "SELECT manifest_text, manifest_hash FROM collection_manifests ORDER BY manifest_version DESC").fetchone()
    container, _ = store.current_guards(CID)
    store.revise_collection(CID, manifest_text=text.replace("storage:\n", RECIPE), expected_manifest_hash=digest,
                            expected_container_hash=container, why="name views by title")
    store.reconcile_views()

    _apply(store, store.preview_filenames(CID))
    store.reconcile_views()
    store.reconcile_views()

    page = (store.root / "Knowledge Base/Records/Work/_history.md").read_text()
    assert "Original.md" not in page
    assert page.count("[[Knowledge Base/Records/Work/Items/First.md]]") >= 3


def test_maintain_memory_reaches_a_store_collection_instead_of_answering_not_found(store):
    """The operation inspection names as the drift remedy runs on the store through the real dispatcher."""
    import json as json_module

    from exomem import commands, writer_lease
    from exomem.collection_store import authority
    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope

    _drifted(store, [(KEY, "Ship the importer", "")])
    sid = store.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_text(json_module.dumps({
        "version": 2, "mode": "store", "default_authority": "file", "store_id": sid, "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": manifest_path("planning"), "authority": "store",
                         "store_id": sid, "semantic_profile": "planning", "source_path": ITEMS,
                         "layout": "markdown-items"}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
    }))
    maintain = next(command for command in commands.PRODUCT_COMMANDS if command.name == "maintain_memory")
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        plan = writer_lease.invoke_command(maintain, store.root, mode="structured-files", collection=CID)
        receipt = writer_lease.invoke_command(
            maintain, store.root, mode="structured-files", collection=CID, apply=True, plan_id=plan["plan_id"],
            source_snapshot=plan["source_snapshot"], why="readable filenames")
    store.reconcile_views()

    assert [move["to"] for move in plan["moves"]] == [f"{ITEMS}/Ship the importer.md"]
    # The writer lease accepts only the closed structured-files receipt as a committed terminal.
    assert receipt["state"] == "committed" and receipt["mutated"] is True
    assert (store.root / ITEMS / "Ship the importer.md").exists()
